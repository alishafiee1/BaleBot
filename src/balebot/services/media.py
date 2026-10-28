"""Media helpers: incoming file representation, HTML extraction, STT, TTS, and OCR fallback."""

from __future__ import annotations

import asyncio
import base64
import html
import json
import logging
import re
import shutil
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from balebot.net.safe_http import SafeHttp

log = logging.getLogger("balebot.services.media")

_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_GOOGLE_STT_KEY = "AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw"


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
    """Synthesize speech audio (MP3 bytes) via Google Translate TTS endpoint.

    Note: Google Translate TTS returns HTTP 400 for `tl=fa`, so Persian (`fa`/`fa-IR`)
    uses `tl=ar` (which reads Persian/Arabic UTF-8 script and returns valid MP3 audio).
    """
    chunks = _split_tts_chunks(text)
    if not chunks:
        return b""
    out = bytearray()
    safe_lang = re.sub(r"[^a-zA-Z\-]", "", lang or "fa") or "fa"
    tts_lang = "ar" if safe_lang.lower().startswith("fa") else safe_lang
    for chunk in chunks:
        q = urllib.parse.quote(chunk)
        url = (
            f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl={tts_lang}&q={q}"
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


def _google_stt_flac_sync(flac_bytes: bytes, language: str) -> str:
    """Send 16kHz mono 16-bit FLAC bytes to Google Speech API v2 and return transcript."""
    if not flac_bytes:
        return ""
    safe_lang = re.sub(r"[^a-zA-Z\-]", "", language or "fa-IR") or "fa-IR"
    url = (
        f"http://www.google.com/speech-api/v2/recognize"
        f"?client=chromium&lang={urllib.parse.quote(safe_lang)}&key={_GOOGLE_STT_KEY}&pFilter=0"
    )
    req = urllib.request.Request(
        url,
        data=flac_bytes,
        headers={
            "Content-Type": "audio/x-flac; rate=16000",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", "ignore")
    except Exception as exc:  # noqa: BLE001
        log.debug("Google STT request failed: %s", exc)
        return ""

    transcripts: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        for res in obj.get("result") or []:
            alts = res.get("alternative") or []
            if alts and isinstance(alts[0], dict):
                t = str(alts[0].get("transcript") or "").strip()
                if t:
                    transcripts.append(t)
                    break
    return " ".join(transcripts).strip()


def _recognize_wav_sync(wav_path: Path, language: str) -> str:
    import speech_recognition as sr  # type: ignore[import-not-found,import-untyped,unused-ignore]

    rec = sr.Recognizer()
    parts: list[str] = []
    with sr.AudioFile(str(wav_path)) as source:
        for _ in range(8):
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


async def _convert_audio_to_flac(data: bytes, safe_ext: str) -> bytes:
    """Convert arbitrary audio bytes to 16kHz mono s16 FLAC via local or container ffmpeg."""
    if shutil.which("ffmpeg"):
        with tempfile.TemporaryDirectory() as tmpdir:
            in_path = Path(tmpdir) / f"in.{safe_ext}"
            flac_path = Path(tmpdir) / "out.flac"
            in_path.write_bytes(data)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-i",
                str(in_path),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-sample_fmt",
                "s16",
                str(flac_path),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(proc.wait(), timeout=30.0)
            except TimeoutError:
                proc.kill()
                return b""
            if flac_path.exists() and flac_path.stat().st_size > 64:
                return flac_path.read_bytes()

    if shutil.which("incus"):
        proc = await asyncio.create_subprocess_exec(
            "incus",
            "--project",
            "balebot",
            "exec",
            "u1",
            "--",
            "ffmpeg",
            "-y",
            "-i",
            "pipe:0",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-sample_fmt",
            "s16",
            "-f",
            "flac",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(data), timeout=30.0)
            if proc.returncode == 0 and len(out) > 64:
                return out
        except TimeoutError:
            proc.kill()
    return b""


async def transcribe_audio_bytes(data: bytes, ext: str = "ogg", language: str = "fa-IR") -> str:
    """Convert audio bytes to 16kHz mono FLAC/WAV and transcribe via Google STT."""
    if not data:
        return ""
    safe_ext = re.sub(r"[^a-zA-Z0-9]", "", ext or "ogg").lower() or "ogg"
    try:
        flac_bytes = await _convert_audio_to_flac(data, safe_ext)
        if flac_bytes:
            text = await asyncio.to_thread(_google_stt_flac_sync, flac_bytes, language)
            if text:
                return text

        if shutil.which("ffmpeg"):
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
                if wav_path.exists() and wav_path.stat().st_size > 44:
                    return await asyncio.to_thread(_recognize_wav_sync, wav_path, language)
    except Exception as exc:  # noqa: BLE001
        log.debug("transcribe_audio_bytes failed: %s", exc)
    return ""


def _ocr_space_sync(data_url: str) -> str:
    """Extract text from a base64 image data_url using OCR.space free endpoint."""
    if not data_url.startswith("data:image/"):
        return ""
    for engine_params in (
        {"apikey": "helloworld", "base64Image": data_url, "OCREngine": "3", "language": "auto"},
        {"apikey": "helloworld", "base64Image": data_url, "OCREngine": "2"},
    ):
        try:
            payload = urllib.parse.urlencode(engine_params).encode("utf-8")
            req = urllib.request.Request(
                "https://api.ocr.space/parse/image",
                data=payload,
                headers={"User-Agent": "Mozilla/5.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
                obj = json.loads(resp.read().decode("utf-8", "ignore"))
            texts: list[str] = []
            for item in obj.get("ParsedResults") or []:
                if isinstance(item, dict):
                    t = str(item.get("ParsedText") or "").strip()
                    if t and t != "*[No text detected]*":
                        texts.append(t)
            joined = "\n".join(texts).strip()
            if joined:
                return joined
        except Exception as exc:  # noqa: BLE001
            log.debug("OCR fallback attempt failed: %s", exc)
    return ""


async def describe_image_fallback(data_url: str) -> str:
    """Extract OCR text and visual metadata when upstream vision model is overloaded (429)."""
    ocr_text = await asyncio.to_thread(_ocr_space_sync, data_url)
    color_info = ""
    try:
        if "," in data_url:
            raw_bytes = base64.b64decode(data_url.split(",", 1)[1])
            cmd: list[str] | None = None
            if shutil.which("ffmpeg"):
                cmd = [
                    "ffmpeg",
                    "-i",
                    "pipe:0",
                    "-vf",
                    "scale=3:3",
                    "-frames:v",
                    "1",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "pipe:1",
                ]
            elif shutil.which("incus"):
                cmd = [
                    "incus",
                    "--project",
                    "balebot",
                    "exec",
                    "u1",
                    "--",
                    "ffmpeg",
                    "-i",
                    "pipe:0",
                    "-vf",
                    "scale=3:3",
                    "-frames:v",
                    "1",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "pipe:1",
                ]
            if cmd:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(raw_bytes), timeout=15.0)
                err_str = err.decode("utf-8", "ignore")
                dim_m = re.search(r" (\d{1,5}x\d{1,5})[\s,]", err_str)
                dim_str = dim_m.group(1) if dim_m else "unknown"
                if len(out) >= 27:
                    r, g, b = out[12], out[13], out[14]
                    color_info = f"Resolution: {dim_str}, Center RGB: ({r}, {g}, {b})"
    except Exception as exc:  # noqa: BLE001
        log.debug("Image color inspection failed: %s", exc)

    parts: list[str] = []
    if color_info:
        parts.append(color_info)
    if ocr_text:
        parts.append(f"Extracted OCR Text from image:\n{ocr_text}")
    else:
        parts.append("No readable text detected in the image via OCR.")
    return "\n".join(parts)
