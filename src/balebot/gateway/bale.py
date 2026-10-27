from typing import Any, Protocol

import httpx

MAX_MESSAGE = 3500


class BaleApi(Protocol):
    async def send_message(
        self, chat_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None
    ) -> None: ...
    async def delete_message(self, chat_id: int, message_id: int) -> None: ...
    async def send_document(self, chat_id: int, name: str, data: bytes) -> None: ...
    async def answer_callback(self, callback_id: str) -> None: ...
    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None: ...
    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]: ...
    async def set_webhook(self, url: str) -> bool: ...


class BaleClient:
    """Thin Bale Bot API client. The token lives in the URL, so nothing here logs URLs or errors."""

    def __init__(self, token: str) -> None:
        self._base = f"https://tapi.bale.ai/bot{token}/"
        self._http = httpx.AsyncClient(timeout=60.0)

    async def _call(self, method: str, payload: dict[str, Any], timeout: float = 40.0) -> Any:
        resp = await self._http.post(self._base + method, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    async def send_message(
        self, chat_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None
    ) -> None:
        chunks = [text[i : i + MAX_MESSAGE] for i in range(0, max(len(text), 1), MAX_MESSAGE)]
        for n, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if keyboard and n == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            await self._call("sendMessage", payload)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self._call(
            "deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=15.0
        )

    async def send_document(self, chat_id: int, name: str, data: bytes) -> None:
        resp = await self._http.post(
            self._base + "sendDocument",
            data={"chat_id": str(chat_id)},
            files={"document": (name, data)},
            timeout=120.0,
        )
        resp.raise_for_status()

    async def answer_callback(self, callback_id: str) -> None:
        await self._call("answerCallbackQuery", {"callback_query_id": callback_id}, timeout=10.0)

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=5.0)

    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]:
        data = await self._call(
            "getUpdates", {"offset": offset, "timeout": timeout}, timeout=timeout + 15
        )
        return list(data.get("result", []))

    async def set_webhook(self, url: str) -> bool:
        return bool((await self._call("setWebhook", {"url": url})).get("ok"))

    async def aclose(self) -> None:
        await self._http.aclose()
