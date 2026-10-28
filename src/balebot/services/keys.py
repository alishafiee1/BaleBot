"""Personal API keys: add-key conversation, vault storage, model menu, and routing."""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from balebot import texts
from balebot.config import Settings
from balebot.models import PendingAction, User, UserKey, UserModel, utcnow
from balebot.net.safe_http import SafeHttp, UnsafeURL
from balebot.services import quota
from balebot.services.model_client import ModelClient, ModelError
from balebot.services.providers import (
    GEN_SYSTEM,
    ProviderConfig,
    build_headers,
    extract_models,
    fetch_docs_text,
    known_provider,
    parse_generated,
    parse_site,
)
from balebot.services.reply import Reply
from balebot.services.runtime_settings import RuntimeConfig
from balebot.vault import Vault, VaultError, key_aad

PENDING_TTL = timedelta(minutes=15)
MAX_KEYS = 5
PAGE = 8
KEY_RE = re.compile(r"[A-Za-z0-9_\-.:=+/]{8,512}")
MODEL_RE = re.compile(r"[\w.\-:/]{1,128}")
YES = {"بله", "آره", "اره", "تایید", "تأیید", "yes", "y", "ok"}
NO = {"خیر", "نه", "no", "n"}
INTENT = re.compile(r"(api\s*-?key|کلید.{0,20}(بدم|بدهم|ثبت|اضافه|وارد|دارم))", re.I)
COMMANDS = {"/addkey", "/keys", "/delkey", "/addmodel", "/models", "/model", "/cancel"}

Deleter = Callable[[], Awaitable[None]]


@dataclass(frozen=True)
class Route:
    """A user's own key + model, loaded after the ownership check."""

    user_id: int
    key_id: int
    base_url: str
    auth: str
    chat_path: str
    ciphertext: bytes
    model: str


class QuotaExhausted(Exception):
    pass


