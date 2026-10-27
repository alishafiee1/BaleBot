from datetime import date
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from balebot.models import UsageDaily


async def used_today(session: AsyncSession, user_id: int, day: date) -> int:
    row = await session.scalar(
        select(UsageDaily.count).where(UsageDaily.user_id == user_id, UsageDaily.day == day)
    )
    return row or 0


async def consume(
    session: AsyncSession, user_id: int, day: date, limit: int, cost: int = 1
) -> bool:
    """Atomically take `cost` requests from today's quota. False when it would exceed the limit."""
    if cost <= 0 or limit < cost:
        return False
    existing = await session.scalar(
        select(UsageDaily.user_id).where(UsageDaily.user_id == user_id, UsageDaily.day == day)
    )
    if existing is None:
        try:
            async with session.begin_nested():
                session.add(UsageDaily(user_id=user_id, day=day, count=0))
        except IntegrityError:
            pass  # a concurrent request created the row first
    result = await session.execute(
        update(UsageDaily)
        .where(
            UsageDaily.user_id == user_id,
            UsageDaily.day == day,
            UsageDaily.count <= limit - cost,
        )
        .values(count=UsageDaily.count + cost)
    )
    return bool(result.rowcount == 1)  # type: ignore[attr-defined]


async def refund(session: AsyncSession, user_id: int, day: date, cost: int = 1) -> None:
    """Give requests back (the model call failed, so the user must not pay for it)."""
    await session.execute(
        update(UsageDaily)
        .where(UsageDaily.user_id == user_id, UsageDaily.day == day)
        .values(count=func_max0(UsageDaily.count - cost))
    )


def func_max0(expr: Any) -> Any:
    """max(expr, 0) in a way both SQLite and PostgreSQL accept."""
    from sqlalchemy import case

    return case((expr < 0, 0), else_=expr)
