import json

from sqlalchemy import select

from balebot import texts
from balebot.config import Settings
from balebot.main import build_state
from balebot.models import PendingAction, User, UserKey, UserModel, utcnow
from balebot.services.keys import Route
from balebot.state import AppState

KEY = "sk-valid-1234567890"


async def say(state: AppState, uid: int, text: str, message_id: int | None = None) -> str:
    deleted = []

    async def delete_incoming() -> None:
        deleted.append(message_id)
        await state.bale.delete_message(uid, message_id or 0)

    reply = await state.chat.handle_message(uid, None, text, delete_incoming)
    return reply.text


async def add_openai_key(state: AppState, uid: int = 1) -> None:
    assert await say(state, uid, "/addkey") == texts.ASK_SITE
    assert "api.openai.com" in await say(state, uid, "openai")
    assert await say(state, uid, "بله") == texts.ASK_KEY
    assert "ثبت شد" in await say(state, uid, KEY, message_id=99)


async def test_full_flow_stores_encrypted_key_and_deletes_message(state, bale) -> None:
    await add_openai_key(state)
    assert bale.deleted == [(1, 99)]  # the message holding the key left the chat
    async with state.sessionmaker() as s:
        row = await s.scalar(select(UserKey))
        models = (await s.scalars(select(UserModel.model).order_by(UserModel.id))).all()
        pending = await s.get(PendingAction, 1)
    assert KEY.encode() not in row.ciphertext and row.last4 == KEY[-4:]
    assert models == ["gpt-a", "gpt-b"] and pending is None
    listing = await say(state, 1, "/keys")
    assert KEY not in listing and KEY[-4:] in listing


async def test_key_never_in_logs_state_or_model_prompt(state, model) -> None:
    await add_openai_key(state)
    async with state.sessionmaker() as s:
        pend = (await s.scalars(select(PendingAction.data))).all()
    assert all(KEY not in p for p in pend)
    assert all(KEY not in json.dumps(c["messages"]) for c in model.calls)


async def test_bad_key_is_rejected_and_not_stored(state, bale) -> None:
    await say(state, 1, "/addkey")
    await say(state, 1, "openai")
    await say(state, 1, "yes")
    assert await say(state, 1, "sk-wrong-0000000000", message_id=7) == texts.KEY_REJECTED
    assert bale.deleted == [(1, 7)]  # deleted even though it was wrong
    async with state.sessionmaker() as s:
        assert await s.scalar(select(UserKey)) is None
        assert await s.get(PendingAction, 1) is not None  # can retry


async def test_malformed_secret_is_still_deleted(state, bale) -> None:
    await say(state, 1, "/addkey")
    await say(state, 1, "openai")
    await say(state, 1, "yes")
    assert await say(state, 1, "a key with spaces", message_id=5) == texts.KEY_BAD_FORMAT
    assert bale.deleted == [(1, 5)]


async def test_user_key_chat_uses_provider_and_free_quota_untouched(state, model, provider) -> None:
    await add_openai_key(state)
    assert "gpt-b" in await say(state, 1, "/model 3")
    assert await say(state, 1, "hello") == "from-provider:gpt-b"
    assert model.calls == []  # base model not used
    for _ in range(5):  # more than the 3 free requests/day
        assert await say(state, 1, "again") == "from-provider:gpt-b"
    chat_req = [r for r in provider.seen if r.url.path.endswith("/chat/completions")][0]
    assert chat_req.headers["authorization"] == f"Bearer {KEY}"
    assert chat_req.headers["host"] == "api.openai.com"
    assert "3" in await say(state, 1, "/usage") and "0 از 3" in await say(state, 1, "/usage")


async def test_menu_lists_base_and_own_models_with_keyboard(state) -> None:
    await add_openai_key(state)
    reply = await state.chat.handle_message(1, None, "/models")
    assert "glm-4.7-flash" in reply.text and "gpt-a" in reply.text and "gpt-b" in reply.text
    assert [b["callback_data"] for row in reply.keyboard for b in row][:2] == [
        "pick:base",
        "pick:m:1",
    ]


async def test_other_users_cannot_see_or_pick_my_models(state) -> None:
    await add_openai_key(state, uid=1)
    other = await state.chat.handle_message(2, None, "/models")
    assert "gpt-a" not in other.text and "••••" not in other.text
    assert await say(state, 2, "/keys") == texts.NO_KEYS
    assert await say(state, 2, "/model 2") == texts.MODEL_BAD
    # forged callback data with someone else's model token
    cb = await state.chat.handle_callback(2, "pick:m:1")
    assert cb.text == texts.MODEL_BAD
    # even a poisoned selected_model cannot route through another user's key
    async with state.sessionmaker() as s:
        u2 = await s.scalar(select(User).where(User.bale_id == 2))
        u2.selected_model = "m:1"
        await s.commit()
        uid2 = u2.id
    assert await state.chat.keys.resolve(uid2, "m:1") is None
    assert await say(state, 2, "/delkey 1") == texts.KEY_NOT_FOUND


