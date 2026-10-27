"""LLM proxy for sandboxed agents.

The Hermes agent inside a sandbox has no provider keys. It talks to this proxy with a
short-lived token, and the proxy picks the user's model, adds the real key, and calls the
upstream provider. Bind this app only to the Incus bridge address (e.g. 10.77.0.1:8001).
"""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from balebot.models import User
from balebot.net.safe_http import UnsafeURL
from balebot.sandbox import tokens
from balebot.services import runtime_settings
from balebot.services.model_client import ModelError, _zai_fallbacks
from balebot.state import AppState

MAX_BODY = 512_000
MAX_TOKENS_CAP = 8192
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


async def to_sse(data: dict[str, Any]) -> AsyncIterator[bytes]:
    """Turn a non-streaming chat completion into the SSE stream the OpenAI SDK expects."""
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    base = {
        "id": data.get("id", "chatcmpl-proxy"),
        "object": "chat.completion.chunk",
        "created": data.get("created", int(time.time())),
        "model": data.get("model", "agent"),
    }
    content = msg.get("content")
    reasoning = msg.get("reasoning_content")
    text = content if (isinstance(content, str) and content.strip()) else reasoning
    if isinstance(text, str) and text.strip():
        chunk_text = {
            **base,
            "choices": [
                {"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}
            ],
        }
        yield f"data: {json.dumps(chunk_text)}\n\n".encode()
    if msg.get("tool_calls"):
        tcs = [{"index": i, **tc} for i, tc in enumerate(msg["tool_calls"]) if isinstance(tc, dict)]
        chunk_tc = {
            **base,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "tool_calls": tcs},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk_tc)}\n\n".encode()
    finish_chunk: dict[str, Any] = {
        **base,
        "choices": [
            {"index": 0, "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}
        ],
    }
    if "usage" in data:
        finish_chunk["usage"] = data["usage"]
    yield f"data: {json.dumps(finish_chunk)}\n\n".encode()
    yield b"data: [DONE]\n\n"


def create_proxy_app(state: AppState) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    used: dict[str, tuple[int, int]] = {}  # run_id -> (count, expires)

    def bump(tok: tokens.AgentToken) -> int:
        now = int(time.time())
        for k, (_, exp) in list(used.items()):
            if exp < now:
                used.pop(k, None)
        count, _ = used.get(tok.run_id, (0, tok.expires))
        used[tok.run_id] = (count + 1, tok.expires)
        return count + 1

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
                url = cfg.base_url.rstrip("/") + "/chat/completions"
                is_zai = "api.z.ai" in cfg.base_url
                candidates = _zai_fallbacks(cfg.base_url, cfg.base_model)
                res = None
                parsed_data: dict[str, Any] | None = None
                for idx, candidate in enumerate(candidates):
                    if idx > 0 and is_zai:
                        await asyncio.sleep(1.0 * idx)
                    req_body: dict[str, Any] = {**clean, "model": candidate, "stream": False}
                    if is_zai:
                        req_body["thinking"] = {"type": "disabled"}
                    try:
                        res = await state.http.request(
                            "POST",
                            url,
                            headers={
                                "Authorization": "Bearer " + state.settings.base_model_api_key
                            },
                            json=req_body,
                            timeout=45.0,
                        )
                    except UnsafeURL:
                        if idx + 1 < len(candidates):
                            continue
                        raise
                    if res.status == 200:
                        try:
                            loaded = json.loads(res.body)
                        except ValueError:
                            if idx + 1 < len(candidates):
                                continue
                            raise
                        if isinstance(loaded, dict):
                            parsed_data = loaded
                            break
                    if res.status not in (429, 500, 502, 503, 504) or idx + 1 >= len(candidates):
                        break
                if parsed_data is None:
                    if res is not None and res.status != 200:
                        return _error(502, f"upstream status {res.status}")
                    return _error(502, "upstream model error")
                data = parsed_data
        except (ModelError, UnsafeURL, ValueError):
            return _error(502, "upstream model error")

        if body.get("stream"):
            return StreamingResponse(to_sse(data), media_type="text/event-stream")
        return JSONResponse(data)

    return app
