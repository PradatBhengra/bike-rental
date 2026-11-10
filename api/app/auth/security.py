import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
import jwt

from app.config import settings


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(settings.bcrypt_rounds)).decode()


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())


def access_token(user: dict, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    claims = {"sub": str(user["id"]), "role": user["role"], "iat": now, "exp": now + timedelta(seconds=settings.access_ttl_s)}
    return jwt.encode(claims, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    """Raises jwt.InvalidTokenError for a bad, tampered or expired token."""
    return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"], options={"require": ["sub", "exp"]})


def random_token() -> tuple[str, str]:
    """A token for the client and the hash we store; a database leak then reveals no usable token."""
    raw = secrets.token_urlsafe(32)
    return raw, sha256(raw)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()
