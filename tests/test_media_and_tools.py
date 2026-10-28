import base64
import json

import httpx
import pytest

from balebot.gateway.updates import process_update
from balebot.sandbox import tokens
from balebot.sandbox.proxy import create_proxy_app
from balebot.services import chat as chat_mod
from balebot.services.media import extract_html_text, sanitize_filename
from balebot.services.model_client import _has_image_content, _zai_fallbacks
from tests.test_sandbox import SECRET, bearer, make_user


def test_vision_model_fallback_selection() -> None:
    msg_text = [{"role": "user", "content": "سلام"}]
    msg_img = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "این چیست؟"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        }
    ]
    assert not _has_image_content(msg_text)
    assert _has_image_content(msg_img)
    assert _zai_fallbacks("https://api.z.ai/api/paas/v4", "glm-4.7-flash", vision=True)[0] == (
        "glm-4.6v-flash"
    )
    assert _zai_fallbacks("https://api.z.ai/api/paas/v4", "glm-4.7-flash", vision=False)[0] == (
        "glm-4.7-flash"
    )


def test_extract_html_text_and_sanitize() -> None:
    raw = (
        b"<html><head><title>Test Page</title></head><body><h1>Hello</h1><p>World</p></body></html>"
    )
    title, text = extract_html_text(raw)
    assert title == "Test Page"
    assert "Hello" in text and "World" in text
    assert sanitize_filename("../../etc/passwd") == "passwd"


async def test_incoming_photo_uses_vision_multimodal(state, model, bale) -> None:
    bale.files_store["pic1"] = (b"\x89PNG\r\n\x1a\nfake", "photos/pic1.png")
    model.reply = "در این تصویر یک نمودار دیده می‌شود."
    await process_update(
        state,
        {
            "update_id": 101,
            "message": {
                "message_id": 11,
                "chat": {"id": 5},
                "from": {"id": 5},
                "photo": [{"file_id": "small"}, {"file_id": "pic1"}],
                "caption": "این عکس چیست؟",
            },
        },
    )
    assert bale.sent[-1][1] == "در این تصویر یک نمودار دیده می‌شود."
    last_msgs = model.calls[-1]["messages"]
    assert _has_image_content(last_msgs)


async def test_incoming_voice_transcribes_and_replies(
    agent_state, incus, bale, monkeypatch: pytest.MonkeyPatch
) -> None:
    bale.files_store["v1"] = (b"OggS-fake-voice", "voice/v1.ogg")

    async def fake_stt(data: bytes, *, ext: str = "ogg", language: str = "fa-IR") -> str:
        return "سلام ساعت چند است"

    monkeypatch.setattr(chat_mod, "transcribe_audio_bytes", fake_stt)
    incus.agent_output = "ساعت ۱۲ ظهر است."

    await process_update(
        agent_state,
        {
            "update_id": 102,
            "message": {
                "message_id": 12,
                "chat": {"id": 7},
                "from": {"id": 7},
                "voice": {"file_id": "v1", "mime_type": "audio/ogg"},
            },
        },
    )
    sent_text = bale.sent[-1][1]
    assert "🎤 متن ویس: «سلام ساعت چند است»" in sent_text
    assert "ساعت ۱۲ ظهر است." in sent_text
    assert incus.pushed["u1/home/agent/in/voice.ogg"] == b"OggS-fake-voice"


async def test_incoming_document_pushed_to_sandbox_in(agent_state, incus, bale) -> None:
    bale.files_store["doc1"] = (b"line1\nline2\n", "docs/report.txt")
    incus.agent_output = "فایل بررسی شد: ۲ خط دارد."
    await process_update(
        agent_state,
        {
            "update_id": 103,
            "message": {
                "message_id": 13,
                "chat": {"id": 9},
                "from": {"id": 9},
                "document": {
                    "file_id": "doc1",
                    "file_name": "report.txt",
                    "mime_type": "text/plain",
                },
                "caption": "چند خط دارد؟",
            },
        },
    )
    assert bale.sent[-1][1] == "فایل بررسی شد: ۲ خط دارد."
    assert incus.pushed["u1/home/agent/in/report.txt"] == b"line1\nline2\n"
    assert "u1/home/agent/.local/bin/web-extract" in incus.pushed
    assert "u1/home/agent/.local/bin/balebot-tts" in incus.pushed
    assert "u1/home/agent/.local/bin/balebot-stt" in incus.pushed
    assert "u1/home/agent/.proxy_token" in incus.pushed


async def test_proxy_vision_extract_and_audio_endpoints(
    agent_state, provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider.extra_keys.add("test-key")
    app = create_proxy_app(agent_state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://proxy"
    ) as c:
        uid = await make_user(agent_state)
        h = bearer(uid)
        user_tok = tokens.issue_user_token(SECRET, uid)
        uh = {"Authorization": f"Bearer {user_tok}"}

        # 1. Vision chat completion switches Z.ai model to glm-4.6v-flash
        vision_body = {
            "model": "balebot",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe"},
                        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                    ],
                }
            ],
            "tools": [{"type": "function", "function": {"name": "noop"}}],
        }
        r_vis = await c.post("/v1/chat/completions", json=vision_body, headers=h)
        assert r_vis.status_code == 200
        sent = json.loads(provider.seen[-1].content)
        assert sent["model"] == "glm-4.6v-flash"
        assert "tools" not in sent

        # 2. Web extract endpoint
        r_ext = await c.post(
            "/v1/web/extract",
            json={"urls": ["https://api.openai.com/"]},
            headers=uh,
        )
        assert r_ext.status_code == 200
        data_ext = r_ext.json()
        assert data_ext["ok"] is True
        assert "API docs" in data_ext["results"][0]["content"]

        # 3. Audio speech (TTS) endpoint
        from balebot.sandbox import proxy as proxy_mod

        async def fake_tts(http, text: str, *, lang: str = "fa") -> bytes:
            return b"ID3-fake-mp3"

        async def fake_stt(data: bytes, *, ext: str = "ogg", language: str = "fa-IR") -> str:
            return "متن تست صوتی"

        monkeypatch.setattr(proxy_mod, "synthesize_speech_bytes", fake_tts)
        monkeypatch.setattr(proxy_mod, "transcribe_audio_bytes", fake_stt)

        r_tts = await c.post("/v1/audio/speech", json={"input": "سلام دنیا"}, headers=uh)
        assert r_tts.status_code == 200
        assert r_tts.content == b"ID3-fake-mp3"

        # 4. Audio transcriptions (STT) endpoint
        r_stt = await c.post(
            "/v1/audio/transcriptions",
            json={
                "audio_b64": base64.b64encode(b"fake").decode("ascii"),
                "ext": "ogg",
                "language": "fa-IR",
            },
            headers=uh,
        )
        assert r_stt.status_code == 200
        assert r_stt.json() == {"ok": True, "text": "متن تست صوتی"}