class KeyService:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        vault: Vault | None,
        http: SafeHttp,
        model_client: ModelClient,
    ) -> None:
        self._settings = settings
        self._sm = sessionmaker
        self._vault = vault
        self._http = http
        self._client = model_client

    # ---- routing -------------------------------------------------------------------------
    async def resolve(self, user_id: int, selected: str | None) -> Route | None:
        """None = use the free base model. Only the user's own rows can ever match."""
        if not selected or not selected.startswith("m:") or not selected[2:].isdigit():
            return None
        async with self._sm() as s:
            row = (
                await s.execute(
                    select(UserModel, UserKey)
                    .join(UserKey, UserKey.id == UserModel.key_id)
                    .where(UserModel.id == int(selected[2:]), UserModel.user_id == user_id)
                    .where(UserKey.user_id == user_id)
                )
            ).first()
        if row is None:
            return None
        um, uk = row
        return Route(user_id, uk.id, uk.base_url, uk.auth, uk.chat_path, uk.ciphertext, um.model)

    async def raw_chat(self, route: Route, body: dict[str, Any]) -> dict[str, Any]:
        """Send an OpenAI-style body through the user's own key. The model is always the route's."""
        if self._vault is None:
            raise ModelError("vault disabled")
        try:
            api_key = self._vault.decrypt(route.ciphertext, key_aad(route.user_id, route.key_id))
            cfg = ProviderConfig(
                name="key",
                base_url=route.base_url,
                auth=route.auth,  # type: ignore[arg-type]
                chat_path=route.chat_path,
            )
        except (VaultError, ValueError):
            raise ModelError("vault") from None
        try:
            res = await self._http.request(
                "POST",
                cfg.base_url + cfg.chat_path,
                headers=build_headers(cfg, api_key),
                json={**body, "model": route.model, "stream": False},
                timeout=120.0,
            )
            if res.status != 200:
                raise ModelError(f"http {res.status}")
            data = json.loads(res.body)
        except UnsafeURL:
            raise ModelError("unsafe") from None
        except ValueError:
            raise ModelError("bad reply") from None
        if not isinstance(data, dict):
            raise ModelError("bad reply")
        return data

    async def chat(self, route: Route, messages: list[dict[str, Any]]) -> str:
        data = await self.raw_chat(route, {"messages": messages})
        try:
            msg = data["choices"][0]["message"]
            content = msg.get("content") or msg.get("reasoning_content")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise ModelError("bad reply") from None
        if not content:
            raise ModelError("empty")
        return str(content)

    # ---- conversation state --------------------------------------------------------------
    async def _pending(self, s: AsyncSession, user_id: int) -> PendingAction | None:
        row = await s.get(PendingAction, user_id)
        if row and row.expires_at < utcnow():
            await s.delete(row)
            await s.commit()
            return None
        return row

    async def _set_pending(self, user_id: int, step: str, data: dict[str, Any]) -> None:
        async with self._sm() as s:
            row = await s.get(PendingAction, user_id)
            payload, expires = json.dumps(data), utcnow() + PENDING_TTL
            if row is None:
                s.add(PendingAction(user_id=user_id, step=step, data=payload, expires_at=expires))
            else:
                row.step, row.data, row.expires_at = step, payload, expires
            await s.commit()

    async def _clear(self, user_id: int) -> None:
        async with self._sm() as s:
            await s.execute(delete(PendingAction).where(PendingAction.user_id == user_id))
            await s.commit()

    async def start_addkey(self, user_id: int) -> Reply:
        if self._vault is None:
            return Reply(texts.KEYS_OFF, texts.BACK_TO_MENU_KEYBOARD)
        await self._set_pending(user_id, "site", {})
        return Reply(texts.ASK_SITE, texts.SITE_PICK_KEYBOARD)

    async def start_agent_prompt(self, user_id: int) -> Reply:
        await self._set_pending(user_id, "agent_task", {})
        return Reply(texts.AGENT_PROMPT, texts.CANCEL_KEYBOARD)

    async def pop_agent_pending(self, user_id: int) -> bool:
        async with self._sm() as s:
            pending = await self._pending(s, user_id)
            if pending is not None and pending.step == "agent_task":
                await s.delete(pending)
                await s.commit()
                return True
        return False

    # ---- entry points --------------------------------------------------------------------
    @staticmethod
    def wants_keys(cmd: str, text: str) -> bool:
        return cmd in COMMANDS or (not cmd and len(text) < 200 and bool(INTENT.search(text)))

    async def handle(
        self,
        user_id: int,
        cmd: str,
        text: str,
        *,
        cfg: RuntimeConfig,
        limit: int,
        day: date,
        delete_incoming: Deleter | None = None,
    ) -> Reply | None:
        """A Reply when this message belongs to the key features, None to chat normally."""
        if cmd == "/cancel":
            await self._clear(user_id)
            return Reply(texts.CANCELLED, texts.MAIN_MENU_KEYBOARD)
        async with self._sm() as s:
            pending = await self._pending(s, user_id)
        if cmd == "/addkey" or (not cmd and pending is None and self.wants_keys("", text)):
            return await self.start_addkey(user_id)
        if cmd == "/keys":
            await self._clear(user_id)
            return await self._list_keys(user_id)
        if cmd == "/delkey":
            await self._clear(user_id)
            return await self._del_key(user_id, text)
        if cmd == "/addmodel":
            await self._clear(user_id)
            return await self._add_model(user_id, text)
        if cmd in ("/models", "/model"):
            await self._clear(user_id)
            parts = text.split()
            num = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            if cmd == "/models":
                return await self.models_page(user_id, cfg, num or 1)
            return await self._pick_number(user_id, cfg, num or 0)
        if cmd:
            await self._clear(user_id)
            return None
        if pending is None or pending.step == "agent_task":
            return None
        return await self._advance(user_id, pending, text, cfg, limit, day, delete_incoming)

    async def handle_callback(
        self,
        user_id: int,
        data: str,
        cfg: RuntimeConfig,
        *,
        limit: int = 20,
        day: date | None = None,
    ) -> Reply | None:
        kind, _, arg = data.partition(":")
        if kind == "page" and arg.isdigit():
            return await self.models_page(user_id, cfg, int(arg))
        if kind == "pick":
            return await self._pick_token(user_id, cfg, arg)
        if kind == "site" and arg:
            return await self._step_site(user_id, arg, cfg, limit, day or date.today())
        if kind == "confirm":
            async with self._sm() as s:
                pending = await self._pending(s, user_id)
            if pending is None or pending.step != "confirm":
                return Reply(texts.CANCELLED, texts.MAIN_MENU_KEYBOARD)
            pdata = json.loads(pending.data)
            if arg == "yes":
                await self._set_pending(user_id, "key", pdata)
                return Reply(texts.ASK_KEY, texts.CANCEL_KEYBOARD)
            await self._clear(user_id)
            return Reply(texts.CANCELLED, texts.MAIN_MENU_KEYBOARD)
        return None

    # ---- add-key steps -------------------------------------------------------------------
    async def _advance(
        self,
        user_id: int,
        pending: PendingAction,
        text: str,
        cfg: RuntimeConfig,
        limit: int,
        day: date,
        delete_incoming: Deleter | None,
    ) -> Reply:
        data = json.loads(pending.data)
        if pending.step == "site":
            return await self._step_site(user_id, text, cfg, limit, day)
        provider = ProviderConfig(**data["cfg"])
        if pending.step == "confirm":
            word = text.strip().lower()
            if word in YES:
                await self._set_pending(user_id, "key", data)
                return Reply(texts.ASK_KEY, texts.CANCEL_KEYBOARD)
            if word in NO:
                await self._clear(user_id)
                return Reply(texts.CANCELLED, texts.MAIN_MENU_KEYBOARD)
            return Reply(
                texts.CONFIRM.format(name=provider.name, host=provider.host),
                texts.CONFIRM_KEYBOARD,
            )
        return await self._step_key(user_id, provider, text, delete_incoming)

    async def _step_site(
        self, user_id: int, text: str, cfg: RuntimeConfig, limit: int, day: date
    ) -> Reply:
        provider = known_provider(text)
        if provider is None:
            site = parse_site(text)
            if site is None:
                return Reply(texts.SITE_UNKNOWN, texts.SITE_PICK_KEYBOARD)
            try:
                provider = await self._generate(user_id, site, cfg, limit, day)
            except QuotaExhausted:
                return Reply(texts.QUOTA_EXHAUSTED, texts.BACK_TO_MENU_KEYBOARD)
            if provider is None:
                return Reply(texts.AI_FAILED, texts.SITE_PICK_KEYBOARD)
        await self._set_pending(user_id, "confirm", {"cfg": provider.model_dump()})
        return Reply(
            texts.CONFIRM.format(name=provider.name, host=provider.host),
            texts.CONFIRM_KEYBOARD,
        )

    async def _generate(
        self, user_id: int, site: str, cfg: RuntimeConfig, limit: int, day: date
    ) -> ProviderConfig | None:
        """The AI reads public docs and returns a config. The key does not exist yet."""
        async with self._sm() as s:
            ok = await quota.consume(s, user_id, day, limit)
            await s.commit()
        if not ok:
            raise QuotaExhausted
        host = site.split("/")[2]
        try:
            docs = await fetch_docs_text(self._http, site)
            raw = await self._client.chat(
                base_url=cfg.base_url,
                api_key=self._settings.base_model_api_key,
                model=cfg.base_model,
                messages=[
                    {"role": "system", "content": GEN_SYSTEM},
                    {"role": "user", "content": f"Site: {host}\n<docs>\n{docs}\n</docs>"},
                ],
            )
            return parse_generated(raw, host)
        except (ValueError, UnsafeURL, ModelError):
            async with self._sm() as s:
                await quota.refund(s, user_id, day)
                await s.commit()
            return None

    async def _step_key(
        self, user_id: int, provider: ProviderConfig, text: str, delete_incoming: Deleter | None
    ) -> Reply:
        # whatever the user sent here may be a secret: remove it from the chat first
        if delete_incoming:
            await delete_incoming()
        key = text.strip()
        if self._vault is None:
            return Reply(texts.KEYS_OFF, texts.BACK_TO_MENU_KEYBOARD)
        if not KEY_RE.fullmatch(key):
            return Reply(texts.KEY_BAD_FORMAT, texts.CANCEL_KEYBOARD)
        models: list[str] = []
        if provider.models_path:
            try:
                res = await self._http.request(
                    "GET",
                    provider.base_url + provider.models_path,
                    headers=build_headers(provider, key),
                )
            except UnsafeURL:
                return Reply(texts.KEY_PROBE_FAILED, texts.CANCEL_KEYBOARD)
            if res.status in (401, 403):
                return Reply(texts.KEY_REJECTED, texts.CANCEL_KEYBOARD)
            if res.status == 200:
                models = extract_models(res.body)
        async with self._sm() as s:
            count = await s.scalar(select(func.count(UserKey.id)).where(UserKey.user_id == user_id))
            if (count or 0) >= MAX_KEYS:
                return Reply(
                    f"حداکثر {MAX_KEYS} کلید می‌توانید داشته باشید. /keys",
                    texts.KEYS_ACTION_KEYBOARD,
                )
            row = UserKey(
                user_id=user_id,
                name=provider.name,
                base_url=provider.base_url,
                auth=provider.auth,
                chat_path=provider.chat_path,
                models_path=provider.models_path,
                ciphertext=b"",
                last4=key[-4:],
            )
            s.add(row)
            await s.flush()
            row.ciphertext = self._vault.encrypt(key, key_aad(user_id, row.id))
            for m in models:
                s.add(UserModel(user_id=user_id, key_id=row.id, model=m))
            await s.execute(delete(PendingAction).where(PendingAction.user_id == user_id))
            await s.commit()
            key_id = row.id
        if models:
            return Reply(
                texts.KEY_SAVED.format(name=provider.name, count=len(models)),
                texts.KEYS_ACTION_KEYBOARD,
            )
        return Reply(
            texts.KEY_SAVED_NO_MODELS.format(name=provider.name, id=key_id),
            texts.KEYS_ACTION_KEYBOARD,
        )

    # ---- key management ------------------------------------------------------------------
    async def _list_keys(self, user_id: int) -> Reply:
        async with self._sm() as s:
            keys = (
                await s.scalars(
                    select(UserKey).where(UserKey.user_id == user_id).order_by(UserKey.id)
                )
            ).all()
            counted = await s.execute(
                select(UserModel.key_id, func.count())
                .where(UserModel.user_id == user_id)
                .group_by(UserModel.key_id)
            )
            counts = {kid: n for kid, n in counted.all()}
        if not keys:
            return Reply(texts.NO_KEYS, texts.KEYS_ACTION_KEYBOARD)
        lines = [texts.KEYS_HEADER] + [
            f"{k.id}. {k.name} — ••••{k.last4} — {counts.get(k.id, 0)} مدل  (حذف: /delkey {k.id})"
            for k in keys
        ]
        return Reply("\n".join(lines), texts.KEYS_ACTION_KEYBOARD)

    async def _del_key(self, user_id: int, text: str) -> Reply:
        parts = text.split()
        if len(parts) < 2 or not parts[1].isdigit():
            return Reply(texts.KEY_NOT_FOUND, texts.KEYS_ACTION_KEYBOARD)
        async with self._sm() as s:
            key = await s.get(UserKey, int(parts[1]))
            if key is None or key.user_id != user_id:
                return Reply(texts.KEY_NOT_FOUND, texts.KEYS_ACTION_KEYBOARD)
            await s.execute(delete(UserModel).where(UserModel.key_id == key.id))
            await s.delete(key)
            user = await s.get(User, user_id)
            sel = user.selected_model if user else None
            if (
                user
                and sel
                and sel.startswith("m:")
                and await s.get(UserModel, int(sel[2:])) is None
            ):
                user.selected_model = None
            await s.commit()
        return Reply(texts.KEY_DELETED, texts.KEYS_ACTION_KEYBOARD)

    async def _add_model(self, user_id: int, text: str) -> Reply:
        parts = text.split()
        if len(parts) != 3 or not parts[1].isdigit() or not MODEL_RE.fullmatch(parts[2]):
            return Reply(texts.ADDMODEL_USAGE, texts.BACK_TO_MENU_KEYBOARD)
        async with self._sm() as s:
            key = await s.get(UserKey, int(parts[1]))
            if key is None or key.user_id != user_id:
                return Reply(texts.KEY_NOT_FOUND, texts.KEYS_ACTION_KEYBOARD)
            s.add(UserModel(user_id=user_id, key_id=key.id, model=parts[2]))
            await s.commit()
        return Reply(texts.MODEL_ADDED, texts.KEYS_ACTION_KEYBOARD)

    # ---- model menu ----------------------------------------------------------------------
    async def _options(
        self, s: AsyncSession, user_id: int, cfg: RuntimeConfig
    ) -> list[tuple[str, str]]:
        base = [("base", texts.BASE_LABEL.format(model=cfg.base_model))]
        rows = (
            await s.execute(
                select(UserModel, UserKey.name)
                .join(UserKey, UserKey.id == UserModel.key_id)
                .where(UserModel.user_id == user_id, UserKey.user_id == user_id)
                .order_by(UserModel.id)
            )
        ).all()
        return base + [(f"m:{um.id}", f"{um.model} — {name}") for um, name in rows]

    async def models_page(self, user_id: int, cfg: RuntimeConfig, page: int) -> Reply:
        async with self._sm() as s:
            opts = await self._options(s, user_id, cfg)
            user = await s.get(User, user_id)
        selected = (user.selected_model if user else None) or "base"
        pages = max((len(opts) + PAGE - 1) // PAGE, 1)
        page = min(max(page, 1), pages)
        start = (page - 1) * PAGE
        chunk = opts[start : start + PAGE]
        lines = [texts.MODELS_HEADER] + [
            f"{start + i}. {'✅ ' if tok == selected else ''}{label}"
            for i, (tok, label) in enumerate(chunk, 1)
        ]
        if pages > 1:
            lines.append(f"صفحه {page} از {pages} — /models {page + 1 if page < pages else 1}")
        keyboard = [
            [
                {
                    "text": ("✅ " if tok == selected else "") + label[:40],
                    "callback_data": f"pick:{tok}",
                }
            ]
            for tok, label in chunk
        ]
        if pages > 1:
            keyboard.append(
                [
                    {"text": "◀", "callback_data": f"page:{max(page - 1, 1)}"},
                    {"text": "▶", "callback_data": f"page:{min(page + 1, pages)}"},
                ]
            )
        keyboard.append(
            [
                {"text": "🔑 افزودن کلید API", "callback_data": "menu:addkey"},
                {"text": "📋 منوی اصلی", "callback_data": "menu:main"},
            ]
        )
        return Reply("\n".join(lines), keyboard)

    async def _pick_number(self, user_id: int, cfg: RuntimeConfig, number: int) -> Reply:
        async with self._sm() as s:
            opts = await self._options(s, user_id, cfg)
        if not 1 <= number <= len(opts):
            return Reply(texts.MODEL_BAD, texts.BACK_TO_MENU_KEYBOARD)
        return await self._pick_token(user_id, cfg, opts[number - 1][0])

    async def _pick_token(self, user_id: int, cfg: RuntimeConfig, token: str) -> Reply:
        async with self._sm() as s:
            # only this user's own options can match, whatever the callback data says
            label = dict(await self._options(s, user_id, cfg)).get(token)
            if label is None:
                return Reply(texts.MODEL_BAD, texts.BACK_TO_MENU_KEYBOARD)
            user = await s.get(User, user_id)
            assert user is not None
            user.selected_model = None if token == "base" else token
            await s.commit()
        return Reply(texts.MODEL_SET.format(label=label), texts.BACK_TO_MENU_KEYBOARD)
