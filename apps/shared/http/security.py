"""Аутентификация запроса и авторизация.

AUTH_MODE:
- ``jwt`` — проверка JWT в этом процессе (monolith, 3-tier backend, gateway, функции);
- ``trusted-headers`` — внутренние сервисы за gateway: личность берётся из X-User-Id /
  X-User-Role, которые выставляет gateway после проверки JWT (протокол P-36: без вызова
  gateway → auth на каждый запрос). Внутренние сервисы доступны только в сети кластера;
  gateway удаляет эти заголовки из входящего клиентского запроса.
"""

from __future__ import annotations

from fastapi import Depends, Request

from apps.shared.auth import ROLES, AuthError, Principal, bearer_token, verify_token
from apps.shared.domain.errors import Forbidden

USER_ID_HEADER = "x-user-id"
USER_ROLE_HEADER = "x-user-role"


async def current_user(request: Request) -> Principal:
    settings = request.app.state.settings
    if settings.auth_mode == "trusted-headers":
        uid, role = request.headers.get(USER_ID_HEADER), request.headers.get(USER_ROLE_HEADER)
        if not uid or role not in ROLES:
            raise AuthError("нет данных пользователя от gateway")
        try:
            return Principal(user_id=int(uid), role=str(role))
        except ValueError as exc:
            raise AuthError("некорректный X-User-Id") from exc
    return verify_token(bearer_token(request.headers.get("authorization")), settings.jwt_secret)


async def moderator_user(user: Principal = Depends(current_user)) -> Principal:
    if not user.is_moderator:
        raise Forbidden("требуется роль moderator или admin")
    return user
