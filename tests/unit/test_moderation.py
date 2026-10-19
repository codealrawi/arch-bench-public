"""Перенесённый модератор должен принимать те же решения, что и исходный модуль medplatforma."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from apps.shared.moderation import ContentModerator

ORIGINAL = (
    Path(__file__).resolve().parents[3]
    / "medplatforma-svd-anomaly"
    / "medplatforma-main-pkg"
    / "services"
    / "moderation_service.py"
)

TEXTS = [
    "Как правильно принимать антибиотики при ангине?",
    "Врачи скрывают это простое средство от рака — лечитесь без врача!",
    "Гарантированное излечение диабета за 3 дня чудо-средством",
    "Купить таблетки со скидкой 70%! Звоните прямо сейчас",
    "Рекомендации по реабилитации после операции на позвоночнике",
    "Пациент жалуется на головную боль, врач назначил МРТ",
    "",
    "telegram http://example.org",
]


@pytest.fixture(scope="module")
def moderator() -> ContentModerator:
    return ContentModerator().train()


def test_untrained_moderator_refuses() -> None:
    with pytest.raises(AssertionError):
        ContentModerator().moderate("текст")


def test_labels_are_in_allowed_set(moderator: ContentModerator) -> None:
    for t in TEXTS:
        r = moderator.moderate(t)
        assert r.label in {"approved", "suspicious", "blocked"}
        assert r.level in (1, 2)
        assert 0.0 <= r.confidence <= 1.0


def test_deterministic(moderator: ContentModerator) -> None:
    other = ContentModerator().train()
    for t in TEXTS:
        assert moderator.moderate(t) == other.moderate(t)


@pytest.mark.skipif(not ORIGINAL.is_file(), reason="исходный модуль medplatforma недоступен")
def test_equivalent_to_original_module(moderator: ContentModerator) -> None:
    spec = importlib.util.spec_from_file_location("orig_moderation", ORIGINAL)
    assert spec is not None and spec.loader is not None
    orig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(orig)
    reference = orig.ContentModerator()
    reference.train()
    for t in TEXTS:
        a, b = moderator.moderate(t), reference.moderate(t)
        assert (a.label, a.level, a.confidence, a.scores) == (
            b.label,
            b.level,
            b.confidence,
            b.scores,
        )
