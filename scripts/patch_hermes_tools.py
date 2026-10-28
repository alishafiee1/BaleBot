#!/usr/bin/env python3
# ruff: noqa: E501
"""Patch Hermes built-in web, vision, TTS, STT, toolsets, and tool_search inside a sandbox container.

Run inside container `u1` / `agent-base` as root or agent:
    python3 patch_hermes_tools.py
"""

from pathlib import Path

HERMES_ROOT = Path("/home/agent/.hermes/hermes-agent")
TOOLS_DIR = HERMES_ROOT / "tools"

PROXY_WEB_BLOCK = """# --- BALEBOT_PROXY_WEB_HOOK ---
def _balebot_proxy_token() -> str:
    import os
    for p in ("/home/agent/.proxy_token", "/home/agent/.hermes/.proxy_token"):
        try:
            if os.path.exists(p):
                t = open(p, encoding="utf-8").read().strip()
                if t:
                    return t
        except Exception:
            pass
    return os.getenv("OPENAI_API_KEY", "").strip()


def _balebot_proxy_base() -> str:
    import os
    return os.getenv("OPENAI_BASE_URL", "http://10.77.0.1:8001/v1").rstrip("/")


def _balebot_proxy_search(query: str, count: int = 5) -> str:
    import json, urllib.request
    url = _balebot_proxy_base() + "/web/search"
    tok = _balebot_proxy_token()
    req = urllib.request.Request(
        url,
        data=json.dumps({"query": query, "limit": count}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    items = data.get("results") or []
    web_res = [
        {
            "title": it.get("title", ""),
            "url": it.get("link", ""),
            "description": it.get("snippet", ""),
            "position": idx + 1,
        }
        for idx, it in enumerate(items[: max(1, int(count or 5))])
    ]
    return json.dumps(
        {
            "success": True,
            "query": query,
            "formatted": data.get("formatted", ""),
            "data": {"web": web_res},
        },
        ensure_ascii=False,
    )


def _balebot_direct_extract_one(u: str, char_limit: int = 15000) -> dict:
    import html, ipaddress, re, socket, urllib.parse, urllib.request
    parsed = urllib.parse.urlparse(u)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return {"url": u, "title": "", "content": "", "error": "Invalid URL scheme"}
    try:
        for info in socket.getaddrinfo(parsed.hostname, None):
            ip_obj = ipaddress.ip_address(info[4][0])
            if ip_obj.is_private or ip_obj.is_loopback or ip_obj.is_link_local:
                return {"url": u, "title": "", "content": "", "error": "Blocked private network address"}
    except Exception as exc:
        return {"url": u, "title": "", "content": "", "error": f"DNS error: {exc}"}
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (compatible; HermesBot/1.0)"})
    with urllib.request.urlopen(req, timeout=15) as r:
        raw = r.read(1_500_000)
    text = raw.decode("utf-8", "ignore")
    tm = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
    title = html.unescape(re.sub(r"\\s+", " ", tm.group(1)).strip())[:300] if tm else ""
    cleaned = re.sub(r"<(script|style|noscript|svg|head)[^>]*>.*?</\\1>", " ", text, flags=re.I | re.S)
    cleaned = re.sub(r"<(br|p|div|li|tr|h[1-6])[^>]*>", "\\n", cleaned, flags=re.I)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = html.unescape(cleaned)
    lines = [re.sub(r"[ \\t]+", " ", ln).strip() for ln in cleaned.splitlines()]
    body = "\\n".join(ln for ln in lines if ln)[: max(500, min(int(char_limit or 15000), 50000))]
    return {"url": u, "title": title, "content": body, "raw_content": body, "error": None}


def _balebot_proxy_extract(urls: list, char_limit: int = 15000) -> str:
    import json, urllib.request
    norm_urls = []
    for item in (urls or [])[:5]:
        if isinstance(item, str) and item.strip():
            norm_urls.append(item.strip())
        elif isinstance(item, dict):
            u = item.get("url") or item.get("href")
            if isinstance(u, str) and u.strip():
                norm_urls.append(u.strip())
    url = _balebot_proxy_base() + "/web/extract"
    tok = _balebot_proxy_token()
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({"urls": norm_urls, "char_limit": int(char_limit or 15000)}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        return json.dumps(
            {"success": True, "results": data.get("results", [])},
            ensure_ascii=False,
        )
    except Exception:
        results = []
        for u in norm_urls:
            try:
                results.append(_balebot_direct_extract_one(u, int(char_limit or 15000)))
            except Exception as exc:
                results.append({"url": u, "title": "", "content": "", "error": str(exc)})
        return json.dumps({"success": True, "results": results}, ensure_ascii=False)
# --- END BALEBOT_PROXY_WEB_HOOK ---
"""

