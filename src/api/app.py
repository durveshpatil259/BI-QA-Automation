"""FastAPI application factory.

Maps the existing typed exception hierarchy onto HTTP status codes in one place,
so routers stay free of try/except and every error response has the same shape.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.core.constants import APP_NAME, APP_TAGLINE
from src.core.exceptions import (
    BITestPilotError,
    DatasourceError,
    LLMConfigError,
    LLMProviderError,
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
    StorageError,
    ValidationError,
)
from src.core.logger import get_logger

_logger = get_logger()

#: Most specific first — the handler walks this in order.
_STATUS_MAP: list[tuple[type[Exception], int]] = [
    (ProjectNotFoundError, 404),
    (ProjectAlreadyExistsError, 409),
    (ValidationError, 400),
    (LLMConfigError, 400),
    (DatasourceError, 400),
    (LLMProviderError, 502),
    (StorageError, 500),
]


def create_app() -> FastAPI:
    app = FastAPI(
        title=APP_NAME,
        description=APP_TAGLINE,
        version="2.0.0",
    )

    # Single-user local tool: the SPA is served from the same origin, but a
    # dev frontend on another port should still work.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(BITestPilotError)
    async def handle_app_error(request: Request, exc: BITestPilotError):
        status = next(
            (code for kind, code in _STATUS_MAP if isinstance(exc, kind)), 500
        )
        if status >= 500:
            _logger.warning("%s -> %s: %s", request.url.path, status, exc)
        return JSONResponse(
            status_code=status,
            content={
                "error": type(exc).__name__,
                "detail": str(exc),
                "stage": None,
                "job_id": None,
            },
        )

    from src.api.routers import (adminviews, analysis, dashboard, projects, reports,
                             settings, testviews)

    app.include_router(projects.router)
    app.include_router(analysis.router)
    app.include_router(reports.router)
    app.include_router(settings.router)
    app.include_router(dashboard.router)
    app.include_router(testviews.router)
    app.include_router(adminviews.router)

    @app.get("/api/health", tags=["meta"])
    def health():
        from src.services.extractors.power_bi.pbixray_extractor import pbixray_available
        from src.services.reporting.pdf_renderer import pdf_engine_available

        return {
            "status": "ok",
            "app": APP_NAME,
            "version": "2.0.0",
            "pbixray": pbixray_available(),
            "pdf_engine": pdf_engine_available(),
        }

    # --- SPA -------------------------------------------------------------
    # Served from the same origin as the API, so no CORS in normal use.
    web_dir = Path(__file__).resolve().parents[2] / "web"
    if web_dir.is_dir():
        app.mount("/static", StaticFiles(directory=web_dir), name="static")

        # The stylesheet and script are cached by the browser under a fixed
        # URL, so an edit to either can keep showing the previous UI until a
        # hard reload — a change looks like it did not happen. Stamping the
        # link with the file's mtime gives each edit its own URL, so a normal
        # reload picks it up. The asset itself stays cacheable.
        @app.get("/", include_in_schema=False)
        def index():
            html = (web_dir / "index.html").read_text(encoding="utf-8")
            for asset in ("styles.css", "app.js"):
                path = web_dir / asset
                if path.exists():
                    html = html.replace(f"/static/{asset}",
                                        f"/static/{asset}?v={int(path.stat().st_mtime)}")
            return HTMLResponse(html)

    return app


app = create_app()
