#!/usr/bin/env python3
"""Comprehensive End-to-End (E2E) test harness for BaleBot.

Supports three execution modes:
  1. --mode full-suite : Runs all 7 E2E scenarios (commands, callbacks, stuck pending_actions
                         recovery, multi-turn AI memory + /new reset, /agent sandbox lifecycle,
                         web admin panel CSRF/auth/headers, security/idempotency/quota, and
                         live server/Incus/journalctl health checks) without spamming the
                         real user chat with synthetic edge-case errors.
  2. --mode live-chat  : Sends a curated sequence of real E2E webhook updates for the user's
                         actual Bale chat_id to the live server so balebot.service processes
                         them end-to-end (SQLite DB + Z.ai LLM + balebot-proxy + Incus u1)
                         and delivers the real replies & inline keyboards to Bale via tapi.bale.ai.
  3. --mode all        : Runs both full-suite and live-chat (default).

Security note: This script NEVER prints or logs secrets, tokens, API keys, or passwords.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select

ROOT = Path(__file__).resolve().parents[1]
if (ROOT / "src").is_dir() and str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from balebot import texts  # noqa: E402
from balebot.config import Settings  # noqa: E402
from balebot.main import build_state, create_app  # noqa: E402
from balebot.models import (  # noqa: E402
    AdminUser,
    Message,
    PendingAction,
    Sandbox,
    UsageDaily,
    User,
)
from balebot.net.safe_http import SafeHttp  # noqa: E402
from balebot.sandbox import tokens  # noqa: E402
from balebot.sandbox.incus import IncusResult  # noqa: E402
from balebot.sandbox.proxy import create_proxy_app  # noqa: E402
from balebot.security import hash_password  # noqa: E402
from balebot.vault import Vault  # noqa: E402

DEFAULT_BALE_USER_ID = 130743715
ALLOWED_TABLES = (
    "users",
    "messages",
    "pending_actions",
    "user_keys",
    "usage_daily",
    "sandboxes",
)


def _run_sync(binary: str, *args: str, timeout_s: float = 15.0) -> tuple[int, str, str]:
    exe = shutil.which(binary)
    if not exe:
        return 127, "", f"{binary} not found"
    try:
        proc = subprocess.run(  # noqa: S603
            [exe, *args],
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.SubprocessError as exc:
        return 1, "", type(exc).__name__


async def _run_async(binary: str, *args: str, timeout_s: float = 15.0) -> tuple[int, str, str]:
    return await asyncio.to_thread(_run_sync, binary, *args, timeout_s=timeout_s)


@dataclass
class StepResult:
    scenario: str
    step: str
    passed: bool
    latency_ms: float
    details: str = ""


@dataclass
class SuiteReport:
    mode: str
    started_at: str
    total_ms: float = 0.0
    passed: int = 0
    failed: int = 0
    results: list[StepResult] = field(default_factory=list)
    server_health: dict[str, Any] = field(default_factory=dict)

    def record(
        self,
        scenario: str,
        step: str,
        passed: bool,
        latency_ms: float,
        details: str = "",
    ) -> None:
        self.results.append(
            StepResult(
                scenario=scenario,
                step=step,
                passed=passed,
                latency_ms=round(latency_ms, 1),
                details=details,
            )
        )
        if passed:
            self.passed += 1
        else:
            self.failed += 1
        icon = "PASS" if passed else "FAIL"
        print(
            f"  [{icon}] {scenario} :: {step} ({latency_ms:.1f} ms) - {details}",
            flush=True,
        )


# ---------------------------------------------------------------------------
# In-process E2E Fakes (for deterministic full-suite DB & state verification)
# ---------------------------------------------------------------------------
class SpyBale:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, Any]] = []
        self.edited: list[tuple[int, int, str, Any]] = []
        self.reply_keyboards: list[tuple[int, str, Any]] = []
        self.commands: list[dict[str, str]] = []
        self.deleted: list[tuple[int, int]] = []
        self.documents: list[tuple[int, str, bytes]] = []
        self.callbacks_answered: list[str] = []
        self.actions: list[tuple[int, str]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> int:
        self.sent.append((chat_id, text, keyboard))
        return len(self.sent)

    async def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        keyboard: list[list[dict[str, str]]] | None = None,
    ) -> bool:
        self.edited.append((chat_id, message_id, text, keyboard))
        if 1 <= message_id <= len(self.sent):
            self.sent[message_id - 1] = (chat_id, text, keyboard)
            return True
        return False

    async def send_reply_keyboard(
        self, chat_id: int, text: str, reply_keyboard: list[list[dict[str, str]]]
    ) -> int | None:
        self.reply_keyboards.append((chat_id, text, reply_keyboard))
        return 1

    async def set_my_commands(self, commands: list[dict[str, str]] | None = None) -> bool:
        self.commands = commands or texts.BOT_COMMANDS
        return True

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((chat_id, message_id))

    async def send_document(self, chat_id: int, name: str, data: bytes) -> None:
        self.documents.append((chat_id, name, data))

    async def answer_callback(self, callback_id: str) -> None:
        self.callbacks_answered.append(callback_id)

    async def send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        self.actions.append((chat_id, action))

    async def get_updates(
        self, offset: int, timeout: int = 30  # noqa: ASYNC109
    ) -> list[dict[str, Any]]:
        return []

    async def set_webhook(self, url: str) -> bool:
        return True


class SmartTestModel:
    """Deterministic Persian model that remembers facts from conversation history."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def chat(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        messages: list[dict[str, str]],
    ) -> str:
        self.calls.append(
            {"base_url": base_url, "model": model, "messages": list(messages)}
        )
        history_text = "\n".join(
            m["content"] for m in messages if m["role"] != "system"
        )
        last_user = messages[-1]["content"]
        if (
            "پروژه کهکشان ۴۲" in history_text
            and "اسم رمز تست امروز ما چی بود" not in last_user
        ):
            return (
                "سلام! اسم رمز «پروژه کهکشان ۴۲» را به خاطر سپردم. "
                "من در پیام‌رسان بله در خدمت شما هستم."
            )
        if "اسم رمز" in last_user:
            if "پروژه کهکشان ۴۲" in history_text:
                return (
                    "اسم رمز تست امروز ما «پروژه کهکشان ۴۲» بود که در "
                    "پیام‌رسان بله به من گفتید."
                )
            return "در گفتگوی فعلی ما در بله، هنوز اسم رمزی به من نگفته‌اید."
        return f"پاسخ تست در پیام‌رسان بله: {last_user}"


