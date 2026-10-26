import re

import httpx
import pytest
from sqlalchemy import select

from balebot.main import create_app
from balebot.models import AdminUser, User
from balebot.panel import routes as panel
from balebot.security import hash_password
from balebot.state import AppState

PW = "initial-password"
NEW_PW = "a-much-better-password"


def token(html: str) -> str:
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    assert m, html[:300]
    return m.group(1)


@pytest.fixture
async def client(state: AppState):
    panel._fails.clear()
    app = create_app(state)
    async with app.router.lifespan_context(app):
        async with state.sessionmaker() as s:
            s.add(AdminUser(username="admin", password_hash=hash_password(PW)))
            await s.commit()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c


async def login(c: httpx.AsyncClient, password: str = PW) -> httpx.Response:
    page = await c.get("/panel/login")
    return await c.post(
        "/panel/login",
        data={"username": "admin", "password": password, "csrf": token(page.text)},
    )


async def full_login(c: httpx.AsyncClient) -> None:
    """Login and complete the forced password change."""
    r = await login(c)
    assert r.headers["location"] == "/panel/password"
    page = await c.get("/panel/password")
    r = await c.post(
        "/panel/password",
        data={"current": PW, "new": NEW_PW, "confirm": NEW_PW, "csrf": token(page.text)},
    )
    assert r.status_code == 303 and r.headers["location"] == "/panel/"


async def test_pages_require_login(client) -> None:
    for path in ("/panel/", "/panel/settings", "/panel/users"):
        r = await client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/panel/login"


async def test_login_wrong_password(client) -> None:
    r = await login(client, "wrong")
    assert r.status_code == 401 and "اشتباه" in r.text


async def test_login_without_csrf_is_rejected(client) -> None:
    r = await client.post("/panel/login", data={"username": "admin", "password": PW})
    assert r.status_code == 403


async def test_first_login_forces_password_change(client) -> None:
    await login(client)
    r = await client.get("/panel/settings")
    assert r.status_code == 303 and r.headers["location"] == "/panel/password"


async def test_password_rules(client) -> None:
    await login(client)
    page = await client.get("/panel/password")
    r = await client.post(
        "/panel/password",
        data={"current": PW, "new": "short", "confirm": "short", "csrf": token(page.text)},
    )
    assert r.status_code == 400 and "حداقل" in r.text


async def test_dashboard_after_full_login(client) -> None:
    await full_login(client)
    r = await client.get("/panel/")
    assert r.status_code == 200 and "کل کاربران" in r.text
    assert (await login(client, PW)).status_code == 401  # old password is gone


async def test_login_is_rate_limited(client) -> None:
    for _ in range(panel.MAX_FAILS):
        await login(client, "wrong")
    assert (await login(client, PW)).status_code == 429


async def test_settings_save_applies_to_bot(client, model, bale) -> None:
    await full_login(client)
    page = await client.get("/panel/settings")
    r = await client.post(
        "/panel/settings",
        data={
            "base_url": "https://models.example/v1",
            "base_model": "glm-panel",
            "system_prompt": "be brief",
            "daily_quota": "1",
            "csrf": token(page.text),
        },
    )
    assert r.status_code == 303
    await client.post(
        "/bale/webhook/s3cret",
        json={"update_id": 1, "message": {"chat": {"id": 5}, "from": {"id": 5}, "text": "hi"}},
    )
    call = model.calls[0]
    assert call["model"] == "glm-panel" and call["base_url"] == "https://models.example/v1"
    assert call["messages"][0]["content"] == "be brief"


async def test_settings_reject_http_url(client) -> None:
    await full_login(client)
    page = await client.get("/panel/settings")
    r = await client.post(
        "/panel/settings",
        data={
            "base_url": "http://insecure.example",
            "base_model": "m",
            "system_prompt": "",
            "daily_quota": "5",
            "csrf": token(page.text),
        },
    )
    assert r.status_code == 400 and "https" in r.text


async def test_ban_and_quota_override(client, state) -> None:
    await state.chat.handle(42, "<script>alert(1)</script>", "/start")
    await full_login(client)
    page = await client.get("/panel/users")
    assert "<script>alert(1)</script>" not in page.text  # escaped
    async with state.sessionmaker() as s:
        uid = await s.scalar(select(User.id).where(User.bale_id == 42))
    t = token(page.text)
    await client.post(f"/panel/users/{uid}/quota", data={"quota": "7", "csrf": t})
    await client.post(f"/panel/users/{uid}/ban", data={"csrf": t})
    async with state.sessionmaker() as s:
        user = await s.get(User, uid)
    assert user.daily_quota == 7 and user.banned is True
    assert "banned" not in (await state.chat.handle(42, None, "hi")).lower()
    assert "مسدود" in await state.chat.handle(42, None, "hi")


async def test_post_without_csrf_is_rejected(client) -> None:
    await full_login(client)
    r = await client.post(
        "/panel/settings",
        data={"base_url": "https://x.example", "base_model": "x", "daily_quota": "5"},
    )
    assert r.status_code == 403
