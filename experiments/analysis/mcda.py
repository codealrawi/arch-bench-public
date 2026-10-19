"""Формула (1): Score(A_k) = Σ w_j r_jk (docs/evaluation-methodology.md).

Экспертные баллы и веса — INPUT. Пока они не получены (null в expert_scores.yaml),
Score не вычисляется; измеримые r_jk вычисляются с явной пометкой.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

CRITERIA = ("scalability", "fault_tolerance", "performance", "complexity", "cost", "security")
BENEFIT = {
    "scalability": True,
    "fault_tolerance": True,
    "performance": True,
    "complexity": False,
    "cost": False,
    "security": True,
}
EXPERT_CRITERIA = ("complexity", "security")


def normalize(values: dict[str, float | None], benefit: bool) -> dict[str, float | None]:
    """r = x/max (benefit) или min/x (cost). None — не определено (без подстановки чисел)."""
    defined = {k: v for k, v in values.items() if v is not None and math.isfinite(v)}
    if any(v < 0 for v in defined.values()):
        raise ValueError("x_jk не может быть отрицательным")
    out: dict[str, float | None] = {k: None for k in values}
    if not defined:
        return out
    if benefit:
        mx = max(defined.values())
        if mx == 0:
            return out
        for k, v in defined.items():
            out[k] = v / mx
    else:
        if any(v == 0 for v in defined.values()):
            return out  # min/x не определено при x = 0 (methodology §4)
        mn = min(defined.values())
        for k, v in defined.items():
            out[k] = mn / v
    return out


@dataclass(frozen=True)
class ExpertInput:
    weights: dict[str, float] | None
    weights_source: str
    scores: dict[str, dict[str, float | None]]  # criterion -> arch -> median score
    complete: bool
    missing: list[str]


def load_expert_input(path: Path, architectures: list[str]) -> ExpertInput:
    if not path.is_file():
        return ExpertInput(None, "missing", {}, False, [f"файл {path} не найден"])
    raw: Any = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    missing: list[str] = []
    weights = raw.get("weights")
    src = str(raw.get("weights_source", "unknown"))
    if not isinstance(weights, dict) or any(weights.get(c) is None for c in CRITERIA):
        missing.append("weights")
        weights = None
    elif not math.isclose(sum(float(weights[c]) for c in CRITERIA), 1.0, abs_tol=1e-6):
        raise ValueError("сумма весов должна быть 1")
    if src != "author_confirmed":
        missing.append(f"weights_source={src} (нужно author_confirmed)")
    scores: dict[str, dict[str, float | None]] = {}
    for c in EXPERT_CRITERIA:
        block = (raw.get("expert_scores") or {}).get(c) or {}
        scores[c] = {a: block.get(a) for a in architectures}
        if any(v is None for v in scores[c].values()):
            missing.append(f"expert_scores.{c}")
    w = {c: float(weights[c]) for c in CRITERIA} if weights else None
    return ExpertInput(w, src, scores, not missing, missing)


def score(
    r: dict[str, dict[str, float | None]], weights: dict[str, float], architectures: list[str]
) -> dict[str, float | None]:
    """Score только если все r_jk определены."""
    out: dict[str, float | None] = {}
    for a in architectures:
        total = 0.0
        for c in CRITERIA:
            v = r[c].get(a)
            if v is None:
                out[a] = None
                break
            total += weights[c] * v
        else:
            out[a] = total
    return out
