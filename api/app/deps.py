import ipaddress
from typing import Annotated

import jwt
from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app import db
from app.auth.security import decode_access_token
from app.errors import Problem

bearer = HTTPBearer(auto_error=False)


def current_user(creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> dict:
    if creds is None:
        raise Problem(401, "Not logged in")
    try:
        claims = decode_access_token(creds.credentials)
    except jwt.InvalidTokenError:
        raise Problem(401, "Session expired") from None
    with db.tx() as conn:
        user = conn.execute("SELECT * FROM app_user WHERE id = %s", (int(claims["sub"]),)).fetchone()
    if user is None:
        raise Problem(401, "Not logged in")
    if user["blocked_at"] is not None:
        raise Problem(403, "Account blocked", "Contact support.")
    return user


def require_role(*roles: str):
    def check(user: Annotated[dict, Depends(current_user)]) -> dict:
        if user["role"] not in roles:
            raise Problem(403, "Not allowed")
        return user

    return check


CurrentUser = Annotated[dict, Depends(current_user)]
Staff = Annotated[dict, Depends(require_role("staff"))]
Admin = Annotated[dict, Depends(require_role("admin"))]


def client_ip(request: Request) -> str | None:
    host = request.client.host if request.client else None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return None
