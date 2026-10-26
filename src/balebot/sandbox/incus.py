"""Thin async wrapper over the `incus` CLI.

Every call is an argument list handed to the process directly (never a shell string), and every
call is pinned to one restricted Incus project, so the bot can only ever touch its own sandboxes.
"""

import asyncio
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class IncusResult:
    rc: int
    out: bytes
    err: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


class IncusRunner(Protocol):
    async def run(
        self, *args: str, stdin: bytes | None = None, timeout: float = 60.0
    ) -> IncusResult: ...


class CliIncus:
    def __init__(self, binary: str, project: str) -> None:
        self._binary = binary
        self._project = project

    async def run(
        self, *args: str, stdin: bytes | None = None, timeout: float = 60.0
    ) -> IncusResult:
        proc = await asyncio.create_subprocess_exec(
            self._binary,
            "--project",
            self._project,
            *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return IncusResult(124, b"", "timeout")
        return IncusResult(proc.returncode or 0, out, err.decode("utf-8", "replace")[-2000:])
