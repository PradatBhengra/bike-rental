from typing import Literal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app import cache, db
from app.bookings import service as bookings
from app.bookings.states import get_booking
from app.catalog.service import get_setting
from app.chat import loop
from app.deps import Admin, CurrentUser, require_role
from app.errors import Problem
from app.timeutil import fmt, rupees, utcnow

router = APIRouter(prefix="/chat", tags=["chat"])
admin = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_role("admin"))])


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


class FeedbackIn(BaseModel):
    rating: Literal[1, -1]
    comment: str | None = Field(None, max_length=1000)


class FlagPatch(BaseModel):
    status: Literal["reviewed", "dismissed"]
    note: str | None = Field(None, max_length=1000)


def _conversation(conn, conversation_id: int, user_id: int) -> dict:
    row = conn.execute("SELECT * FROM conversation WHERE id = %s AND user_id = %s", (conversation_id, user_id)).fetchone()
    if row is None:
        raise Problem(404, "Conversation not found")
    return row


@router.post("/conversations", status_code=201)
def start(user: CurrentUser):
    with db.tx() as conn:
        return conn.execute("INSERT INTO conversation (user_id) VALUES (%s) RETURNING id, title, created_at", (user["id"],)).fetchone()


@router.get("/conversations")
def conversations(user: CurrentUser):
    with db.tx() as conn:
        return conn.execute(
            "SELECT id, title, last_message_at FROM conversation WHERE user_id = %s ORDER BY last_message_at DESC LIMIT 30",
            (user["id"],),
        ).fetchall()


@router.get("/conversations/{conversation_id}/messages")
def messages(conversation_id: int, user: CurrentUser):
    with db.tx() as conn:
        _conversation(conn, conversation_id, user["id"])
        rows = conn.execute(
            """SELECT m.id, m.role, m.content, m.citations, m.cards, m.created_at, f.rating
               FROM message m LEFT JOIN feedback f ON f.message_id = m.id AND f.user_id = %s
               WHERE m.conversation_id = %s AND m.role <> 'tool' ORDER BY m.id""",
            (user["id"], conversation_id),
        ).fetchall()
        statuses = {a["id"]: a["status"] for a in conn.execute(
            "SELECT id, status FROM chat_action WHERE conversation_id = %s", (conversation_id,)).fetchall()}
    for row in rows:  # cards show their current state, not the one they were saved with
        for card in row["cards"]:
            card["status"] = statuses.get(card.get("action_id"), card.get("status"))
    return rows


