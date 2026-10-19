"""Выполнение внешних команд с журналированием; режим dry-run для проверки без инфраструктуры."""

from __future__ import annotations

import logging
import shlex
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("arch-bench.shell")


class CommandError(RuntimeError):
    pass


@dataclass
class Shell:
    dry_run: bool = False
    history: list[list[str]] = field(default_factory=list)

    def run(
        self,
        cmd: Sequence[str],
        *,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
        timeout: float | None = None,
        capture: bool = True,
        stdin_text: str | None = None,
    ) -> str:
        """``stdin_text`` — для секретов (faas-cli --password-stdin): в журнал не пишется."""
        argv = [str(c) for c in cmd]
        self.history.append(argv)
        log.info("$ %s", shlex.join(argv))
        if self.dry_run:
            return ""
        proc = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env) if env is not None else None,
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=False,
            input=stdin_text,
        )
        if check and proc.returncode != 0:
            raise CommandError(
                f"{shlex.join(argv)} → код {proc.returncode}: " f"{(proc.stderr or '')[-2000:]}"
            )
        return proc.stdout or ""

    def popen(
        self,
        cmd: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        stdout: Path | None = None,
    ) -> subprocess.Popen[bytes] | None:
        argv = [str(c) for c in cmd]
        self.history.append(argv)
        log.info("$ %s &", shlex.join(argv))
        if self.dry_run:
            return None
        out = stdout.open("wb") if stdout else subprocess.DEVNULL
        return subprocess.Popen(
            argv, env=dict(env) if env is not None else None, stdout=out, stderr=subprocess.STDOUT
        )
