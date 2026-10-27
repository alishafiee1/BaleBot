import asyncio
import json
import time

import httpx
import pytest
from sqlalchemy import select

from balebot import texts
from balebot.models import Sandbox, User, utcnow
from balebot.sandbox import tokens
from balebot.sandbox.manager import sandbox_name
from balebot.sandbox.proxy import create_proxy_app
from balebot.state import AppState

SECRET = "p" * 32


# ---- tokens ------------------------------------------------------------------------------
def test_token_roundtrip_and_tamper() -> None:
    t = tokens.issue(SECRET, 7, ttl=60, max_calls=5)
    got = tokens.verify(SECRET, t)
    assert (got.user_id, got.max_calls) == (7, 5)
    with pytest.raises(tokens.BadToken):
        tokens.verify("q" * 32, t)  # other secret
    with pytest.raises(tokens.BadToken):
        tokens.verify(SECRET, t.replace(".7.", ".8.", 1))  # another user id
    with pytest.raises(tokens.BadToken):
        tokens.verify(SECRET, t, now=time.time() + 3600)  # expired
    with pytest.raises(tokens.BadToken):
        tokens.verify(SECRET, "garbage")
    with pytest.raises(ValueError):
        tokens.issue("short", 1, ttl=1, max_calls=1)


# ---- the /agent command ----------------------------------------------------------------------
async def say(st: AppState, uid: int, text: str):
    sent: list[str] = []

    async def progress(note: str) -> None:
        sent.append(note)

    reply = await st.chat.handle_message(uid, None, text, progress=progress)
    return reply, sent


async def test_agent_off_by_default(state) -> None:
    reply, _ = await say(state, 1, "/agent do something")
    assert reply.text == texts.AGENT_OFF


async def test_agent_usage_when_empty(agent_state) -> None:
    reply, _ = await say(agent_state, 1, "/agent")
    assert reply.text == texts.AGENT_USAGE


async def test_agent_runs_in_isolated_sandbox(agent_state, incus) -> None:
    incus.outbox = {"result.txt": b"data"}
    reply, progress = await say(agent_state, 1, "/agent summarize my notes")
    assert reply.text == "agent says hi" and reply.files == [("result.txt", b"data")]
    assert progress == [texts.AGENT_RUNNING]
    assert "u1" in incus.instances

    # creation: limits and flags are set, and the disk quota is applied
    copy = next(c for c in incus.calls if c[0] == "copy")
    assert copy[1:3] == ("agent-base", "u1")
    flags = [copy[i + 1] for i, a in enumerate(copy) if a == "-c"]
    assert {"limits.cpu=1", "limits.memory=512MiB", "limits.processes=256"} <= set(flags)
    assert {"security.privileged=false", "security.nesting=false"} <= set(flags)
    assert any(
        c[:4] == ("config", "device", "override", "u1") and "size=3GiB" in c for c in incus.calls
    )

    # the task goes in as a file, never on a command line
    assert incus.pushed["u1/home/agent/in/query.txt"] == b"summarize my notes"
    assert not any("summarize" in a for c in incus.calls for a in c)

    call = incus.hermes_call()
    assert call[call.index("--user") + 1] == "1000"  # not root
    assert "sh" not in call and "-c" not in call[call.index("--") :]  # no shell string
    assert call[call.index("--base-url") + 1] == "http://10.77.0.1:8001/v1"
    token = call[call.index("--api-key") + 1]
    assert token.startswith("bbt.") and tokens.verify(SECRET, token).user_id > 0


async def test_agent_never_gets_provider_keys(agent_state, incus) -> None:
    await say(agent_state, 1, "/agent hi")
    blob = " ".join(a for c in incus.calls for a in c)
    assert "test-key" not in blob  # the base model key stays out of every sandbox call


async def test_agent_output_never_leaks_the_run_token(agent_state, incus) -> None:
    incus.agent_output = "my token is {token}"
    reply, _ = await say(agent_state, 1, "/agent hi")
    assert "bbt." not in reply.text and "[redacted]" in reply.text


async def test_agent_charges_quota_and_refunds_on_infrastructure_failure(
    agent_state, incus
) -> None:
    await say(agent_state, 1, "/agent ok")
    reply, _ = await say(agent_state, 1, "/usage")
    assert "5 از 20" in reply.text
    incus.instances.clear()
    incus.fail = {"copy"}
    failed, _ = await say(agent_state, 1, "/agent again")
    assert failed.text == texts.AGENT_FAILED
    assert "5 از 20" in (await say(agent_state, 1, "/usage"))[0].text  # refunded