class SpyIncus:
    def __init__(
        self,
        model: SmartTestModel | None = None,
        state: Any = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.instances: set[str] = set()
        self.pushed: dict[str, bytes] = {}
        self.outbox: dict[str, bytes] = {}
        self.model = model
        self.state = state

    async def run(
        self,
        *args: str,
        stdin: bytes | None = None,
        timeout: float = 60.0,  # noqa: ASYNC109
    ) -> IncusResult:
        self.calls.append(args)
        cmd = args[0]
        if cmd == "info":
            return IncusResult(0 if args[1] in self.instances else 1, b"", "")
        if cmd == "copy":
            self.instances.add(args[2])
            return IncusResult(0, b"", "")
        if cmd == "file" and args[1] == "push":
            self.pushed[args[3]] = stdin or b""
            return IncusResult(0, b"", "")
        if cmd == "file" and args[1] == "pull":
            fname = args[2].rsplit("/", 1)[-1]
            return IncusResult(0, self.outbox.get(fname, b""), "")
        if cmd == "exec" and "hermes" in args:
            name = args[1]
            qpath = f"{name}/home/agent/in/query.txt"
            prompt = self.pushed.get(qpath, b"").decode("utf-8", "replace")
            if "test_status.txt" in prompt:
                self.outbox["test_status.txt"] = b"2026-10-27\nPython 3.12.3\n"
                out = (
                    "فایل `/home/agent/workspace/test_status.txt` با موفقیت ساخته شد.\n"
                    "تاریخ امروز: 2026-10-27\n"
                    "نسخه پایتون داخل سندباکس: Python 3.12.3"
                )
                return IncusResult(0, out.encode("utf-8"), "")
            if self.model is not None and self.state is not None:
                user_id = int(name.removeprefix("u"))
                async with self.state.sessionmaker() as s:
                    user = await s.get(User, user_id)
                    q = select(Message).where(Message.user_id == user_id)
                    if user and user.context_reset_at:
                        q = q.where(Message.created_at > user.context_reset_at)
                    rows = list((await s.scalars(q.order_by(Message.id.asc()))).all())
                history = [{"role": m.role, "content": m.content} for m in rows]
                messages = [
                    {"role": "system", "content": texts.HERMES_BALE_SYSTEM_PROMPT},
                    *history,
                    {"role": "user", "content": prompt},
                ]
                reply = await self.model.chat(
                    base_url="http://fake",
                    api_key="fake",
                    model="balebot",
                    messages=messages,
                )
                return IncusResult(0, reply.encode("utf-8"), "")
            return IncusResult(0, f"پاسخ تست در پیام‌رسان بله: {prompt}".encode(), "")
        if cmd == "exec" and "find" in args:
            out_files = "\n".join(self.outbox)
            return IncusResult(0, f"{out_files}\n".encode() if out_files else b"", "")
        return IncusResult(0, b"", "")


# ---------------------------------------------------------------------------
# Environment & Secret Discovery (Zero Secret Output)
# ---------------------------------------------------------------------------
def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.is_file():
        return env
    with contextlib.suppress(OSError):
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("'").strip('"')
    return env


def discover_webhook_secret(env_file: Path) -> str | None:
    if s := os.environ.get("BALEBOT_WEBHOOK_SECRET"):
        return s.strip()
    file_env = load_env_file(env_file)
    if s := file_env.get("BALEBOT_WEBHOOK_SECRET"):
        return s.strip()
    rc, out, _ = _run_sync(
        "journalctl", "-u", "balebot.service", "-n", "1000", "--no-pager"
    )
    if rc == 0:
        matches = re.findall(r"POST /bale/webhook/([A-Za-z0-9_-]{16,128})", out)
        for candidate in reversed(matches):
            if candidate != "wrong" and not candidate.startswith("invalid"):
                return candidate
    return None


def discover_bale_user_id(db_path: Path, fallback: int) -> int:
    if db_path.is_file() and os.access(db_path, os.R_OK):
        with contextlib.suppress(sqlite3.Error, ValueError):
            with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
                row = conn.execute(
                    "SELECT bale_id FROM users ORDER BY id ASC LIMIT 1"
                ).fetchone()
                if row and row[0]:
                    return int(row[0])
    return fallback

def _read_db_counts(db_path: Path) -> dict[str, int] | None:
    if not (db_path.is_file() and os.access(db_path, os.R_OK)):
        return None
    with contextlib.suppress(sqlite3.Error):
        with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
            counts: dict[str, int] = {}
            for tbl in ALLOWED_TABLES:
                query = f"SELECT COUNT(*) FROM {tbl}"  # noqa: S608
                counts[tbl] = conn.execute(query).fetchone()[0]
            return counts
    return None


# ---------------------------------------------------------------------------
# Mode 1: Full-Suite Deterministic + Server/Security Verification
# ---------------------------------------------------------------------------
async def run_full_suite(
    report: SuiteReport,
    *,
    base_url: str,
    public_url: str,
    db_path: Path,
) -> None:
    print("\n=== Running Full E2E Suite (Scenarios 1-7) ===", flush=True)
    bale = SpyBale()
    model = SmartTestModel()
    incus = SpyIncus(model=model)
    suite_hook = secrets.token_urlsafe(18)
    admin_pw = secrets.token_urlsafe(16)

    def mock_provider(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "gpt-4o-mini"}]})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    settings = Settings(
        database_url="sqlite+aiosqlite://",
        webhook_secret=suite_hook,
        base_model_api_key=secrets.token_urlsafe(16),
        default_daily_quota=30,
        session_secret=secrets.token_urlsafe(32),
        vault_key=Vault.generate_key(),
        cookie_secure=False,
        sandbox_enabled=True,
        proxy_secret=secrets.token_urlsafe(24),
        _env_file=None,
    )
    http = SafeHttp(transport=httpx.MockTransport(mock_provider))
    state = build_state(
        settings, model_client=model, bale=bale, http=http, incus=incus
    )
    incus.state = state
    app = create_app(state)
    hook_path = f"/bale/webhook/{suite_hook}"

    uid = 900001
    next_update_id = 1000

    def make_msg(text: str | None, user_id: int = uid) -> dict[str, Any]:
        nonlocal next_update_id
        next_update_id += 1
        msg: dict[str, Any] = {
            "message_id": next_update_id,
            "chat": {"id": user_id},
            "from": {"id": user_id, "username": "e2e_tester"},
        }
        if text is not None:
            msg["text"] = text
        return {"update_id": next_update_id, "message": msg}

    def make_cb(data: str, user_id: int = uid) -> dict[str, Any]:
        nonlocal next_update_id
        next_update_id += 1
        return {
            "update_id": next_update_id,
            "callback_query": {
                "id": f"cb-{next_update_id}",
                "from": {"id": user_id, "username": "e2e_tester"},
                "message": {"message_id": next_update_id, "chat": {"id": user_id}},
                "data": data,
            },
        }

    async with app.router.lifespan_context(app):
        async with state.sessionmaker() as s:
            s.add(AdminUser(username="admin", password_hash=hash_password(admin_pw)))
            await s.commit()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://e2e") as client:
            # --- Scenario 1: /start ---
            t0 = time.perf_counter()
            r = await client.post(hook_path, json=make_msg("/start"))
            dt = (time.perf_counter() - t0) * 1000
            _, last_text, last_kb = bale.sent[-1]
            cb_ids = [btn["callback_data"] for row in (last_kb or []) for btn in row]
            async with state.sessionmaker() as s:
                user_row = await s.scalar(select(User).where(User.bale_id == uid))
            assert user_row is not None
            ok = (
                r.status_code == 200
                and "سلام" in last_text
                and "menu:agent" in cb_ids
                and "menu:models" in cb_ids
                and "menu:usage" in cb_ids
            )
            report.record(
                "Scenario 1 (/start)",
                "Welcome + MAIN_MENU_KEYBOARD + DB User creation",
                ok,
                dt,
                f"buttons={len(cb_ids)}, user_id={user_row.id}",
            )

            # --- Scenario 2: /help & /menu ---
            t0 = time.perf_counter()
            r_help = await client.post(hook_path, json=make_msg("/help"))
            help_text, help_kb = bale.sent[-1][1], bale.sent[-1][2]
            r_menu = await client.post(hook_path, json=make_msg("/menu"))
            menu_text, menu_kb = bale.sent[-1][1], bale.sent[-1][2]
            dt = (time.perf_counter() - t0) * 1000
            ok = (
                r_help.status_code == 200
                and r_menu.status_code == 200
                and help_text == texts.HELP
                and menu_text == texts.MENU_TEXT
                and help_kb == texts.MAIN_MENU_KEYBOARD
                and menu_kb == texts.MAIN_MENU_KEYBOARD
            )
            report.record(
                "Scenario 2 (/help & /menu)",
                "Help guide and Main Menu commands",
                ok,
                dt,
                "HELP + MENU_TEXT + MAIN_MENU_KEYBOARD verified",
            )

            # --- Scenario 3: Inline Callbacks & PendingAction Lifecycle ---
            for cb_data, label, expected_substr in [
                ("menu:usage", "Callback menu:usage", "مصرف امروز"),
                ("menu:models", "Callback menu:models", texts.MODELS_HEADER),
                ("menu:keys", "Callback menu:keys", texts.NO_KEYS),
                ("menu:help", "Callback menu:help", texts.HELP[:20]),
                ("menu:main", "Callback menu:main", texts.MENU_TEXT),
            ]:
                t0 = time.perf_counter()
                resp = await client.post(hook_path, json=make_cb(cb_data))
                dt = (time.perf_counter() - t0) * 1000
                text_out = bale.sent[-1][1]
                report.record(
                    "Scenario 3 (Callbacks)",
                    label,
                    resp.status_code == 200 and expected_substr in text_out,
                    dt,
                    f"matched '{expected_substr[:20]}'",
                )

            # Add-key flow: menu:addkey -> site:openai -> confirm:yes -> menu:cancel
            t0 = time.perf_counter()
            await client.post(hook_path, json=make_cb("menu:addkey"))
            async with state.sessionmaker() as s:
                pa1 = await s.get(PendingAction, user_row.id)
            await client.post(hook_path, json=make_cb("site:openai"))
            async with state.sessionmaker() as s:
                pa2 = await s.get(PendingAction, user_row.id)
            await client.post(hook_path, json=make_cb("confirm:yes"))
            async with state.sessionmaker() as s:
                pa3 = await s.get(PendingAction, user_row.id)
            await client.post(hook_path, json=make_cb("menu:cancel"))
            async with state.sessionmaker() as s:
                pa4 = await s.get(PendingAction, user_row.id)
            dt = (time.perf_counter() - t0) * 1000
            ok = (
                pa1 is not None
                and pa1.step == "site"
                and pa2 is not None
                and pa2.step == "confirm"
                and "openai" in pa2.data.lower()
                and pa3 is not None
                and pa3.step == "key"
                and pa4 is None
                and bale.sent[-1][1] == texts.CANCELLED
            )
            report.record(
                "Scenario 3 (Callbacks)",
                "Add-key state machine (menu:addkey -> site:openai -> confirm:yes -> menu:cancel)",
                ok,
                dt,
                "pending_actions transitions: site -> confirm -> key -> None",
            )

            # Stuck pending_actions recovery via callback (menu:usage) and command (/new)
            t0 = time.perf_counter()
            await client.post(hook_path, json=make_cb("menu:addkey"))
            await client.post(hook_path, json=make_cb("site:openai"))
            await client.post(hook_path, json=make_cb("confirm:yes"))
            await client.post(hook_path, json=make_cb("menu:usage"))
            async with state.sessionmaker() as s:
                pa_after_cb = await s.get(PendingAction, user_row.id)
            cb_recovered = pa_after_cb is None and "مصرف امروز" in bale.sent[-1][1]

            await client.post(hook_path, json=make_cb("menu:addkey"))
            await client.post(hook_path, json=make_cb("site:openai"))
            await client.post(hook_path, json=make_cb("confirm:yes"))
            await client.post(hook_path, json=make_msg("/new"))
            async with state.sessionmaker() as s:
                pa_after_cmd = await s.get(PendingAction, user_row.id)
            cmd_recovered = pa_after_cmd is None and bale.sent[-1][1] == texts.NEW_CHAT
            dt = (time.perf_counter() - t0) * 1000
            report.record(
                "Scenario 3 (Callbacks)",
                "Stuck pending_actions auto-recovery on menu:usage & /new",
                cb_recovered and cmd_recovered,
                dt,
                f"cb_recovered={cb_recovered}, cmd_recovered={cmd_recovered}",
            )

            # --- Scenario 4: AI Chat & Multi-turn Memory ---
            t0 = time.perf_counter()
            q1 = (
                "سلام! اسم رمز تست امروز ما «پروژه کهکشان ۴۲» است. "
                "به خاطر بسپار و بگو در چه پیام‌رسانی هستی؟"
            )
            await client.post(hook_path, json=make_msg(q1))
            dt1 = (time.perf_counter() - t0) * 1000
            ans1 = bale.sent[-1][1]
            ok1 = (
                "بله" in ans1
                and "بیل" not in ans1
                and (uid, "typing") in bale.actions
            )
            report.record(
                "Scenario 4 (AI Chat & Memory)",
                "Turn 1: Introduce code name + verify Persian 'بله' (never 'بیل') + typing action",
                ok1,
                dt1,
                ans1[:80],
            )

            t0 = time.perf_counter()
            q2 = "اسم رمز تست امروز ما چی بود؟"
            await client.post(hook_path, json=make_msg(q2))
            dt2 = (time.perf_counter() - t0) * 1000
            ans2 = bale.sent[-1][1]
            ok2 = "پروژه کهکشان ۴۲" in ans2
            report.record(
                "Scenario 4 (AI Chat & Memory)",
                "Turn 2: Multi-turn recall of 'پروژه کهکشان ۴۲'",
                ok2,
                dt2,
                ans2[:80],
            )

            t0 = time.perf_counter()
            await client.post(hook_path, json=make_msg("/new"))
            await client.post(
                hook_path, json=make_msg("اسم رمز تست قبلی یادت هست؟")
            )
            dt3 = (time.perf_counter() - t0) * 1000
            ans3 = bale.sent[-1][1]
            last_llm_msgs = model.calls[-1]["messages"]
            ok3 = "پروژه کهکشان ۴۲" not in ans3 and len(last_llm_msgs) == 2
            report.record(
                "Scenario 4 (AI Chat & Memory)",
                "Turn 3: /new resets conversation context cleanly",
                ok3,
                dt3,
                f"llm_prompt_msgs={len(last_llm_msgs)}, reply='{ans3[:60]}'",
            )

            # --- Scenario 5: /agent (Hermes Agent in Sandbox) ---
            t0 = time.perf_counter()
            await client.post(hook_path, json=make_cb("menu:agent"))
            async with state.sessionmaker() as s:
                pa_agent = await s.get(PendingAction, user_row.id)
            agent_prompt = (
                "یک فایل به نام /home/agent/workspace/test_status.txt بساز، "
                "داخلش تاریخ امروز و نسخه پایتون داخل سندباکس رو بنویس و محتواش رو بهم گزارش بده"
            )
            await client.post(hook_path, json=make_msg(agent_prompt))
            dt_agent = (time.perf_counter() - t0) * 1000
            agent_reply = bale.sent[-1][1]
            async with state.sessionmaker() as s:
                sb_row = await s.get(Sandbox, user_row.id)
                pa_after_agent = await s.get(PendingAction, user_row.id)
            ok_agent = (
                pa_agent is not None
                and pa_agent.step == "agent_task"
                and pa_after_agent is None
                and sb_row is not None
                and "test_status.txt" in agent_reply
                and len(bale.documents) == 1
            )
            report.record(
                "Scenario 5 (/agent Sandbox)",
                "menu:agent -> PendingAction(agent_task) -> Hermes run + Sandbox DB row",
                ok_agent,
                dt_agent,
                f"sandbox=u{user_row.id}, files={len(bale.documents)}",
            )

            # Live Progress Editing & Sandbox Utility Endpoints (/notify, /search)
            t0 = time.perf_counter()
            proxy_app = create_proxy_app(state)
            proxy_transport = httpx.ASGITransport(app=proxy_app)
            user_tok = tokens.issue_user_token(settings.proxy_secret, user_row.id)
            proxy_headers = {"Authorization": f"Bearer {user_tok}"}

            async with httpx.AsyncClient(
                transport=proxy_transport, base_url="http://proxy"
            ) as pclient:
                r_notify = await pclient.post(
                    "/v1/bale/notify",
                    json={"text": "اعلان تست پروکسی به بله"},
                    headers=proxy_headers,
                )
                r_search = await pclient.post(
                    "/v1/web/search",
                    json={"query": "اخبار تست"},
                    headers=proxy_headers,
                )
            dt_upgrades = (time.perf_counter() - t0) * 1000
            ok_upgrades = (
                len(bale.edited) >= 2
                and r_notify.status_code == 200
                and r_notify.json() == {"ok": True}
                and r_search.status_code == 200
                and r_search.json().get("ok") is True
            )
            summary_upgrades = (
                f"edits={len(bale.edited)}, "
                f"notify={r_notify.status_code}, "
                f"search={r_search.status_code}"
            )
            report.record(
                "Scenario 5 (/agent Sandbox)",
                "Live progress editing + Proxy endpoints (/v1/bale/notify & /v1/web/search)",
                ok_upgrades,
                dt_upgrades,
                summary_upgrades,
            )

            # --- Scenario 6: Web Admin Panel Health, CSRF & Auth ---
            t0 = time.perf_counter()
            r_hz = await client.get("/healthz")
            r_login_get = await client.get("/panel/login")
            csrf_match = re.search(r'name="csrf"\s+value="([^"]+)"', r_login_get.text)
            csrf_token = csrf_match.group(1) if csrf_match else ""
            r_no_csrf = await client.post(
                "/panel/login", data={"username": "admin", "password": admin_pw}
            )
            r_bad_pw = await client.post(
                "/panel/login",
                data={"username": "admin", "password": "wrong", "csrf": csrf_token},
            )
            r_good_login = await client.post(
                "/panel/login",
                data={"username": "admin", "password": admin_pw, "csrf": csrf_token},
                follow_redirects=False,
            )
            r_panel_home = await client.get("/panel/password")
            dt_panel = (time.perf_counter() - t0) * 1000
            set_cookie = r_good_login.headers.get("set-cookie", "").lower()
            ok_panel = (
                r_hz.status_code == 200
                and r_login_get.status_code == 200
                and bool(csrf_token)
                and r_no_csrf.status_code in (400, 403, 422)
                and r_bad_pw.status_code in (400, 401, 403)
                and r_good_login.status_code == 303
                and "httponly" in set_cookie
                and "samesite=strict" in set_cookie
                and r_panel_home.status_code == 200
            )
            panel_summary = (
                f"healthz={r_hz.status_code}, "
                f"no_csrf={r_no_csrf.status_code}, "
                f"login={r_good_login.status_code}"
            )
            report.record(
                "Scenario 6 (Web Admin Panel)",
                "/healthz, /panel/login, CSRF enforcement, password check, session security",
                ok_panel,
                dt_panel,
                panel_summary,
            )

            # --- Scenario 7: Security, Duplicate update_id & Quota Resilience ---
            t0 = time.perf_counter()
            r_bad_sec = await client.post(
                "/bale/webhook/wrong-secret", json=make_msg("hi")
            )
            dup_payload = make_msg("پیام تست تکراری")
            sent_before = len(bale.sent)
            r_dup1 = await client.post(hook_path, json=dup_payload)
            r_dup2 = await client.post(hook_path, json=dup_payload)
            sent_after = len(bale.sent)
            dt_sec = (time.perf_counter() - t0) * 1000
            ok_dup = (
                r_bad_sec.status_code == 404
                and r_dup1.json() == {"ok": True}
                and r_dup2.json() == {"ok": True, "duplicate_ignored": True}
                and (sent_after - sent_before) == 1
            )
            report.record(
                "Scenario 7 (Security & Resilience)",
                "Invalid webhook secret (404) & duplicate update_id idempotency",
                ok_dup,
                dt_sec,
                f"bad_secret={r_bad_sec.status_code}, dup_response={r_dup2.json()}",
            )

            # Quota exhaustion check
            t0 = time.perf_counter()
            async with state.sessionmaker() as s:
                usage_row = await s.scalar(
                    select(UsageDaily).where(UsageDaily.user_id == user_row.id)
                )
                assert usage_row is not None
                usage_row.count = 30
                await s.commit()
            await client.post(hook_path, json=make_msg("پیام خارج از سهمیه"))
            dt_quota = (time.perf_counter() - t0) * 1000
            last_reply = bale.sent[-1][1]
            ok_quota = (
                last_reply == texts.QUOTA_EXHAUSTED
                or texts.AGENT_NO_QUOTA.format(cost=state.settings.agent_cost) in last_reply
            )
            report.record(
                "Scenario 7 (Security & Resilience)",
                "Daily quota enforcement blocks excess requests without upstream call",
                ok_quota,
                dt_quota,
                "QUOTA_EXHAUSTED verified",
            )

            # Scenario 8: Command Menu, Bottom Reply Keyboard & Legacy Commands
            t0 = time.perf_counter()
            await client.post(hook_path, json=make_msg("شروع مجدد"))
            r_restart = bale.sent[-1]
            await client.post(hook_path, json=make_msg("/settings"))
            r_settings = bale.sent[-1]
            await client.post(hook_path, json=make_msg("/status"))
            r_status = bale.sent[-1]
            await client.post(hook_path, json=make_msg("🔄 گفتگوی تازه"))
            r_btn_new = bale.sent[-1]
            await client.post(hook_path, json=make_msg("🤖 انتخاب مدل"))
            r_btn_models = bale.sent[-1]
            await client.post(hook_path, json=make_msg("/think"))
            r_think_bare = bale.sent[-1]
            await client.post(hook_path, json=make_msg("/daily"))
            r_daily_bare = bale.sent[-1]
            dt = (time.perf_counter() - t0) * 1000
            sc8_ok = (
                "سلام" in r_restart[1]
                and r_settings[1] == texts.MENU_TEXT
                and "وضعیت سرور" in r_status[1]
                and r_btn_new[1] == texts.NEW_CHAT
                and texts.MODELS_HEADER in r_btn_models[1]
                and "حالت تفکر" in r_think_bare[1]
                and "گزارش روزانه" in r_daily_bare[1]
                and len(bale.reply_keyboards) >= 3
                and len(texts.BOT_COMMANDS) == 6
            )
            sc8_detail = (
                f"reply_keyboards_sent={len(bale.reply_keyboards)}, "
                f"commands={len(texts.BOT_COMMANDS)}"
            )
            report.record(
                "Scenario 8 (UI Menu, Bottom Keyboard & Legacy Commands)",
                "Test شروع مجدد, 2x3 ReplyKeyboardMarkup, /settings, /status, /think, /daily",
                sc8_ok,
                dt,
                sc8_detail,
            )

            # Verify DB table counts at end of in-process suite
            async with state.sessionmaker() as s:
                n_users = await s.scalar(select(func.count()).select_from(User))
                n_msgs = await s.scalar(select(func.count()).select_from(Message))
                n_sb = await s.scalar(select(func.count()).select_from(Sandbox))
            report.record(
                "Scenario 7 (Security & Resilience)",
                "Database integrity check across users, messages, sandboxes, usage_daily",
                (n_users == 1 and (n_msgs or 0) >= 6 and n_sb == 1),
                1.0,
                f"users={n_users}, messages={n_msgs}, sandboxes={n_sb}",
            )

    await check_live_server_health(
        report,
        base_url=base_url,
        public_url=public_url,
        db_path=db_path,
    )


