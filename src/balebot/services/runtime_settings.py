from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from balebot.config import Settings
from balebot.models import AppSetting


@dataclass(frozen=True)
class RuntimeConfig:
    base_url: str
    base_model: str
    system_prompt: str
    daily_quota: int


FIELDS = ("base_url", "base_model", "system_prompt", "daily_quota")


async def load(session: AsyncSession, settings: Settings) -> RuntimeConfig:
    """Admin-panel values override the env defaults."""
    rows = {r.key: r.value for r in (await session.scalars(select(AppSetting))).all()}
    return RuntimeConfig(
        base_url=rows.get("base_url", settings.default_base_url),
        base_model=rows.get("base_model", settings.default_base_model),
        system_prompt=rows.get("system_prompt", settings.default_system_prompt),
        daily_quota=int(rows.get("daily_quota", settings.default_daily_quota)),
    )


async def save(session: AsyncSession, values: dict[str, str | int]) -> None:
    for key, value in values.items():
        if key not in FIELDS:
            raise KeyError(key)
        row = await session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=str(value)))
        else:
            row.value = str(value)