async def test_agent_not_enough_quota_creates_nothing(agent_state, incus) -> None:
    for _ in range(4):
        await say(agent_state, 1, "/agent x")
    reply, _ = await say(agent_state, 1, "/agent x")  # 5th run would need 25 > 20
    assert reply.text == texts.AGENT_NO_QUOTA.format(cost=5)
    assert len([c for c in incus.calls if "hermes" in c]) == 4


async def test_agent_on_own_key_is_free(agent_state, incus) -> None:
    from tests.test_keys import add_openai_key  # reuse the add-key flow

    await add_openai_key(agent_state)
    await say(agent_state, 1, "/model 2")
    reply, _ = await say(agent_state, 1, "/agent hi")
    assert reply.text == "agent says hi"
    assert "0 از 20" in (await say(agent_state, 1, "/usage"))[0].text


async def test_disk_quota_failure_never_runs_unlimited(agent_state, incus) -> None:
    incus.fail = {"disk"}
    reply, _ = await say(agent_state, 1, "/agent hi")
    assert reply.text == texts.AGENT_FAILED
    assert "u1" not in incus.instances  # destroyed instead of running without a disk limit
    assert not any("hermes" in c for c in incus.calls)


async def test_second_run_waits_for_the_first(agent_state, incus) -> None:
    incus.gate = asyncio.Event()
    first = asyncio.create_task(say(agent_state, 1, "/agent long job"))
    for _ in range(50):
        if agent_state.sandbox.is_busy(1):
            break
        await asyncio.sleep(0.01)
    busy, _ = await say(agent_state, 1, "/agent another")
    assert busy.text == texts.AGENT_BUSY
    incus.gate.set()
    assert (await first)[0].text == "agent says hi"


async def test_timeout_keeps_partial_text(agent_state, incus) -> None:
    incus.agent_rc = 124
    incus.agent_output = "partial"
    reply, _ = await say(agent_state, 1, "/agent hi")
    assert reply.text.startswith("partial") and texts.AGENT_TIMEOUT in reply.text


async def test_files_named_by_the_sandbox_are_not_trusted(agent_state, incus) -> None:
    incus.outbox = {"../etc-passwd": b"x", ".hidden": b"x", "a b.txt": b"ok", "x" * 200: b"x"}
    reply, _ = await say(agent_state, 1, "/agent hi")
    assert [n for n, _ in reply.files] == ["a b.txt"]


async def test_other_users_get_other_sandboxes(agent_state, incus) -> None:
    await say(agent_state, 1, "/agent a")
    await say(agent_state, 2, "/agent b")
    assert incus.instances == {"u1", "u2"}
    assert incus.pushed["u1/home/agent/in/query.txt"] == b"a"
    assert incus.pushed["u2/home/agent/in/query.txt"] == b"b"
    assert sandbox_name(7) == "u7"


async def test_reap_stops_idle_and_destroys_old(agent_state, incus) -> None:
    await say(agent_state, 1, "/agent a")
    await say(agent_state, 2, "/agent b")
    async with agent_state.sessionmaker() as s:
        rows = {r.user_id: r for r in (await s.scalars(select(Sandbox))).all()}
        user_ids = {u.bale_id: u.id for u in (await s.scalars(select(User))).all()}
        rows[user_ids[1]].last_used = utcnow().replace(year=2000)  # very old
        rows[user_ids[2]].last_used = utcnow() - __import__("datetime").timedelta(hours=2)
        await s.commit()
    stopped, destroyed = await agent_state.sandbox.reap()
    assert (stopped, destroyed) == (1, 1)
    assert "u1" not in incus.instances and "u2" in incus.instances


# ---- the LLM proxy ----
@pytest.fixture
async def proxy(agent_state, provider):
    provider.extra_keys.add("test-key")  # the base model key the proxy adds
    app = create_proxy_app(agent_state)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as c:
        yield c


def bearer(uid: int = 1, calls: int = 10, ttl: int = 60) -> dict[str, str]:
    return {"Authorization": "Bearer " + tokens.issue(SECRET, uid, ttl=ttl, max_calls=calls)}


async def make_user(st: AppState, bale_id: int = 1) -> int:
    await st.chat.handle(bale_id, None, "/start")
    async with st.sessionmaker() as s:
        return await s.scalar(select(User.id).where(User.bale_id == bale_id))


BODY = {"model": "evil-override", "messages": [{"role": "user", "content": "hi"}]}


async def test_proxy_rejects_missing_bad_and_expired_tokens(proxy, agent_state) -> None:
    uid = await make_user(agent_state)
    assert (await proxy.post("/v1/chat/completions", json=BODY)).status_code == 401
    assert (
        await proxy.post("/v1/chat/completions", json=BODY, headers={"Authorization": "Bearer x"})
    ).status_code == 401
    expired = bearer(uid, ttl=-5)
    assert (await proxy.post("/v1/chat/completions", json=BODY, headers=expired)).status_code == 401


