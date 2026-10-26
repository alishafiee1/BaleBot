"""LLM proxy for sandboxed agents.

The Hermes agent inside a sandbox has no provider keys. It talks to this proxy with a
short-lived token, and the proxy picks the user's model, adds the real key, and calls the
provider. Even a fully compromised sandbox can therefore only make a bounded number of model
calls as that one user; it can never read a key.
"""

import json
import time
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from balebot.models import User
from balebot.net.safe_http import UnsafeURL
from balebot.sandbox import tokens
from balebot.services import runtime_settings
from balebot.services.model_client import ModelError
from balebot.state import AppState

MAX_BODY = 2_000_000
MAX_TOKENS_CAP = 8192
# only these OpenAI fields are forwarded; anything else the sandbox sends is dropped
ALLOWED = {
    "messages",
    "tools",
    "tool_choice",
    "temperature",
    "top_p",
    "max_tokens",
    "stop",
    "response_format",
    "parallel_tool_calls",
}


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": {"message": message, "type": "proxy_error"}}, status_code=status)


def to_sse(data: dict[str, Any]) -> Iterator[str]:
    """Wrap a complete chat completion as an SSE stream (the upstream call is never streamed)."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    delta: dict[str, Any] = {"role": "assistant"}
    if msg.get("content"):
        delta["content"] = msg["content"]
    if msg.get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **tc} for i, tc in enumerate(msg["tool_calls"])]
    base = {
        "id": data.get("id", "chatcmpl-proxy"),
        "object": "chat.completion.chunk",
        "model": data.get("model", "agent"),
    }
    yield (
        "data: "
        + json.dumps({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]})
        + "\n\n"
    )
    final = {
        **base,
        "choices": [
            {"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}
        ],
    }
    if data.get("usage"):
        final["usage"] = data["usage"]
    yield "data: " + json.dumps(final) + "\n\n"
    yield "data: [DONE]\n\n"


def create_proxy_app(state: AppState) -> FastAPI:
    app = FastAPI(title="BaleBot LLM proxy", docs_url=None, redoc_url=None, openapi_url=None)
    calls: dict[str, tuple[int, int]] = {}  # run_id -> (count, expires)

    def bump(tok: tokens.AgentToken) -> int:
        now = time.time()
        for rid in [r for r, (_, exp) in calls.items() if exp < now]:
            del calls[rid]
        count = calls.get(tok.run_id, (0, tok.expires))[0] + 1
        calls[tok.run_id] = (count, tok.expires)
        return count

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/models")
    async def models() -> dict[str, Any]:
        return {"object": "list", "data": [{"id": "agent", "object": "model"}]}

    @app.post("/v1/chat/completions", response_model=None)
    async def completions(request: Request) -> Any:
        auth = request.headers.get("authorization", "")
        try:
            tok = tokens.verify(state.settings.proxy_secret, auth.removeprefix("Bearer ").strip())
        except tokens.BadToken:
            return _error(401, "invalid or expired token")
        raw = await request.body()
        if len(raw) > MAX_BODY:
            return _error(413, "request too large")
        try:
            body = json.loads(raw)
        except ValueError:
            return _error(400, "invalid json")
        if not isinstance(body, dict) or not isinstance(body.get("messages"), list):
            return _error(400, "messages required")
        clean = {k: v for k, v in body.items() if k in ALLOWED}
        if isinstance(clean.get("max_tokens"), int):
            clean["max_tokens"] = min(clean["max_tokens"], MAX_TOKENS_CAP)
        if bump(tok) > tok.max_calls:
            return _error(429, "model call limit reached for this run")

        async with state.sessionmaker() as s:
            user = await s.get(User, tok.user_id)
            cfg = await runtime_settings.load(s, state.settings)
        if user is None or user.banned:
            return _error(403, "not allowed")
        try:
            route = (
                await state.chat.keys.resolve(user.id, user.selected_model)
                if state.chat.keys
                else None
            )
            if route is not None:
                assert state.chat.keys is not None
                data = await state.chat.keys.raw_chat(route, clean)
            else:
                res = await state.http.request(
                    "POST",
                    cfg.base_url.rstrip("/") + "/chat/completions",
                    headers={"Authorization": "Bearer " + state.settings.base_model_api_key},
                    json={**clean, "model": cfg.base_model, "stream": False},
                    timeout=180.0,
                )
                if res.status != 200:
                    raise ModelError(f"http {res.status}")
                data = json.loads(res.body)
        except (ModelError, UnsafeURL, ValueError):
            return _error(502, "model call failed")
        if body.get("stream"):
            return StreamingResponse(to_sse(data), media_type="text/event-stream")
        return JSONResponse(data)

    return app
