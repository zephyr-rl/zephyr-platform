"""API process entrypoint: ``uvicorn app.main:app``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1 import (
    auth,
    combined,
    listings,
    messages,
    notifications,
    orders,
    reminders,
    reports,
    search,
    stock,
    stores,
    system,
)
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.core.platform import build_platform


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)
    platform = build_platform(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        await platform.db.dispose()

    app = FastAPI(
        title="Zephyr Platform",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.env != "production" else None,
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    app.state.platform = platform

    api = APIRouter(prefix="/api/v1")
    api.include_router(system.router)
    api.include_router(auth.router)
    api.include_router(stores.router)
    api.include_router(stock.router)
    api.include_router(listings.router)
    api.include_router(orders.router)
    api.include_router(messages.router)
    api.include_router(reports.router)
    api.include_router(reminders.router)
    api.include_router(search.router)
    api.include_router(combined.router)
    api.include_router(notifications.router)
    for router in platform.module_context.routers:
        api.include_router(router)
    app.include_router(api)

    if settings.static_dir:
        _mount_panel(app, Path(settings.static_dir))
    return app


def _mount_panel(app: FastAPI, root: Path) -> None:
    """Serve the built single-page panel; unknown paths fall back to index.html."""
    index = root / "index.html"
    app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:  # sync: file checks run in the threadpool
        candidate = (root / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(root.resolve()):
            return FileResponse(candidate)
        return FileResponse(index)


app = create_app()
