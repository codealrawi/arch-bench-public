"""Единая точка входа анализа.

    python -m experiments.analysis.run_analysis --results results --out analysis/out

SLA_MS обязателен (env или config): без него анализ завершается ошибкой протокола P-48.
Все выходные числа вычисляются из results/ при каждом запуске.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import platform
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from experiments.analysis import aggregate, collect, figures, post_series, tables
from experiments.config import (
    DEFAULT_CONFIG_PATH,
    EXPERT_SCORES_PATH,
    RAW_DIR,
    RESULTS_DIR,
    ConfigError,
    SlaMissingError,
    config_sha256,
    load_bench_config,
    resolve_sla_ms,
)

log = logging.getLogger("arch-bench.analysis")
NOT_SCIENTIFIC = "PIPELINE CHECK — NOT A SCIENTIFIC RESULT"


def sha256_file(path: Path) -> str:
    """Хеш байтов файла: рисунки и meta привязываются к конкретному aggregates.csv."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(results: Path, out: Path, config_path: Path, expert_path: Path) -> dict[str, Any]:
    cfg = load_bench_config(config_path)
    sla_ms = resolve_sla_ms(cfg)
    run_level, inv = collect.collect(results, cfg)
    if run_level.empty:
        raise collect.ResultsError(f"в {results} нет ни одного валидного прогона")
    st = cfg.statistics
    agg = aggregate.aggregate(run_level, st.ci_level, st.bootstrap_b, st.bootstrap_seed)
    out.mkdir(parents=True, exist_ok=True)
    tdir = out / "tables"
    run_level.to_csv(out / "run_level.csv", index=False, encoding="utf-8")
    agg.to_csv(out / "aggregates.csv", index=False, encoding="utf-8")
    agg_sha = sha256_file(out / "aggregates.csv")

    t = {
        "table1_rt_avg": tables.table1(agg),
        "table2_summary": tables.table2(agg, cfg, sla_ms),
        "littles_law": tables.littles_table(agg, cfg),
        "saturation_fit": tables.saturation_table(agg, run_level),
        "scaling_efficiency": tables.scaling_table(agg),
        "asymptotic_check": tables.asymptotic_table(agg, cfg),
        "mann_whitney_holm": tables.stats_table(run_level, cfg),
        "resources_cpu": tables.resources_table(agg, cfg),
        "table_rt_p95": tables._pivot(agg, "rt_p95"),
        "table_rps": tables._pivot(agg, "rps"),
    }
    t3, t3_notes = tables.table3(agg, cfg, sla_ms, expert_path)
    t["table3_mcda"] = t3
    for name, df in t.items():
        tables.write_table(df, tdir, name)
    figs = figures.make_figures(agg, out / "figures", agg_sha)

    environments = sorted(inv.environments)
    # анализ по плану серии 2 (docs/post-series-analysis-plan.md)
    ps_meta = post_series.run(
        run_level, cfg, out / "post_series", agg_sha, scientific=environments == ["server"]
    )
    meta = {
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "protocol_version": cfg.protocol_version,
        "config_sha256": config_sha256(config_path),
        "sla_ms": sla_ms,
        "sla_metric": cfg.sla.sla_metric,
        "sla_source": cfg.sla.source,
        "environments": environments,
        "scientific": environments == ["server"],
        "aggregates_sha256": agg_sha,
        "n_run_level_rows": int(len(run_level)),
        "runs": inv.runs,
        "invalid_runs": inv.invalid,
        "excluded_runs": inv.excluded,
        "python": platform.python_version(),
        "littles_warnings": (
            int(t["littles_law"]["warning"].sum()) if not t["littles_law"].empty else 0
        ),
        "table3_notes": t3_notes,
        "figures": [p.name for p in figs],
        "post_series": ps_meta,
    }
    (out / "analysis_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    from experiments.analysis.report import write_report

    write_report(out, cfg, meta, t, run_level, agg)
    return meta


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ap = argparse.ArgumentParser(prog="experiments.analysis.run_analysis")
    ap.add_argument("--results", default=str(RAW_DIR))
    ap.add_argument("--out", default=str(RESULTS_DIR))
    ap.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    ap.add_argument("--experts", default=str(EXPERT_SCORES_PATH))
    args = ap.parse_args(argv)
    try:
        meta = run(Path(args.results), Path(args.out), Path(args.config), Path(args.experts))
    except SlaMissingError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (ConfigError, collect.ResultsError) as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        return 1
    if not meta["scientific"]:
        print(f"ВНИМАНИЕ: {NOT_SCIENTIFIC} (environments={meta['environments']})")
    print(f"готово: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