PROXY_VISION_BLOCK = """# --- BALEBOT_PROXY_VISION_HOOK ---
def _balebot_ocr_and_meta(data_url: str) -> str:
    import base64, json, re, subprocess, urllib.parse, urllib.request
    ocr_text = ""
    for params in (
        {"apikey": "helloworld", "base64Image": data_url, "OCREngine": "3", "language": "auto"},
        {"apikey": "helloworld", "base64Image": data_url, "OCREngine": "2"},
    ):
        try:
            payload = urllib.parse.urlencode(params).encode("utf-8")
            req = urllib.request.Request(
                "https://api.ocr.space/parse/image",
                data=payload,
                headers={"User-Agent": "Mozilla/5.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=20) as r:
                obj = json.loads(r.read().decode("utf-8", "ignore"))
            parts = []
            for it in obj.get("ParsedResults") or []:
                if isinstance(it, dict):
                    t = str(it.get("ParsedText") or "").strip()
                    if t and t != "*[No text detected]*":
                        parts.append(t)
            if parts:
                ocr_text = "\\n".join(parts)
                break
        except Exception:
            pass
    meta_info = ""
    try:
        if "," in data_url:
            raw_bytes = base64.b64decode(data_url.split(",", 1)[1])
            p = subprocess.run(
                ["ffmpeg", "-i", "pipe:0", "-vf", "scale=3:3", "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                input=raw_bytes,
                capture_output=True,
                timeout=10,
            )
            err_s = p.stderr.decode("utf-8", "ignore")
            m = re.search(r" (\\d{1,5}x\\d{1,5})[\\s,]", err_s)
            dim = m.group(1) if m else "unknown"
            if len(p.stdout) >= 27:
                r, g, b = p.stdout[12], p.stdout[13], p.stdout[14]
                meta_info = f"Resolution: {dim}, Center RGB: ({r}, {g}, {b})"
    except Exception:
        pass
    out = []
    if meta_info:
        out.append(meta_info)
    if ocr_text:
        out.append(f"Extracted OCR text:\\n{ocr_text}")
    else:
        out.append("No text detected in image via OCR.")
    return "\\n".join(out)


async def _balebot_proxy_vision(image_url: str, user_prompt: str) -> str:
    import asyncio, base64, json, os, urllib.request
    tok = ""
    for p in ("/home/agent/.proxy_token", "/home/agent/.hermes/.proxy_token"):
        if os.path.exists(p):
            tok = open(p, encoding="utf-8").read().strip()
            if tok:
                break
    if not tok:
        tok = os.getenv("OPENAI_API_KEY", "").strip()
    base = os.getenv("OPENAI_BASE_URL", "http://10.77.0.1:8001/v1").rstrip("/")
    raw_src = (image_url or "").strip()
    if raw_src.startswith("file://"):
        raw_src = raw_src[7:]
    expanded = os.path.expanduser(raw_src)
    if os.path.exists(expanded) and os.path.isfile(expanded):
        ext = os.path.splitext(expanded)[1].lower()
        mime = "image/png" if ext == ".png" else ("image/webp" if ext == ".webp" else "image/jpeg")
        b64 = base64.b64encode(open(expanded, "rb").read()).decode("ascii")
        data_url = f"data:{mime};base64,{b64}"
    elif raw_src.startswith("data:image/"):
        data_url = raw_src
    elif raw_src.startswith(("http://", "https://")):
        req_dl = urllib.request.Request(raw_src, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req_dl, timeout=20) as r_dl:
            b64 = base64.b64encode(r_dl.read()).decode("ascii")
        data_url = f"data:image/jpeg;base64,{b64}"
    else:
        data_url = raw_src

    prompt = (
        f"Analyze this image thoroughly and answer the user's request in Persian: {user_prompt}"
    )
    payload = {
        "model": "balebot",
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            }
        ],
    }
    try:
        req = urllib.request.Request(
            base + "/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            resp = json.loads(r.read().decode("utf-8", "ignore"))
        msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
        text = msg.get("content") or msg.get("reasoning_content") or ""
        if text:
            return json.dumps({"success": True, "analysis": text}, ensure_ascii=False)
    except Exception:
        pass

    meta = await asyncio.to_thread(_balebot_ocr_and_meta, data_url)
    fb_prompt = (
        f"اطلاعات استخراج‌شده از تصویر کاربر:\\n{meta}\\n\\n"
        f"درخواست کاربر دربارهٔ این تصویر: {user_prompt}\\n"
        "بر اساس متن و مشخصات استخراج‌شده از تصویر، به فارسی دقیق و کامل پاسخ بده."
    )
    fb_payload = {
        "model": "balebot",
        "messages": [{"role": "user", "content": fb_prompt}],
    }
    req_fb = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(fb_payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"},
        method="POST",
    )
    with urllib.request.urlopen(req_fb, timeout=45) as r_fb:
        resp_fb = json.loads(r_fb.read().decode("utf-8", "ignore"))
    msg_fb = ((resp_fb.get("choices") or [{}])[0]).get("message") or {}
    text_fb = msg_fb.get("content") or msg_fb.get("reasoning_content") or meta
    return json.dumps({"success": True, "analysis": text_fb}, ensure_ascii=False)
# --- END BALEBOT_PROXY_VISION_HOOK ---
"""