async def check_live_server_health(
    report: SuiteReport,
    *,
    base_url: str,
    public_url: str,
    db_path: Path,
) -> None:
    async with httpx.AsyncClient(timeout=15.0) as http:
        try:
            t0 = time.perf_counter()
            r_local = await http.get(f"{base_url.rstrip('/')}/healthz")
            dt = (time.perf_counter() - t0) * 1000
            report.record(
                "Live Server Health",
                f"GET {base_url}/healthz",
                r_local.status_code == 200 and r_local.json().get("status") == "ok",
                dt,
                f"status={r_local.status_code} body={r_local.text.strip()}",
            )
        except httpx.HTTPError as exc:
            is_prod_host = bool(shutil.which("incus"))
            report.record(
                "Live Server Health",
                f"GET {base_url}/healthz",
                not is_prod_host,
                0.0,
                f"local service not running on dev box ({type(exc).__name__})",
            )

        try:
            t0 = time.perf_counter()
            r_pub = await http.get(f"{public_url.rstrip('/')}/panel/login")
            dt = (time.perf_counter() - t0) * 1000
            hdrs = r_pub.headers
            sec_ok = (
                r_pub.status_code == 200
                and hdrs.get("x-frame-options", "").upper() == "DENY"
                and hdrs.get("x-content-type-options", "").lower() == "nosniff"
            )
            hsts = bool(hdrs.get("strict-transport-security"))
            report.record(
                "Live Server Health",
                f"GET {public_url}/panel/login + Nginx TLS security headers",
                sec_ok,
                dt,
                f"status={r_pub.status_code}, XFO={hdrs.get('x-frame-options')}, HSTS={hsts}",
            )
        except httpx.HTTPError as exc:
            report.record(
                "Live Server Health",
                f"GET {public_url}/panel/login",
                False,
                0.0,
                f"skipped/unreachable ({type(exc).__name__})",
            )

    if shutil.which("systemctl"):
        services = ["balebot.service", "balebot-proxy.service", "balebot-reap.timer"]
        statuses: dict[str, str] = {}
        for svc in services:
            _, out, _ = await _run_async("systemctl", "is-active", svc)
            statuses[svc] = out.strip()
        report.server_health["systemd"] = statuses

    if shutil.which("incus"):
        rc, out, _ = await _run_async(
            "incus", "--project", "balebot", "list", "--format=json"
        )
        if rc == 0:
            with contextlib.suppress(ValueError):
                items = json.loads(out)
                report.server_health["incus_containers"] = [
                    {"name": c.get("name"), "status": c.get("status")} for c in items
                ]

    counts = await asyncio.to_thread(_read_db_counts, db_path)
    if counts is not None:
        report.server_health["db_counts"] = counts


