from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from balebot.config import Settings
from balebot.gateway.bale import BaleApi
from balebot.net.safe_http import SafeHttp
from balebot.sandbox.manager import SandboxManager
from balebot.services.chat import ChatService
from balebot.services.model_client import ModelClient


@dataclass
class AppState:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker
    model_client: ModelClient
    bale: BaleApi
    chat: ChatService
    http: SafeHttp
    sandbox: SandboxManager | None = None
