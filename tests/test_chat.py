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


async def test_help_and_menu_commands_and_callbacks(state: AppState, model) -> None:
    start_reply = await state.chat.handle_message(1, "ali", "/start")
    assert "سلام" in start_reply.text
    assert start_reply.keyboard == texts.MAIN_MENU_KEYBOARD

    menu_reply = await state.chat.handle_message(1, "ali", "/menu")
    assert menu_reply.text == texts.MENU_TEXT
    assert menu_reply.keyboard == texts.MAIN_MENU_KEYBOARD

    help_reply = await state.chat.handle_message(1, "ali", "/help")
    assert help_reply.text == texts.HELP
    assert help_reply.keyboard == texts.MAIN_MENU_KEYBOARD

    cb_help = await state.chat.handle_callback(1, "menu:help")
    assert cb_help is not None and cb_help.text == texts.HELP

    cb_usage = await state.chat.handle_callback(1, "menu:usage")
    assert cb_usage is not None and "مصرف امروز" in cb_usage.text

    cb_new = await state.chat.handle_callback(1, "menu:new")
    assert cb_new is not None and cb_new.text == texts.NEW_CHAT

    cb_models = await state.chat.handle_callback(1, "menu:models")
    assert cb_models is not None and texts.MODELS_HEADER in cb_models.text

    cb_keys = await state.chat.handle_callback(1, "menu:keys")
    assert cb_keys is not None and cb_keys.text == texts.NO_KEYS

    # Test menu:addkey starts addkey flow, and normal command or menu:cancel clears pending_actions
    cb_addkey = await state.chat.handle_callback(1, "menu:addkey")
    assert cb_addkey is not None and cb_addkey.text == texts.ASK_SITE
    assert cb_addkey.keyboard == texts.SITE_PICK_KEYBOARD

    # Sending /start clears pending addkey state so normal chat works
    await state.chat.handle_message(1, "ali", "/start")
    chat_reply = await state.chat.handle_message(1, "ali", "سلام خوبی؟")
    assert chat_reply.text.startswith("echo:")


async def test_button_commands_and_legacy_commands(state: AppState, model) -> None:
    # Bottom persistent keyboard button presses
    r_restart = await state.chat.handle_message(1, "ali", "شروع مجدد")
    assert "سلام" in r_restart.text
    assert r_restart.keyboard == texts.MAIN_MENU_KEYBOARD
    assert r_restart.reply_keyboard == texts.BOTTOM_REPLY_KEYBOARD

    r_menu_btn = await state.chat.handle_message(1, "ali", "📋 منوی اصلی")
    assert r_menu_btn.text == texts.MENU_TEXT
    assert r_menu_btn.reply_keyboard == texts.BOTTOM_REPLY_KEYBOARD

    r_new_btn = await state.chat.handle_message(1, "ali", "🔄 گفتگوی تازه")
    assert r_new_btn.text == texts.NEW_CHAT

    r_models_btn = await state.chat.handle_message(1, "ali", "🤖 انتخاب مدل")
    assert texts.MODELS_HEADER in r_models_btn.text

    r_keys_btn = await state.chat.handle_message(1, "ali", "🔑 کلیدهای من")
    assert r_keys_btn.text == texts.NO_KEYS

    r_usage_btn = await state.chat.handle_message(1, "ali", "📊 سهمیه امروز")
    assert "مصرف امروز" in r_usage_btn.text

    r_help_btn = await state.chat.handle_message(1, "ali", "📖 راهنما")
    assert r_help_btn.text == texts.HELP

    # Legacy slash commands from old bot
    r_settings = await state.chat.handle_message(1, "ali", "/settings")
    assert r_settings.text == texts.MENU_TEXT
    assert r_settings.reply_keyboard == texts.BOTTOM_REPLY_KEYBOARD

    r_info = await state.chat.handle_message(1, "ali", "/info")
    assert "وضعیت ربات" in r_info.text
    assert r_info.reply_keyboard == texts.BOTTOM_REPLY_KEYBOARD

    r_status = await state.chat.handle_message(1, "ali", "/status")
    assert "وضعیت سرور" in r_status.text

    r_think_empty = await state.chat.handle_message(1, "ali", "/think")
    assert "حالت تفکر" in r_think_empty.text

    r_think_q = await state.chat.handle_message(1, "ali", "/think سلام دنیا")
    assert r_think_q.text.startswith("echo:سلام دنیا")

    r_daily_empty = await state.chat.handle_message(1, "ali", "/daily")
    assert "گزارش روزانه" in r_daily_empty.text
