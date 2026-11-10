import logging

from fastapi import Request, Response
from fastapi.exceptions import RequestValidationError
from psycopg import errors as pgerr
from starlette.exceptions import HTTPException

from app.jsonutil import dumps

log = logging.getLogger("app")
PROBLEM = "application/problem+json"


class Problem(Exception):
    """An error the client should see, sent as RFC 9457 problem details."""

    def __init__(self, status: int, title: str, detail: str | None = None, **extra):
        self.status, self.title, self.detail, self.extra = status, title, detail, extra


def _problem(status: int, title: str, detail: str | None = None, headers: dict | None = None, **extra) -> Response:
    body = {"type": "about:blank", "title": title, "status": status, **extra}
    if detail:
        body["detail"] = detail
    return Response(dumps(body), status, headers=headers, media_type=PROBLEM)


def install(app):
    @app.exception_handler(Problem)
    async def problem(request: Request, exc: Problem):
        headers = {"Retry-After": str(exc.extra["retry_after"])} if "retry_after" in exc.extra else None
        return _problem(exc.status, exc.title, exc.detail, headers, **exc.extra)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError):
        errors = [{"loc": list(e["loc"]), "msg": e["msg"]} for e in exc.errors()]
        detail = "; ".join(f"{'.'.join(map(str, e['loc'][1:])) or 'body'}: {e['msg']}" for e in errors)
        return _problem(422, "Invalid request", detail, errors=errors)

    @app.exception_handler(HTTPException)
    async def http(request: Request, exc: HTTPException):
        return _problem(exc.status_code, str(exc.detail))

    @app.exception_handler(pgerr.IntegrityError)
    async def refused_by_database(request: Request, exc: pgerr.IntegrityError):
        # The database is the last line of defence; a refusal there is the client's mistake, not a crash.
        status, title = {
            pgerr.UniqueViolation: (409, "Already exists"),
            pgerr.ExclusionViolation: (409, "Overlaps an existing entry"),
            pgerr.ForeignKeyViolation: (409, "Refers to something that does not exist, or is still in use"),
        }.get(type(exc), (422, "Not allowed by a database rule"))
        log.info("database refused %s %s: %s", request.method, request.url.path, exc.diag.constraint_name)
        return _problem(status, title, constraint=exc.diag.constraint_name)

    @app.exception_handler(Exception)
    async def crash(request: Request, exc: Exception):
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return _problem(500, "Something went wrong")
