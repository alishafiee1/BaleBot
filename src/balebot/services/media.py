"""Media helpers: incoming file representation, HTML extraction, STT, and TTS."""

from __future__ import annotations

import asyncio
import html
import logging
import re
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from balebot.net.safe_http import SafeHttp

log = logging.getLogger("balebot.services.media")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass(frozen=True)
class IncomingMedia:
    kind: str  # "photo" | "voice" | "audio" | "document"
    filename: str
    data: bytes
    mime_type: str


def sanitize_filename(name: str, default: str = "file.bin") -> str:
    base = (name or "").replace("\\", "/").strip().rsplit("/", 1)[-1]
    cleaned = _SAFE_NAME_RE.sub("_", base).lstrip(".")
    if not cleaned:
        return default
    return cleaned[:64]


def extract_html_text(raw: bytes, char_limit: int = 15000) -> tuple[str, str]:
    """Return (title, clean_text) from raw HTML or plain-text bytes."""
    text = raw.decode("utf-8", "ignore")
    title_match = re.search(r"<title[^>]*>(.*?)</title>", text, re.IGNORECASE | re.DOTALL)
    title = html.unescape(re.sub(r"\s+", " ", title_match.group(1)).strip()) if title_match else ""

    cleaned = re.sub(
        r"<(script|style|noscript|svg|head)[^>]*>.*?</\1>",
        " ",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    cleaned = re.sub(r"<(br|p|div|li|tr|h[1-6])[^>]*>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in cleaned.splitlines()]
    body = "\n".join(ln for ln in lines if ln)
    limit = max(500, min(char_limit, 100_000))
    return title[:300], body[:limit]


def _split_tts_chunks(text: str, max_len: int = 180) -> list[str]:
    cleaned = re.sub(r"[*_`#~>\[\]]+", " ", text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return []
    words = cleaned.split(" ")
    chunks: list[str] = []
    cur = ""
    for w in words:
        if len(w) > max_len:
            if cur:
                chunks.append(cur)
                cur = ""
            for i in range(0, len(w), max_len):
                chunks.append(w[i : i + max_len])
            continue
        candidate = f"{cur} {w}".strip() if cur else w
        if len(candidate) <= max_len:
            cur = candidate
        else:
            chunks.append(cur)
            cur = w
    if cur:
        chunks.append(cur)
    return chunks[:20]


async def synthesize_speech_bytes(http: SafeHttp, text: str, lang: str = "fa") -> bytes:
    """Synthesize speech audio (MP3 bytes) via Google Translate TTS endpoint."""
    chunks = _split_tts_chunks(text)
    if not chunks:
        return b""
    out = bytearray()
    safe_lang = re.sub(r"[^a-zA-Z\-]", "", lang or "fa") or "fa"
    for chunk in chunks:
        q = urllib.parse.quote(chunk)
        url = (
            f"https://translate.google.com/translate_tts"
            f"?ie=UTF-8&client=tw-ob&tl={safe_lang}&q={q}"
        )
        try:
            res = await http.request(
                "GET",
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=15.0,
            )
            if res.status == 200 and res.body:
                out.extend(res.body)
        except Exception as exc:  # noqa: BLE001
            log.debug("TTS chunk fetch failed: %s", exc)
    return bytes(out)


def _recognize_wav_sync(wav_path: Path, language: str) -> str:
    import speech_recognition as sr  # type: ignore[import-not-found,import-untyped,unused-ignore]

    rec = sr.Recognizer()
    parts: list[str] = []
    with sr.AudioFile(str(wav_path)) as source:
        for _ in range(8):  # up to ~200s total (8 * 25s)
            audio = rec.record(source, duration=25)
            if not audio.frame_data:
                break
            try:
                part = rec.recognize_google(audio, language=language)
                if isinstance(part, str) and part.strip():
                    parts.append(part.strip())
            except sr.UnknownValueError:
                continue
            except Exception as exc:  # noqa: BLE001
                log.debug("recognize_google error: %s", exc)
                break
    return " ".join(parts).strip()


async def transcribe_audio_bytes(
    data: bytes, ext: str = "ogg", language: str = "fa-IR"
) -> str:
    """Convert audio bytes with ffmpeg to 16kHz mono WAV and transcribe via Google STT."""
    if not data:
        return ""
    safe_ext = re.sub(r"[^a-zA-Z0-9]", "", ext or "ogg").lower() or "ogg"
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            in_path = Path(tmpdir) / f"in.{safe_ext}"
            wav_path = Path(tmpdir) / "out.wav"
            in_path.write_bytes(data)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                str(in_path),
                "-ar",
                "16000",
                "-ac",
                "1",
                str(wav_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=30.0)
            except TimeoutError:
                proc.kill()
                return ""
            if not wav_path.exists() or wav_path.stat().st_size <= 44:
                return ""
            return await asyncio.to_thread(_recognize_wav_sync, wav_path, language)
    except Exception as exc:  # noqa: BLE001
        log.debug("transcribe_audio_bytes failed: %s", exc)
        return ""
