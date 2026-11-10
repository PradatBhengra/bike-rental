import redis

from app.config import settings

r = redis.Redis.from_url(settings.redis_url, decode_responses=True)


def allow(key: str, limit: int, window_s: int) -> tuple[bool, int]:
    """Fixed-window rate limit. Returns (allowed, seconds until the window resets)."""
    r.set(key, 0, nx=True, ex=window_s)  # starts the window only if it is not running
    count = r.incr(key)
    if count <= limit:
        return True, 0
    return False, max(r.ttl(key), 1)