PROXY_TTS_BLOCK = """# --- BALEBOT_PROXY_TTS_HOOK ---
def _balebot_direct_tts_bytes(text: str, lang: str = "fa") -> bytes:
    import re, urllib.parse, urllib.request
    cleaned = re.sub(r"[*_`#~>\\[\\]]+", " ", text or "")
    cleaned = re.sub(r"\\s+", " ", cleaned).strip()
    if not cleaned:
        return b""
    words = cleaned.split(" ")
    chunks = []
    cur = ""
    for w in words:
        cand = f"{cur} {w}".strip() if cur else w
        if len(cand) <= 180:
            cur = cand
        else:
            if cur:
                chunks.append(cur)
            cur = w[:180]
    if cur:
        chunks.append(cur)
    tts_lang = "ar" if (lang or "fa").lower().startswith("fa") else lang
    out = bytearray()
    for ch in chunks[:20]:
        q = urllib.parse.quote(ch)
        u = f"https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl={tts_lang}&q={q}"
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            out.extend(r.read())
    return bytes(out)


def _balebot_proxy_tts(text: str, output_path: str | None = None) -> str:
    import datetime, json, os, shutil, urllib.request
    tok = ""
    for p in ("/home/agent/.proxy_token", "/home/agent/.hermes/.proxy_token"):
        if os.path.exists(p):
            tok = open(p, encoding="utf-8").read().strip()
            if tok:
                break
    if not tok:
        tok = os.getenv("OPENAI_API_KEY", "").strip()
    base = os.getenv("OPENAI_BASE_URL", "http://10.77.0.1:8001/v1").rstrip("/")
    out_dir = "/home/agent/out"
    os.makedirs(out_dir, exist_ok=True)
    if output_path and output_path.strip():
        final_path = os.path.abspath(os.path.expanduser(output_path.strip()))
        os.makedirs(os.path.dirname(final_path), exist_ok=True)
    else:
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        final_path = os.path.join(out_dir, f"tts_{ts}.mp3")
    audio = b""
    try:
        req = urllib.request.Request(
            base + "/audio/speech",
            data=json.dumps({"input": text, "lang": "fa"}).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            audio = r.read()
    except Exception:
        audio = _balebot_direct_tts_bytes(text, "fa")
    if not audio:
        return json.dumps({"success": False, "error": "TTS synthesis returned empty audio"})
    with open(final_path, "wb") as f:
        f.write(audio)
    if not final_path.startswith(out_dir + "/"):
        try:
            shutil.copy2(final_path, os.path.join(out_dir, os.path.basename(final_path)))
        except Exception:
            pass
    return f"MEDIA:{final_path}\\n" + json.dumps(
        {"success": True, "file_path": final_path, "bytes": len(audio)},
        ensure_ascii=False,
    )
# --- END BALEBOT_PROXY_TTS_HOOK ---
"""

