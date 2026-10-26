from datetime import timedelta

from sqlalchemy import select

from balebot import texts
from balebot.models import Message, User, utcnow
from balebot.services.chat import purge_old_messages
from balebot.state import AppState


async def test_start_shows_quota(state: AppState) -> None:
    reply = await state.chat.handle(1, "ali", "/start")
    assert "3" in reply and "سلام" in reply


async def test_quota_exhausts_then_blocks(state: AppState, model) -> None:
    for _ in range(3):
        assert (await state.chat.handle(1, None, "hi")).startswith("echo:")
    assert await state.chat.handle(1, None, "hi") == texts.QUOTA_EXHAUSTED
    assert len(model.calls) == 3


async def test_quota_is_per_user(state: AppState) -> None:
    for _ in range(3):
        await state.chat.handle(1, None, "hi")
    assert (await state.chat.handle(2, None, "hi")).startswith("echo:")


async def test_failed_model_call_is_refunded(state: AppState, model) -> None:
    model.fail = True
    for _ in range(5):
        assert await state.chat.handle(1, None, "hi") == texts.MODEL_ERROR
    model.fail = False
    for _ in range(3):
        assert (await state.chat.handle(1, None, "hi")).startswith("echo:")


async def test_history_is_sent_and_new_resets_it(state: AppState, model) -> None:
    await state.chat.handle(1, None, "first")
    await state.chat.handle(1, None, "second")
    roles = [m["content"] for m in model.calls[-1]["messages"]]
    assert "first" in roles and "echo:first" in roles
    await state.chat.handle(1, None, "/new")
    await state.chat.handle(1, None, "third")
    assert [m["role"] for m in model.calls[-1]["messages"]] == ["system", "user"]


async def test_banned_user_is_refused(state: AppState, model) -> None:
    await state.chat.handle(1, None, "/start")
    async with state.sessionmaker() as s:
        user = await s.scalar(select(User).where(User.bale_id == 1))
        user.banned = True
        await s.commit()
    assert await state.chat.handle(1, None, "hi") == texts.BANNED
    assert model.calls == []


async def test_too_long_input_rejected(state: AppState, model) -> None:
    reply = await state.chat.handle(1, None, "x" * 5000)
    assert reply == texts.TOO_LONG.format(max=4000)
    assert model.calls == []


async def test_purge_removes_only_old_messages(state: AppState) -> None:
    await state.chat.handle(1, None, "recent")
    async with state.sessionmaker() as s:
        uid = await s.scalar(select(User.id).where(User.bale_id == 1))
        s.add(
            Message(
                user_id=uid, role="user", content="old", created_at=utcnow() - timedelta(days=200)
            )
        )
        await s.commit()
        assert await purge_old_messages(s, 183) == 1
        await s.commit()
        left = (await s.scalars(select(Message.content))).all()
    assert "old" not in left and "recent" in left
