"""Проверка результатов эксперимента.

    python scripts/validate_results.py [--results results] [--out analysis/out]
                                       [--for-article] [--deep]

Код выхода 0 — все проверки пройдены, 1 — есть ошибки (перечислены).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from experiments.analysis.validate import validate  # noqa: E402
from experiments.config import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    RAW_DIR,
    RESULTS_DIR,
    load_bench_config,
)  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", default=str(RAW_DIR))
    ap.add_argument("--out", default=str(RESULTS_DIR))
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--for-article", action="store_true")
    ap.add_argument("--deep", action="store_true", help="пересчитать метрики из сырых данных")
    args = ap.parse_args(argv)
    rep = validate(
        Path(args.results),
        Path(args.out),
        load_bench_config(args.config),
        for_article=args.for_article,
        deep=args.deep,
    )
    for w in rep.warnings:
        print(f"WARN  {w}")
    for e in rep.errors:
        print(f"FAIL  {e}")
    print(
        f"{'PASS' if rep.ok else 'FAIL'}: {rep.checks} проверок, {len(rep.errors)} ошибок, "
        f"{len(rep.warnings)} предупреждений"
    )
    return 0 if rep.ok else 1


if __name__ == "__main__":
    sys.exit(main())