PROXY_STT_BLOCK = """# --- BALEBOT_PROXY_STT_HOOK ---
def _balebot_google_stt_file(file_path: str, language: str = "fa-IR") -> dict:
    import json, subprocess, tempfile, urllib.parse, urllib.request
    with tempfile.TemporaryDirectory() as td:
        flac_p = f"{td}/out.flac"
        p = subprocess.run(
            ["ffmpeg", "-y", "-i", str(file_path), "-ac", "1", "-ar", "16000", "-sample_fmt", "s16", flac_p],
            capture_output=True,
            timeout=30,
        )
        if p.returncode != 0:
            return {"success": False, "text": "", "error": "ffmpeg conversion failed"}
        flac_bytes = open(flac_p, "rb").read()
    url = (
        "http://www.google.com/speech-api/v2/recognize"
        f"?client=chromium&lang={urllib.parse.quote(language or 'fa-IR')}"
        "&key=AIzaSyBOti4mM-6x9WDnZIjIeyEU21OpBXqWBgw&pFilter=0"
    )
    req = urllib.request.Request(
        url,
        data=flac_bytes,
        headers={"Content-Type": "audio/x-flac; rate=16000", "User-Agent": "Mozilla/5.0"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        raw = r.read().decode("utf-8", "ignore")
    parts = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        for res in obj.get("result") or []:
            alts = res.get("alternative") or []
            if alts and isinstance(alts[0], dict):
                t = str(alts[0].get("transcript") or "").strip()
                if t:
                    parts.append(t)
                    break
    text = " ".join(parts).strip()
    return {"success": True, "text": text, "transcript": text}
# --- END BALEBOT_PROXY_STT_HOOK ---
"""


def _strip_old_hook(s: str, start_marker: str, end_marker: str) -> str:
    if start_marker in s and end_marker in s:
        i = s.index(start_marker)
        j = s.index(end_marker) + len(end_marker)
        while j < len(s) and s[j] == "\n":
            j += 1
        return s[:i] + s[j:]
    return s


