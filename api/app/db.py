import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import JsonbDumper, set_json_dumps
from psycopg_pool import ConnectionPool

from app.config import settings
from app.jsonutil import dumps

set_json_dumps(dumps)
# A dict parameter is sent as jsonb. A list still becomes a Postgres array, so wrap JSON lists in Jsonb().
psycopg.adapters.register_dumper(dict, JsonbDumper)

pool: ConnectionPool | None = None


def open_pool(url: str | None = None) -> None:
    global pool
    pool = ConnectionPool(
        url or settings.database_url,
        kwargs={"row_factory": dict_row, "options": "-c timezone=UTC"},
        min_size=1,
        max_size=10,
        open=True,
    )
    pool.wait()


def close_pool() -> None:
    global pool
    if pool is not None:
        pool.close()
        pool = None


def tx():
    """A connection whose transaction commits when the block ends cleanly and rolls back on an exception."""
    return pool.connection()
