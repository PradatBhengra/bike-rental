from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Request, Response
from pydantic import AfterValidator, BaseModel, EmailStr, Field

from app import cache, db
from app.auth import service
from app.auth.security import access_token, hash_password, verify_password
from app.config import settings
from app.deps import CurrentUser, client_ip
from app.errors import Problem
from app.timeutil import utcnow

router = APIRouter(tags=["auth"])
COOKIE = "refresh_token"


def _fits_bcrypt(value: str) -> str:
    if len(value.encode()) > 72:  # bcrypt reads only the first 72 bytes
        raise ValueError("Password must be at most 72 bytes")
    return value


Password = Annotated[str, Field(min_length=8), AfterValidator(_fits_bcrypt)]


class SignupIn(BaseModel):
    email: EmailStr
    password: Password
    full_name: str = Field(min_length=1, max_length=80)


class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(max_length=200)


class TokenIn(BaseModel):
    token: str


class EmailIn(BaseModel):
    email: EmailStr


class ResetIn(BaseModel):
    token: str
    password: Password


class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    phone: str | None
    role: Literal["customer", "staff", "admin"]
    staff_station_id: int | None
    tracking_consent: bool
    email_verified_at: datetime | None
    created_at: datetime


class SessionOut(BaseModel):
    access_token: str
    user: UserOut


class ProfileIn(BaseModel):
    full_name: str | None = Field(None, min_length=1, max_length=80)
    phone: str | None = Field(None, pattern=r"^\+?[0-9 ]{7,15}$")
    tracking_consent: bool | None = None


class PasswordChangeIn(BaseModel):
    current_password: str
    new_password: Password


def _limit(key: str, limit: int, window_s: int) -> None:
    ok, retry_after = cache.allow(key, limit, window_s)
    if not ok:
        minutes = retry_after // 60 + 1
        raise Problem(429, "Too many attempts", f"Try again in {minutes} minutes.", retry_after=retry_after)


def _set_cookie(response: Response, raw: str) -> None:
    # Path-scoped: the browser sends the refresh token to the auth routes only.
    response.set_cookie(COOKIE, raw, max_age=settings.refresh_ttl_s, httponly=True, secure=settings.cookie_secure,
                        samesite="strict", path="/api/v1/auth")


def _start_session(conn, response: Response, user: dict) -> dict:
    raw, _ = service.issue_refresh(conn, user["id"], utcnow())
    _set_cookie(response, raw)
    return {"access_token": access_token(user), "user": user}


@router.post("/auth/signup", status_code=201, response_model=SessionOut)
def signup(body: SignupIn, request: Request, response: Response):
    ip = client_ip(request)
    _limit(f"rl:signup:{ip}", 10, 3600)
    with db.tx() as conn:
        user = service.signup(conn, body.email, body.password, body.full_name.strip(), ip, utcnow())
        return _start_session(conn, response, user)


@router.post("/auth/login", response_model=SessionOut)
def login(body: LoginIn, request: Request, response: Response):
    ip = client_ip(request)
    _limit(f"rl:login:ip:{ip}", 50, 900)
    _limit(f"rl:login:email:{body.email.lower()}", 10, 900)
    with db.tx() as conn:
        user = service.login(conn, body.email, body.password, ip)
        if user and user["blocked_at"] is None:
            return _start_session(conn, response, user)
    if user:
        raise Problem(403, "Account blocked", "Contact support.")
    raise Problem(401, "Wrong email or password")


@router.post("/auth/refresh", response_model=SessionOut)
def refresh(request: Request, response: Response, refresh_token: Annotated[str | None, Cookie()] = None):
    if refresh_token is None:
        raise Problem(401, "Not logged in")
    with db.tx() as conn:
        rotated = service.rotate(conn, refresh_token, client_ip(request), utcnow())
    if rotated is None:
        raise Problem(401, "Session expired", "Log in again.")
    user, raw = rotated
    _set_cookie(response, raw)
    return {"access_token": access_token(user), "user": user}


@router.post("/auth/logout", status_code=204)
def logout(request: Request, response: Response, refresh_token: Annotated[str | None, Cookie()] = None):
    if refresh_token:
        with db.tx() as conn:
            service.logout(conn, refresh_token, client_ip(request), utcnow())
    response.delete_cookie(COOKIE, path="/api/v1/auth")


@router.post("/auth/verify-email", status_code=204)
def verify_email(body: TokenIn):
    with db.tx() as conn:
        service.verify_email(conn, body.token, utcnow())


@router.post("/auth/resend-verification", status_code=202)
def resend_verification(user: CurrentUser):
    _limit(f"rl:verify:{user['id']}", 3, 3600)
    if user["email_verified_at"] is None:
        with db.tx() as conn:
            service.send_verify_email(conn, user, utcnow())
    return {}


@router.post("/auth/password-reset", status_code=202)
def password_reset(body: EmailIn, request: Request):
    _limit(f"rl:reset:{client_ip(request)}", 5, 3600)
    with db.tx() as conn:
        service.request_password_reset(conn, body.email, utcnow())
    return {}  # the same answer whether or not the email exists


@router.post("/auth/password-reset/confirm", status_code=204)
def password_reset_confirm(body: ResetIn, request: Request):
    with db.tx() as conn:
        service.confirm_password_reset(conn, body.token, body.password, client_ip(request), utcnow())


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return user


@router.patch("/me", response_model=UserOut)
def update_me(body: ProfileIn, user: CurrentUser):
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return user
    sets = ", ".join(f"{col} = %({col})s" for col in changes)  # column names come from ProfileIn, not the request
    with db.tx() as conn:
        return conn.execute(
            f"UPDATE app_user SET {sets} WHERE id = %(id)s RETURNING *", {**changes, "id": user["id"]}
        ).fetchone()


@router.post("/me/password", status_code=204)
def change_password(body: PasswordChangeIn, user: CurrentUser):
    if not verify_password(body.current_password, user["password_hash"]):
        raise Problem(400, "Current password is wrong")
    with db.tx() as conn:
        conn.execute(
            "UPDATE app_user SET password_hash = %s WHERE id = %s", (hash_password(body.new_password), user["id"])
        )
