import uuid
from datetime import datetime, timedelta

from psycopg import errors as pgerr

from app import mail
from app.auth.security import hash_password, random_token, sha256, verify_password
from app.config import settings
from app.errors import Problem

VERIFY_TTL = timedelta(hours=24)
RESET_TTL = timedelta(hours=1)
# Checked when the email is unknown, so a wrong email takes as long as a wrong password.
_DUMMY_HASH = hash_password("not-a-real-password")


def log_event(conn, user_id: int | None, email: str | None, event: str, ip: str | None) -> None:
    conn.execute(
        "INSERT INTO auth_event (user_id, email_tried, event, ip) VALUES (%s, %s, %s, %s)",
        (user_id, email, event, ip),
    )


def signup(conn, email: str, password: str, full_name: str, ip: str | None, now: datetime) -> dict:
    try:
        with conn.transaction():  # a savepoint: the unique violation must not abort the outer transaction
            user = conn.execute(
                "INSERT INTO app_user (email, password_hash, full_name) VALUES (%s, %s, %s) RETURNING *",
                (email, hash_password(password), full_name),
            ).fetchone()
    except pgerr.UniqueViolation:
        raise Problem(409, "Email already registered", "Log in, or reset your password.") from None
    log_event(conn, user["id"], email, "signup", ip)
    send_verify_email(conn, user, now)
    return user


def login(conn, email: str, password: str, ip: str | None) -> dict | None:
    """Returns None on a wrong email or password. The caller raises after commit, so the audit row is kept."""
    user = conn.execute("SELECT * FROM app_user WHERE email = %s", (email,)).fetchone()
    if not verify_password(password, user["password_hash"] if user else _DUMMY_HASH) or user is None:
        log_event(conn, user["id"] if user else None, email, "login_fail", ip)
        return None
    log_event(conn, user["id"], email, "login_ok", ip)
    return user


def issue_refresh(conn, user_id: int, now: datetime, family_id: uuid.UUID | None = None) -> tuple[str, int]:
    raw, token_hash = random_token()
    row = conn.execute(
        "INSERT INTO refresh_token (user_id, token_hash, family_id, expires_at) VALUES (%s, %s, %s, %s) RETURNING id",
        (user_id, token_hash, family_id or uuid.uuid4(), now + timedelta(seconds=settings.refresh_ttl_s)),
    ).fetchone()
    return raw, row["id"]


def rotate(conn, raw: str, ip: str | None, now: datetime) -> tuple[dict, str] | None:
    """Swaps a refresh token for a new one. Returns None instead of raising, so a family revoke commits."""
    row = conn.execute("SELECT * FROM refresh_token WHERE token_hash = %s FOR UPDATE", (sha256(raw),)).fetchone()
    if row is None:
        return None
    if row["revoked_at"] is not None:
        # A token that was already swapped came back: treat it as stolen and end every session it led to.
        conn.execute(
            "UPDATE refresh_token SET revoked_at = %s WHERE family_id = %s AND revoked_at IS NULL",
            (now, row["family_id"]),
        )
        log_event(conn, row["user_id"], None, "refresh_reuse", ip)
        return None
    if row["expires_at"] <= now:
        return None
    user = conn.execute("SELECT * FROM app_user WHERE id = %s", (row["user_id"],)).fetchone()
    if user["blocked_at"] is not None:
        return None
    new_raw, new_id = issue_refresh(conn, user["id"], now, row["family_id"])
    conn.execute("UPDATE refresh_token SET revoked_at = %s, replaced_by = %s WHERE id = %s", (now, new_id, row["id"]))
    log_event(conn, user["id"], None, "refresh", ip)
    return user, new_raw


def logout(conn, raw: str, ip: str | None, now: datetime) -> None:
    row = conn.execute(
        "UPDATE refresh_token SET revoked_at = %s WHERE token_hash = %s AND revoked_at IS NULL RETURNING user_id",
        (now, sha256(raw)),
    ).fetchone()
    if row:
        log_event(conn, row["user_id"], None, "logout", ip)


def _new_email_token(conn, user_id: int, purpose: str, ttl: timedelta, now: datetime) -> str:
    raw, token_hash = random_token()
    conn.execute(
        "INSERT INTO email_token (user_id, purpose, token_hash, expires_at) VALUES (%s, %s, %s, %s)",
        (user_id, purpose, token_hash, now + ttl),
    )
    return raw


def _use_email_token(conn, raw: str, purpose: str, now: datetime) -> int:
    row = conn.execute(
        """UPDATE email_token SET used_at = %s
           WHERE token_hash = %s AND purpose = %s AND used_at IS NULL AND expires_at > %s
           RETURNING user_id""",
        (now, sha256(raw), purpose, now),
    ).fetchone()
    if row is None:
        raise Problem(400, "This link is invalid or has expired", "Ask for a new one.")
    return row["user_id"]


def send_verify_email(conn, user: dict, now: datetime) -> None:
    raw = _new_email_token(conn, user["id"], "verify", VERIFY_TTL, now)
    mail.queue_email(
        conn, user["id"], user["email"], "Confirm your email",
        f"Hi {user['full_name']},\n\nConfirm your email address:\n{settings.app_url}/verify-email?token={raw}\n\n"
        "The link works once and expires in 24 hours.",
        kind="verify_email",
    )


def verify_email(conn, raw: str, now: datetime) -> None:
    user_id = _use_email_token(conn, raw, "verify", now)
    conn.execute("UPDATE app_user SET email_verified_at = coalesce(email_verified_at, %s) WHERE id = %s", (now, user_id))


def request_password_reset(conn, email: str, now: datetime) -> None:
    """Does nothing for an unknown email; the route answers the same either way."""
    user = conn.execute("SELECT * FROM app_user WHERE email = %s AND blocked_at IS NULL", (email,)).fetchone()
    if user is None:
        return
    raw = _new_email_token(conn, user["id"], "reset", RESET_TTL, now)
    mail.queue_email(
        conn, user["id"], user["email"], "Reset your password",
        f"Hi {user['full_name']},\n\nChoose a new password:\n{settings.app_url}/reset-password?token={raw}\n\n"
        "The link works once and expires in 1 hour. If you did not ask for this, ignore this email.",
        kind="password_reset",
    )


def confirm_password_reset(conn, raw: str, password: str, ip: str | None, now: datetime) -> None:
    user_id = _use_email_token(conn, raw, "reset", now)
    conn.execute("UPDATE app_user SET password_hash = %s WHERE id = %s", (hash_password(password), user_id))
    # A reset ends every session: whoever knew the old password is logged out.
    conn.execute("UPDATE refresh_token SET revoked_at = %s WHERE user_id = %s AND revoked_at IS NULL", (now, user_id))
    log_event(conn, user_id, None, "reset", ip)
