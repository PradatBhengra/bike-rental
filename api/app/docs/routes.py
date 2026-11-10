from fastapi import APIRouter, Depends, Form, UploadFile

from app import db
from app.deps import Admin, require_role
from app.docs import chunking, ingest
from app.errors import Problem

router = APIRouter(tags=["help"])
admin = APIRouter(prefix="/admin/documents", tags=["admin"], dependencies=[Depends(require_role("admin"))])


@router.get("/docs")
def list_docs():
    with db.tx() as conn:
        return conn.execute(
            "SELECT slug, title, doc_type FROM document WHERE active_version_id IS NOT NULL ORDER BY doc_type, title"
        ).fetchall()


@router.get("/docs/{slug}")
def get_doc(slug: str):
    """The active version, split into the same sections the chatbot cites, each with its anchor."""
    with db.tx() as conn:
        doc = conn.execute(
            """SELECT d.slug, d.title, d.doc_type, d.route_geojson, v.markdown, v.version_no
               FROM document d JOIN document_version v ON v.id = d.active_version_id WHERE d.slug = %s""",
            (slug,),
        ).fetchone()
    if doc is None:
        raise Problem(404, "Page not found")
    return {**{k: doc[k] for k in ("slug", "title", "doc_type", "route_geojson", "version_no")},
            "sections": chunking.split_sections(doc.pop("markdown"), doc["title"])}


@admin.get("")
def admin_list():
    with db.tx() as conn:
        return conn.execute(
            """SELECT d.id, d.slug, d.title, d.doc_type, av.version_no AS active_version,
                      lv.version_no AS latest_version, lv.status AS latest_status, lv.error AS latest_error,
                      (SELECT count(*) FROM chunk c WHERE c.version_id = d.active_version_id)::int AS chunks
               FROM document d
               LEFT JOIN document_version av ON av.id = d.active_version_id
               LEFT JOIN LATERAL (SELECT * FROM document_version WHERE document_id = d.id
                                  ORDER BY version_no DESC LIMIT 1) lv ON true
               ORDER BY d.doc_type, d.title"""
        ).fetchall()


@admin.post("", status_code=202)
async def upload(file: UploadFile, admin_user: Admin, doc_type: str | None = Form(None)):
    """A Markdown file with front matter (slug, title, type). The same text as a stored version is a no-op."""
    raw = await file.read()
    if len(raw) > 500_000:
        raise Problem(413, "File too large", "The limit is 500 KB.")
    try:
        meta, body = chunking.parse_front_matter(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Problem(422, "Invalid request", "Upload a UTF-8 Markdown file.") from None
    if not meta.get("slug") or not meta.get("title"):
        raise Problem(422, "Invalid request", "The front matter needs slug and title.")
    with db.tx() as conn:
        version_id, created = ingest.add_version(conn, meta["slug"], meta["title"], doc_type or meta.get("type", "policy"),
                                                 body, meta.get("bike_type"), meta.get("route"))
    return {"version_id": version_id, "created": created}


@admin.post("/{document_id}/reindex", status_code=202)
def reindex(document_id: int, admin_user: Admin):
    from app.jobs import enqueue

    with db.tx() as conn:
        row = conn.execute(
            "SELECT id FROM document_version WHERE document_id = %s ORDER BY version_no DESC LIMIT 1", (document_id,)
        ).fetchone()
        if row is None:
            raise Problem(404, "Document not found")
        conn.execute("UPDATE document_version SET status = 'queued' WHERE id = %s", (row["id"],))
        enqueue(conn, "index_version", {"version_id": row["id"]})
    return {"version_id": row["id"]}
