"""Golden-set evaluation, run by hand because of provider rate limits.

    python -m app.eval retrieval   # recall@5 and MRR of search_docs (no LLM needed)
    python -m app.eval tools       # did the model pick the expected tool and arguments on its first step

Each run is saved in eval_run / eval_result with the git commit, so runs can be compared.
"""

import json
import pathlib
import subprocess
import sys
from datetime import datetime

from app import db
from app.chat import tools as chat_tools
from app.chat.llm import LLM
from app.chat.loop import PROMPT
from app.config import settings
from app.docs import search
from app.timeutil import IST, utcnow

GOLDEN = pathlib.Path(__file__).parent.parent / "eval" / "golden.json"


def _cases(conn, kind: str) -> list[dict]:
    for case in json.loads(GOLDEN.read_text(encoding="utf-8"))[kind]:
        conn.execute(
            """INSERT INTO eval_case (kind, question, expected) VALUES (%s, %s, %s)
               ON CONFLICT (kind, question) DO UPDATE SET expected = EXCLUDED.expected""",
            (kind, case["question"], case["expected"]),
        )
    return conn.execute("SELECT * FROM eval_case WHERE kind = %s AND active ORDER BY id", (kind,)).fetchall()


def retrieval(conn, case: dict) -> dict:
    found = search.search(conn, case["question"])
    got = [f"{r['slug']}#{r['anchor']}" for r in found["results"]]
    rank = next((i + 1 for i, ref in enumerate(got) if ref in case["expected"]), None)
    return {"passed": rank is not None, "rank": rank, "got": got}


def tool_choice(conn, case: dict) -> dict:
    now = datetime.now(IST)
    messages = [{"role": "system", "content": PROMPT.format(company=settings.company_name, name="Asha",
                                                            now=now.strftime("%A %d %B %Y, %H:%M"))},
                {"role": "user", "content": case["question"]}]
    calls = [p for p in LLM().stream(messages, chat_tools.schemas(), "small") if p[0] == "tool"]
    if not calls:
        return {"passed": False, "got": None}
    name, args = calls[0][2], json.loads(calls[0][3] or "{}")
    want = case["expected"]
    ok = name == want["tool"] and all(str(v).lower() in str(args.get(k, "")).lower() for k, v in want.get("args", {}).items())
    return {"passed": ok, "got": {"tool": name, "args": args}}


def main(kind: str) -> None:
    db.open_pool()
    sha = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip() or None
    check = {"retrieval": retrieval, "tools": tool_choice}[kind]
    with db.tx() as conn:
        cases = _cases(conn, "retrieval" if kind == "retrieval" else "tool")
        provider = "local" if kind == "retrieval" else ("groq" if settings.groq_api_key else "openrouter")
        model = settings.embed_model if kind == "retrieval" else settings.llm_small
        run_id = conn.execute("INSERT INTO eval_run (kind, git_sha, provider, model) VALUES (%s, %s, %s, %s) RETURNING id",
                              (kind, sha, provider, model)).fetchone()["id"]
    results = []
    for case in cases:
        with db.tx() as conn:
            result = check(conn, case)
            conn.execute("INSERT INTO eval_result (run_id, case_id, passed, details) VALUES (%s, %s, %s, %s)",
                         (run_id, case["id"], result["passed"], result))
        results.append(result)
        print(("PASS " if result["passed"] else "FAIL ") + case["question"])
    metrics = {"cases": len(results), "passed": sum(r["passed"] for r in results)}
    metrics["accuracy" if kind == "tools" else "recall_at_5"] = round(metrics["passed"] / max(len(results), 1), 3)
    if kind == "retrieval":
        metrics["mrr"] = round(sum(1 / r["rank"] for r in results if r["rank"]) / max(len(results), 1), 3)
    with db.tx() as conn:
        conn.execute("UPDATE eval_run SET metrics = %s, finished_at = %s WHERE id = %s", (metrics, utcnow(), run_id))
    print(metrics)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "retrieval")
