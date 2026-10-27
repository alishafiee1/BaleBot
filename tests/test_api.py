import httpx
import pytest

from balebot.main import create_app
from balebot.models import AdminUser
from balebot.security import hash_password
from balebot.state import AppState


@pytest.fixture
async def client(state: AppState):
    app = create_app(state)
    async with app.router.lifespan_context(app):
        async with state.sessionmaker() as s:
            s.add(AdminUser(username="admin", password_hash=hash_password("pw-for-tests")))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


def update(text: str | None, user_id: int = 7) -> dict:
    msg = {"chat": {"id": user_id}, "from": {"id": user_id, "username": "u"}}
    if text is not None:
        msg["text"] = text
    return {"update_id": 1, "message": msg}


async def test_webhook_rejects_wrong_secret(client, bale) -> None:
    r = await client.post("/bale/webhook/wrong", json=update("hi"))
    assert r.status_code == 404 and bale.sent == []


async def test_webhook_replies(client, bale) -> None:
    r = await client.post("/bale/webhook/s3cret", json=update("hi"))
    assert r.status_code == 200
    assert bale.sent == [(7, "echo:hi")]


async def test_non_text_message_gets_unsupported(client, bale) -> None:
    await client.post("/bale/webhook/s3cret", json=update(None))
    assert "متنی" in bale.sent[0][1]


async def test_admin_requires_auth(client) -> None:
    assert (await client.get("/admin/api/settings")).status_code == 401
    bad = await client.get("/admin/api/settings", auth=("admin", "nope"))
    assert bad.status_code == 401


async def test_admin_changes_base_model_and_quota(client, model, bale) -> None:
    auth = ("admin", "pw-for-tests")
    r = await client.put(
        "/admin/api/settings", auth=auth, json={"base_model": "glm-x", "daily_quota": 1}
    )
    assert r.status_code == 200 and r.json()["base_model"] == "glm-x"
    await client.post("/bale/webhook/s3cret", json=update("a"))
    await client.post("/bale/webhook/s3cret", json=update("b"))
    assert model.calls[0]["model"] == "glm-x"
    assert len(model.calls) == 1  # quota 1 from the panel


async def test_admin_rejects_http_base_url(client) -> None:
    r = await client.put(
        "/admin/api/settings", auth=("admin", "pw-for-tests"), json={"base_url": "http://x.example"}
    )
    assert r.status_code == 422


async def test_webhook_ignores_duplicate_update(client, bale, model) -> None:
    payload = update("hello-once")
    r1 = await client.post("/bale/webhook/s3cret", json=payload)
    r2 = await client.post("/bale/webhook/s3cret", json=payload)
    assert r1.status_code == 200 and r1.json() == {"ok": True}
    assert r2.status_code == 200 and r2.json() == {"ok": True, "duplicate_ignored": True}
    assert bale.sent == [(7, "echo:hello-once")]
    assert len(model.calls) == 1
    assert (7, "typing") in bale.actions
