import logging
from typing import Any

from balebot import texts
from balebot.services.reply import Reply
from balebot.state import AppState

log = logging.getLogger("balebot.gateway")


async def _send(state: AppState, chat_id: int, reply: Reply) -> None:
    try:
        await state.bale.send_message(chat_id, reply.text, reply.keyboard)
    except Exception:  # noqa: BLE001
        if not reply.keyboard:
            raise
        # buttons not accepted: the numbered text list still works
        await state.bale.send_message(chat_id, reply.text)


async def _send_files(state: AppState, chat_id: int, reply: Reply) -> None:
    for name, data in reply.files:
        try:
            await state.bale.send_document(chat_id, name, data)
        except Exception as exc:  # noqa: BLE001
            log.warning("send document failed: %s", type(exc).__name__)


async def process_update(state: AppState, update: dict[str, Any]) -> None:
    """Turn one Bale update into at most one reply. Never raises, so polling keeps going."""
    try:
        if cq := update.get("callback_query"):
            await _callback(state, cq)
        elif message := update.get("message"):
            await _message(state, message)
    except Exception as exc:  # noqa: BLE001
        log.warning("update failed: %s", type(exc).__name__)


async def _message(state: AppState, message: dict[str, Any]) -> None:
    chat_id = int(message["chat"]["id"])
    sender = message.get("from") or {}
    bale_id = int(sender.get("id", chat_id))
    text = message.get("text")
    if text is None:
        await _send(state, chat_id, Reply(texts.UNSUPPORTED))
        return
    message_id = message.get("message_id")

    async def delete_incoming() -> None:
        # best effort: a failed delete must not leak the reason (it could echo the message)
        if message_id is None:
            return
        try:
            await state.bale.delete_message(chat_id, int(message_id))
        except Exception as exc:  # noqa: BLE001
            log.warning("delete message failed: %s", type(exc).__name__)

    async def progress(note: str) -> None:
        await state.bale.send_message(chat_id, note)

    reply = await state.chat.handle_message(
        bale_id, sender.get("username"), text, delete_incoming, progress
    )
    await _send(state, chat_id, reply)
    await _send_files(state, chat_id, reply)


async def _callback(state: AppState, cq: dict[str, Any]) -> None:
    try:
        await state.bale.answer_callback(str(cq["id"]))
    except Exception as exc:  # noqa: BLE001
        log.info("answerCallbackQuery failed: %s", type(exc).__name__)
    chat_id = int(((cq.get("message") or {}).get("chat") or {}).get("id") or cq["from"]["id"])
    reply = await state.chat.handle_callback(int(cq["from"]["id"]), str(cq.get("data", "")))
    if reply is not None:
        await _send(state, chat_id, reply)
