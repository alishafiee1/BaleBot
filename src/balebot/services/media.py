"""Media helpers: incoming file representation, HTML extraction, STT, TTS, and OCR fallback."""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import html
import json
import logging
import re
import shutil
import struct
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


_IMAGE_FALLBACK_CACHE: dict[str, str] = {}


def _extract_raw_image_bytes(data_url: str) -> bytes:
    if not data_url:
        return b""
    if "," in data_url and data_url.startswith("data:"):
        try:
            return base64.b64decode(data_url.split(",", 1)[1])
        except Exception:  # noqa: BLE001
            return b""
    return b""


def get_cached_image_description(data_url: str) -> str:
    """Return cached visual + OCR description for a data_url if already computed."""
    raw = _extract_raw_image_bytes(data_url)
    if not raw:
        return ""
    key = hashlib.sha256(raw).hexdigest()
    return _IMAGE_FALLBACK_CACHE.get(key, "")


def _parse_image_dimensions(raw: bytes) -> str:
    """Extract widthxheight from PNG or JPEG header in pure Python."""
    if len(raw) >= 24 and raw[:8] == b"\x89PNG\r\n\x1a\n" and raw[12:16] == b"IHDR":
        w, h = struct.unpack(">II", raw[16:24])
        if w > 0 and h > 0:
            return f"{w}x{h}"
    if len(raw) > 4 and raw[:2] == b"\xff\xd8":
        idx = 2
        n = len(raw)
        while idx + 8 < n:
            if raw[idx] != 0xFF:
                idx += 1
                continue
            marker = raw[idx + 1]
            while marker == 0xFF and idx + 2 < n:
                idx += 1
                marker = raw[idx + 1]
            if marker in (0xD8, 0xD9):
                idx += 2
                continue
            if idx + 4 > n:
                break
            seg_len = struct.unpack(">H", raw[idx + 2 : idx + 4])[0]
            if seg_len < 2 or idx + 2 + seg_len > n:
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3) and idx + 9 <= n:
                h, w = struct.unpack(">HH", raw[idx + 5 : idx + 9])
                if w > 0 and h > 0:
                    return f"{w}x{h}"
            idx += 2 + seg_len
    return ""


