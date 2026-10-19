"""Доменные ошибки → HTTP-статусы (apps/shared/http/errors.py)."""

from __future__ import annotations


class DomainError(Exception):
    status_code = 400
    code = "bad_request"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class ValidationFailed(DomainError):
    status_code = 422
    code = "validation_error"


class Unauthorized(DomainError):
    status_code = 401
    code = "unauthorized"


class Forbidden(DomainError):
    status_code = 403
    code = "forbidden"


class NotFound(DomainError):
    status_code = 404
    code = "not_found"


class Conflict(DomainError):
    status_code = 409
    code = "conflict"


class UpstreamUnavailable(DomainError):
    status_code = 503
    code = "upstream_unavailable"
