"""Автоматический сбор метаданных окружения (протокол P-02, P-04, P-13; reproducibility.md §2).

Значения не задаются вручную: всё читается из системы в момент запуска. Если сведение
недоступно (нет инструмента, нет прав), записывается null и причина в ``unavailable`` —
пропуск не маскируется правдоподобным значением.

    python -m experiments.meta [--rtt-host HOST --rtt-port PORT] [--out meta_host.json]
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import platform
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:  # на машине генератора файл запускается отдельно, без репозитория (--standalone)
    from experiments.config import REPO_ROOT
except ImportError:  # pragma: no cover
    REPO_ROOT = Path(__file__).resolve().parent

_TOOLS = {
    "k6": ["k6", "version"],
    "docker": ["docker", "version", "--format", "{{.Server.Version}}"],
    "docker_compose": ["docker", "compose", "version", "--short"],
    "kubectl": ["kubectl", "version", "--client", "-o", "json"],
    "k3s": ["k3s", "--version"],
    "faas_cli": ["faas-cli", "version", "--short-version"],
    "openfaas_helm": ["helm", "list", "-n", "openfaas", "-o", "json"],
    "git": ["git", "--version"],
}


def _run(cmd: list[str], cwd: Path | None = None, timeout: float = 20) -> tuple[str | None, str]:
    exe = shutil.which(cmd[0])
    if exe is None:
        return None, f"{cmd[0]} не найден в PATH"
    try:
        out = subprocess.run(
            [exe, *cmd[1:]], cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"{cmd[0]}: {exc}"
    if out.returncode != 0:
        return None, f"{cmd[0]}: код {out.returncode}: {out.stderr.strip()[:200]}"
    return out.stdout.strip(), ""


def cpu_info() -> dict[str, Any]:
    info: dict[str, Any] = {"logical_cpus": os.cpu_count(), "machine": platform.machine()}
    model = None
    cpuinfo = Path("/proc/cpuinfo")
    if cpuinfo.is_file():
        m = re.search(r"^model name\s*:\s*(.+)$", cpuinfo.read_text(), re.MULTILINE)
        model = m.group(1).strip() if m else None
        info["physical_cores"] = (
            len(set(re.findall(r"^core id\s*:\s*(\d+)$", cpuinfo.read_text(), re.MULTILINE)))
            or None
        )
    elif sys.platform == "win32":
        out, _ = _run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name",
            ]
        )
        model = out
    info["model"] = model or platform.processor() or None
    return info


def memory_total_bytes() -> int | None:
    meminfo = Path("/proc/meminfo")
    if meminfo.is_file():
        m = re.search(r"^MemTotal:\s+(\d+) kB", meminfo.read_text(), re.MULTILINE)
        return int(m.group(1)) * 1024 if m else None
    if sys.platform == "win32":

        class MemStatus(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        st = MemStatus()
        st.dwLength = ctypes.sizeof(MemStatus)
        # windll есть только на Windows: на Linux mypy считает атрибут неизвестным
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined,unused-ignore]
        if kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
            return int(st.ullTotalPhys)
    return None


def os_info() -> dict[str, Any]:
    info: dict[str, Any] = {
        "system": platform.system(),
        "release": platform.release(),
        "version": platform.version(),
        "platform": platform.platform(),
    }
    osr = Path("/etc/os-release")
    if osr.is_file():
        m = re.search(r'^PRETTY_NAME="?([^"\n]+)"?$', osr.read_text(), re.MULTILINE)
        info["distribution"] = m.group(1) if m else None
    return info


def network_interfaces() -> list[dict[str, Any]]:
    out, _ = _run(["ip", "-j", "addr"])
    if out:
        try:
            return [
                {
                    "name": i.get("ifname"),
                    "mtu": i.get("mtu"),
                    "addresses": [a.get("local") for a in i.get("addr_info", [])],
                }
                for i in json.loads(out)
            ]
        except json.JSONDecodeError:
            pass
    try:
        return [{"name": name, "index": idx} for idx, name in socket.if_nameindex()]
    except OSError:
        return []


def tcp_rtt(host: str, port: int, samples: int = 20, timeout: float = 3.0) -> dict[str, Any]:
    """RTT как время установления TCP-соединения (ICMP требует прав root)."""
    times: list[float] = []
    errors = 0
    for _ in range(samples):
        t0 = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout):
                times.append((time.perf_counter() - t0) * 1000)
        except OSError:
            errors += 1
        time.sleep(0.05)
    if not times:
        return {
            "host": host,
            "port": port,
            "samples": samples,
            "errors": errors,
            "method": "tcp_connect",
            "median_ms": None,
        }
    ts = sorted(times)
    return {
        "host": host,
        "port": port,
        "samples": samples,
        "errors": errors,
        "method": "tcp_connect",
        "min_ms": ts[0],
        "median_ms": statistics.median(ts),
        "p95_ms": ts[min(len(ts) - 1, int(0.95 * len(ts)))],
        "max_ms": ts[-1],
    }


def git_state(repo: Path = REPO_ROOT) -> dict[str, Any]:
    commit, err = _run(["git", "rev-parse", "HEAD"], cwd=repo)
    if commit is None:
        return {"commit": None, "dirty": None, "unavailable": err}
    status, _ = _run(["git", "status", "--porcelain"], cwd=repo)
    return {"commit": commit, "dirty": bool(status)}


def tool_versions() -> tuple[dict[str, str | None], dict[str, str]]:
    versions: dict[str, str | None] = {}
    missing: dict[str, str] = {}
    for name, cmd in _TOOLS.items():
        out, err = _run(cmd)
        versions[name] = out
        if out is None:
            missing[name] = err
    return versions, missing


def collect(rtt_host: str | None = None, rtt_port: int | None = None) -> dict[str, Any]:
    app_version: str | None
    try:
        from apps.shared import __version__ as app_version
    except ImportError:  # standalone на генераторе
        app_version = None

    versions, missing = tool_versions()
    meta: dict[str, Any] = {
        "collected_at_utc": datetime.now(UTC).isoformat(),
        "hostname": socket.gethostname(),
        "cpu": cpu_info(),
        "memory_total_bytes": memory_total_bytes(),
        "os": os_info(),
        "python": {
            "version": platform.python_version(),
            "implementation": platform.python_implementation(),
            "executable": sys.executable,
        },
        "tools": versions,
        "network_interfaces": network_interfaces(),
        "git": git_state(),
        "app_version": app_version,
        "unavailable": missing,
    }
    if rtt_host and rtt_port:
        meta["rtt"] = tcp_rtt(rtt_host, rtt_port)
    return meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="experiments.meta")
    ap.add_argument("--rtt-host")
    ap.add_argument("--rtt-port", type=int)
    ap.add_argument("--out")
    ap.add_argument(
        "--standalone", action="store_true", help="запуск вне репозитория (машина генератора)"
    )
    args = ap.parse_args(argv)
    text = json.dumps(collect(args.rtt_host, args.rtt_port), ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
