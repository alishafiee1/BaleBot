import base64
import os
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from balebot import texts
from balebot.config import Settings
from balebot.models import Message, User, utcnow
from balebot.sandbox import tokens
from balebot.sandbox.manager import SandboxError, SandboxManager
from balebot.services import quota, runtime_settings
from balebot.services.keys import Deleter, KeyService
from balebot.services.media import IncomingMedia, transcribe_audio_bytes
from balebot.services.model_client import ModelClient, ModelError
from balebot.services.reply import Reply

BUTTON_COMMANDS: dict[str, str] = {
    "شروع مجدد": "/start",
    "🔄 گفتگوی تازه": "/new",
    "گفتگوی تازه": "/new",
    "📋 منوی اصلی": "/menu",
    "منوی اصلی": "/menu",
    "🤖 انتخاب مدل": "/models",
    "انتخاب مدل": "/models",
    "🔑 کلیدهای من": "/keys",
    "کلیدهای من": "/keys",
    "📊 سهمیه امروز": "/usage",
    "سهمیه امروز": "/usage",
    "📖 راهنما": "/help",
    "راهنما": "/help",
}

_PHOTO_SANDBOX_HINTS = (
    "/agent",
    "در فایل",
    "ذخیره کن",
    "اسکریپت",
    "ترمینال",
    "/home/agent",
)


async def get_or_create_user(session: AsyncSession, bale_id: int, username: str | None) -> User:
    user = await session.scalar(select(User).where(User.bale_id == bale_id))
    if user is None:
        user = User(bale_id=bale_id, username=username)
        session.add(user)
        await session.flush()
    elif username and user.username != username:
        user.username = username
    return user


async def purge_old_messages(session: AsyncSession, days: int) -> int:
    cutoff = utcnow() - timedelta(days=days)
    result = await session.execute(delete(Message).where(Message.created_at < cutoff))
    return result.rowcount or 0  # type: ignore[attr-defined]