async def test_proxy_forces_users_model_and_drops_unknown_fields(
    proxy, agent_state, provider
) -> None:
    uid = await make_user(agent_state)
    body = {**BODY, "user": "x", "logit_bias": {"1": 1}, "max_tokens": 10**6}
    r = await proxy.post("/v1/chat/completions", json=body, headers=bearer(uid))
    assert r.status_code == 200
    sent = json.loads(provider.seen[-1].content)
    assert sent["model"] == "glm-4.7-flash"  # not the sandbox's choice
    assert "user" not in sent and "logit_bias" not in sent
    assert sent["max_tokens"] == 8192
    assert provider.seen[-1].headers["authorization"] == "Bearer test-key"
    assert "test-key" not in r.text  # the key is added upstream and never returned


async def test_proxy_caps_model_calls_per_run(proxy, agent_state) -> None:
    uid = await make_user(agent_state)
    h = bearer(uid, calls=2)
    codes = [
        (await proxy.post("/v1/chat/completions", json=BODY, headers=h)).status_code
        for _ in range(3)
    ]
    assert codes == [200, 200, 429]


async def test_proxy_uses_the_users_own_key_when_selected(proxy, agent_state, provider) -> None:
    from tests.test_keys import KEY, add_openai_key

    await add_openai_key(agent_state, uid=1)
    await say(agent_state, 1, "/model 3")
    async with agent_state.sessionmaker() as s:
        uid = await s.scalar(select(User.id).where(User.bale_id == 1))
    r = await proxy.post("/v1/chat/completions", json=BODY, headers=bearer(uid))
    assert r.status_code == 200 and "from-provider:gpt-b" in r.text
    last = provider.seen[-1]
    assert (
        last.headers["authorization"] == f"Bearer {KEY}"
        and last.headers["host"] == "api.openai.com"
    )
    assert KEY not in r.text


async def test_proxy_blocks_banned_users_and_unknown_users(proxy, agent_state) -> None:
    uid = await make_user(agent_state)
    async with agent_state.sessionmaker() as s:
        (await s.get(User, uid)).banned = True
        await s.commit()
    assert (
        await proxy.post("/v1/chat/completions", json=BODY, headers=bearer(uid))
    ).status_code == 403
    assert (
        await proxy.post("/v1/chat/completions", json=BODY, headers=bearer(9999))
    ).status_code == 403


async def test_proxy_streams_sse_when_asked(proxy, agent_state) -> None:
    uid = await make_user(agent_state)
    r = await proxy.post("/v1/chat/completions", json={**BODY, "stream": True}, headers=bearer(uid))
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.text.rstrip().endswith("data: [DONE]") and "from-provider" in r.text


async def test_proxy_validates_body(proxy, agent_state) -> None:
    uid = await make_user(agent_state)
    h = bearer(uid)
    assert (
        await proxy.post("/v1/chat/completions", content=b"not json", headers=h)
    ).status_code == 400
    assert (
        await proxy.post("/v1/chat/completions", json={"model": "x"}, headers=h)
    ).status_code == 400


async def test_agent_menu_button_accepts_next_message_as_task(agent_state: AppState, incus) -> None:
    cb = await agent_state.chat.handle_callback(1, "menu:agent")
    assert cb is not None and cb.text == texts.AGENT_PROMPT
    assert cb.keyboard == texts.CANCEL_KEYBOARD

    reply = await agent_state.chat.handle_message(1, None, "یک اسکریپت تست اجرا کن")
    assert reply.text == "agent says hi"
    assert incus.pushed["u1/home/agent/in/query.txt"] == "یک اسکریپت تست اجرا کن".encode()


async def test_proxy_retries_on_upstream_timeout_and_429(
    agent_state: AppState, provider
) -> None:
    provider.extra_keys.add("test-key")
    calls = 0
    orig_handler = provider.handler

    def flaky_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/chat/completions"):
            calls += 1
            if calls == 1:
                raise httpx.ReadTimeout("first attempt timed out")
            if calls == 2:
                return httpx.Response(429, json={"error": {"message": "rate limit"}})
        return orig_handler(request)

    agent_state.http = __import__("balebot.net.safe_http", fromlist=["SafeHttp"]).SafeHttp(
        transport=httpx.MockTransport(flaky_handler)
    )
    app = create_proxy_app(agent_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as c:
        uid = await make_user(agent_state)
        r = await c.post("/v1/chat/completions", json=BODY, headers=bearer(uid))
        assert r.status_code == 200
        assert calls == 3