def patch_web() -> None:
    p = TOOLS_DIR / "web_tools.py"
    if not p.exists():
        return
    s = p.read_text(encoding="utf-8")
    s = _strip_old_hook(
        s, "# --- BALEBOT_PROXY_WEB_HOOK ---", "# --- END BALEBOT_PROXY_WEB_HOOK ---"
    )
    s = PROXY_WEB_BLOCK + "\n" + s
    if "def check_web_api_key() -> bool:\n    return True\n" not in s:
        s = s.replace(
            "def check_web_api_key() -> bool:\n",
            "def check_web_api_key() -> bool:\n    return True\n",
            1,
        )
    for sig_arg in ("limit", "count"):
        target = f"def web_search_tool(query: str, {sig_arg}: int = 5) -> str:\n"
        hook_line = f"    if not os.getenv('FIRECRAWL_API_KEY'):\n        return _balebot_proxy_search(query, {sig_arg})\n"
        if target in s and "_balebot_proxy_search" not in s.split(target, 1)[1][:300]:
            s = s.replace(target, target + hook_line, 1)

    ext_async = "async def web_extract_tool(urls: list[Any], format: str | None = None, char_limit: Optional[int] = None) -> str:\n"
    ext_hook = "    if not os.getenv('FIRECRAWL_API_KEY') and not os.getenv('KEENABLE_API_KEY'):\n        return _balebot_proxy_extract(urls, char_limit or 15000)\n"
    if ext_async in s and "_balebot_proxy_extract" not in s.split(ext_async, 1)[1][:300]:
        s = s.replace(ext_async, ext_async + ext_hook, 1)
    elif (
        'if not urls:\n        return json.dumps({"error": "No URLs provided for extraction"})\n'
        in s
        and "_balebot_proxy_extract" not in s.split("No URLs provided for extraction", 1)[1][:300]
    ):
        s = s.replace(
            'if not urls:\n        return json.dumps({"error": "No URLs provided for extraction"})\n',
            'if not urls:\n        return json.dumps({"error": "No URLs provided for extraction"})\n'
            "    if not os.getenv('FIRECRAWL_API_KEY') and not os.getenv('KEENABLE_API_KEY'):\n"
            "        return _balebot_proxy_extract(urls)\n",
            1,
        )
    p.write_text(s, encoding="utf-8")
    print("Patched", p)


def patch_vision() -> None:
    p = TOOLS_DIR / "vision_tools.py"
    if not p.exists():
        return
    s = p.read_text(encoding="utf-8")
    s = _strip_old_hook(
        s, "# --- BALEBOT_PROXY_VISION_HOOK ---", "# --- END BALEBOT_PROXY_VISION_HOOK ---"
    )
    s = PROXY_VISION_BLOCK + "\n" + s
    if "def check_vision_requirements() -> bool:\n    return True\n" not in s:
        s = s.replace(
            "def check_vision_requirements() -> bool:\n",
            "def check_vision_requirements() -> bool:\n    return True\n",
            1,
        )
    handle_sig = "async def _handle_vision_analyze(args: dict[str, Any], **kw: Any) -> str:\n"
    handle_hook = (
        "    try:\n"
        "        return await _balebot_proxy_vision(args.get('image_url', ''), args.get('question', ''))\n"
        "    except Exception as _e:\n"
        "        return json.dumps({'success': False, 'error': f'Vision proxy error: {_e}'})\n"
    )
    if handle_sig in s and "_balebot_proxy_vision" not in s.split(handle_sig, 1)[1][:300]:
        s = s.replace(handle_sig, handle_sig + handle_hook, 1)

    for v_sig in (
        "async def vision_analyze_tool(\n    image_url: str, user_prompt: str, model: str | None = None,\n    task_id: Optional[str] = None, region: Optional[list] = None) -> str:\n",
        "async def vision_analyze_tool(\n    image_url: str,\n    user_prompt: str,\n    model: Optional[str] = None\n) -> str:\n",
    ):
        v_hook = (
            "    try:\n"
            "        return await _balebot_proxy_vision(image_url, user_prompt)\n"
            "    except Exception as _e:\n"
            "        return json.dumps({'success': False, 'error': f'Vision proxy error: {_e}'})\n"
        )
        if v_sig in s and "_balebot_proxy_vision" not in s.split(v_sig, 1)[1][:300]:
            s = s.replace(v_sig, v_sig + v_hook, 1)
    p.write_text(s, encoding="utf-8")
    print("Patched", p)


