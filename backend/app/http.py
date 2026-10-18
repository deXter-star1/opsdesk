"""Thin HTTP plumbing: every route is either a `query` (read snapshot) or a `command`
(one write transaction, optional idempotency key). Route handlers stay one-liners that call
services, so all the behaviour that matters is testable without HTTP.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Callable

from pydantic import BaseModel, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import db
from .errors import AppError, Busy, Forbidden, Unprocessable
from .services import idempotency
from .services.auth import SESSION_COOKIE, authenticate

log = logging.getLogger("opsdesk.http")

# Defence in depth against CSRF on cookie-authenticated mutations: browsers will not attach
# this custom header to cross-site form posts, and cross-origin fetch() with it would need a
# CORS preflight that this server never approves. (Cookies are also SameSite=Lax.)
CSRF_HEADER = "x-requested-with"


def error_response(exc: AppError) -> JSONResponse:
    headers = {"Retry-After": "1"} if isinstance(exc, Busy) else None
    return JSONResponse(exc.to_body(), status_code=exc.status, headers=headers)


async def app_error_handler(request: Request, exc: AppError):
    return error_response(exc)


async def unhandled_error_handler(request: Request, exc: Exception):
    if isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc):
        # Write lock not acquired within busy_timeout. Transient: the client retries with the
        # same Idempotency-Key, so a retry can never double-apply.
        return error_response(Busy("Server is busy, please retry"))
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"error": {"code": "internal_error", "message": "Unexpected server error",
                                   "details": {}}}, status_code=500)


async def parse_body(request: Request, model: type[BaseModel] | None) -> tuple[BaseModel | None, bytes]:
    raw = await request.body()
    if model is None:
        return None, raw
    try:
        data = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise Unprocessable("Request body must be valid JSON", code="invalid_json") from None
    try:
        return model.model_validate(data), raw
    except ValidationError as e:
        fields = [{"field": ".".join(str(p) for p in err["loc"]), "message": err["msg"]} for err in e.errors()]
        raise Unprocessable("Invalid request", details={"fields": fields}) from None


def query(handler: Callable) -> Callable:
    """handler(conn, user, request) -> JSON-able. Runs in a read snapshot."""
    async def endpoint(request: Request):
        def run():
            with db.read_tx() as conn:
                user = authenticate(conn, request.cookies.get(SESSION_COOKIE))
                return handler(conn, user, request)
        return JSONResponse(await run_in_threadpool(run))
    endpoint.__name__ = handler.__name__
    return endpoint


def command(body: type[BaseModel] | None = None, status_code: int = 200) -> Callable:
    """handler(conn, user, request, body) -> JSON-able. Runs in ONE write transaction.

    If the client sends Idempotency-Key, the key check, the mutation and the stored response
    are all part of that transaction (see services/idempotency.py)."""
    def deco(handler: Callable) -> Callable:
        async def endpoint(request: Request):
            if request.headers.get(CSRF_HEADER) is None:
                raise Forbidden("Missing X-Requested-With header", code="csrf_check_failed")
            parsed, raw = await parse_body(request, body)
            key = request.headers.get("idempotency-key")

            def run():
                with db.write_tx() as conn:
                    user = authenticate(conn, request.cookies.get(SESSION_COOKIE))
                    if key:
                        fp = idempotency.fingerprint(request.method, request.url.path, raw)
                        stored = idempotency.begin(conn, user.id, key, fp)
                        if stored is not None:
                            return stored["status"], stored["body"], True
                    result = handler(conn, user, request, parsed)
                    if key:
                        idempotency.complete(conn, user.id, key, status_code, result)
                    return status_code, result, False

            status, result, replayed = await run_in_threadpool(run)
            resp = JSONResponse(result, status_code=status)
            if replayed:
                resp.headers["Idempotent-Replayed"] = "true"
            return resp
        endpoint.__name__ = handler.__name__
        return endpoint
    return deco


def int_param(request: Request, name: str, default: int | None = None) -> int | None:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        raise Unprocessable(f"{name} must be an integer", code="bad_param") from None


def list_param(request: Request, name: str) -> list[str]:
    """Accepts ?status=a&status=b and ?status=a,b"""
    out: list[str] = []
    for v in request.query_params.getlist(name):
        out += [p for p in v.split(",") if p]
    return out
