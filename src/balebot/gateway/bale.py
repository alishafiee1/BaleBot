from typing import Any, Protocol

import httpx

from balebot import texts

MAX_MESSAGE = 3500
MAX_FILE_BYTES = 15 * 1024 * 1024


class BaleApi(Protocol):
    async def send_message(
        self, chat_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None
    ) -> int | None: ...
    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> bool: ...
    async def send_reply_keyboard(
        self, chat_id: int, text: str, reply_keyboard: list[list[dict[str, str]]]
    ) -> int | None: ...
    async def set_my_commands(self, commands: list[dict[str, str]] | None = None) -> bool: ...
    async def delete_message(self, chat_id: int, message_id: int) -> None: ...
    async def send_document(self, chat_id: int, name: str, data: bytes) -> None: ...
    async def get_file(self, file_id: str) -> tuple[bytes, str]: ...
    async def answer_callback(self, callback_id: str) -> None: ...
    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None: ...
    async def get_updates(self, offset: int, timeout: int = 30) -> list[dict[str, Any]]: ...
    async def set_webhook(self, url: str) -> bool: ...


class BaleClient:
    """Thin Bale Bot API client. The token lives in the URL, so nothing here logs URLs or errors."""

    def __init__(self, token: str) -> None:
        self._base = f"https://tapi.bale.ai/bot{token}/"
        self._file_base = f"https://tapi.bale.ai/file/bot{token}/"
        self._http = httpx.AsyncClient(timeout=60.0)

    async def _call(self, method: str, payload: dict[str, Any], timeout: float = 40.0) -> Any:
        resp = await self._http.post(self._base + method, json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    async def send_message(
        self, chat_id: int, text: str, keyboard: list[list[dict[str, str]]] | None = None
    ) -> int | None:
        chunks = [text[i : i + MAX_MESSAGE] for i in range(0, max(len(text), 1), MAX_MESSAGE)]
        first_mid: int | None = None
        for n, chunk in enumerate(chunks):
            payload: dict[str, Any] = {"chat_id": chat_id, "text": chunk}
            if keyboard and n == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": keyboard}
            resp = await self._call("sendMessage", payload)
            if first_mid is None and isinstance(resp, dict):
                mid = resp.get("result", {}).get("message_id")
                if mid is not None:
                    try:
                        first_mid = int(mid)
                    except (ValueError, TypeError):
                        pass
        return first_mid

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> bool:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:MAX_MESSAGE],
        }
        if keyboard is not None:
            payload["reply_markup"] = {"inline_keyboard": keyboard}
        try:
            resp = await self._call("editMessageText", payload, timeout=20.0)
            return bool(isinstance(resp, dict) and resp.get("ok"))
        except Exception:  # noqa: BLE001
            return False

    async def send_reply_keyboard(
        self, chat_id: int, text: str, reply_keyboard: list[list[dict[str, str]]]
    ) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text[:MAX_MESSAGE],
            "reply_markup": {"keyboard": reply_keyboard, "resize_keyboard": True},
        }
        try:
            resp = await self._call("sendMessage", payload, timeout=20.0)
            if isinstance(resp, dict):
                mid = resp.get("result", {}).get("message_id")
                if mid is not None:
                    try:
                        return int(mid)
                    except (ValueError, TypeError):
                        pass
        except Exception:  # noqa: BLE001
            return None
        return None

    async def set_my_commands(self, commands: list[dict[str, str]] | None = None) -> bool:
        cmds = commands if commands is not None else texts.BOT_COMMANDS
        try:
            data = await self._call("setMyCommands", {"commands": cmds}, timeout=15.0)
            return bool(isinstance(data, dict) and data.get("ok"))
        except Exception:  # noqa: BLE001
            return False

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self._call(
            "deleteMessage", {"chat_id": chat_id, "message_id": message_id}, timeout=15.0
        )

    async def get_file(self, file_id: str) -> tuple[bytes, str]:
        data = await self._call("getFile", {"file_id": file_id}, timeout=20.0)
        result = data.get("result") if isinstance(data, dict) else None
        file_path = str((result or {}).get("file_path") or "").lstrip("/")
        if not file_path:
            raise ValueError("missing file_path")
        resp = await self._http.get(self._file_base + file_path, timeout=60.0)
        resp.raise_for_status()
        if len(resp.content) > MAX_FILE_BYTES:
            raise ValueError("file too large")
        return resp.content, file_path

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
