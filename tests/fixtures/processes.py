"""Запуск компонентов настоящими uvicorn-процессами (HTTP между ними).  Только для тестов."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx

from tests.conftest import REPO_ROOT


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@contextmanager
def uvicorn_process(
    factory: str, env: dict[str, str], health: str = "/readiness", workers: int = 1
) -> Iterator[str]:
    """``uvicorn <factory> --factory`` в отдельном процессе; отдаёт базовый URL."""
    port = free_port()
    full_env = {**os.environ, **env, "PYTHONPATH": str(REPO_ROOT)}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            factory,
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
            "--workers",
            str(workers),
        ],
        env=full_env,
        cwd=str(REPO_ROOT),
    )
    url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 90
        while True:
            if proc.poll() is not None:
                raise RuntimeError(f"{factory} завершился с кодом {proc.returncode}")
            try:
                if httpx.get(url + health, timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise TimeoutError(f"{factory} не поднялся за 90 с")
            time.sleep(0.2)
        yield url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
