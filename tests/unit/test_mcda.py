from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from experiments.analysis import mcda
from experiments.config import EXPERT_SCORES_PATH

ARCHS = ["monolith", "three_tier", "microservices", "serverless"]


def test_normalize_benefit_and_cost() -> None:
    assert mcda.normalize({"a": 2.0, "b": 4.0}, benefit=True) == {"a": 0.5, "b": 1.0}
    assert mcda.normalize({"a": 2.0, "b": 4.0}, benefit=False) == {"a": 1.0, "b": 0.5}


def test_normalize_undefined_cases() -> None:
    assert mcda.normalize({"a": None, "b": 4.0}, True) == {"a": None, "b": 1.0}
    assert mcda.normalize({"a": 0.0, "b": 0.0}, True) == {"a": None, "b": None}
    assert mcda.normalize({"a": 0.0, "b": 1.0}, False) == {"a": None, "b": None}
    with pytest.raises(ValueError):
        mcda.normalize({"a": -1.0}, True)


def test_score_requires_all_criteria() -> None:
    r = {c: {"x": 1.0, "y": 0.5} for c in mcda.CRITERIA}
    w = {c: 1 / 6 for c in mcda.CRITERIA}
    assert mcda.score(r, w, ["x", "y"]) == pytest.approx({"x": 1.0, "y": 0.5})
    r["security"]["y"] = None
    assert mcda.score(r, w, ["x", "y"])["y"] is None


def test_repository_expert_file_is_incomplete_and_blocks_score() -> None:
    inp = mcda.load_expert_input(EXPERT_SCORES_PATH, ARCHS)
    # экспертные данные ещё не получены (протокол P-59): Score не должен вычисляться
    assert not inp.complete
    assert "weights" in inp.missing


def test_complete_expert_file(tmp_path: Path) -> None:
    data = {
        "weights_source": "author_confirmed",
        "weights": dict(zip(mcda.CRITERIA, [0.25, 0.2, 0.2, 0.15, 0.1, 0.1], strict=True)),
        "expert_scores": {c: {a: 5 for a in ARCHS} for c in mcda.EXPERT_CRITERIA},
    }  # TEST DATA ONLY
    p = tmp_path / "e.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    inp = mcda.load_expert_input(p, ARCHS)
    assert inp.complete and inp.weights is not None


def test_weights_must_sum_to_one(tmp_path: Path) -> None:
    data = {
        "weights_source": "author_confirmed",
        "weights": {c: 0.5 for c in mcda.CRITERIA},
        "expert_scores": {},
    }
    p = tmp_path / "e.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(ValueError):
        mcda.load_expert_input(p, ARCHS)