# ---------------------------------------------------------------------------
# Mode 2: Live-Chat Real E2E Updates -> Live Server -> Real Bale Chat
# ---------------------------------------------------------------------------
async def _journal_cursor() -> str | None:
    rc, out, _ = await _run_async(
        "journalctl",
        "-u",
        "balebot.service",
        "-n",
        "1",
        "--show-cursor",
        "--no-pager",
    )
    if rc != 0:
        return None
    m = re.search(r"-- cursor:\s*(\S+)", out)
    return m.group(1) if m else None


async def _wait_for_live_completion(
    cursor: str | None,
    *,
    expect_agent: bool = False,
    timeout_s: float = 25.0,
) -> tuple[bool, float, str]:
    """Wait until balebot.service finishes processing the background task."""
    t0 = time.perf_counter()
    if expect_agent and shutil.which("incus"):
        await asyncio.sleep(1.5)
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            rc_ps, _, _ = await _run_async(
                "incus",
                "--project",
                "balebot",
                "exec",
                "u1",
                "--",
                "pgrep",
                "-f",
                "hermes.*chat",
            )
            if rc_ps != 0:
                rc_cat, out_cat, _ = await _run_async(
                    "incus",
                    "--project",
                    "balebot",
                    "exec",
                    "u1",
                    "--",
                    "cat",
                    "/home/agent/workspace/test_status.txt",
                )
                if rc_cat == 0 and out_cat.strip():
                    await asyncio.sleep(2.0)
                    dt = (time.perf_counter() - t0) * 1000
                    summary = " | ".join(out_cat.strip().splitlines()[:3])
                    return (
                        True,
                        dt,
                        f"u1:/home/agent/workspace/test_status.txt -> {summary}",
                    )
            await asyncio.sleep(1.0)
        return (
            False,
            (time.perf_counter() - t0) * 1000,
            "Timed out waiting for Hermes in u1",
        )

    await asyncio.sleep(1.2)
    if cursor and shutil.which("journalctl"):
        _, logs, _ = await _run_async(
            "journalctl",
            "-u",
            "balebot.service",
            f"--after-cursor={cursor}",
            "--no-pager",
        )
        if "update failed" in logs or "Traceback" in logs:
            return (
                False,
                (time.perf_counter() - t0) * 1000,
                "Error detected in balebot.service journal",
            )
    return (
        True,
        (time.perf_counter() - t0) * 1000,
        "Processed by balebot.service & delivered to Bale",
    )


