from typing import Protocol

import httpx


class ModelError(Exception):
    """The model backend failed; the caller must not charge the user's quota."""


class ModelClient(Protocol):
    async def chat(
        self, *, base_url: str, api_key: str, model: str, messages: list[dict[str, str]]
    ) -> str: ...


class OpenAICompatClient:
    """Any OpenAI-compatible /chat/completions endpoint (Z.ai, OpenAI, OpenRouter, ...)."""

    def __init__(self, timeout: float = 120.0) -> None:
        self._http = httpx.AsyncClient(timeout=timeout)

    async def chat(
        self, *, base_url: str, api_key: str, model: str, messages: list[dict[str, str]]
    ) -> str:
        try:
            resp = await self._http.post(
                base_url.rstrip("/") + "/chat/completions",
                headers={"Authorization": "Bearer " + api_key},
                json={"model": model, "messages": messages},
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            # type only: the message/URL could carry a key
            raise ModelError(type(exc).__name__) from None
        if not content:
            raise ModelError("empty reply")
        return str(content)

    async def aclose(self) -> None:
        await self._http.aclose()
