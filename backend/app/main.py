"""ASGI app.  Run:  uvicorn app.main:app --port 8000"""
from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import config
from .errors import AppError, NotFound
from .http import app_error_handler, unhandled_error_handler
from .migrate import migrate
from .realtime import broker
from .routes import api_routes

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)s %(name)s %(message)s")


@asynccontextmanager
async def lifespan(app):
    migrate()
    await broker.start()
    yield
    await broker.stop()


def _spa_routes() -> list:
    dist = Path(config.settings.static_dir)
    index = dist / "index.html"

    async def spa(request: Request):
        if request.url.path.startswith("/api/"):
            raise NotFound("No such API route")
        if not index.exists():
            return PlainTextResponse("Frontend not built. Run: cd frontend && npm install && npm run build",
                                     status_code=503)
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    routes = []
    if (dist / "assets").is_dir():
        routes.append(Mount("/assets", app=StaticFiles(directory=dist / "assets"), name="assets"))
    routes.append(Route("/{path:path}", spa, methods=["GET"]))
    return routes


async def _not_found(request: Request, exc):
    if request.url.path.startswith("/api/"):
        return JSONResponse(NotFound("Not found").to_body(), status_code=404)
    return PlainTextResponse("Not found", status_code=404)


def create_app() -> Starlette:
    return Starlette(
        routes=api_routes + _spa_routes(),
        lifespan=lifespan,
        exception_handlers={AppError: app_error_handler, Exception: unhandled_error_handler, 404: _not_found},
    )


app = create_app()