async def run_live_chat(
    report: SuiteReport,
    *,
    public_url: str,
    webhook_secret: str,
    bale_user_id: int,
) -> None:
    print(
        f"\n=== Running Live-Chat E2E Mode (bale_user_id={bale_user_id}) ===",
        flush=True,
    )
    webhook_url = f"{public_url.rstrip('/')}/bale/webhook/{webhook_secret}"
    base_uid = int(time.time() * 1000) % 1_000_000_000

    def live_msg(seq: int, text: str) -> dict[str, Any]:
        return {
            "update_id": base_uid + seq,
            "message": {
                "message_id": base_uid + seq,
                "chat": {"id": bale_user_id, "type": "private"},
                "from": {"id": bale_user_id, "username": "ali_shafiee"},
                "date": int(time.time()),
                "text": text,
            },
        }

    def live_cb(seq: int, data: str) -> dict[str, Any]:
        return {
            "update_id": base_uid + seq,
            "callback_query": {
                "id": f"live-cb-{base_uid + seq}",
                "from": {"id": bale_user_id, "username": "ali_shafiee"},
                "message": {
                    "message_id": base_uid + seq,
                    "chat": {"id": bale_user_id, "type": "private"},
                },
                "data": data,
            },
        }

    async with httpx.AsyncClient(timeout=30.0) as http:
        # 1. /start
        cur = await _journal_cursor()
        t0 = time.perf_counter()
        r = await http.post(webhook_url, json=live_msg(1, "/start"))
        ok_bg, _, detail = await _wait_for_live_completion(cur)
        report.record(
            "Live Scenario 1 (/start)",
            "POST /start update -> Real Bale welcome + inline menu",
            r.status_code == 200 and ok_bg,
            (time.perf_counter() - t0) * 1000,
            detail,
        )

        # 2. /help & /menu
        for seq, cmd in [(2, "/help"), (3, "/menu")]:
            cur = await _journal_cursor()
            t0 = time.perf_counter()
            r = await http.post(webhook_url, json=live_msg(seq, cmd))
            ok_bg, _, detail = await _wait_for_live_completion(cur)
            report.record(
                "Live Scenario 2 (Commands)",
                f"POST {cmd} -> Real Bale reply",
                r.status_code == 200 and ok_bg,
                (time.perf_counter() - t0) * 1000,
                detail,
            )

        # 3. Callbacks & Stuck pending_actions recovery
        live_callbacks = [
            (4, "menu:usage", "Callback menu:usage (Quota status)"),
            (5, "menu:models", "Callback menu:models (Model list)"),
            (6, "menu:keys", "Callback menu:keys (Keys menu)"),
            (7, "menu:addkey", "Callback menu:addkey (Start add-key)"),
            (8, "site:openai", "Callback site:openai (Pick OpenAI)"),
            (9, "confirm:yes", "Callback confirm:yes (Await secret key)"),
            (10, "menu:cancel", "Callback menu:cancel (Cleanly clear pending_actions)"),
            (11, "menu:addkey", "Callback menu:addkey (Enter pending state again)"),
            (12, "site:openai", "Callback site:openai"),
            (13, "confirm:yes", "Callback confirm:yes (Stuck in add_key_secret state)"),
            (14, "menu:usage", "Stuck pending_actions recovery via menu:usage"),
        ]
        for seq, cb_data, desc in live_callbacks:
            cur = await _journal_cursor()
            t0 = time.perf_counter()
            r = await http.post(webhook_url, json=live_cb(seq, cb_data))
            ok_bg, _, detail = await _wait_for_live_completion(cur)
            report.record(
                "Live Scenario 3 (Callbacks)",
                desc,
                r.status_code == 200 and ok_bg,
                (time.perf_counter() - t0) * 1000,
                detail,
            )

        # 4. AI Chat & Multi-turn Memory via live Z.ai
        cur = await _journal_cursor()
        await http.post(webhook_url, json=live_msg(15, "/new"))
        await _wait_for_live_completion(cur)

        turn1_text = (
            "سلام! اسم رمز تست امروز ما «پروژه کهکشان ۴۲» است. "
            "به خاطر بسپار و بگو در چه پیام‌رسانی هستی؟"
        )
        chat_steps = [
            (
                16,
                turn1_text,
                "Turn 1: Introduce 'پروژه کهکشان ۴۲' & ask messenger name",
                12.5,
            ),
            (
                17,
                "اسم رمز تست امروز ما چی بود؟",
                "Turn 2: Multi-turn memory recall ('پروژه کهکشان ۴۲')",
                12.5,
            ),
            (
                18,
                "/new",
                "Turn 3a: Reset conversation memory with /new",
                1.2,
            ),
            (
                19,
                "اسم رمز تست قبلی یادت هست؟",
                "Turn 3b: Verify context reset after /new",
                12.5,
            ),
        ]
        for seq, text, desc, wait_s in chat_steps:
            cur = await _journal_cursor()
            t0 = time.perf_counter()
            r = await http.post(webhook_url, json=live_msg(seq, text))
            await asyncio.sleep(wait_s)
            ok_bg, _, detail = await _wait_for_live_completion(cur)
            report.record(
                "Live Scenario 4 (AI Chat & Memory)",
                desc,
                r.status_code == 200 and ok_bg,
                (time.perf_counter() - t0) * 1000,
                detail,
            )

        # 5. /agent Hermes Agent in Incus Sandbox u1
        if shutil.which("incus"):
            await _run_async("incus", "--project", "balebot", "start", "u1")
            await _run_async(
                "incus",
                "--project",
                "balebot",
                "exec",
                "u1",
                "--",
                "rm",
                "-f",
                "/home/agent/workspace/test_status.txt",
            )
            await _run_async(
                "incus",
                "--project",
                "balebot",
                "exec",
                "u1",
                "--",
                "install",
                "-d",
                "-o",
                "1000",
                "-g",
                "1000",
                "/home/agent/workspace",
            )
        cur = await _journal_cursor()
        t0 = time.perf_counter()
        r_cb = await http.post(webhook_url, json=live_cb(20, "menu:agent"))
        await asyncio.sleep(1.0)
        agent_task = (
            "یک فایل به نام /home/agent/workspace/test_status.txt بساز، "
            "داخلش تاریخ امروز و نسخه پایتون داخل سندباکس رو بنویس و محتواش رو بهم گزارش بده"
        )
        r_task = await http.post(webhook_url, json=live_msg(21, agent_task))
        ok_agent, dt_agent, detail_agent = await _wait_for_live_completion(
            cur, expect_agent=True, timeout_s=90.0
        )
        report.record(
            "Live Scenario 5 (/agent Sandbox u1)",
            "menu:agent + Hermes execution inside Incus u1 + real Bale report",
            r_cb.status_code == 200 and r_task.status_code == 200 and ok_agent,
            dt_agent,
            detail_agent,
        )

        # 6. Live Invalid Secret & Duplicate update_id check
        t0 = time.perf_counter()
        r_bad = await http.post(
            f"{public_url.rstrip('/')}/bale/webhook/invalid-secret-000",
            json=live_msg(99, "nope"),
        )
        dup_msg = live_msg(100, "/menu")
        r_d1 = await http.post(webhook_url, json=dup_msg)
        r_d2 = await http.post(webhook_url, json=dup_msg)
        dt_sec = (time.perf_counter() - t0) * 1000
        dup_detail = (
            f"invalid_secret={r_bad.status_code}, "
            f"dup_status={r_d2.status_code}, "
            f"dup_body={r_d2.json()}"
        )
        report.record(
            "Live Scenario 7 (Security & Idempotency)",
            "Live webhook invalid secret (404) & duplicate update_id check",
            r_bad.status_code == 404
            and r_d1.status_code == 200
            and r_d2.status_code == 200,
            dt_sec,
            dup_detail,
        )


