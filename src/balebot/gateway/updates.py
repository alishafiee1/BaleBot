import logging
import time
from typing import Any

from balebot import texts
from balebot.gateway.bale import MAX_MESSAGE
from balebot.services.reply import Reply
from balebot.state import AppState

log = logging.getLogger("balebot.gateway")


async def _send(
    state: AppState,
    chat_id: int,
    reply: Reply,
    progress_msg_id: int | None = None,
) -> None:
    chunks = [
        reply.text[i : i + MAX_MESSAGE]
        for i in range(0, max(len(reply.text), 1), MAX_MESSAGE)
    ]
    if progress_msg_id is not None and chunks:
        kb = reply.keyboard if len(chunks) == 1 else None
        edited = await state.bale.edit_message(chat_id, progress_msg_id, chunks[0], keyboard=kb)
        if not edited:
            await state.bale.send_message(chat_id, chunks[0], keyboard=kb)
        for n, chunk in enumerate(chunks[1:], start=1):
            chunk_kb = reply.keyboard if n == len(chunks) - 1 else None
            await state.bale.send_message(chat_id, chunk, keyboard=chunk_kb)
        return

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
    t0 = time.perf_counter()
    uid = update.get("update_id", "-")
    kind = "callback" if "callback_query" in update else "message"
    try:
        if cq := update.get("callback_query"):
            await _callback(state, cq)
        elif message := update.get("message"):
            await _message(state, message)
        dt_ms = (time.perf_counter() - t0) * 1000
        log.info("processed update_id=%s kind=%s elapsed_ms=%.1f", uid, kind, dt_ms)
    except Exception as exc:  # noqa: BLE001
        log.warning("update failed update_id=%s: %s", uid, type(exc).__name__)


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

    progress_msg_id: int | None = None
    last_edit_ts: float = 0.0

    async def progress(note: str) -> None:
        nonlocal progress_msg_id, last_edit_ts
        progress_msg_id = await state.bale.send_message(chat_id, note)
        last_edit_ts = time.monotonic()

    async def on_stage(stage_text: str) -> None:
        nonlocal last_edit_ts
        if progress_msg_id is not None:
            await state.bale.edit_message(chat_id, progress_msg_id, stage_text)
            last_edit_ts = time.monotonic()

    stripped = text.strip()
    if not stripped.startswith("/") or stripped == "/agent" or stripped.startswith("/agent "):
        try:
            await state.bale.send_chat_action(chat_id, "typing")
        except Exception as exc:  # noqa: BLE001
            log.debug("sendChatAction failed: %s", type(exc).__name__)

    reply = await state.chat.handle_message(
        bale_id,
        sender.get("username"),
        text,
        delete_incoming,
        progress=progress,
        on_stage=on_stage,
    )
    await _send(state, chat_id, reply, progress_msg_id=progress_msg_id)
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