def patch_tts() -> None:
    p = TOOLS_DIR / "tts_tool.py"
    if not p.exists():
        return
    s = p.read_text(encoding="utf-8")
    s = _strip_old_hook(
        s, "# --- BALEBOT_PROXY_TTS_HOOK ---", "# --- END BALEBOT_PROXY_TTS_HOOK ---"
    )
    s = PROXY_TTS_BLOCK + "\n" + s
    if "def check_tts_requirements() -> bool:\n    return True\n" not in s:
        s = s.replace(
            "def check_tts_requirements() -> bool:\n",
            "def check_tts_requirements() -> bool:\n    return True\n",
            1,
        )
    for t_sig in (
        "def text_to_speech_tool(\n    text: str, output_path: Optional[str] = None, speed: Optional[float] = None,\n    instructions: Optional[str] = None, provider: Optional[str] = None) -> str:\n",
        "def text_to_speech_tool(\n    text: str,\n    output_path: Optional[str] = None,\n    voice: Optional[str] = None,\n) -> str:\n",
    ):
        t_hook = (
            "    try:\n"
            "        return _balebot_proxy_tts(text, output_path)\n"
            "    except Exception as _e:\n"
            "        return json.dumps({'success': False, 'error': f'TTS proxy error: {_e}'})\n"
        )
        if t_sig in s and "_balebot_proxy_tts" not in s.split(t_sig, 1)[1][:300]:
            s = s.replace(t_sig, t_sig + t_hook, 1)
    p.write_text(s, encoding="utf-8")
    print("Patched", p)


def patch_stt() -> None:
    p = TOOLS_DIR / "transcription_tools.py"
    if not p.exists():
        return
    s = p.read_text(encoding="utf-8")
    s = _strip_old_hook(
        s, "# --- BALEBOT_PROXY_STT_HOOK ---", "# --- END BALEBOT_PROXY_STT_HOOK ---"
    )
    s = PROXY_STT_BLOCK + "\n" + s
    stt_sig = "def transcribe_audio(\n    file_path: str, model: Optional[str] = None, source: Optional[str] = None) -> dict[str, Any]:\n"
    stt_hook = (
        "    try:\n"
        "        return _balebot_google_stt_file(file_path)\n"
        "    except Exception as _e:\n"
        "        return {'success': False, 'text': '', 'error': str(_e)}\n"
    )
    if stt_sig in s and "_balebot_google_stt_file" not in s.split(stt_sig, 1)[1][:300]:
        s = s.replace(stt_sig, stt_sig + stt_hook, 1)
    p.write_text(s, encoding="utf-8")
    print("Patched", p)


def patch_toolsets_and_tool_search() -> None:
    ts_p = HERMES_ROOT / "toolsets.py"
    if ts_p.exists():
        s = ts_p.read_text(encoding="utf-8")
        old_web = '"web": _ts("Web research and content extraction tools", ["web_search", "web_extract"]),'
        new_web = '"web": _ts("Web research, vision, and speech tools", ["web_search", "web_extract", "vision_analyze", "text_to_speech"]),'
        if old_web in s:
            s = s.replace(old_web, new_web, 1)
            ts_p.write_text(s, encoding="utf-8")
            print("Patched", ts_p)

    tsearch_p = TOOLS_DIR / "tool_search.py"
    if tsearch_p.exists():
        s = tsearch_p.read_text(encoding="utf-8")
        old_raw = 'if not isinstance(raw, dict):  # legacy bool / None\n            raw = {"enabled": "off" if raw is False else "auto"}'
        new_raw = 'if not isinstance(raw, dict):  # legacy bool / None\n            raw = {"enabled": "off"}'
        if old_raw in s:
            s = s.replace(old_raw, new_raw, 1)
            tsearch_p.write_text(s, encoding="utf-8")
            print("Patched", tsearch_p)


if __name__ == "__main__":
    patch_web()
    patch_vision()
    patch_tts()
    patch_stt()
    patch_toolsets_and_tool_search()
