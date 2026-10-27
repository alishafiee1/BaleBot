from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from balebot.config import Settings
from balebot.db import create_all
from balebot.main import build_state
from balebot.net import safe_http
from balebot.net.safe_http import SafeHttp
from balebot.sandbox.incus import IncusResult
from balebot.services.model_client import ModelError
from balebot.state import AppState
from balebot.vault import Vault


class FakeModel:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = False
        self.reply: str | None = None

    async def chat(self, *, base_url: str, api_key: str, model: str, messages: list) -> str:
        self.calls.append({"base_url": base_url, "model": model, "messages": messages})
        if self.fail:
            raise ModelError("boom")
        return self.reply or f"echo:{messages[-1]['content']}"


class FakeBale:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.keyboards: list = []
        self.deleted: list[tuple[int, int]] = []
        self.documents: list[tuple[int, str, bytes]] = []
        self.actions: list[tuple[int, str]] = []

    async def send_message(self, chat_id: int, text: str, keyboard=None) -> None:
        self.sent.append((chat_id, text))
        self.keyboards.append(keyboard)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))

    async def send_document(self, chat_id: int, name: str, data: bytes) -> None:
        self.documents.append((chat_id, name, data))

    async def answer_callback(self, callback_id: str) -> None:
        pass

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append((chat_id, action))

    async def get_updates(self, offset: int, timeout: int = 30) -> list:
        return []

    async def set_webhook(self, url: str) -> bool:
        return True


class FakeProvider:
    """Stands in for any API host the user points the bot at."""

    def __init__(self) -> None:
        self.valid_key = "sk-valid-1234567890"
        self.extra_keys: set[str] = set()  # more keys the fake host accepts (e.g. the base key)
        self.models = ["gpt-a", "gpt-b"]
        self.seen: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        path = request.url.path
        if path == "/":
            return httpx.Response(200, text="<html><body>API docs</body></html>")
        accepted = {f"Bearer {k}" for k in (self.valid_key, *self.extra_keys)}
        if request.headers.get("authorization") not in accepted:
            return httpx.Response(401)
        if path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": m} for m in self.models]})
        if path.endswith("/chat/completions"):
            body = json.loads(request.content)
            content = "from-provider:" + body["model"]
            return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})
        return httpx.Response(404)


@pytest.fixture(autouse=True)
def public_dns(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_resolve(host: str) -> list[str]:
        return ["93.184.216.34"]

    monkeypatch.setattr(safe_http, "resolve_public", fake_resolve)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite://",
        webhook_secret="s3cret",
        base_model_api_key="test-key",
        default_daily_quota=3,
        session_secret="x" * 40,
        vault_key=Vault.generate_key(),
        cookie_secure=False,
        _env_file=None,
    )


@pytest.fixture
def model() -> FakeModel:
    return FakeModel()


@pytest.fixture
def bale() -> FakeBale:
    return FakeBale()


@pytest.fixture
def provider() -> FakeProvider:
    return FakeProvider()


@pytest.fixture
def incus() -> FakeIncus:
    return FakeIncus()


@pytest.fixture
async def agent_state(
    settings: Settings, model: FakeModel, bale: FakeBale, provider: FakeProvider, incus: FakeIncus
) -> AsyncIterator[AppState]:
    agent_settings = settings.model_copy(
        update={
            "sandbox_enabled": True,
            "proxy_secret": "p" * 32,
            "default_daily_quota": 20,
            "sandbox_idle_minutes": 30,
        }
    )
    http = SafeHttp(transport=httpx.MockTransport(provider.handler))
    st = build_state(agent_settings, model_client=model, bale=bale, http=http, incus=incus)
    await create_all(st.engine)
    try:
        yield st
    finally:
        await st.engine.dispose()


@pytest.fixture
async def state(
    settings: Settings, model: FakeModel, bale: FakeBale, provider: FakeProvider
) -> AsyncIterator[AppState]:
    http = SafeHttp(transport=httpx.MockTransport(provider.handler))
    st = build_state(settings, model_client=model, bale=bale, http=http)
    await create_all(st.engine)
    try:
        yield st
    finally:
        await st.engine.dispose()


class FakeIncus:
    """Records every incus call and answers like a small, well-behaved server."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.instances: set[str] = set()
        self.pushed: dict[str, bytes] = {}
        self.outbox: dict[str, bytes] = {}
        self.agent_output = "agent says hi"
        self.agent_rc = 0
        self.fail: set[str] = set()  # subcommand keys that should fail: "copy", "disk", "start"
        self.gate: asyncio.Event | None = None  # hold the agent run until set

    def _res(self, ok: bool = True, out: bytes = b"") -> IncusResult:
        return IncusResult(0 if ok else 1, out, "" if ok else "failed")

    async def run(
        self, *args: str, stdin: bytes | None = None, timeout: float = 60.0
    ) -> IncusResult:
        self.calls.append(args)
        cmd = args[0]
        if cmd == "info":
            return self._res(args[1] in self.instances)
        if cmd == "copy":
            if "copy" in self.fail:
                return self._res(False)
            self.instances.add(args[2])
            return self._res()
        if cmd == "config" and args[1:3] == ("device", "override"):
            return self._res("disk" not in self.fail)
        if cmd == "delete":
            self.instances.discard(args[1])
            return self._res()
        if cmd == "start":
            return self._res("start" not in self.fail)
        if cmd == "file" and args[1] == "push":
            self.pushed[args[3]] = stdin or b""
            return self._res()
        if cmd == "file" and args[1] == "pull":
            name = args[2].rsplit("/", 1)[-1]
            return self._res(name in self.outbox, self.outbox.get(name, b""))
        if cmd == "exec" and "hermes" in args:
            if self.gate is not None:
                await self.gate.wait()
            return IncusResult(
                self.agent_rc,
                self.agent_output.replace("{token}", args[args.index("--api-key") + 1]).encode(),
                "",
            )
        if cmd == "exec" and "find" in args:
            return self._res(out="\n".join(self.outbox).encode())
        return self._res()

    def hermes_call(self) -> tuple[str, ...]:
        return next(c for c in self.calls if "hermes" in c)
