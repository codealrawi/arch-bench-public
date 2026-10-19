"""Мини-эксперимент «холодный старт» функций OpenFaaS (протокол v2.0, §11).

    python -m experiments.coldstart --environment server --target-host 127.0.0.1 [--dry-run]
    (число повторений и пауза — bench.yaml: coldstart)

Основная серия исключает холодный старт дизайном (min ≥ 1 тёплая реплика, P-34); здесь он
измеряется отдельно. Для каждой функции и повторения:
  HPA снят → Deployment = 0 реплик → ожидание удаления подов → холодный запрос через шлюз →
  тёплый запрос → реплики = min, HPA восстановлен → пауза.
Неуспех холодного запроса (scale-from-zero недоступен в CE) записывается как результат.
Запросы — к /_/ready функции: время подъёма без нагрузки БД-операцией.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from experiments.config import RAW_DIR, load_bench_config
from experiments.orchestrator.shell import Shell
from infrastructure.common import FN_NAMESPACE
from infrastructure.openfaas import function_names, hpas

FIELDS = ["function", "repeat", "kind", "ok", "status", "latency_ms", "error", "t"]


def timed_get(url: str, timeout_s: float) -> dict[str, Any]:
    t0 = time.perf_counter()
    try:
        r = httpx.get(url, timeout=timeout_s)
        return {
            "ok": r.status_code == 200,
            "status": r.status_code,
            "latency_ms": (time.perf_counter() - t0) * 1000,
            "error": "",
        }
    except httpx.HTTPError as exc:
        return {
            "ok": False,
            "status": 0,
            "latency_ms": (time.perf_counter() - t0) * 1000,
            "error": type(exc).__name__,
        }


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Статистики по (функция, вид запроса) — только из измеренных строк."""
    out: dict[str, Any] = {}
    for fn in sorted({r["function"] for r in rows}):
        for kind in ("cold", "warm"):
            sel = [r for r in rows if r["function"] == fn and r["kind"] == kind]
            ok = sorted(float(r["latency_ms"]) for r in sel if r["ok"])
            key = f"{fn}.{kind}"
            if not ok:
                out[key] = {"n": len(sel), "n_ok": 0}
                continue
            p95 = ok[min(len(ok) - 1, int(round(0.95 * (len(ok) - 1))))]
            out[key] = {
                "n": len(sel),
                "n_ok": len(ok),
                "median_ms": statistics.median(ok),
                "p95_ms": p95,
                "min_ms": ok[0],
                "max_ms": ok[-1],
            }
    return out


def run(args: argparse.Namespace) -> Path:
    cfg = load_bench_config()
    sh = Shell(dry_run=args.dry_run)
    base = f"http://{args.target_host}:{cfg.network.openfaas_node_port}"
    hpa_by_fn = {h["metadata"]["name"]: h for h in hpas(cfg)}
    out_dir = Path(args.out) / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    repeats, pause_s = cfg.coldstart.repeats, cfg.coldstart.pause_s
    rows: list[dict[str, Any]] = []
    for fn in function_names(cfg):
        comp = cfg.architectures["serverless"].components[f"fn_{fn}"]
        for i in range(1, repeats + 1):
            if fn in hpa_by_fn:
                sh.run(["kubectl", "-n", FN_NAMESPACE, "delete", "hpa", fn, "--ignore-not-found"])
            sh.run(["kubectl", "-n", FN_NAMESPACE, "scale", f"deploy/{fn}", "--replicas=0"])
            sh.run(
                [
                    "kubectl",
                    "-n",
                    FN_NAMESPACE,
                    "wait",
                    "--for=delete",
                    "pod",
                    "-l",
                    f"faas_function={fn}",
                    "--timeout=300s",
                ],
                check=False,
                timeout=330,
            )
            url = f"{base}/function/{fn}/_/ready"
            for kind in ("cold", "warm"):
                res = (
                    {"ok": True, "status": 0, "latency_ms": 0.0, "error": "dry-run"}
                    if args.dry_run
                    else timed_get(url, cfg.load.timeout_s)
                )
                rows.append({"function": fn, "repeat": i, "kind": kind, "t": time.time(), **res})
            sh.run(
                ["kubectl", "-n", FN_NAMESPACE, "scale", f"deploy/{fn}", f"--replicas={comp.min}"]
            )
            if fn in hpa_by_fn:
                sh.run(["kubectl", "apply", "-f", "-"], stdin_text=json.dumps(hpa_by_fn[fn]))
            if not args.dry_run:
                time.sleep(pause_s)
    with (out_dir / "coldstart.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "environment": args.environment,
                "repeats": repeats,
                "pause_s": pause_s,
                "target": base,
                "dry_run": args.dry_run,
                "summary": summarize(rows),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return out_dir


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="experiments.coldstart")
    ap.add_argument("--target-host", required=True)
    ap.add_argument("--environment", choices=["local-validation", "server"], required=True)
    ap.add_argument("--out", default=str(RAW_DIR / "coldstart"))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    print(run(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
