"""Метаданные собираются из системы, а не задаются; отсутствующее явно помечается."""

from __future__ import annotations

import os
import platform
import socket
import threading

from experiments import meta


def test_collect_reads_real_system_values() -> None:
    m = meta.collect()
    assert m["python"]["version"] == platform.python_version()
    assert m["cpu"]["logical_cpus"] == os.cpu_count()
    assert m["hostname"] == socket.gethostname()
    assert m["memory_total_bytes"] is None or m["memory_total_bytes"] > 0
    # каждый инструмент либо имеет версию, либо причину отсутствия
    for tool, version in m["tools"].items():
        assert (version is not None) or (tool in m["unavailable"])


def test_git_missing_is_explicit() -> None:
    g = meta.git_state()
    assert set(g) >= {"commit", "dirty"}
    if g["commit"] is None:
        assert g["unavailable"]


def test_tcp_rtt_against_local_server() -> None:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)
    port = srv.getsockname()[1]
    stop = threading.Event()

    def accept() -> None:
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                c, _ = srv.accept()
                c.close()
            except OSError:
                pass

    t = threading.Thread(target=accept, daemon=True)
    t.start()
    try:
        r = meta.tcp_rtt("127.0.0.1", port, samples=5)
    finally:
        stop.set()
        t.join()
        srv.close()
    assert r["errors"] == 0 and r["samples"] == 5
    assert 0 <= r["min_ms"] <= r["median_ms"] <= r["max_ms"]


def test_tcp_rtt_unreachable_reports_none() -> None:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # порт закрыт
    r = meta.tcp_rtt("127.0.0.1", port, samples=2, timeout=0.5)
    assert r["median_ms"] is None and r["errors"] == 2
