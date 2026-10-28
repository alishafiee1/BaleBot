import asyncio
from typing import Any, Protocol

import httpx


class ModelError(Exception):
    """The model backend failed; the caller must not charge the user's quota."""


class ModelClient(Protocol):
    async def chat(
        self, *, base_url: str, api_key: str, model: str, messages: list[dict[str, Any]]
    ) -> str: ...


def _extract_reply_text(data: dict[str, Any]) -> str:
    msg = data["choices"][0]["message"]
    content = msg.get("content")
    if isinstance(content, str) and content.strip():
        return content
    reasoning = msg.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning.strip():
        return reasoning
    return ""


def _has_image_content(messages: list[dict[str, Any]]) -> bool:
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("image_url", "image"):
                    return True
    return False


def _zai_fallbacks(base_url: str, model: str, *, vision: bool = False) -> list[str]:
    if "api.z.ai" not in base_url:
        return [model]
    if vision:
        return ["glm-4.6v-flash", "glm-4v-flash", "glm-4.6v-flash"]
    alt = "glm-4.7-flash" if model == "glm-4.5-flash" else "glm-4.5-flash"
    return [model, alt, model, alt]


class OpenAICompatClient:
    """Any OpenAI-compatible /chat/completions endpoint (Z.ai, OpenAI, OpenRouter, ...)."""

    def __init__(self, timeout: float = 45.0) -> None:
        self._http = httpx.AsyncClient(timeout=timeout)

    async def chat(
        self, *, base_url: str, api_key: str, model: str, messages: list[dict[str, Any]]
    ) -> str:
        url = base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": "Bearer " + api_key}
        has_vision = _has_image_content(messages)
        candidates = _zai_fallbacks(base_url, model, vision=has_vision)
        is_zai = "api.z.ai" in base_url
        last_exc: Exception | None = None
        for idx, candidate in enumerate(candidates):
            if idx > 0 and is_zai:
                await asyncio.sleep(1.2 * idx)
            try:
                payload: dict[str, Any] = {"model": candidate, "messages": messages}
                if is_zai:
                    payload["thinking"] = {"type": "disabled"}
                resp = await self._http.post(
                    url,
                    headers=headers,
                    json=payload,
                )
                if resp.status_code in (429, 500, 502, 503, 504) and idx + 1 < len(candidates):
                    continue
                resp.raise_for_status()
                content = _extract_reply_text(resp.json())
            except (httpx.HTTPError, KeyError, IndexError, ValueError, TypeError) as exc:
                last_exc = exc
                if idx + 1 < len(candidates):
                    continue
                # type only: the message/URL could carry a key
                raise ModelError(type(exc).__name__) from None
            if content:
                return str(content)
        if last_exc is not None:
            raise ModelError(type(last_exc).__name__) from None
        raise ModelError("empty reply")

    async def aclose(self) -> None:
        await self._http.aclose()
