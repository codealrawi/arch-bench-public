"""Rate limiting эндпоинтов аутентификации (защита от перебора паролей).

Скользящее окно 60 с на пару (IP клиента, группа). Счётчики — в памяти процесса: при N
процессах фактический предел на узел ≤ N × лимит (документировано в docs/api.md). На операции
профиля нагрузки эксперимента ограничение не распространяется.
"""

from __future__ import annotations

import time
from collections import deque

from fastapi import HTTPException, Request

WINDOW_S = 60.0
_MAX_KEYS = 100_000


class SlidingWindowLimiter:
    def __init__(self, limit_per_minute: int):
        if limit_per_minute < 1:
            raise ValueError("лимит должен быть ≥ 1")
        self.limit = limit_per_minute
        self._hits: dict[str, deque[float]] = {}

    def allow(self, key: str, now: float | None = None) -> tuple[bool, float]:
        """(разрешено, через сколько секунд освободится слот)."""
        t = time.monotonic() if now is None else now
        q = self._hits.get(key)
        if q is None:
            if len(self._hits) >= _MAX_KEYS:  # защита памяти от перебора адресов
                self._hits.clear()
            q = self._hits[key] = deque()
        while q and t - q[0] >= WINDOW_S:
            q.popleft()
        if len(q) >= self.limit:
            return False, WINDOW_S - (t - q[0])
        q.append(t)
        return True, 0.0


def client_ip(request: Request) -> str:
    if request.app.state.settings.trust_proxy:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def auth_rate_limit(request: Request) -> None:
    limiter: SlidingWindowLimiter = request.app.state.auth_limiter
    ok, retry = limiter.allow(f"{client_ip(request)}:auth")
    if not ok:
        raise HTTPException(
            status_code=429,
            detail="слишком много попыток, повторите позже",
            headers={"Retry-After": str(max(1, int(retry) + 1))},
        )
