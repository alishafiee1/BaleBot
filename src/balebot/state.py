import asyncio
import json
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from balebot.config import Settings
from balebot.gateway.bale import BaleApi
from balebot.net.safe_http import SafeHttp
from balebot.sandbox.manager import SandboxManager
from balebot.services.chat import ChatService
from balebot.services.model_client import ModelClient

MAX_SEEN_UPDATES = 2048


@dataclass
class AppState:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    model_client: ModelClient
    bale: BaleApi
    chat: ChatService
    http: SafeHttp
    sandbox: SandboxManager | None = None
    seen_updates: OrderedDict[tuple[str, str], None] = field(default_factory=OrderedDict)
    bg_tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    def claim_update(self, update: dict[str, Any]) -> bool:
        """Return True on first sight of (update_id, payload), False on duplicate delivery."""
        raw_id = update.get("update_id")
        if raw_id is None:
            return True
        fingerprint = json.dumps(update, sort_keys=True, ensure_ascii=False, default=str)
        key = (str(raw_id), fingerprint)
        if key in self.seen_updates:
            self.seen_updates.move_to_end(key)
            return False
        self.seen_updates[key] = None
        if len(self.seen_updates) > MAX_SEEN_UPDATES:
            self.seen_updates.popitem(last=False)
        return True
