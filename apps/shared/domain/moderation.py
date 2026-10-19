"""Модерация как зависимость контента: в процессе (A, B, D) или по HTTP (C, сервис moderation).

Алгоритм — ContentModerator (перенесён из medplatforma без изменений). Решение одинаково
при любом способе вызова: удалённый клиент получает тот же ModerationVerdict.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx

from apps.shared.domain.errors import UpstreamUnavailable
from apps.shared.moderation import ContentModerator


@dataclass(frozen=True)
class ModerationVerdict:
    label: str
    level: int
    confidence: float
    reasons: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def verdict_from(moderator: ContentModerator, text: str) -> ModerationVerdict:
    r = moderator.moderate(text)
    return ModerationVerdict(
        label=r.label, level=int(r.level), confidence=float(r.confidence), reasons=list(r.reasons)
    )


class ModerationClient(Protocol):
    async def moderate(self, text: str) -> ModerationVerdict: ...


class LocalModeration:
    """Модерация в том же процессе (синхронная, CPU-bound — как в протоколе)."""

    def __init__(self, moderator: ContentModerator | None = None):
        self.moderator = moderator or ContentModerator().train()

    async def moderate(self, text: str) -> ModerationVerdict:
        return verdict_from(self.moderator, text)


class RemoteModeration:
    """Вызов сервиса moderation (микросервисная конфигурация)."""

    def __init__(self, base_url: str, client: httpx.AsyncClient):
        self.url = base_url.rstrip("/") + "/api/v1/internal/moderate"
        self.client = client

    async def moderate(self, text: str) -> ModerationVerdict:
        try:
            r = await self.client.post(self.url, json={"text": text})
        except httpx.HTTPError as exc:
            raise UpstreamUnavailable(f"moderation недоступен: {type(exc).__name__}") from exc
        if r.status_code != 200:
            raise UpstreamUnavailable(f"moderation ответил {r.status_code}")
        d = r.json()
        return ModerationVerdict(
            label=str(d["label"]),
            level=int(d["level"]),
            confidence=float(d["confidence"]),
            reasons=[str(x) for x in d.get("reasons", [])],
        )