def _gradio_upload_bytes(base_url: str, prefix: str, data: bytes) -> str:
    boundary = uuid.uuid4().hex
    body = (
        (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="image.jpg"\r\n'
            "Content-Type: image/jpeg\r\n\r\n"
        ).encode()
        + data
        + f"\r\n--{boundary}--\r\n".encode()
    )
    req = urllib.request.Request(  # noqa: S310
        f"{base_url}{prefix}/upload",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "Mozilla/5.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        uploaded = json.loads(resp.read().decode("utf-8", "ignore"))
    if isinstance(uploaded, list) and uploaded and isinstance(uploaded[0], str):
        return uploaded[0]
    return ""


def _gradio_sse_call(
    base_url: str,
    prefix: str,
    endpoint: str,
    payload_data: list[Any],
    max_wait: float = 16.0,
) -> str:
    ep = endpoint.lstrip("/")
    req = urllib.request.Request(  # noqa: S310
        f"{base_url}{prefix}/call/{ep}",
        data=json.dumps({"data": payload_data}).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        ev_obj = json.loads(resp.read().decode("utf-8", "ignore"))
    event_id = ev_obj.get("event_id") if isinstance(ev_obj, dict) else ""
    if not isinstance(event_id, str) or not event_id:
        return ""
    req2 = urllib.request.Request(  # noqa: S310
        f"{base_url}{prefix}/call/{ep}/{event_id}",
        headers={"User-Agent": "Mozilla/5.0"},
    )
    t_start = time.monotonic()
    cur_event = ""
    last_data = ""
    with urllib.request.urlopen(req2, timeout=max_wait) as resp2:  # noqa: S310
        while time.monotonic() - t_start < max_wait:
            raw_line = resp2.readline()
            if not raw_line:
                break
            line = raw_line.decode("utf-8", "ignore").strip()
            if line.startswith("event:"):
                cur_event = line.split(":", 1)[1].strip()
            elif line.startswith("data:"):
                d = line.split(":", 1)[1].strip()
                if d and d != "null":
                    last_data = d
                if cur_event in ("complete", "error"):
                    break
    if cur_event != "complete" or not last_data:
        return ""
    try:
        parsed = json.loads(last_data)
        if isinstance(parsed, list) and parsed:
            val = str(parsed[0] or "").strip()
            val = re.sub(r"\s*⏱\s*Took\s+[0-9.]+\s+seconds\s*$", "", val).strip()
            return val
    except Exception as exc:  # noqa: BLE001
        log.debug("Gradio parse failed: %s", exc)
    return ""


def _gradio_vlm_caption_sync(raw_bytes: bytes) -> str:
    """Caption visual scene and objects using CPU-hosted Florence-2 & BLIP-Large Spaces."""
    if len(raw_bytes) < 64:
        return ""

    specs: list[tuple[str, str, str, str, Any]] = [
        (
            "florence_sd3",
            "https://gokaygokay-florence-2-sd3-captioner.hf.space",
            "",
            "run_example",
            lambda up: [{"path": up, "meta": {"_type": "gradio.FileData"}}],
        ),
        (
            "blip_large",
            "https://tonyassi-blip-image-captioning-large.hf.space",
            "/gradio_api",
            "predict",
            lambda up: [{"path": up, "meta": {"_type": "gradio.FileData"}}, 15, 60],
        ),
        (
            "florence_base",
            "https://prithivmlmods-florence-2-image-caption.hf.space",
            "/gradio_api",
            "describe_image",
            lambda up: [{"path": up, "meta": {"_type": "gradio.FileData"}}, "Florence-2-base"],
        ),
    ]

    def _run_spec(spec: tuple[str, str, str, str, Any]) -> tuple[str, str]:
        name, url, pfx, ep, build_args = spec
        try:
            up = _gradio_upload_bytes(url, pfx, raw_bytes)
            if not up:
                return name, ""
            ans = _gradio_sse_call(url, pfx, ep, build_args(up), max_wait=15.0)
            return name, ans
        except Exception as exc:  # noqa: BLE001
            log.debug("VLM captioner %s failed: %s", name, exc)
            return name, ""

    results: dict[str, str] = {}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=3)
    try:
        futs = [pool.submit(_run_spec, s) for s in specs]
        deadline = time.monotonic() + 15.5
        pending = set(futs)
        while pending and time.monotonic() < deadline:
            rem = max(0.2, deadline - time.monotonic())
            done, pending = concurrent.futures.wait(
                pending,
                timeout=min(1.0, rem),
                return_when=concurrent.futures.FIRST_COMPLETED,
            )
            for f in done:
                try:
                    k, v = f.result()
                    if v:
                        results[k] = v
                except Exception as exc:  # noqa: BLE001
                    log.debug("VLM future error: %s", exc)
            has_detailed = bool(results.get("florence_sd3") or results.get("florence_base"))
            has_blip = bool(results.get("blip_large"))
            if has_detailed and has_blip:
                break
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    captions: list[str] = []
    if results.get("florence_sd3"):
        captions.append(f"- Detailed Scene Caption: {results['florence_sd3']}")
    if results.get("florence_base") and results.get("florence_base") != results.get("florence_sd3"):
        captions.append(f"- Visual Layout & Objects: {results['florence_base']}")
    if results.get("blip_large"):
        captions.append(f"- Main Subject Summary: {results['blip_large']}")
    return "\n".join(captions)


def _ocr_space_sync(data_url: str) -> str:
    """Extract text from a base64 image data_url using OCR.space free endpoint."""
    if not data_url.startswith("data:image/"):
        return ""
    raw = _extract_raw_image_bytes(data_url)
    if len(raw) < 64:
        return ""
    for engine_params in (
        {"apikey": "helloworld", "base64Image": data_url, "OCREngine": "3", "language": "auto"},
        {"apikey": "helloworld", "base64Image": data_url, "OCREngine": "2"},
    ):
        try:
            payload = urllib.parse.urlencode(engine_params).encode()
            req = urllib.request.Request(
                "https://api.ocr.space/parse/image",
                data=payload,
                headers={"User-Agent": "Mozilla/5.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=18) as resp:  # noqa: S310
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


async def _inspect_image_colors(raw_bytes: bytes) -> str:
    if not raw_bytes:
        return ""
    dim_str = _parse_image_dimensions(raw_bytes) or "unknown"
    color_info = f"Resolution: {dim_str}" if dim_str != "unknown" else ""
    try:
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
            out, err = await asyncio.wait_for(proc.communicate(raw_bytes), timeout=12.0)
            err_str = err.decode("utf-8", "ignore")
            dim_m = re.search(r" (\d{1,5}x\d{1,5})[\s,]", err_str)
            if dim_m:
                dim_str = dim_m.group(1)
            if len(out) >= 27:
                tl = (out[0], out[1], out[2])
                c = (out[12], out[13], out[14])
                br = (out[24], out[25], out[26])
                color_info = (
                    f"Resolution: {dim_str}, "
                    f"TopLeft RGB: {tl}, Center RGB: {c}, BottomRight RGB: {br}"
                )
    except Exception as exc:  # noqa: BLE001
        log.debug("Image color inspection failed: %s", exc)
    return color_info


async def describe_image_fallback(data_url: str) -> str:
    """Extract VLM scene description, OCR text, and color metadata when vision is 429."""
    raw_bytes = _extract_raw_image_bytes(data_url)
    cache_key = hashlib.sha256(raw_bytes).hexdigest() if raw_bytes else ""
    if cache_key and cache_key in _IMAGE_FALLBACK_CACHE:
        return _IMAGE_FALLBACK_CACHE[cache_key]

    vlm_task = asyncio.to_thread(_gradio_vlm_caption_sync, raw_bytes)
    ocr_task = asyncio.to_thread(_ocr_space_sync, data_url)
    color_task = _inspect_image_colors(raw_bytes)
    vlm_caption, ocr_text, color_info = await asyncio.gather(vlm_task, ocr_task, color_task)

    parts: list[str] = []
    if vlm_caption:
        parts.append(f"Visual Scene & Objects Description (VLM):\n{vlm_caption}")
    if color_info:
        parts.append(f"Image Technical Metadata: {color_info}")
    if ocr_text:
        parts.append(f"Extracted Text on Image (OCR):\n{ocr_text}")
    else:
        parts.append("Extracted Text on Image (OCR): No readable text written on the image.")
    result = "\n\n".join(parts)
    if cache_key and (vlm_caption or ocr_text):
        if len(_IMAGE_FALLBACK_CACHE) >= 64:
            _IMAGE_FALLBACK_CACHE.pop(next(iter(_IMAGE_FALLBACK_CACHE)), None)
        _IMAGE_FALLBACK_CACHE[cache_key] = result
    return result
