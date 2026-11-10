import json
from datetime import date, datetime, time
from decimal import Decimal

from psycopg.types.range import Range


def _default(o):
    if isinstance(o, (datetime, date, time)):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o)
    if isinstance(o, Range):
        return {"lower": _default(o.lower) if o.lower else None, "upper": _default(o.upper) if o.upper else None}
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def dumps(obj) -> str:
    return json.dumps(obj, default=_default)
