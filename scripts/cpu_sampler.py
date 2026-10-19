"""Сэмплер загрузки CPU машины генератора нагрузки (критерий невалидности §12.2(б)).

    python cpu_sampler.py --out generator_cpu.csv --interval 1 --duration 400

Только стандартная библиотека: запускается на генераторе без установки зависимостей.
Linux — /proc/stat; Windows — GetSystemTimes.
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from pathlib import Path


def _times() -> tuple[float, float]:
    """(busy, total) в условных единицах с момента загрузки."""
    if sys.platform.startswith("linux"):
        parts = Path("/proc/stat").read_text().splitlines()[0].split()[1:]
        vals = [float(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0.0)  # idle + iowait
        total = sum(vals[:8])  # без guest (уже учтён в user)
        return total - idle, total
    if sys.platform == "win32":
        idle, kernel, user = (ctypes.c_ulonglong() for _ in range(3))
        ctypes.windll.kernel32.GetSystemTimes(  # type: ignore[attr-defined,unused-ignore]
            ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)
        )
        total = float(kernel.value + user.value)  # kernel включает idle
        return total - float(idle.value), total
    raise OSError(f"платформа {sys.platform} не поддерживается")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--interval", type=float, required=True)
    ap.add_argument("--duration", type=float, required=True)
    a = ap.parse_args(argv)
    end = time.time() + a.duration
    prev = _times()
    with open(a.out, "w", encoding="utf-8") as f:
        f.write("t,cpu_busy_share\n")
        while time.time() < end:
            time.sleep(a.interval)
            cur = _times()
            d_total = cur[1] - prev[1]
            share = (cur[0] - prev[0]) / d_total if d_total > 0 else 0.0
            f.write(f"{time.time():.3f},{share:.4f}\n")
            f.flush()
            prev = cur
    return 0


if __name__ == "__main__":
    sys.exit(main())
