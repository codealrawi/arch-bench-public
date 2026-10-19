"""Аутентификация (одинакова во всех архитектурах, протокол §5).

- access-токен: JWT HS256, claims sub/role/iat/exp/typ=access; проверка подписи и exp
  локально в первом компоненте, принявшем запрос (gateway проверяет сам, без вызова auth);
- refresh-токен: 256 бит случайности; в БД хранится только SHA-256; ротация при refresh;
- пароли: PBKDF2-SHA256 с солью; сравнение за постоянное время.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass

import jwt

JWT_ALGORITHM = "HS256"
_PBKDF2_PREFIX = "pbkdf2_sha256"
ROLES = ("patient", "doctor", "moderator", "admin")


class AuthError(Exception):
    """Токен или учётные данные отклонены."""


@dataclass(frozen=True)
class Principal:
    user_id: int
    role: str

    @property
    def is_moderator(self) -> bool:
        return self.role in ("moderator", "admin")


def hash_password(password: str, salt: bytes, iterations: int) -> str:
    """PBKDF2-SHA256; формат ``pbkdf2_sha256$<iter>$<salt b64>$<hash b64>``."""
    if iterations < 1:
        raise ValueError("iterations должно быть ≥ 1")
    if not salt:
        raise ValueError("соль не может быть пустой")
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return "$".join(
        (
            _PBKDF2_PREFIX,
            str(iterations),
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        )
    )


def new_password_hash(password: str, iterations: int) -> str:
    return hash_password(password, secrets.token_bytes(16), iterations)


def verify_password(password: str, encoded: str) -> bool:
    try:
        prefix, iter_s, salt_b64, hash_b64 = encoded.split("$")
        if prefix != _PBKDF2_PREFIX:
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
        iterations = int(iter_s)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return hmac.compare_digest(actual, expected)


def issue_token(principal: Principal, secret: str, ttl_s: int, now: float | None = None) -> str:
    if ttl_s <= 0:
        raise ValueError("ttl_s должен быть > 0")
    iat = int(time.time() if now is None else now)
    payload = {
        "sub": str(principal.user_id),
        "role": principal.role,
        "typ": "access",
        "iat": iat,
        "exp": iat + ttl_s,
    }
    return jwt.encode(payload, secret, algorithm=JWT_ALGORITHM)


def verify_token(token: str, secret: str) -> Principal:
    """Проверяет подпись, срок и тип токена; возвращает Principal или бросает AuthError."""
    try:
        payload = jwt.decode(
            token, secret, algorithms=[JWT_ALGORITHM], options={"require": ["exp", "iat", "sub"]}
        )
        if payload.get("typ", "access") != "access":
            raise AuthError("не access-токен")
        role = str(payload.get("role", ""))
        if role not in ROLES:
            raise AuthError("неизвестная роль")
        return Principal(user_id=int(payload["sub"]), role=role)
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise AuthError(str(exc)) from exc


def bearer_token(authorization_header: str | None) -> str:
    if not authorization_header:
        raise AuthError("нет заголовка Authorization")
    scheme, _, token = authorization_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise AuthError("ожидается схема Bearer")
    return token.strip()


def new_refresh_token() -> tuple[str, str]:
    """(токен для клиента, SHA-256 для хранения в БД)."""
    token = secrets.token_urlsafe(32)
    return token, refresh_token_hash(token)


def refresh_token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