@router.post("/conversations/{conversation_id}/messages")
def send(conversation_id: int, body: MessageIn, user: CurrentUser):
    """The answer streams back as server-sent events: status, token, card, citations, done or error."""
    with db.tx() as conn:
        conversation = _conversation(conn, conversation_id, user["id"])
        limit = get_setting(conn, "chat_messages_per_hour")
    ok, retry_after = cache.allow(f"rl:chat:{user['id']}", limit, 3600)  # the provider's free quota is shared by everyone
    if not ok:
        raise Problem(429, "Too many messages", f"You can send {limit} messages an hour.", retry_after=retry_after)
    return StreamingResponse(loop.run_turn(conversation, user, body.text.strip(), utcnow()), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _decide(action_id: int, user: dict, confirm: bool) -> dict:
    now = utcnow()
    with db.tx() as conn:
        action = conn.execute("SELECT * FROM chat_action WHERE id = %s AND user_id = %s FOR UPDATE",
                              (action_id, user["id"])).fetchone()
        if action is None:
            raise Problem(404, "Not found")
        if action["status"] != "proposed":
            raise Problem(409, "Already decided", f"This card is {action['status']}.")
        if action["expires_at"] <= now:
            conn.execute("UPDATE chat_action SET status = 'expired', decided_at = %s WHERE id = %s", (now, action_id))
            return {"id": action_id, "status": "expired", "result": {"error": "This card expired. Ask again for a fresh one."}}
        status, result = "rejected", None
        if confirm:
            try:
                with conn.transaction():  # a refusal undoes only the attempt, not the card's new status
                    result = _execute(conn, action, user, now)
                status = "executed"
            except Problem as p:
                status, result = "failed", {"error": p.title + (f": {p.detail}" if p.detail else "")}
        conn.execute("UPDATE chat_action SET status = %s, decided_at = %s, result = %s WHERE id = %s",
                     (status, now, result, action_id))
        note = {"executed": f"The customer confirmed: {result and result.get('summary')}.",
                "failed": f"The customer confirmed, but it failed: {result and result.get('error')}.",
                "rejected": f"The customer declined the {action['kind']} proposal."}[status]
        conn.execute("INSERT INTO message (conversation_id, role, content) VALUES (%s, 'note', %s)", (action["conversation_id"], note))
    return {"id": action_id, "status": status, "result": result}


def _execute(conn, action: dict, user: dict, now) -> dict:
    """Runs the same service functions as the REST routes, which re-check everything."""
    p = action["payload"]
    if action["kind"] == "book":
        from datetime import datetime

        b = bookings.create_booking(conn, user["id"], p["station_id"], p["bike_type_id"],
                                    datetime.fromisoformat(p["start"]), datetime.fromisoformat(p["end"]), now)
        return {"booking_id": b["id"], "code": b["code"], "pay": True,
                "summary": f"booked {b['code']}, {rupees(b['quoted_paise'])}, waiting for payment"}
    booking = get_booking(conn, p["booking_id"], user["id"], for_update=True)
    if action["kind"] == "cancel":
        q = bookings.cancel_booking(conn, booking, user["id"], now, reason="cancelled in chat")
        return {"booking_id": booking["id"], "summary": f"cancelled {booking['code']}, refund {rupees(q['refund_paise'])}"}
    if action["kind"] == "extend":
        from datetime import datetime

        r = bookings.extend_booking(conn, booking, datetime.fromisoformat(p["end"]), now)
        return {"booking_id": booking["id"],
                "summary": f"extended {booking['code']} to {fmt(r['booking']['end_at'])}, {rupees(r['extra_paise'])} added as a due"}
    raise Problem(422, "Unknown action")


@router.post("/actions/{action_id}/confirm")
def confirm(action_id: int, user: CurrentUser):
    return _decide(action_id, user, True)


@router.post("/actions/{action_id}/reject")
def reject(action_id: int, user: CurrentUser):
    return _decide(action_id, user, False)


@router.post("/messages/{message_id}/feedback", status_code=204)
def feedback(message_id: int, body: FeedbackIn, user: CurrentUser):
    with db.tx() as conn:
        msg = conn.execute(
            """SELECT m.id FROM message m JOIN conversation c ON c.id = m.conversation_id
               WHERE m.id = %s AND c.user_id = %s AND m.role = 'assistant'""",
            (message_id, user["id"]),
        ).fetchone()
        if msg is None:
            raise Problem(404, "Message not found")
        conn.execute(
            """INSERT INTO feedback (message_id, user_id, rating, comment) VALUES (%s, %s, %s, %s)
               ON CONFLICT (message_id, user_id) DO UPDATE SET rating = EXCLUDED.rating, comment = EXCLUDED.comment""",
            (message_id, user["id"], body.rating, body.comment),
        )
        if body.rating == -1:
            conn.execute("INSERT INTO flagged_answer (message_id, reason) VALUES (%s, 'thumbs_down') ON CONFLICT DO NOTHING",
                         (message_id,))
            conn.execute("UPDATE answer_cache SET disabled_at = now() WHERE source_message_id = %s", (message_id,))


@admin.get("/flagged")
def flagged(status: Literal["open", "reviewed", "dismissed"] = "open"):
    with db.tx() as conn:
        return conn.execute(
            """SELECT f.id, f.reason, f.status, f.note, f.created_at, m.id AS message_id, m.content AS answer, m.citations, m.model,
                      (SELECT content FROM message q WHERE q.conversation_id = m.conversation_id AND q.role = 'user'
                       AND q.id < m.id ORDER BY q.id DESC LIMIT 1) AS question,
                      (SELECT coalesce(json_agg(json_build_object('tool', t.tool_name, 'arguments', t.arguments, 'status', t.status)
                                                ORDER BY t.id), '[]') FROM tool_call t WHERE t.message_id = m.id) AS tools
               FROM flagged_answer f JOIN message m ON m.id = f.message_id
               WHERE f.status = %s ORDER BY f.id DESC LIMIT 100""",
            (status,),
        ).fetchall()


@admin.patch("/flagged/{flag_id}", status_code=204)
def review(flag_id: int, body: FlagPatch, admin_user: Admin):
    with db.tx() as conn:
        if not conn.execute("UPDATE flagged_answer SET status = %s, note = %s, reviewer_id = %s WHERE id = %s RETURNING id",
                            (body.status, body.note, admin_user["id"], flag_id)).fetchone():
            raise Problem(404, "Not found")


@admin.get("/query-log")
def query_log(limit: int = 50):
    with db.tx() as conn:
        return conn.execute(
            """SELECT q.id, q.query, q.timings, q.created_at,
                      (SELECT coalesce(json_agg(c.section_path ORDER BY array_position(q.reranked_ids, c.id)), '[]')
                       FROM chunk c WHERE c.id = ANY(q.reranked_ids)) AS top_sections
               FROM query_log q ORDER BY q.id DESC LIMIT %s""",
            (max(1, min(limit, 200)),),
        ).fetchall()


@admin.get("/eval-runs")
def eval_runs():
    with db.tx() as conn:
        return conn.execute(
            "SELECT id, kind, git_sha, provider, model, started_at, finished_at, metrics FROM eval_run ORDER BY id DESC LIMIT 50"
        ).fetchall()
