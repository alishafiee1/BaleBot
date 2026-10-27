"""LLM proxy for sandboxed agents.

The Hermes agent inside a sandbox has no provider keys. It talks to this proxy with a
short-lived token, and the proxy picks the user's model, adds the real key, and calls the
upstream provider. Bind this app only to the Incus bridge address (e.g. 10.77.0.1:8001).
"""

import asyncio
import json
import logging
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from balebot import texts
from balebot.models import Message, User
from balebot.net.safe_http import UnsafeURL
from balebot.sandbox import tokens
from balebot.services import runtime_settings
from balebot.services.model_client import ModelError, _zai_fallbacks
from balebot.state import AppState

log = logging.getLogger("balebot.sandbox.proxy")

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
    return JSONResponse(
        {"error": {"message": message, "type": "proxy_error"}}, status_code=status
    )


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
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk_text)}\n\n".encode()
    if msg.get("tool_calls"):
        tcs = [
            {"index": i, **tc}
            for i, tc in enumerate(msg["tool_calls"])
            if isinstance(tc, dict)
        ]
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
    notify_counts: dict[int, tuple[int, Any]] = {}

    def bump(tok: tokens.AgentToken) -> int:
        now = int(time.time())
        for k, (_, exp) in list(used.items()):
            if exp < now:
                used.pop(k, None)
        count, _ = used.get(tok.run_id, (0, tok.expires))
        used[tok.run_id] = (count + 1, tok.expires)
        return count + 1

    @app.get("/v1/props")
    async def props() -> dict[str, Any]:
        return {"default_generation_settings": {"n_ctx": 128000}}

    @app.get("/v1/models")
    @app.get("/api/v1/models")
    async def models() -> dict[str, Any]:
        return {
            "object": "list",
            "data": [
                {
                    "id": "agent",
                    "object": "model",
                    "owned_by": "balebot",
                    "context_length": 128000,
                }
            ],
        }

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
            history: list[dict[str, str]] = []
            if user is not None:
                q = select(Message).where(Message.user_id == user.id)
                if user.context_reset_at:
                    q = q.where(Message.created_at > user.context_reset_at)
                limit_msgs = getattr(
                    state.settings, "max_context_messages", state.settings.history_turns * 2
                )
                q = q.order_by(Message.id.desc()).limit(limit_msgs)
                rows = list((await s.scalars(q)).all())
                history = [{"role": m.role, "content": m.content} for m in reversed(rows)]

        if user is None or user.banned:
            return _error(403, "not allowed")

        sys_prompt = texts.HERMES_BALE_SYSTEM_PROMPT
        if history:
            hist_text = "\n".join(f"{m['role']}: {m['content']}" for m in history)
            sys_prompt = f"{sys_prompt}\n\nتاریخچه گفتگوی اخیر کاربر در بله:\n{hist_text}"

        messages = list(clean.get("messages", []))
        if messages and messages[0].get("role") == "system":
            orig = str(messages[0].get("content", ""))
            if texts.HERMES_BALE_SYSTEM_PROMPT not in orig:
                messages[0] = {"role": "system", "content": f"{sys_prompt}\n\n{orig}"}
        else:
            messages.insert(0, {"role": "system", "content": sys_prompt})
        clean["messages"] = messages

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

    @app.post("/v1/bale/notify")
    async def bale_notify(request: Request) -> JSONResponse:
        auth = request.headers.get("authorization", "")
        token_str = auth.removeprefix("Bearer ").strip()
        try:
            user_id = tokens.verify_any_token(state.settings.proxy_secret, token_str)
        except tokens.BadToken:
            return _error(401, "invalid or expired token")

        async with state.sessionmaker() as s:
            user = await s.get(User, user_id)
        if user is None or user.banned:
            return _error(403, "not allowed")

        today = datetime.now(ZoneInfo(state.settings.timezone)).date()
        count, count_day = notify_counts.get(user_id, (0, today))
        if count_day != today:
            count = 0
        if count >= 30:
            return _error(429, "daily notification limit reached (max 30/day)")

        try:
            body = await request.json()
        except Exception:
            return _error(400, "invalid json")
        if not isinstance(body, dict):
            return _error(400, "json object required")

        text = str(body.get("text", "")).strip()
        if not text:
            return _error(400, "text is required")
        if len(text) > 4000:
            return _error(400, "text too long (max 4000 characters)")

        try:
            await state.bale.send_message(user.bale_id, text)
        except Exception:
            return _error(502, "failed to send message via bale")

        notify_counts[user_id] = (count + 1, today)
        return JSONResponse({"ok": True})

    @app.api_route("/v1/web/search", methods=["GET", "POST"])
    async def web_search(request: Request) -> JSONResponse:
        auth = request.headers.get("authorization", "")
        token_str = auth.removeprefix("Bearer ").strip()
        try:
            user_id = tokens.verify_any_token(state.settings.proxy_secret, token_str)
        except tokens.BadToken:
            return _error(401, "invalid or expired token")

        async with state.sessionmaker() as s:
            user = await s.get(User, user_id)
        if user is None or user.banned:
            return _error(403, "not allowed")

        query = ""
        if request.method == "POST":
            try:
                body = await request.json()
                if isinstance(body, dict):
                    query = str(body.get("query", "")).strip()
            except Exception as exc:  # noqa: BLE001
                log.debug("failed to parse web_search json body: %s", exc)
        if not query:
            query = request.query_params.get("q", "").strip()
        if not query:
            return _error(400, "query is required")

        results: list[dict[str, str]] = []
        try:
            encoded_q = urllib.parse.quote(query)
            rss_url = f"https://news.google.com/rss/search?q={encoded_q}&hl=fa&gl=IR&ceid=IR:fa"
            res = await state.http.request("GET", rss_url, timeout=10.0)
            if res.status == 200:
                root = ET.fromstring(res.body)  # noqa: S314
                for item in root.findall(".//item")[:8]:
                    t_elem = item.find("title")
                    l_elem = item.find("link")
                    d_elem = item.find("description")
                    title = t_elem.text.strip() if t_elem is not None and t_elem.text else ""
                    link = l_elem.text.strip() if l_elem is not None and l_elem.text else ""
                    raw_desc = d_elem.text if d_elem is not None and d_elem.text else ""
                    snippet = re.sub(r"<[^>]+>", "", raw_desc).strip()
                    if title:
                        results.append({"title": title, "link": link, "snippet": snippet})
        except Exception as exc:  # noqa: BLE001
            log.debug("RSS search attempt failed: %s", exc)

        if not results:
            try:
                ddg_url = f"https://html.duckduckgo.com/html/?q={urllib.parse.quote(query)}"
                res = await state.http.request(
                    "GET", ddg_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10.0
                )
                if res.status == 200:
                    body_str = res.body.decode("utf-8", "ignore")
                    matches = re.findall(
                        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', body_str, re.DOTALL
                    )
                    title_matches = re.findall(
                        r'<a[^>]+class="result__url"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
                        body_str,
                        re.DOTALL,
                    )
                    for idx in range(min(len(matches), 5)):
                        snippet = re.sub(r"<[^>]+>", "", matches[idx]).strip()
                        link = title_matches[idx][0].strip() if idx < len(title_matches) else ""
                        raw_title = title_matches[idx][1] if idx < len(title_matches) else ""
                        title = re.sub(r"<[^>]+>", "", raw_title).strip()
                        if snippet or title:
                            results.append({"title": title, "link": link, "snippet": snippet})
            except Exception as exc:  # noqa: BLE001
                log.debug("DDG search attempt failed: %s", exc)

        if results:
            lines = [f"نتایج جستجو برای: {query}"]
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r['title']}\n   پیوند: {r['link']}\n   خلاصه: {r['snippet']}")
            formatted = "\n\n".join(lines)
        else:
            formatted = f"نتیجه‌ای برای «{query}» یافت نشد."

        return JSONResponse(
            {"ok": True, "query": query, "results": results, "formatted": formatted}
        )

    return app
