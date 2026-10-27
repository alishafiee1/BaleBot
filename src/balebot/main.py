import hmac
import logging
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from balebot.api import admin
from balebot.config import Settings, get_settings
from balebot.db import create_all, make_engine, make_sessionmaker
from balebot.gateway.bale import BaleApi, BaleClient
from balebot.gateway.updates import process_update
from balebot.net.safe_http import SafeHttp
from balebot.panel import routes as panel
from balebot.sandbox.incus import CliIncus, IncusRunner
from balebot.sandbox.manager import SandboxManager
from balebot.sandbox.proxy import create_proxy_app
from balebot.services.chat import ChatService
from balebot.services.keys import KeyService
from balebot.services.model_client import ModelClient, OpenAICompatClient
from balebot.state import AppState
from balebot.vault import Vault


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs full request URLs at INFO, and the Bale token is part of the URL
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def build_state(
    settings: Settings,
    *,
    model_client: ModelClient | None = None,
    bale: BaleApi | None = None,
    http: SafeHttp | None = None,
    incus: IncusRunner | None = None,
) -> AppState:
    engine = make_engine(settings.database_url)
    sm = make_sessionmaker(engine)
    client = model_client or OpenAICompatClient()
    vault = Vault.from_b64(settings.vault_key) if settings.vault_key else None
    safe = http or SafeHttp()
    keys = KeyService(settings, sm, vault, safe, client)
    chat = ChatService(client, settings, sm, keys)
    sandbox = None
    if settings.sandbox_enabled:
        runner = incus or CliIncus(settings.incus_bin, settings.incus_project)
        sandbox = SandboxManager(settings, runner, sm)
        chat.sandbox = sandbox
    return AppState(
        settings=settings,
        engine=engine,
        sessionmaker=sm,
        model_client=client,
        bale=bale or BaleClient(settings.bale_token),
        chat=chat,
        http=safe,
        sandbox=sandbox,
    )


def create_app(state: AppState | None = None) -> FastAPI:
    settings = state.settings if state else get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging()
        app.state.ctx = state or build_state(settings)
        await create_all(app.state.ctx.engine)
        yield
        await app.state.ctx.engine.dispose()

    app = FastAPI(title="BaleBot", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret or secrets.token_urlsafe(32),
        session_cookie="balebot_panel",
        max_age=8 * 3600,
        same_site="strict",
        https_only=settings.cookie_secure,
        path="/panel",
    )
    app.include_router(admin.router)
    app.include_router(panel.router)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/panel/")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/bale/webhook/{secret}")
    async def webhook(
        secret: str, request: Request, background: BackgroundTasks
    ) -> dict[str, bool]:
        ctx: AppState = request.app.state.ctx
        expected = ctx.settings.webhook_secret
        if not expected or not hmac.compare_digest(secret, expected):
            raise HTTPException(404)
        update: dict[str, Any] = await request.json()
        if not isinstance(update, dict):
            raise HTTPException(400)
        if not ctx.claim_update(update):
            return {"ok": True, "duplicate_ignored": True}
        background.add_task(process_update, ctx, update)
        return {"ok": True}

    return app


def app_factory() -> FastAPI:
    return create_app()


def proxy_app_factory() -> FastAPI:
    """The LLM proxy for sandboxes. Run it bound ONLY to the sandbox bridge address."""
    state = build_state(get_settings())
    return create_proxy_app(state)
