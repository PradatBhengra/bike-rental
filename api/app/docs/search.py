"""Hybrid retrieval: vector search and keyword search, merged with reciprocal rank fusion, then reranked."""

import time

from app.docs import models

VECTOR_K = 30
KEYWORD_K = 30
RERANK_K = 20
TOP_K = 5
RRF_K = 60

ACTIVE = "FROM chunk c JOIN document d ON d.active_version_id = c.version_id WHERE (%(type)s::text IS NULL OR d.doc_type = %(type)s)"


def rrf(ranked_lists: list[list[int]], k: int = RRF_K) -> list[int]:
    """Reciprocal rank fusion: each list adds 1 / (k + rank) to an id's score. Ranks start at 1."""
    scores: dict[int, float] = {}
    for ranked in ranked_lists:
        for rank, chunk_id in enumerate(ranked, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda cid: (-scores[cid], cid))


def search(conn, query: str, doc_type: str | None = None) -> dict:
    """Returns the top sections and what each stage found, for the query log."""
    timings = {}
    t = time.perf_counter()
    qvec = models.vector_literal(models.embed_query(query))
    timings["embed_ms"] = round((time.perf_counter() - t) * 1000)

    t = time.perf_counter()
    # A filtered HNSW search can return fewer rows than asked for; iterative scan keeps reading the index until it has them.
    conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
    vector_ids = [r["id"] for r in conn.execute(
        f"SELECT c.id {ACTIVE} ORDER BY c.embedding <=> %(q)s::vector LIMIT {VECTOR_K}", {"q": qvec, "type": doc_type}
    ).fetchall()]
    # websearch_to_tsquery would AND every word, so a long question matches almost nothing. Instead: the question's
    # English lexemes (stemmed, stopwords dropped) joined with OR, read back with the 'simple' config so they are not
    # stemmed twice. A question that is all stopwords gives NULL and the keyword list is just empty.
    keyword_ids = [r["id"] for r in conn.execute(
        f"""WITH q AS (SELECT to_tsquery('simple', string_agg(quote_literal(lexeme), ' | ')) AS q
                       FROM unnest(to_tsvector('english', %(text)s)))
            SELECT c.id {ACTIVE.replace('FROM chunk c', 'FROM q, chunk c')} AND q.q IS NOT NULL AND c.tsv @@ q.q
            ORDER BY ts_rank(c.tsv, q.q) DESC, c.id LIMIT {KEYWORD_K}""",
        {"text": query, "type": doc_type},
    ).fetchall()]
    timings["search_ms"] = round((time.perf_counter() - t) * 1000)

    fused_ids = rrf([vector_ids, keyword_ids])[:RERANK_K]
    rows = {r["id"]: r for r in conn.execute(
        """SELECT c.id, c.section_no, c.anchor, c.heading, c.section_path, c.body, d.slug, d.title, d.doc_type
           FROM chunk c JOIN document d ON d.id = (SELECT document_id FROM document_version WHERE id = c.version_id)
           WHERE c.id = ANY(%s)""",
        (fused_ids,),
    ).fetchall()}
    t = time.perf_counter()
    candidates = [rows[cid] for cid in fused_ids]
    scores = models.reranker().rerank(query, [f"{r['section_path']}\n{r['body']}" for r in candidates]) if candidates else []
    ranked = [r for _, r in sorted(zip(scores, candidates, strict=True), key=lambda pair: -pair[0])][:TOP_K]
    timings["rerank_ms"] = round((time.perf_counter() - t) * 1000)
    return {"results": ranked, "vector_ids": vector_ids, "keyword_ids": keyword_ids, "fused_ids": fused_ids,
            "reranked_ids": [r["id"] for r in ranked], "timings": timings}


def log_query(conn, query: str, found: dict, message_id: int | None = None, tool_call_id: int | None = None) -> None:
    conn.execute(
        """INSERT INTO query_log (message_id, tool_call_id, query, vector_ids, keyword_ids, fused_ids, reranked_ids, timings)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
        (message_id, tool_call_id, query, found["vector_ids"], found["keyword_ids"], found["fused_ids"],
         found["reranked_ids"], found["timings"]),
    )
