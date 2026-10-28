import logging
import os
import time
from typing import Any

from balebot import texts
from balebot.gateway.bale import MAX_MESSAGE
from balebot.services.media import IncomingMedia, sanitize_filename
from balebot.services.reply import Reply
from balebot.state import AppState

log = logging.getLogger("balebot.gateway")


async def _send(
    state: AppState,
    chat_id: int,
    reply: Reply,
    progress_msg_id: int | None = None,
) -> None:
    if reply.reply_keyboard is not None:
        try:
            await state.bale.send_reply_keyboard(
                chat_id, texts.KEYBOARD_READY, reply.reply_keyboard
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("send_reply_keyboard failed: %s", exc)

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


async def _extract_media(state: AppState, message: dict[str, Any]) -> IncomingMedia | None:
    photos = message.get("photo")
    if isinstance(photos, list) and photos:
        best = photos[-1]
        if isinstance(best, dict) and best.get("file_id"):
            data, file_path = await state.bale.get_file(str(best["file_id"]))
            ext = os.path.splitext(file_path)[1].lower() or ".jpg"
            mime = (
                "image/png"
                if ext == ".png"
                else ("image/webp" if ext == ".webp" else "image/jpeg")
            )
            return IncomingMedia(
                kind="photo",
                filename=sanitize_filename(f"photo{ext}", "photo.jpg"),
                data=data,
                mime_type=mime,
            )

    voice = message.get("voice")
    if isinstance(voice, dict) and voice.get("file_id"):
        data, file_path = await state.bale.get_file(str(voice["file_id"]))
        ext = os.path.splitext(file_path)[1].lower() or ".ogg"
        mime = str(voice.get("mime_type") or "audio/ogg")
        return IncomingMedia(
            kind="voice",
            filename=sanitize_filename(f"voice{ext}", "voice.ogg"),
            data=data,
            mime_type=mime,
        )

    audio = message.get("audio")
    if isinstance(audio, dict) and audio.get("file_id"):
        data, file_path = await state.bale.get_file(str(audio["file_id"]))
        ext = os.path.splitext(file_path)[1].lower() or ".mp3"
        orig_name = str(audio.get("file_name") or f"audio{ext}")
        mime = str(audio.get("mime_type") or "audio/mpeg")
        return IncomingMedia(
            kind="audio",
            filename=sanitize_filename(orig_name, f"audio{ext}"),
            data=data,
            mime_type=mime,
        )

    doc = message.get("document")
    if isinstance(doc, dict) and doc.get("file_id"):
        data, file_path = await state.bale.get_file(str(doc["file_id"]))
        ext = os.path.splitext(file_path)[1].lower() or ".bin"
        orig_name = str(doc.get("file_name") or f"document{ext}")
        mime = str(doc.get("mime_type") or "application/octet-stream")
        kind = "document"
        if mime.startswith("image/") or ext in (".jpg", ".jpeg", ".png", ".webp"):
            kind = "photo"
        elif mime.startswith("audio/") or ext in (".ogg", ".oga", ".mp3", ".wav", ".m4a"):
            kind = "audio"
        return IncomingMedia(
            kind=kind,
            filename=sanitize_filename(orig_name, f"document{ext}"),
            data=data,
            mime_type=mime,
        )

    return None


async def _message(state: AppState, message: dict[str, Any]) -> None:
    chat_id = int(message["chat"]["id"])
    sender = message.get("from") or {}
    bale_id = int(sender.get("id", chat_id))
    raw_text = message.get("text")
    caption = message.get("caption")
    text = (
        raw_text
        if isinstance(raw_text, str)
        else (caption if isinstance(caption, str) else None)
    )
    has_media = any(k in message for k in ("photo", "voice", "audio", "document"))
    if text is None and not has_media:
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

    stripped = (text or "").strip()
    is_agent_cmd = stripped == "/agent" or stripped.startswith("/agent ")
    if has_media or not stripped.startswith("/") or is_agent_cmd:
        try:
            await state.bale.send_chat_action(chat_id, "typing")
        except Exception as exc:  # noqa: BLE001
            log.debug("sendChatAction failed: %s", type(exc).__name__)

    media: IncomingMedia | None = None
    if has_media:
        try:
            media = await _extract_media(state, message)
        except Exception as exc:  # noqa: BLE001
            log.warning("failed to download incoming media: %s", type(exc).__name__)
            if not stripped:
                await _send(state, chat_id, Reply(texts.MODEL_ERROR))
                return

    reply = await state.chat.handle_message(
        bale_id,
        sender.get("username"),
        text or "",
        delete_incoming,
        progress=progress,
        on_stage=on_stage,
        media=media,
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
