#!/usr/bin/env python3
"""Patch Hermes built-in web, vision, and TTS tools inside a sandbox container.

Run inside container `u1` / `agent-base` as root or agent:
    python3 patch_hermes_tools.py
"""

from pathlib import Path

TOOLS_DIR = Path("/home/agent/.hermes/hermes-agent/tools")

PROXY_WEB_BLOCK = '''# --- BALEBOT_PROXY_WEB_HOOK ---
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
        }
        for it in items
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


def _balebot_proxy_extract(urls: list[str], char_limit: int = 15000) -> str:
    import json, urllib.request
    url = _balebot_proxy_base() + "/web/extract"
    tok = _balebot_proxy_token()
    req = urllib.request.Request(
        url,
        data=json.dumps({"urls": urls, "char_limit": char_limit}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8", "ignore"))
    return json.dumps(
        {"success": True, "results": data.get("results", [])},
        ensure_ascii=False,
    )
# --- END BALEBOT_PROXY_WEB_HOOK ---
'''

PROXY_VISION_BLOCK = '''# --- BALEBOT_PROXY_VISION_HOOK ---
async def _balebot_proxy_vision(image_url: str, user_prompt: str) -> str:
    import base64, json, os, urllib.request
    tok = ""
    for p in ("/home/agent/.proxy_token", "/home/agent/.hermes/.proxy_token"):
        if os.path.exists(p):
            tok = open(p, encoding="utf-8").read().strip()
            if tok:
                break
    if not tok:
        tok = os.getenv("OPENAI_API_KEY", "").strip()
    base = os.getenv("OPENAI_BASE_URL", "http://10.77.0.1:8001/v1").rstrip("/")
    raw_src = image_url.strip()
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
    return json.dumps({"success": True, "analysis": text}, ensure_ascii=False)
# --- END BALEBOT_PROXY_VISION_HOOK ---
'''

PROXY_TTS_BLOCK = '''# --- BALEBOT_PROXY_TTS_HOOK ---
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
    req = urllib.request.Request(
        base + "/audio/speech",
        data=json.dumps({"input": text, "lang": "fa"}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {tok}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        audio = r.read()
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
'''


def patch_web() -> None:
    p = TOOLS_DIR / "web_tools.py"
    if not p.exists():
        return
    s = p.read_text(encoding="utf-8")
    if "BALEBOT_PROXY_WEB_HOOK" in s:
        return
    s = PROXY_WEB_BLOCK + "\n" + s
    s = s.replace(
        "def check_web_api_key() -> bool:\n",
        "def check_web_api_key() -> bool:\n    return True\n",
        1,
    )
    s = s.replace(
        "def web_search_tool(query: str, count: int = 5) -> str:\n",
        "def web_search_tool(query: str, count: int = 5) -> str:\n"
        "    if not os.getenv('FIRECRAWL_API_KEY'):\n"
        "        try:\n"
        "            return _balebot_proxy_search(query, count)\n"
        "        except Exception as _e:\n"
        "            return json.dumps({'error': f'Proxy web_search failed: {_e}'})\n",
        1,
    )
    s = s.replace(
        "if not urls:\n        return json.dumps({\"error\": \"No URLs provided for extraction\"})\n",
        "if not urls:\n        return json.dumps({\"error\": \"No URLs provided for extraction\"})\n"
        "    if not os.getenv('FIRECRAWL_API_KEY') and not os.getenv('KEENABLE_API_KEY'):\n"
        "        try:\n"
        "            return _balebot_proxy_extract(urls)\n"
        "        except Exception as _e:\n"
        "            return json.dumps({'error': f'Proxy web_extract failed: {_e}'})\n",
        1,
    )
    p.write_text(s, encoding="utf-8")
    print("Patched", p)


def patch_vision() -> None:
    p = TOOLS_DIR / "vision_tools.py"
    if not p.exists():
        return
    s = p.read_text(encoding="utf-8")
    if "BALEBOT_PROXY_VISION_HOOK" in s:
        return
    s = PROXY_VISION_BLOCK + "\n" + s
    s = s.replace(
        "def check_vision_requirements() -> bool:\n",
        "def check_vision_requirements() -> bool:\n    return True\n",
        1,
    )
    s = s.replace(
        "async def vision_analyze_tool(\n"
        "    image_url: str,\n"
        "    user_prompt: str,\n"
        "    model: Optional[str] = None\n"
        ") -> str:\n",
        "async def vision_analyze_tool(\n"
        "    image_url: str,\n"
        "    user_prompt: str,\n"
        "    model: Optional[str] = None\n"
        ") -> str:\n"
        "    try:\n"
        "        return await _balebot_proxy_vision(image_url, user_prompt)\n"
        "    except Exception as _e:\n"
        "        return json.dumps({'success': False, 'error': f'Vision proxy error: {_e}'})\n",
        1,
    )
    p.write_text(s, encoding="utf-8")
    print("Patched", p)


def patch_tts() -> None:
    p = TOOLS_DIR / "tts_tool.py"
    if not p.exists():
        return
    s = p.read_text(encoding="utf-8")
    if "BALEBOT_PROXY_TTS_HOOK" in s:
        return
    s = PROXY_TTS_BLOCK + "\n" + s
    s = s.replace(
        "def check_tts_requirements() -> bool:\n",
        "def check_tts_requirements() -> bool:\n    return True\n",
        1,
    )
    s = s.replace(
        "def text_to_speech_tool(\n"
        "    text: str,\n"
        "    output_path: Optional[str] = None,\n"
        "    voice: Optional[str] = None,\n"
        ") -> str:\n",
        "def text_to_speech_tool(\n"
        "    text: str,\n"
        "    output_path: Optional[str] = None,\n"
        "    voice: Optional[str] = None,\n"
        ") -> str:\n"
        "    try:\n"
        "        return _balebot_proxy_tts(text, output_path)\n"
        "    except Exception as _e:\n"
        "        return json.dumps({'success': False, 'error': f'TTS proxy error: {_e}'})\n",
        1,
    )
    p.write_text(s, encoding="utf-8")
    print("Patched", p)


if __name__ == "__main__":
    patch_web()
    patch_vision()
    patch_tts()
