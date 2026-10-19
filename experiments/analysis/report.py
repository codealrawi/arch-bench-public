"""Автоматический отчёт analysis/out/report.md — только числа из таблиц этого запуска."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from experiments.analysis.tables import ARCH_RU, to_markdown
from experiments.config import BenchConfig

NOT_SCIENTIFIC = "PIPELINE CHECK — NOT A SCIENTIFIC RESULT"


def write_report(
    out: Path,
    cfg: BenchConfig,
    meta: dict[str, Any],
    t: dict[str, pd.DataFrame],
    run_level: pd.DataFrame,
    agg: pd.DataFrame,
) -> Path:
    lines: list[str] = []
    if not meta["scientific"]:
        lines += [
            f"# {NOT_SCIENTIFIC}",
            "",
            f"Окружения прогонов: {meta['environments']}. Научными считаются только прогоны "
            "`environment=server` (протокол §2). Числа ниже нельзя использовать в статье.",
            "",
        ]
    lines += [
        "# Отчёт эксперимента arch-bench",
        "",
        f"- Сгенерирован: {meta['generated_at_utc']}",
        f"- Протокол: v{meta['protocol_version']}; SHA-256 конфигурации: "
        f"`{meta['config_sha256']}`",
        f"- SLA_MS = {meta['sla_ms']:g} мс по {meta['sla_metric']} "
        f"(AUTHOR-defined experimental threshold; {meta['sla_source']})",
        f"- Профиль: {cfg.load.mix}; Z = {cfg.load.think_time_s} с; ступень: разгон "
        f"{cfg.load.ramp_s} с + прогрев {cfg.load.warmup_s} с + измерение {cfg.load.measure_s} с",
        f"- Данные: {cfg.data.users} пользователей, {cfg.data.posts} записей, seed "
        f"{cfg.data.seed}; сброс БД перед каждой ступенью",
        f"- Python анализа: {meta['python']}",
        "",
    ]
    lines += ["## Прогоны", ""]
    counts = run_level.groupby("architecture")["run"].nunique().to_dict()
    for a, n in counts.items():
        lines.append(f"- {ARCH_RU.get(a, a)}: {n} валидных прогонов")
    lines.append(f"- Невалидные прогоны: {len(meta['invalid_runs'])}")
    for r in meta["invalid_runs"]:
        lines.append(f"  - {r['architecture']}/{r['run_dir']}: {r['reason'][:200]}")
    lines.append(f"- Исключённые валидные прогоны: {len(meta['excluded_runs'])}")
    for r in meta["excluded_runs"]:
        lines.append(f"  - {r['architecture']}/{r['run']}: {r['reason']}")
    failed = run_level[run_level["status"] == "failed"]
    lines += ["", f"- Ступеней со статусом failed: {len(failed)}", ""]
    for _, r in failed.iterrows():
        lines.append(f"  - {r['architecture']}/{r['run']}/{int(r['vus'])} VU: {r['failed_reason']}")

    titles = {
        "table1_rt_avg": "Таблица 1 — RT_avg, мс: mean ± SD [95 % бутстреп-ДИ] по прогонам",
        "table2_summary": "Таблица 2 — сводные показатели",
        "table_rt_p95": "RT_p95, мс",
        "table_rps": "Пропускная способность, запросов/с",
        "littles_law": "Проверка закона Литтла (X_pred = N/(RT_avg+Z))",
        "saturation_fit": "Модель (2): T(R) = T0·(1 + (R/Rsat)^α)",
        "scaling_efficiency": "Формула (3): коэффициент масштабирования E",
        "asymptotic_check": "Формула (4): диагностика после насыщения",
        "mann_whitney_holm": "Манна–Уитни по средним прогонов, поправка Холма внутри ступени, "
        "δ Клиффа",
        "resources_cpu": "CPU-секунды за окно измерения по группам",
        "table3_mcda": "Таблица 3 — формула (1)",
    }
    for key, title in titles.items():
        df = t.get(key)
        if df is None:
            continue
        lines += ["", f"## {title}", "", to_markdown(df)]
        notes = df.attrs.get("notes") if hasattr(df, "attrs") else None
        if notes:
            lines += [f"> {n}" for n in notes]
    if meta["table3_notes"]:
        lines += ["", *[f"> {n}" for n in meta["table3_notes"]]]
    if meta["littles_warnings"]:
        lines += [
            "",
            f"> Предупреждения закона Литтла: {meta['littles_warnings']} (отклонение > "
            f"{cfg.statistics.littles_law_tolerance:.0%}).",
        ]
    lines += ["", "## Рисунки", ""] + [f"- `figures/{f}`" for f in meta["figures"]]
    p = out / "report.md"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p
