"""Per-user Incus sandboxes.

Each user gets at most one container (`u<user.id>`), cloned on first `/agent` use from the
pre-built `agent-base` image. Idle containers are stopped; old ones are destroyed.
"""

import asyncio
import re
from dataclasses import dataclass, field
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from balebot.config import Settings
from balebot.models import Sandbox, utcnow
from balebot.sandbox.incus import IncusRunner

AGENT_UID = "1000"
HOME = "/home/agent"
QUERY_PATH = f"{HOME}/in/query.txt"
OUTBOX = f"{HOME}/out"
MAX_OUTPUT = 12_000
MAX_FILES = 5
MAX_FILE_KB = 8_000
FILE_NAME = re.compile(r"[A-Za-z0-9._\- ]{1,80}")


class SandboxError(Exception):
    pass


@dataclass
class AgentResult:
    text: str
    ok: bool
    timed_out: bool = False
    files: list[tuple[str, bytes]] = field(default_factory=list)


def sandbox_name(user_id: int) -> str:
    return f"u{int(user_id)}"


class SandboxManager:
    def __init__(
        self, settings: Settings, incus: IncusRunner, sessionmaker: async_sessionmaker[AsyncSession]
    ) -> None:
        self._s = settings
        self._incus = incus
        self._sm = sessionmaker
        self._locks: dict[int, asyncio.Lock] = {}
        self._slots = asyncio.Semaphore(settings.agent_max_concurrent)

    def is_busy(self, user_id: int) -> bool:
        lock = self._locks.get(user_id)
        return bool(lock and lock.locked())

    # ---- lifecycle ---------------------------------------------------------------------
    async def ensure(self, user_id: int) -> str:
        name = sandbox_name(user_id)
        if not (await self._incus.run("info", name)).ok:
            await self._create(name)
            async with self._sm() as s:
                if await s.get(Sandbox, user_id) is None:
                    s.add(Sandbox(user_id=user_id))
                    await s.commit()
        started = await self._incus.run("start", name)
        if not started.ok and "already running" not in started.err.lower():
            raise SandboxError("could not start sandbox")
        for _ in range(20):
            if (
                await self._incus.run(
                    "exec",
                    name,
                    "--",
                    "sh",
                    "-c",
                    "ip -4 route show default | grep -q default",
                    timeout=10,
                )
            ).ok:
                return name
            await asyncio.sleep(0.5)
        raise SandboxError("sandbox did not become ready")

    async def _create(self, name: str) -> None:
        s = self._s
        copy = await self._incus.run(
            "copy",
            s.sandbox_image,
            name,
            "-c",
            f"limits.cpu={s.sandbox_cpu}",
            "-c",
            f"limits.memory={s.sandbox_memory}",
            "-c",
            f"limits.processes={s.sandbox_processes}",
            "-c",
            "security.privileged=false",
            "-c",
            "security.nesting=false",
            "-c",
            "boot.autostart=false",
            timeout=300,
        )
        if not copy.ok:
            raise SandboxError("could not create sandbox")
        disk = await self._incus.run(
            "config", "device", "override", name, "root", f"size={s.sandbox_disk}"
        )
        if not disk.ok:
            await self._incus.run("delete", name, "--force")
            raise SandboxError("could not apply the disk quota")  # never run without one

    async def destroy(self, user_id: int) -> None:
        await self._incus.run("delete", sandbox_name(user_id), "--force", timeout=120)
        async with self._sm() as s:
            row = await s.get(Sandbox, user_id)
            if row:
                await s.delete(row)
                await s.commit()

    async def reap(self) -> tuple[int, int]:
        """Stop idle sandboxes and destroy old ones. -> (stopped, destroyed)."""
        now = utcnow()
        idle = now - timedelta(minutes=self._s.sandbox_idle_minutes)
        old = now - timedelta(days=self._s.sandbox_destroy_days)
        stopped = destroyed = 0
        async with self._sm() as s:
            rows = (await s.scalars(select(Sandbox))).all()
        for row in rows:
            if self.is_busy(row.user_id):
                continue
            if row.last_used < old:
                await self.destroy(row.user_id)
                destroyed += 1
            elif row.last_used < idle:
                if (await self._incus.run("stop", sandbox_name(row.user_id), "--timeout", "10")).ok:
                    stopped += 1
        return stopped, destroyed

    # ---- running the agent ------------------------------------------------------------------
    async def run_agent(self, user_id: int, prompt: str, *, token: str) -> AgentResult:
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock, self._slots:
            name = await self.ensure(user_id)
            try:
                return await self._run(name, prompt, token)
            finally:
                async with self._sm() as s:
                    row = await s.get(Sandbox, user_id)
                    if row:
                        row.last_used = utcnow()
                        await s.commit()

    def _as_agent(self, name: str, *cmd: str) -> tuple[str, ...]:
        return (
            "exec", name, "--user", AGENT_UID, "--group", AGENT_UID, "--cwd", HOME,
            "--env", f"HOME={HOME}",
            "--env", f"PATH={HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "--", *cmd,
        )  # fmt: skip

    async def _run(self, name: str, prompt: str, token: str) -> AgentResult:
        s = self._s
        # the prompt travels as a file, never on a command line
        await self._incus.run(*self._as_agent(name, "rm", "-rf", OUTBOX, f"{HOME}/in"))
        await self._incus.run(*self._as_agent(name, "mkdir", "-p", OUTBOX, f"{HOME}/in"))
        pushed = await self._incus.run(
            "file", "push", "-", f"{name}{QUERY_PATH}",
            "--mode", "0600", "--uid", AGENT_UID, "--gid", AGENT_UID,
            stdin=prompt.encode(),
        )  # fmt: skip
        if not pushed.ok:
            raise SandboxError("could not hand the task to the sandbox")
        cmd = [
            "timeout", "-k", "5", str(s.agent_timeout),
            "hermes", "chat", "--yolo", "-Q", "--oneshot", "--query-file", QUERY_PATH,
            "--provider", "custom", "--base-url", s.proxy_url, "--api-key", token,
            "--model", "agent", "--max-turns", str(s.agent_max_turns),
            "--ignore-user-config",
        ]  # fmt: skip
        if s.agent_toolsets.strip():
            cmd += ["-t", s.agent_toolsets.strip()]
        res = await self._incus.run(*self._as_agent(name, *cmd), timeout=s.agent_timeout + 30)
        text = res.out.decode("utf-8", "replace").replace(token, "[redacted]").strip()
        timed_out = res.rc in (124, 137)
        if timed_out:
            await self._incus.run(*self._as_agent(name, "pkill", "-f", "hermes_cli"), timeout=5)
        files = await self._collect(name) if not timed_out else []
        return AgentResult(text[:MAX_OUTPUT], ok=res.ok, timed_out=timed_out, files=files)

    async def _collect(self, name: str) -> list[tuple[str, bytes]]:
        listing = await self._incus.run(
            *self._as_agent(
                name, "find", OUTBOX, "-maxdepth", "1", "-type", "f",
                "-size", f"-{MAX_FILE_KB}k", "-printf", "%f\n",
            )
        )  # fmt: skip
        files: list[tuple[str, bytes]] = []
        for fname in listing.out.decode("utf-8", "replace").splitlines()[:MAX_FILES]:
            if not FILE_NAME.fullmatch(fname) or fname.startswith("."):
                continue  # names come from the sandbox: never trust them
            pulled = await self._incus.run("file", "pull", f"{name}{OUTBOX}/{fname}", "-")
            if pulled.ok and len(pulled.out) <= MAX_FILE_KB * 1000:
                files.append((fname, pulled.out))
        return files
