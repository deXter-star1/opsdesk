"""Domain errors. Services raise these; the HTTP layer turns them into a uniform JSON shape:

    {"error": {"code": "...", "message": "...", "details": {...}}}

Status code meaning (kept consistent so the frontend can react generically):
    401 unauthenticated      - no/expired session
    403 forbidden            - you can see the item but your role does not allow this action
    404 not_found            - does not exist OR you are not allowed to know it exists
    409 conflict             - the item's current state does not allow it / you acted on stale data
    422 validation_failed    - the request itself is malformed or incomplete
    503 busy                 - transient contention; safe to retry (same Idempotency-Key)
"""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    status = 500
    code = "internal_error"

    def __init__(self, message: str, *, code: str | None = None, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        if code:
            self.code = code
        self.details = details or {}

    def to_body(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


class Unauthorized(AppError):
    status = 401
    code = "unauthenticated"


class Forbidden(AppError):
    status = 403
    code = "forbidden"


class NotFound(AppError):
    status = 404
    code = "not_found"


class Conflict(AppError):
    status = 409
    code = "conflict"


class Unprocessable(AppError):
    status = 422
    code = "validation_failed"


class Busy(AppError):
    status = 503
    code = "busy"
