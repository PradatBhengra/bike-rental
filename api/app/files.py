"""Object storage (Garage, through the S3 API). Postgres keeps one file_object row per stored object."""

import hashlib

import boto3
from botocore.config import Config

from app.config import settings
from app.errors import Problem

_client = None


def s3():
    global _client
    if _client is None:
        _client = boto3.client(
            "s3", endpoint_url=settings.s3_endpoint, region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key, aws_secret_access_key=settings.s3_secret_key,
            config=Config(s3={"addressing_style": "path"}, request_checksum_calculation="when_required",
                          response_checksum_validation="when_required"),
        )
    return _client


def put(conn, key: str, content_type: str, data: bytes, uploaded_by: int | None = None) -> int:
    s3().put_object(Bucket=settings.s3_bucket, Key=key, Body=data, ContentType=content_type)
    return conn.execute(
        """INSERT INTO file_object (bucket, object_key, content_type, size_bytes, sha256, uploaded_by)
           VALUES (%s, %s, %s, %s, %s, %s)
           ON CONFLICT (object_key) DO UPDATE SET size_bytes = EXCLUDED.size_bytes, sha256 = EXCLUDED.sha256
           RETURNING id""",
        (settings.s3_bucket, key, content_type, len(data), hashlib.sha256(data).hexdigest(), uploaded_by),
    ).fetchone()["id"]


def get(conn, file_id: int) -> tuple[str, str, bytes]:
    """Returns (file name, content type, bytes)."""
    row = conn.execute("SELECT * FROM file_object WHERE id = %s", (file_id,)).fetchone()
    if row is None:
        raise Problem(404, "File not found")
    data = s3().get_object(Bucket=row["bucket"], Key=row["object_key"])["Body"].read()
    return row["object_key"].rsplit("/", 1)[-1], row["content_type"], data
