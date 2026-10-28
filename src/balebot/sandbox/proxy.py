"""LLM proxy for sandboxed agents.

The Hermes agent inside a sandbox has no provider keys. It talks to this proxy with a
short-lived token, and the proxy picks the user's model, adds the real key, and calls the
upstream provider. Bind this app only to the Incus bridge address (e.g. 10.77.0.1:8001).
"""

import asyncio
import base64
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

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select

from balebot import texts
from balebot.models import Message, User
from balebot.net.safe_http import UnsafeURL
from balebot.sandbox import tokens
from balebot.services import runtime_settings
from balebot.services.media import (
    extract_html_text,
    synthesize_speech_bytes,
    transcribe_audio_bytes,
)
from balebot.services.model_client import (
    ModelError,
    _has_image_content,
    _zai_fallbacks,
    rewrite_vision_messages_for_text_fallback,
)
from balebot.state import AppState

log = logging.getLogger("balebot.sandbox.proxy")

MAX_BODY = 12_000_000
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
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text},
                    "finish_reason": None,
                }
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
    notify_counts: dict[int, tuple[int, Any]] = {}

    def bump(tok: tokens.AgentToken) -> int:
        now = int(time.time())
        for k, (_, exp) in list(used.items()):
            if exp < now:
                used.pop(k, None)
        count, _ = used.get(tok.run_id, (0, tok.expires))
        used[tok.run_id] = (count + 1, tok.expires)
        return count + 1

    async def _auth_user(request: Request) -> tuple[User | None, JSONResponse | None]:
        auth = request.headers.get("authorization", "")
        token_str = auth.removeprefix("Bearer ").strip()
        try:
            user_id = tokens.verify_any_token(state.settings.proxy_secret, token_str)
        except tokens.BadToken:
            return None, _error(401, "invalid or expired token")
        async with state.sessionmaker() as s:
            user = await s.get(User, user_id)
        if user is None or user.banned:
            return None, _error(403, "not allowed")
        return user, None

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
                    "id": "balebot",
                    "object": "model",
                    "owned_by": "balebot",
                    "context_length": 128000,
                },
                {
                    "id": "agent",
                    "object": "model",
                    "owned_by": "balebot",
                    "context_length": 128000,
                },
            ],
        }

    @app.get("/v1/models/{model_id}")
    @app.get("/api/v1/models/{model_id}")
    async def get_model(model_id: str) -> dict[str, Any]:
        return {
            "id": model_id,
            "object": "model",
            "owned_by": "balebot",
            "context_length": 128000,
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
                has_vision = _has_image_content(messages)
                candidates = _zai_fallbacks(cfg.base_url, cfg.base_model, vision=has_vision)
                res = None
                parsed_data: dict[str, Any] | None = None
                for idx, candidate in enumerate(candidates):
                    if idx > 0 and is_zai:
                        await asyncio.sleep(1.0 * idx)
                    req_body: dict[str, Any] = {**clean, "model": candidate, "stream": False}
                    if is_zai:
                        req_body["thinking"] = {"type": "disabled"}
                        if has_vision:
                            req_body.pop("tools", None)
                            req_body.pop("tool_choice", None)
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
                if parsed_data is None and has_vision and is_zai:
                    fallback_msgs = await rewrite_vision_messages_for_text_fallback(messages)
                    fb_body: dict[str, Any] = {
                        **clean,
                        "messages": fallback_msgs,
                        "model": cfg.base_model,
                        "stream": False,
                        "thinking": {"type": "disabled"},
                    }
                    fb_body.pop("tools", None)
                    fb_body.pop("tool_choice", None)
                    res = await state.http.request(
                        "POST",
                        url,
                        headers={"Authorization": "Bearer " + state.settings.base_model_api_key},
                        json=fb_body,
                        timeout=45.0,
                    )
                    if res.status == 200:
                        loaded = json.loads(res.body)
                        if isinstance(loaded, dict):
                            parsed_data = loaded

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
        user, err = await _auth_user(request)
        if err is not None or user is None:
            return err or _error(401, "unauthorized")

        today = datetime.now(ZoneInfo(state.settings.timezone)).date()
        count, count_day = notify_counts.get(user.id, (0, today))
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

        notify_counts[user.id] = (count + 1, today)
        return JSONResponse({"ok": True})

    @app.api_route("/v1/web/search", methods=["GET", "POST"])
    async def web_search(request: Request) -> JSONResponse:
        _, err = await _auth_user(request)
        if err is not None:
            return err

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
        encoded_q = urllib.parse.quote(query)
        rss_urls = [
            f"https://news.google.com/rss/search?q={encoded_q}&hl=fa&gl=IR&ceid=IR:fa",
            f"https://www.bing.com/news/search?q={encoded_q}&format=rss",
            f"https://www.bing.com/search?q={encoded_q}&format=rss",
        ]
        news_keys = ("اخبار", "خبر", "ایران", "تهران", "جهان", "news", "iran")
        if any(k in query.lower() for k in news_keys):
            rss_urls.extend(
                [
                    "https://www.irna.ir/rss",
                    "https://www.mehrnews.com/rss",
                ]
            )
        for rss_url in rss_urls:
            if len(results) >= 8:
                break
            try:
                res = await state.http.request(
                    "GET", rss_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8.0
                )
                if res.status == 200 and res.body:
                    root = ET.fromstring(res.body)  # noqa: S314
                    for item in root.findall(".//item")[:5]:
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
                log.debug("RSS search attempt failed (%s): %s", rss_url, exc)

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

    @app.api_route("/v1/web/extract", methods=["GET", "POST"])
    async def web_extract(request: Request) -> JSONResponse:
        _, err = await _auth_user(request)
        if err is not None:
            return err

        urls: list[str] = []
        char_limit = 15000
        if request.method == "POST":
            try:
                body = await request.json()
                if isinstance(body, dict):
                    raw_urls = body.get("urls")
                    if isinstance(raw_urls, list):
                        for item in raw_urls[:5]:
                            if isinstance(item, str) and item.strip():
                                urls.append(item.strip())
                            elif isinstance(item, dict):
                                u = item.get("url") or item.get("href")
                                if isinstance(u, str) and u.strip():
                                    urls.append(u.strip())
                    elif isinstance(body.get("url"), str) and body["url"].strip():
                        urls.append(body["url"].strip())
                    if isinstance(body.get("char_limit"), int):
                        char_limit = max(1000, min(body["char_limit"], 50000))
            except Exception as exc:  # noqa: BLE001
                log.debug("failed to parse web_extract json body: %s", exc)
        if not urls:
            q_url = request.query_params.get("url", "").strip()
            if q_url:
                urls.append(q_url)
        if not urls:
            return _error(400, "urls is required")

        extracted: list[dict[str, Any]] = []
        for u in urls[:5]:
            try:
                res = await state.http.request(
                    "GET",
                    u,
                    headers={"User-Agent": "Mozilla/5.0 (compatible; HermesBot/1.0)"},
                    timeout=15.0,
                )
                if res.status == 200 and res.body:
                    title, content = extract_html_text(res.body, char_limit=char_limit)
                    extracted.append(
                        {
                            "url": u,
                            "title": title,
                            "content": content,
                            "raw_content": content,
                            "error": None,
                        }
                    )
                else:
                    extracted.append(
                        {
                            "url": u,
                            "title": "",
                            "content": "",
                            "error": f"HTTP {res.status}",
                        }
                    )
            except UnsafeURL:
                extracted.append(
                    {
                        "url": u,
                        "title": "",
                        "content": "",
                        "error": "Blocked: URL targets a private or internal network address",
                    }
                )
            except Exception as exc:  # noqa: BLE001
                extracted.append(
                    {
                        "url": u,
                        "title": "",
                        "content": "",
                        "error": f"Fetch failed: {type(exc).__name__}",
                    }
                )

        return JSONResponse({"ok": True, "success": True, "results": extracted})

    @app.post("/v1/audio/speech", response_model=None)
    async def audio_speech(request: Request) -> Response:
        _, err = await _auth_user(request)
        if err is not None:
            return err

        try:
            body = await request.json()
        except Exception:
            return _error(400, "invalid json")
        if not isinstance(body, dict):
            return _error(400, "json object required")

        text = str(body.get("input") or body.get("text") or "").strip()
        if not text:
            return _error(400, "text is required")
        lang = str(body.get("lang") or body.get("language") or "fa").strip() or "fa"
        audio_bytes = await synthesize_speech_bytes(state.http, text[:3500], lang=lang)
        if not audio_bytes:
            return _error(502, "TTS synthesis failed")
        if body.get("response_format") == "json":
            return JSONResponse(
                {
                    "ok": True,
                    "audio_b64": base64.b64encode(audio_bytes).decode("ascii"),
                    "format": "mp3",
                }
            )
        return Response(content=audio_bytes, media_type="audio/mpeg")

    @app.post("/v1/audio/transcriptions")
    async def audio_transcriptions(request: Request) -> JSONResponse:
        _, err = await _auth_user(request)
        if err is not None:
            return err

        raw = await request.body()
        if len(raw) > MAX_BODY:
            return _error(413, "request too large")
        try:
            body = json.loads(raw)
        except Exception:
            return _error(400, "invalid json")
        if not isinstance(body, dict):
            return _error(400, "json object required")

        audio_b64 = str(body.get("audio_b64") or "").strip()
        if not audio_b64:
            return _error(400, "audio_b64 is required")
        try:
            audio_bytes = base64.b64decode(audio_b64)
        except Exception:
            return _error(400, "invalid base64")
        ext = str(body.get("ext") or "ogg").strip() or "ogg"
        lang = str(body.get("language") or "fa-IR").strip() or "fa-IR"
        transcript = await transcribe_audio_bytes(audio_bytes, ext=ext, language=lang)
        return JSONResponse({"ok": True, "text": transcript})

    return app
