import redis

from app.config import settings

r = redis.Redis.from_url(settings.redis_url, decode_responses=True)


def allow(key: str, limit: int, window_s: int) -> tuple[bool, int]:
    """Fixed-window rate limit. Returns (allowed, seconds until the window resets)."""
    pipe = r.pipeline()  # one round trip; EXPIRE NX sets the window only once, so no key is left without one
    pipe.incr(key)
    pipe.expire(key, window_s, nx=True)
    count, _ = pipe.execute()
    if count <= limit:
        return True, 0
    return False, max(r.ttl(key), 1)
