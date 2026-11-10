"""Document versions and indexing. Run `python -m app.docs.ingest ../corpus` to load the help documents."""

import hashlib
import pathlib
import sys

from psycopg.types.json import Jsonb

from app import db
from app.docs import chunking, models
from app.errors import Problem
from app.jobs import enqueue

DOC_TYPES = ("policy", "manual", "error_codes", "repair", "riding", "route", "station", "pass")


def add_version(conn, slug: str, title: str, doc_type: str, markdown: str, bike_type_slug: str | None = None,
                route: list | None = None, source_file_id: int | None = None) -> tuple[int, bool]:
    """Saves a new version and queues it for indexing. Returns (version id, created). The same text is a no-op."""
    if doc_type not in DOC_TYPES:
        raise Problem(422, "Invalid request", f"doc_type must be one of {', '.join(DOC_TYPES)}")
    if not chunking.split_sections(markdown, title):
        raise Problem(422, "Invalid request", "The document has no text.")
    bike_type_id = None
    if bike_type_slug:
        row = conn.execute("SELECT id FROM bike_type WHERE slug = %s", (bike_type_slug,)).fetchone()
        bike_type_id = row["id"] if row else None
    doc = conn.execute(
        """INSERT INTO document (slug, title, doc_type, bike_type_id, route_geojson) VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (slug) DO UPDATE SET title = EXCLUDED.title, doc_type = EXCLUDED.doc_type,
                 bike_type_id = EXCLUDED.bike_type_id, route_geojson = EXCLUDED.route_geojson
           RETURNING id""",
        (slug, title, doc_type, bike_type_id,
         Jsonb({"type": "LineString", "coordinates": route}) if route else None),
    ).fetchone()
    if bike_type_id and doc_type == "manual":
        conn.execute("UPDATE bike_type SET manual_document_id = %s WHERE id = %s", (doc["id"], bike_type_id))
    content_hash = hashlib.sha256(markdown.encode()).hexdigest()
    existing = conn.execute(
        "SELECT id FROM document_version WHERE document_id = %s AND content_hash = %s", (doc["id"], content_hash)
    ).fetchone()
    if existing:
        return existing["id"], False
    version = conn.execute(
        """INSERT INTO document_version (document_id, version_no, content_hash, markdown, source_file_id)
           VALUES (%s, (SELECT coalesce(max(version_no), 0) + 1 FROM document_version WHERE document_id = %s), %s, %s, %s)
           RETURNING id""",
        (doc["id"], doc["id"], content_hash, markdown, source_file_id),
    ).fetchone()
    enqueue(conn, "index_version", {"version_id": version["id"]}, dedupe_key=f"index_version:{version['id']}")
    return version["id"], True


def index_version(conn, version_id: int) -> int:
    """Chunks and embeds a version, then makes it the active one. Text seen before reuses its stored vector."""
    version = conn.execute(
        """SELECT v.*, d.title FROM document_version v JOIN document d ON d.id = v.document_id
           WHERE v.id = %s FOR UPDATE OF v""",
        (version_id,),
    ).fetchone()
    conn.execute("DELETE FROM chunk WHERE version_id = %s", (version_id,))
    parts = chunking.chunks(version["markdown"], version["title"])
    known = {
        r["content_hash"]: r["embedding"]
        for r in conn.execute(
            "SELECT DISTINCT ON (content_hash) content_hash, embedding::text AS embedding FROM chunk WHERE content_hash = ANY(%s)",
            ([p["content_hash"] for p in parts],),
        ).fetchall()
    }
    new = [p for p in parts if p["content_hash"] not in known]
    if new:
        vectors = models.embedder().embed([f"{p['section_path']}\n{p['body']}" for p in new])
        known.update({p["content_hash"]: models.vector_literal(v) for p, v in zip(new, vectors, strict=True)})
    for p in parts:
        conn.execute(
            """INSERT INTO chunk (version_id, ord, section_no, section_path, anchor, heading, body, content_hash,
                                  token_count, embedding)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)""",
            (version_id, p["ord"], p["section_no"], p["section_path"], p["anchor"], p["heading"], p["body"],
             p["content_hash"], p["token_count"], known[p["content_hash"]]),
        )
    # one transaction switches the active version and retires cached answers
    conn.execute("UPDATE document_version SET status = 'ready', indexed_at = now(), error = NULL WHERE id = %s", (version_id,))
    conn.execute("UPDATE document SET active_version_id = %s WHERE id = %s", (version_id, version["document_id"]))
    conn.execute("UPDATE corpus_state SET version = version + 1")
    return len(parts)


def index_version_job(conn, payload: dict) -> None:
    index_version(conn, payload["version_id"])


def prune_old_versions(conn, keep: int = 3) -> int:
    """Deletes chunks of old versions, so the HNSW index holds mostly live rows. The markdown stays for history."""
    return conn.execute(
        """DELETE FROM chunk WHERE version_id IN (
               SELECT v.id FROM document_version v JOIN document d ON d.id = v.document_id
               WHERE v.id <> d.active_version_id
                 AND v.version_no <= (SELECT max(version_no) FROM document_version WHERE document_id = d.id) - %s)""",
        (keep,),
    ).rowcount


def load_file(conn, path: pathlib.Path, index_now: bool = True) -> bool:
    meta, body = chunking.parse_front_matter(path.read_text(encoding="utf-8"))
    version_id, created = add_version(conn, meta["slug"], meta["title"], meta["type"], body, meta.get("bike_type"),
                                      meta.get("route"))
    if created and index_now:
        index_version(conn, version_id)
        conn.execute("UPDATE job SET status = 'done', finished_at = now() WHERE dedupe_key = %s",
                     (f"index_version:{version_id}",))
    return created


def main(folder: str) -> None:
    db.open_pool()
    files = sorted(pathlib.Path(folder).glob("*.md"))
    with db.tx() as conn:
        changed = [p.name for p in files if load_file(conn, p)]
    models.embedder(), models.reranker()  # downloads the models into the cache volume now
    print(f"{len(files)} documents, {len(changed)} new or changed: {', '.join(changed) or 'none'}")
    db.close_pool()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "../corpus")