async def test_delete_key_resets_selection_and_removes_models(state, model) -> None:
    await add_openai_key(state)
    await say(state, 1, "/model 2")
    assert await say(state, 1, "/delkey 1") == texts.KEY_DELETED
    async with state.sessionmaker() as s:
        assert await s.scalar(select(UserKey)) is None
        assert (await s.scalars(select(UserModel))).all() == []
        user = await s.scalar(select(User))
    assert user.selected_model is None
    assert (await say(state, 1, "hi")).startswith("echo:")  # back on the base model


async def test_ciphertext_cannot_be_used_by_another_user(state) -> None:
    await add_openai_key(state)
    async with state.sessionmaker() as s:
        row = await s.scalar(select(UserKey))
    stolen = Route(2, row.id, row.base_url, row.auth, row.chat_path, row.ciphertext, "gpt-a")
    from balebot.services.model_client import ModelError

    try:
        await state.chat.keys.chat(stolen, [{"role": "user", "content": "x"}])
    except ModelError:
        return
    raise AssertionError("another user's route must not decrypt")


async def test_intent_phrase_starts_flow(state) -> None:
    reply = await say(state, 1, "می‌خواهم یه کلید api بهت بدم")
    assert reply == texts.ASK_SITE
    assert await say(state, 1, "/cancel") == texts.CANCELLED
    async with state.sessionmaker() as s:
        assert await s.get(PendingAction, 1) is None


async def test_unknown_site_uses_ai_and_charges_one_request(state, model) -> None:
    model.reply = '{"name":"Ex","base_url":"https://api.example.com/v1"}'
    await say(state, 1, "/addkey")
    reply = await say(state, 1, "https://docs.example.com")
    assert "api.example.com" in reply
    assert "KEY" not in json.dumps(model.calls[0]["messages"])
    assert "docs" in model.calls[0]["messages"][1]["content"]  # fetched page reached the AI
    assert "2 " in await say(state, 1, "/usage") or "باقی‌مانده: 2" in await say(state, 1, "/usage")


async def test_poisoned_docs_cannot_redirect_key_to_other_host(state, model) -> None:
    model.reply = '{"name":"Ex","base_url":"https://collector.attacker.net/v1"}'
    await say(state, 1, "/addkey")
    assert await say(state, 1, "https://docs.example.com") == texts.AI_FAILED
    async with state.sessionmaker() as s:
        pend = await s.get(PendingAction, 1)
    assert json.loads(pend.data) == {}  # still at the site step; no key was requested
    assert "باقی‌مانده: 3" in await say(state, 1, "/usage")  # request refunded


async def test_confirm_no_cancels(state) -> None:
    await say(state, 1, "/addkey")
    await say(state, 1, "openai")
    assert await say(state, 1, "خیر") == texts.CANCELLED
    async with state.sessionmaker() as s:
        assert await s.get(PendingAction, 1) is None


async def test_pending_expires(state) -> None:
    await say(state, 1, "/addkey")
    async with state.sessionmaker() as s:
        row = await s.get(PendingAction, 1)
        row.expires_at = utcnow().replace(year=2000)
        await s.commit()
    assert (await say(state, 1, "openai")).startswith("echo:")  # treated as a normal message


async def test_keys_off_without_vault(settings: Settings, model, bale) -> None:
    from balebot.db import create_all

    st = build_state(settings.model_copy(update={"vault_key": ""}), model_client=model, bale=bale)
    await create_all(st.engine)
    assert await st.chat.handle(1, None, "/addkey") == texts.KEYS_OFF


async def test_max_keys(state) -> None:
    for _ in range(5):
        await add_openai_key(state)
    await say(state, 1, "/addkey")
    await say(state, 1, "openai")
    await say(state, 1, "yes")
    assert "حداکثر" in await say(state, 1, KEY)


async def test_addmodel_manual(state) -> None:
    await add_openai_key(state)
    assert await say(state, 1, "/addmodel 1 my-model") == texts.MODEL_ADDED
    assert await say(state, 1, "/addmodel 1 bad name!") == texts.ADDMODEL_USAGE
    assert await say(state, 2, "/addmodel 1 hijack") == texts.KEY_NOT_FOUND
