from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
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
from balebot.services.model_client import ModelClient, ModelError
from balebot.services.reply import Reply


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
        sessionmaker: async_sessionmaker,
        keys: KeyService | None = None,
    ) -> None:
        self._client = client
        self._settings = settings
        self._sm = sessionmaker
        self.keys = keys
        self.sandbox: SandboxManager | None = None  # set by build_state when enabled

    def _today(self):
        return datetime.now(ZoneInfo(self._settings.timezone)).date()

    async def handle(self, bale_id: int, username: str | None, text: str) -> str:
        return (await self.handle_message(bale_id, username, text)).text

    async def handle_callback(self, bale_id: int, data: str) -> Reply | None:
        if self.keys is None:
            return None
        async with self._sm() as s:
            user = await get_or_create_user(s, bale_id, None)
            cfg = await runtime_settings.load(s, self._settings)
            user_id, banned = user.id, user.banned
            await s.commit()
        if banned:
            return Reply(texts.BANNED)
        return await self.keys.handle_callback(user_id, data, cfg)

    async def handle_message(
        self,
        bale_id: int,
        username: str | None,
        text: str,
        delete_incoming: Deleter | None = None,
        progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> Reply:
        text = text.strip()
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
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""

        if self.keys is not None:
            reply = await self.keys.handle(
                user_id, cmd, text, cfg=cfg, limit=limit, day=day, delete_incoming=delete_incoming
            )
            if reply is not None:
                return reply
        if len(text) > self._settings.max_input_chars:
            return Reply(texts.TOO_LONG.format(max=self._settings.max_input_chars))

        if cmd == "/agent":
            return await self._agent(user_id, selected, text, limit, day, progress)
        if cmd in ("/start", "/usage", "/new"):
            return Reply(await self._simple_command(user_id, cmd, limit, day))
        if not text:
            return Reply(texts.UNSUPPORTED)

        route = await self.keys.resolve(user_id, selected) if self.keys else None
        if route is None:  # free base model: counts against the daily quota
            async with self._sm() as s:
                if not await quota.consume(s, user_id, day, limit):
                    await s.commit()
                    return Reply(texts.QUOTA_EXHAUSTED)
                await s.commit()
        async with self._sm() as s:
            history = await self._history(s, user_id)

        messages = [
            {"role": "system", "content": cfg.system_prompt},
            *history,
            {"role": "user", "content": text},
        ]
        try:
            if route is not None:
                assert self.keys is not None
                reply = await self.keys.chat(route, messages)
            else:
                reply = await self._client.chat(
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
            s.add(Message(user_id=user_id, role="user", content=text))
            s.add(Message(user_id=user_id, role="assistant", content=reply))
            await s.commit()
        return Reply(reply)

    async def _agent(
        self,
        user_id: int,
        selected: str | None,
        text: str,
        limit: int,
        day,
        progress: Callable[[str], Awaitable[None]] | None,
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
        if progress:
            await progress(texts.AGENT_RUNNING)
        token = tokens.issue(
            s.proxy_secret, user_id, ttl=s.agent_timeout + 120, max_calls=s.agent_max_llm_calls
        )
        try:
            result = await self.sandbox.run_agent(user_id, task, token=token)
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
        return Reply(text_out, files=result.files)

    async def _simple_command(self, user_id: int, cmd: str, limit: int, day) -> str:
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