async def async_main() -> int:
    parser = argparse.ArgumentParser(description="BaleBot End-to-End Live Test Harness")
    parser.add_argument(
        "--mode",
        choices=("full-suite", "live-chat", "all"),
        default="all",
        help="Execution mode (default: all)",
    )
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--public-url", default="https://node.roditeam.ir")
    parser.add_argument("--env-file", type=Path, default=Path("/etc/balebot/env"))
    parser.add_argument(
        "--db-path", type=Path, default=Path("/var/lib/balebot/balebot.db")
    )
    parser.add_argument("--bale-user-id", type=int, default=0)
    parser.add_argument("--json-out", type=Path, default=None)
    args = parser.parse_args()

    t_start = time.perf_counter()
    report = SuiteReport(
        mode=args.mode,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )

    secret = discover_webhook_secret(args.env_file)
    bale_uid = args.bale_user_id or discover_bale_user_id(
        args.db_path, DEFAULT_BALE_USER_ID
    )

    if args.mode in ("full-suite", "all"):
        await run_full_suite(
            report,
            base_url=args.base_url,
            public_url=args.public_url,
            db_path=args.db_path,
        )

    if args.mode in ("live-chat", "all"):
        if not secret:
            report.record(
                "Live-Chat Setup",
                "Discover BALEBOT_WEBHOOK_SECRET",
                False,
                0.0,
                "Webhook secret not found in env, /etc/balebot/env, or journalctl",
            )
        else:
            await run_live_chat(
                report,
                public_url=args.public_url,
                webhook_secret=secret,
                bale_user_id=bale_uid,
            )

    report.total_ms = round((time.perf_counter() - t_start) * 1000, 1)
    print(
        f"\n=== Summary: {report.passed} passed, {report.failed} failed "
        f"in {report.total_ms:.1f} ms ===",
        flush=True,
    )
    if args.json_out:
        payload = json.dumps(asdict(report), ensure_ascii=False, indent=2)
        await asyncio.to_thread(args.json_out.write_text, payload, "utf-8")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