class ChatService:
    """One entry point per incoming text. DB sessions are never held across network calls."""

    def __init__(
        self,
        client: ModelClient,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        keys: KeyService | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._sm = sessionmaker
        self.keys = keys
        self.sandbox: SandboxManager | None = None  # set by build_state when enabled

    def _today(self) -> date:
        return datetime.now(ZoneInfo(self._settings.timezone)).date()

    async def handle(self, bale_id: int, username: str | None, text: str) -> str:
        return (await self.handle_message(bale_id, username, text)).text

    async def handle_callback(self, bale_id: int, data: str) -> Reply | None:
        day = self._today()
        async with self._sm() as s:
            user = await get_or_create_user(s, bale_id, None)
            cfg = await runtime_settings.load(s, self._settings)
            limit = user.daily_quota if user.daily_quota is not None else cfg.daily_quota
            user_id, banned = user.id, user.banned
            await s.commit()
        if banned:
            return Reply(texts.BANNED)
        kind, _, arg = data.partition(":")
        if kind == "menu":
            if self.keys is not None and arg != "addkey":
                await self.keys._clear(user_id)
            if arg == "main":
                return Reply(texts.MENU_TEXT, texts.MAIN_MENU_KEYBOARD)
            if arg == "help":
                return Reply(texts.HELP, texts.MAIN_MENU_KEYBOARD)
            if arg == "usage":
                return Reply(
                    await self._simple_command(user_id, "/usage", limit, day),
                    texts.BACK_TO_MENU_KEYBOARD,
                )
            if arg == "new":
                return Reply(
                    await self._simple_command(user_id, "/new", limit, day),
                    texts.BACK_TO_MENU_KEYBOARD,
                )
            if arg == "models" and self.keys is not None:
                return await self.keys.models_page(user_id, cfg, 1)
            if arg == "keys" and self.keys is not None:
                return await self.keys._list_keys(user_id)
            if arg == "addkey" and self.keys is not None:
                return await self.keys.start_addkey(user_id)
            if arg == "agent":
                if self.sandbox is None or not self._settings.sandbox_enabled:
                    return Reply(texts.AGENT_OFF, texts.BACK_TO_MENU_KEYBOARD)
                if self.keys is not None:
                    return await self.keys.start_agent_prompt(user_id)
                return Reply(texts.AGENT_PROMPT, texts.BACK_TO_MENU_KEYBOARD)
            if arg == "cancel":
                return Reply(texts.CANCELLED, texts.MAIN_MENU_KEYBOARD)
            return Reply(texts.MENU_TEXT, texts.MAIN_MENU_KEYBOARD)
        if self.keys is None:
            return None
        return await self.keys.handle_callback(user_id, data, cfg, limit=limit, day=day)

    async def handle_message(
        self,
        bale_id: int,
        username: str | None,
        text: str,
        delete_incoming: Deleter | None = None,
        progress: Callable[[str], Awaitable[None]] | None = None,
        on_stage: Callable[[str], Awaitable[None]] | None = None,
        media: IncomingMedia | None = None,
    ) -> Reply:
        text = text.strip()
        if text in BUTTON_COMMANDS:
            text = BUTTON_COMMANDS[text]
        day = self._today()
        # a pending add-key step may legitimately receive a long secret; its own checks apply
        async with self._sm() as s:
            user = await get_or_create_user(s, bale_id, username)
            cfg = await runtime_settings.load(s, self._settings)
            limit = user.daily_quota if user.daily_quota is not None else cfg.daily_quota
            user_id, banned, selected = user.id, user.banned, user.selected_model
            await s.commit()
        if banned:
            return Reply(texts.BANNED)

        if media is not None:
            return await self._handle_media(
                user_id=user_id,
                selected=selected,
                text=text,
                media=media,
                cfg=cfg,
                limit=limit,
                day=day,
                progress=progress,
                on_stage=on_stage,
            )

        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

        if cmd == "/think":
            rest = text.partition(" ")[2].strip()
            if not rest:
                return Reply(
                    "🧠 حالت تفکر و ابزارهای هرمس همیشه فعال است؛ "
                    "کافی است سوال یا خواستهٔ خود را مستقیماً بنویسید.",
                    texts.MAIN_MENU_KEYBOARD,
                )
            text = rest
            cmd = ""

        if cmd == "/daily":
            rest = text.partition(" ")[2].strip()
            if not rest:
                return Reply(
                    "⏰ برای تنظیم یا لغو گزارش روزانه، کافی است به فارسی بنویسید؛ "
                    "مثلاً: «هر روز ساعت ۸ صبح خلاصه اخبار را برایم بفرست».",
                    texts.MAIN_MENU_KEYBOARD,
                )
            text = rest
            cmd = ""

        if self.keys is not None:
            reply = await self.keys.handle(
                user_id, cmd, text, cfg=cfg, limit=limit, day=day, delete_incoming=delete_incoming
            )
            if reply is not None:
                return reply
        if len(text) > self._settings.max_input_chars:
            return Reply(texts.TOO_LONG.format(max=self._settings.max_input_chars))

        if cmd == "/agent":
            return await self._agent(
                user_id, selected, text, limit, day, progress, on_stage=on_stage
            )
        if cmd in ("/help", "/guide"):
            return Reply(texts.HELP, texts.MAIN_MENU_KEYBOARD)
        if cmd in ("/menu", "/settings"):
            return Reply(
                texts.MENU_TEXT,
                texts.MAIN_MENU_KEYBOARD,
                reply_keyboard=texts.BOTTOM_REPLY_KEYBOARD,
            )
        if cmd in ("/info", "/status"):
            usage_str = await self._simple_command(user_id, "/usage", limit, day)
            return Reply(
                texts.STATUS_INFO.format(model=selected or cfg.base_model, usage=usage_str),
                texts.MAIN_MENU_KEYBOARD,
                reply_keyboard=texts.BOTTOM_REPLY_KEYBOARD,
            )
        if cmd in ("/start", "/usage", "/new"):
            kb = texts.MAIN_MENU_KEYBOARD if cmd == "/start" else texts.BACK_TO_MENU_KEYBOARD
            return Reply(
                await self._simple_command(user_id, cmd, limit, day),
                kb,
                reply_keyboard=texts.BOTTOM_REPLY_KEYBOARD if cmd == "/start" else None,
            )
        if not text:
            return Reply(texts.UNSUPPORTED)

        if self.keys is not None and await self.keys.pop_agent_pending(user_id):
            return await self._agent(
                user_id, selected, f"/agent {text}", limit, day, progress, on_stage=on_stage
            )

        if (
            not cmd.startswith("/")
            and self.sandbox is not None
            and self._settings.sandbox_enabled
            and self._settings.unified_hermes
            and len(self._settings.proxy_secret) >= 16
        ):
            agent_reply = await self._agent(
                user_id, selected, f"/agent {text}", limit, day, progress, on_stage=on_stage
            )
            if agent_reply.text not in (texts.AGENT_FAILED, texts.AGENT_EMPTY):
                return agent_reply

        return await self._direct_chat(user_id, selected, text, cfg, limit, day)

    async def _direct_chat(
        self,
        user_id: int,
        selected: str | None,
        text: str,
        cfg: runtime_settings.RuntimeConfig,
        limit: int,
        day: date,
        *,
        user_content: Any = None,
        stored_user_text: str | None = None,
    ) -> Reply:
        route = await self.keys.resolve(user_id, selected) if self.keys else None
        if route is None:  # free base model: counts against the daily quota
            async with self._sm() as s:
                if not await quota.consume(s, user_id, day, limit):
                    await s.commit()
                    return Reply(texts.QUOTA_EXHAUSTED, texts.MAIN_MENU_KEYBOARD)
                await s.commit()
        async with self._sm() as s:
            history = await self._history(s, user_id)

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": cfg.system_prompt},
            *history,
            {"role": "user", "content": user_content if user_content is not None else text},
        ]
        try:
            if route is not None:
                assert self.keys is not None
                reply_text = await self.keys.chat(route, messages)
            else:
                reply_text = await self._client.chat(
                    base_url=cfg.base_url,
                    api_key=self._settings.base_model_api_key,
                    model=cfg.base_model,
                    messages=messages,
                )
        except ModelError:
            if route is None:
                async with self._sm() as s:
                    await quota.refund(s, user_id, day)
                    await s.commit()
            return Reply(texts.MODEL_ERROR)

        async with self._sm() as s:
            s.add(Message(user_id=user_id, role="user", content=stored_user_text or text))
            s.add(Message(user_id=user_id, role="assistant", content=reply_text))
            await s.commit()
        return Reply(reply_text)

    async def _handle_media(
        self,
        *,
        user_id: int,
        selected: str | None,
        text: str,
        media: IncomingMedia,
        cfg: runtime_settings.RuntimeConfig,
        limit: int,
        day: date,
        progress: Callable[[str], Awaitable[None]] | None,
        on_stage: Callable[[str], Awaitable[None]] | None,
    ) -> Reply:
        input_files = [(media.filename, media.data)]
        has_sandbox = (
            self.sandbox is not None
            and self._settings.sandbox_enabled
            and len(self._settings.proxy_secret) >= 16
        )

        if media.kind == "photo":
            caption = text.removeprefix("/agent").strip() or (
                "این تصویر را با دقت بررسی و تحلیل کن "
                "و تمام جزئیات یا متن داخل آن را به فارسی توضیح بده."
            )
            b64 = base64.b64encode(media.data).decode("ascii")
            data_url = f"data:{media.mime_type};base64,{b64}"
            multimodal_content = [
                {"type": "text", "text": caption},
                {"type": "image_url", "image_url": {"url": data_url}},
            ]
            wants_sandbox = has_sandbox and any(h in text for h in _PHOTO_SANDBOX_HINTS)
            if wants_sandbox:
                vision_desc = ""
                try:
                    route = await self.keys.resolve(user_id, selected) if self.keys else None
                    vision_msgs: list[dict[str, Any]] = [
                        {"role": "system", "content": cfg.system_prompt},
                        {"role": "user", "content": multimodal_content},
                    ]
                    if route is not None and self.keys is not None:
                        vision_desc = await self.keys.chat(route, vision_msgs)
                    else:
                        vision_desc = await self._client.chat(
                            base_url=cfg.base_url,
                            api_key=self._settings.base_model_api_key,
                            model=cfg.base_model,
                            messages=vision_msgs,
                        )
                except ModelError:
                    vision_desc = ""
                agent_task = (
                    f"کاربر تصویر «{media.filename}» را ارسال کرده که در مسیر "
                    f"/home/agent/in/{media.filename} قرار دارد.\n"
                    f"تحلیل بینایی ماشین از تصویر:\n{vision_desc}\n\n"
                    f"درخواست کاربر: {caption}"
                )
                return await self._agent(
                    user_id,
                    selected,
                    f"/agent {agent_task[:self._settings.max_input_chars]}",
                    limit,
                    day,
                    progress,
                    on_stage=on_stage,
                    input_files=input_files,
                )

            return await self._direct_chat(
                user_id,
                selected,
                caption,
                cfg,
                limit,
                day,
                user_content=multimodal_content,
                stored_user_text=f"[تصویر ارسالی: {media.filename}] {caption}",
            )

        if media.kind in ("voice", "audio"):
            ext = os.path.splitext(media.filename)[1].lstrip(".") or "ogg"
            transcript = await transcribe_audio_bytes(media.data, ext=ext)
            clean_caption = text.removeprefix("/agent").strip()
            if not transcript and not clean_caption:
                if has_sandbox and self._settings.unified_hermes:
                    task = (
                        f"کاربر یک پیام صوتی در /home/agent/in/{media.filename} فرستاده است. "
                        "با `/home/agent/.local/bin/balebot-stt` یا `ffmpeg` آن را بررسی کن."
                    )
                    return await self._agent(
                        user_id,
                        selected,
                        f"/agent {task}",
                        limit,
                        day,
                        progress,
                        on_stage=on_stage,
                        input_files=input_files,
                    )
                return Reply(
                    "متأسفانه صدای ارسالی قابل تشخیص نبود. "
                    "لطفاً واضح‌تر صحبت کنید یا پیام متنی بفرستید."
                )

            effective_text = (
                f"{transcript}\n{clean_caption}".strip() if transcript else clean_caption
            )
            if has_sandbox and (text.startswith("/agent") or self._settings.unified_hermes):
                agent_task = (
                    f"کاربر یک پیام صوتی فرستاده است (/home/agent/in/{media.filename}).\n"
                    f"متن پیاده‌سازی‌شده از ویس کاربر: «{effective_text}»\n"
                    "به درخواست کاربر پاسخ کامل بده."
                )
                reply = await self._agent(
                    user_id,
                    selected,
                    f"/agent {agent_task[:self._settings.max_input_chars]}",
                    limit,
                    day,
                    progress,
                    on_stage=on_stage,
                    input_files=input_files,
                )
                if reply.text not in (texts.AGENT_FAILED, texts.AGENT_EMPTY):
                    if transcript and transcript not in reply.text:
                        reply.text = f"🎤 متن ویس: «{transcript}»\n\n{reply.text}"
                    return reply

            reply = await self._direct_chat(
                user_id,
                selected,
                effective_text,
                cfg,
                limit,
                day,
                stored_user_text=f"[پیام صوتی] {effective_text}",
            )
            if transcript and reply.text != texts.MODEL_ERROR and transcript not in reply.text:
                reply.text = f"🎤 متن ویس: «{transcript}»\n\n{reply.text}"
            return reply

        # document
        clean_caption = (
            text.removeprefix("/agent").strip() or "محتوای این فایل را بررسی و تحلیل کن."
        )
        if has_sandbox and (text.startswith("/agent") or self._settings.unified_hermes):
            agent_task = (
                f"کاربر فایل «{media.filename}» را ارسال کرده که در مسیر "
                f"/home/agent/in/{media.filename} قرار دارد.\n"
                f"درخواست کاربر: {clean_caption}"
            )
            reply = await self._agent(
                user_id,
                selected,
                f"/agent {agent_task[:self._settings.max_input_chars]}",
                limit,
                day,
                progress,
                on_stage=on_stage,
                input_files=input_files,
            )
            if reply.text not in (texts.AGENT_FAILED, texts.AGENT_EMPTY):
                return reply

        preview = media.data[:6000].decode("utf-8", "ignore").strip()
        doc_prompt = f"کاربر فایل «{media.filename}» را فرستاده است.\nدرخواست: {clean_caption}"
        if preview:
            doc_prompt += f"\n\nبخشی از محتوای فایل:\n{preview[:3000]}"
        return await self._direct_chat(
            user_id,
            selected,
            doc_prompt[: self._settings.max_input_chars],
            cfg,
            limit,
            day,
        )

    async def _agent(
        self,
        user_id: int,
        selected: str | None,
        text: str,
        limit: int,
        day: date,
        progress: Callable[[str], Awaitable[None]] | None,
        on_stage: Callable[[str], Awaitable[None]] | None = None,
        input_files: list[tuple[str, bytes]] | None = None,
    ) -> Reply:
        """Run a task with Hermes tools inside the user's sandbox."""
        s = self._settings
        if self.sandbox is None or not s.sandbox_enabled or len(s.proxy_secret) < 16:
            return Reply(texts.AGENT_OFF)
        parts = text.split(None, 1)
        task = parts[1].strip() if len(parts) > 1 else ""
        if not task:
            return Reply(texts.AGENT_USAGE)
        if len(task) > s.max_input_chars:
            return Reply(texts.TOO_LONG.format(max=s.max_input_chars))
        if self.sandbox.is_busy(user_id):
            return Reply(texts.AGENT_BUSY)
        # runs on the user's own key are free; the free base model pays the agent cost
        own_key = bool(self.keys and await self.keys.resolve(user_id, selected))
        cost = 0 if own_key else s.agent_cost
        if cost:
            async with self._sm() as db:
                ok = await quota.consume(db, user_id, day, limit, cost)
                await db.commit()
            if not ok:
                return Reply(texts.AGENT_NO_QUOTA.format(cost=cost))

        async with self._sm() as db:
            user = await db.get(User, user_id)
            session_name = (
                f"bale-{int(user.context_reset_at.timestamp())}"
                if user and user.context_reset_at
                else "bale"
            )
            history = await self._history(db, user_id)

        prompt_for_agent = task
        if history:
            lines = ["[سابقهٔ گفتگوی اخیر با کاربر — برای حفظ زمینهٔ مکالمه:]"]
            for m in history[-10:]:
                role_label = "کاربر" if m["role"] == "user" else "دستیار"
                lines.append(f"{role_label}: {m['content']}")
            lines.append(f"\n[پیام جدید کاربر که باید الان به آن پاسخ دهی:]\n{task}")
            prompt_for_agent = "\n".join(lines)

        if progress:
            await progress(texts.AGENT_RUNNING)
        token = tokens.issue(
            s.proxy_secret, user_id, ttl=s.agent_timeout + 120, max_calls=s.agent_max_llm_calls
        )
        try:
            result = await self.sandbox.run_agent(
                user_id,
                prompt_for_agent,
                token=token,
                session_name=session_name,
                on_stage=on_stage,
                input_files=input_files,
            )
        except (SandboxError, OSError):
            result = None
        if result is None or (not result.ok and not result.text and not result.timed_out):
            if cost:
                async with self._sm() as db:
                    await quota.refund(db, user_id, day, cost)
                    await db.commit()
            return Reply(texts.AGENT_FAILED)
        text_out = result.text or texts.AGENT_EMPTY
        if result.timed_out:
            text_out = f"{text_out}\n\n{texts.AGENT_TIMEOUT}"

        if result.ok:
            async with self._sm() as db:
                db.add(Message(user_id=user_id, role="user", content=task))
                db.add(Message(user_id=user_id, role="assistant", content=text_out))
                await db.commit()

        return Reply(text_out, files=result.files)

    async def _simple_command(self, user_id: int, cmd: str, limit: int, day: date) -> str:
        async with self._sm() as s:
            if cmd == "/new":
                user = await s.get(User, user_id)
                assert user is not None
                user.context_reset_at = utcnow()
                await s.commit()
                return texts.NEW_CHAT
            used = await quota.used_today(s, user_id, day)
        tpl = texts.WELCOME if cmd == "/start" else texts.USAGE
        return tpl.format(limit=limit, used=used, remaining=max(limit - used, 0))

    async def _history(self, s: AsyncSession, user_id: int) -> list[dict[str, str]]:
        user = await s.get(User, user_id)
        q = select(Message).where(Message.user_id == user_id)
        if user and user.context_reset_at:
            q = q.where(Message.created_at > user.context_reset_at)
        q = q.order_by(Message.id.desc()).limit(self._settings.history_turns * 2)
        rows = list((await s.scalars(q)).all())
        return [{"role": m.role, "content": m.content} for m in reversed(rows)]
