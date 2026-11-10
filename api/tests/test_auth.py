import re
from datetime import timedelta

from app import db
from app.auth import service
from app.timeutil import utcnow
from tests.conftest import PASSWORD, auth

SIGNUP = {"email": "asha@example.com", "password": "correct horse battery", "full_name": "Asha Kumari"}


def emailed_token(kind: str) -> str:
    with db.tx() as conn:
        job = conn.execute(
            "SELECT payload FROM job WHERE kind = 'send_email' AND payload->>'kind' = %s ORDER BY id DESC", (kind,)
        ).fetchone()
    return re.search(r"token=([\w-]+)", job["payload"]["text"]).group(1)


def refresh_with(client, token: str):
    client.cookies.clear()
    return client.post("/api/v1/auth/refresh", headers={"Cookie": f"refresh_token={token}"})


def test_readyz(client):
    assert client.get("/readyz").json() == {"db": "ok", "redis": "ok"}


def test_signup_starts_a_session(client):
    r = client.post("/api/v1/auth/signup", json=SIGNUP)
    assert r.status_code == 201
    body = r.json()
    assert body["user"]["email"] == "asha@example.com" and body["user"]["role"] == "customer"
    assert "password_hash" not in body["user"]
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie and "Path=/api/v1/auth" in cookie
    me = client.get("/api/v1/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200 and me.json()["full_name"] == "Asha Kumari"


def test_duplicate_email_ignores_case(client):
    client.post("/api/v1/auth/signup", json=SIGNUP)
    r = client.post("/api/v1/auth/signup", json={**SIGNUP, "email": "ASHA@Example.com"})
    assert r.status_code == 409
    assert r.headers["content-type"] == "application/problem+json"


def test_password_rules(client):
    short = client.post("/api/v1/auth/signup", json={**SIGNUP, "password": "short"})
    assert short.status_code == 422
    # 25 three-byte characters = 75 bytes: bcrypt would silently drop the last three
    long = client.post("/api/v1/auth/signup", json={**SIGNUP, "password": "य" * 25})
    assert long.status_code == 422 and "72 bytes" in long.json()["detail"]


def test_login_wrong_password_is_audited(client, make_user):
    user = make_user()
    r = client.post("/api/v1/auth/login", json={"email": user["email"], "password": "wrong password"})
    assert r.status_code == 401
    unknown = client.post("/api/v1/auth/login", json={"email": "nobody@example.com", "password": "wrong password"})
    assert unknown.status_code == 401 and unknown.json()["title"] == r.json()["title"]
    with db.tx() as conn:
        events = conn.execute("SELECT user_id, event FROM auth_event ORDER BY id").fetchall()
    assert events == [{"user_id": user["id"], "event": "login_fail"}, {"user_id": None, "event": "login_fail"}]


def test_login_rate_limit(client, make_user):
    user = make_user()
    for _ in range(10):
        assert client.post("/api/v1/auth/login", json={"email": user["email"], "password": "wrong password"}).status_code == 401
    r = client.post("/api/v1/auth/login", json={"email": user["email"], "password": PASSWORD})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_refresh_rotates_and_reuse_revokes_the_family(client, make_user):
    user = make_user()
    login = client.post("/api/v1/auth/login", json={"email": user["email"], "password": PASSWORD})
    first = login.cookies["refresh_token"]

    r = client.post("/api/v1/auth/refresh")
    assert r.status_code == 200 and r.json()["user"]["id"] == user["id"]
    second = r.cookies["refresh_token"]
    assert second != first

    # the old token comes back: someone copied it
    assert refresh_with(client, first).status_code == 401
    # ...so the newest token is dead too
    assert refresh_with(client, second).status_code == 401
    with db.tx() as conn:
        live = conn.execute("SELECT count(*) AS n FROM refresh_token WHERE revoked_at IS NULL").fetchone()["n"]
        reuse = conn.execute("SELECT count(*) AS n FROM auth_event WHERE event = 'refresh_reuse'").fetchone()["n"]
    assert live == 0 and reuse == 2  # both rejected tokens are logged


def test_expired_refresh_token(make_user):
    user = make_user()
    with db.tx() as conn:
        raw, _ = service.issue_refresh(conn, user["id"], utcnow() - timedelta(days=15))
        assert service.rotate(conn, raw, None, utcnow()) is None


def test_logout_revokes(client, make_user):
    user = make_user()
    client.post("/api/v1/auth/login", json={"email": user["email"], "password": PASSWORD})
    token = client.cookies["refresh_token"]
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert refresh_with(client, token).status_code == 401


def test_blocked_user(client, make_user):
    user = make_user(blocked_at=utcnow())
    assert client.post("/api/v1/auth/login", json={"email": user["email"], "password": PASSWORD}).status_code == 403
    assert client.get("/api/v1/me", headers=auth(user)).status_code == 403


def test_bad_tokens(client):
    assert client.get("/api/v1/me").status_code == 401
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer not.a.token"}).status_code == 401


def test_verify_email_link_works_once(client):
    client.post("/api/v1/auth/signup", json=SIGNUP)
    token = emailed_token("verify_email")
    assert client.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 204
    assert client.post("/api/v1/auth/verify-email", json={"token": token}).status_code == 400
    with db.tx() as conn:
        assert conn.execute("SELECT email_verified_at FROM app_user").fetchone()["email_verified_at"] is not None


def test_password_reset(client, make_user):
    user = make_user()
    client.post("/api/v1/auth/login", json={"email": user["email"], "password": PASSWORD})
    assert client.post("/api/v1/auth/password-reset", json={"email": "nobody@example.com"}).status_code == 202
    assert client.post("/api/v1/auth/password-reset", json={"email": user["email"]}).status_code == 202
    token = emailed_token("password_reset")

    new = {"token": token, "password": "a brand new password"}
    assert client.post("/api/v1/auth/password-reset/confirm", json=new).status_code == 204
    assert client.post("/api/v1/auth/password-reset/confirm", json=new).status_code == 400
    assert client.post("/api/v1/auth/refresh").status_code == 401  # the reset ended the old session
    assert client.post("/api/v1/auth/login", json={"email": user["email"], "password": PASSWORD}).status_code == 401
    ok = client.post("/api/v1/auth/login", json={"email": user["email"], "password": "a brand new password"})
    assert ok.status_code == 200


def test_update_profile(client, make_user):
    user = make_user()
    r = client.patch("/api/v1/me", json={"tracking_consent": True, "phone": "+91 98350 12345"}, headers=auth(user))
    assert r.status_code == 200 and r.json()["tracking_consent"] is True
    assert client.patch("/api/v1/me", json={"phone": "abc"}, headers=auth(user)).status_code == 422
    # a field outside ProfileIn is ignored, so nobody promotes themselves
    client.patch("/api/v1/me", json={"role": "admin", "full_name": "Still A Customer"}, headers=auth(user))
    assert client.get("/api/v1/me", headers=auth(user)).json()["role"] == "customer"
