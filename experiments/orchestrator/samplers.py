"""Фоновые сэмплеры на время ступени: соединения PostgreSQL и число реплик (k8s).

CPU по контейнерам берётся из Prometheus/cAdvisor (scripts/export_metrics.py); здесь —
то, что Prometheus в базовой установке не даёт: pg_stat_activity и реплики во времени.
"""

from __future__ import annotations

import asyncio
import csv
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import asyncpg

PG_SQL = """
SELECT coalesce(state, 'unknown') AS state, count(*) AS n
FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()
GROUP BY 1
"""


class _Sampler(threading.Thread):
    def __init__(self, path: Path, step_s: float):
        super().__init__(daemon=True)
        self.path, self.step_s = path, step_s
        # не «_stop»: так называется внутренний метод threading.Thread, его вызывает join()
        self._halt = threading.Event()
        self.errors: list[str] = []

    def stop(self) -> None:
        self._halt.set()
        self.join(timeout=30)


class PgConnectionsSampler(_Sampler):
    def __init__(self, path: Path, dsn: str, step_s: float):
        super().__init__(path, step_s)
        self.dsn = dsn

    def run(self) -> None:
        asyncio.run(self._loop())

    async def _loop(self) -> None:
        with self.path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t", "state", "n"])
            conn: asyncpg.Connection | None = None
            try:
                while not self._halt.is_set():
                    t = time.time()
                    try:
                        if conn is None or conn.is_closed():
                            conn = await asyncpg.connect(self.dsn, timeout=5)
                        for r in await conn.fetch(PG_SQL):
                            w.writerow([f"{t:.3f}", r["state"], r["n"]])
                        f.flush()
                    except (asyncpg.PostgresError, asyncpg.InterfaceError, OSError) as exc:
                        # обрыв фиксируется как пропуск в данных (критерий покрытия
                        # invalid.py), сэмплер переподключается, а не умирает молча
                        self.errors.append(f"{t:.0f}: {type(exc).__name__}: {exc}")
                        if conn is not None:
                            conn.terminate()
                            conn = None
                    await asyncio.sleep(self.step_s)
            finally:
                if conn is not None and not conn.is_closed():
                    await conn.close()


class PodsSampler(_Sampler):
    """Желаемое/готовое число реплик всех Deployment в namespace (kubectl)."""

    def __init__(self, path: Path, namespaces: list[str], step_s: float):
        super().__init__(path, step_s)
        self.namespaces = namespaces

    def run(self) -> None:
        kubectl = shutil.which("kubectl")
        with self.path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t", "namespace", "deployment", "replicas", "ready_replicas"])
            if kubectl is None:
                self.errors.append("kubectl не найден")
                return
            while not self._halt.is_set():
                t = time.time()
                for ns in self.namespaces:
                    try:
                        out = subprocess.run(
                            [kubectl, "-n", ns, "get", "deploy", "-o", "json"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            check=True,
                        ).stdout
                        for item in json.loads(out)["items"]:
                            st = item.get("status", {})
                            w.writerow(
                                [
                                    f"{t:.3f}",
                                    ns,
                                    item["metadata"]["name"],
                                    st.get("replicas", 0),
                                    st.get("readyReplicas", 0),
                                ]
                            )
                    except (subprocess.SubprocessError, json.JSONDecodeError, KeyError) as exc:
                        self.errors.append(f"{t:.0f} {ns}: {exc}")
                f.flush()
                self._halt.wait(self.step_s)
