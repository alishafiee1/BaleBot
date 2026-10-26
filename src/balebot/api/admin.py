import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, HttpUrl
from sqlalchemy import select

from balebot.models import AdminUser
from balebot.security import hash_password, verify_password
from balebot.services import runtime_settings
from balebot.state import AppState

router = APIRouter(prefix="/admin/api")
_basic = HTTPBasic()

# real hash computed once so unknown usernames cost the same time as wrong passwords
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def _state(request: Request) -> AppState:
    return request.app.state.ctx  # type: ignore[no-any-return]


async def require_admin(
    request: Request, creds: Annotated[HTTPBasicCredentials, Depends(_basic)]
) -> AdminUser:
    async with _state(request).sessionmaker() as s:
        admin = await s.scalar(select(AdminUser).where(AdminUser.username == creds.username))
    ok = verify_password(admin.password_hash if admin else _DUMMY_HASH, creds.password)
    if not (admin and ok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, headers={"WWW-Authenticate": "Basic"})
    return admin


class SettingsOut(BaseModel):
    base_url: str
    base_model: str
    system_prompt: str
    daily_quota: int


class SettingsIn(BaseModel):
    base_url: HttpUrl | None = None
    base_model: str | None = Field(default=None, min_length=1, max_length=128)
    system_prompt: str | None = Field(default=None, max_length=4000)
    daily_quota: int | None = Field(default=None, ge=0, le=100_000)


@router.get("/settings", response_model=SettingsOut)
async def get_settings(
    request: Request, _: Annotated[AdminUser, Depends(require_admin)]
) -> SettingsOut:
    st = _state(request)
    async with st.sessionmaker() as s:
        cfg = await runtime_settings.load(s, st.settings)
    return SettingsOut(**cfg.__dict__)


@router.put("/settings", response_model=SettingsOut)
async def put_settings(
    body: SettingsIn, request: Request, _: Annotated[AdminUser, Depends(require_admin)]
) -> SettingsOut:
    st = _state(request)
    values = {
        k: str(v) if k == "base_url" else v for k, v in body.model_dump(exclude_none=True).items()
    }
    if "base_url" in values and not str(values["base_url"]).startswith("https://"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "base_url must be https")
    async with st.sessionmaker() as s:
        await runtime_settings.save(s, values)
        await s.commit()
        cfg = await runtime_settings.load(s, st.settings)
    return SettingsOut(**cfg.__dict__)
