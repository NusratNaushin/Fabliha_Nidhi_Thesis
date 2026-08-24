"""FastAPI application entry point.

    uvicorn backend.app.main:app --reload
    -> http://localhost:8000/docs
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from backend.app.api import (
    audits,
    loinc,
    mappings,
    releases,
    snomed,
    standardized_results,
)
from backend.app.config import settings
from backend.app.database import SessionLocal, engine
from backend.app.services import release_service
from backend.app.services.snowstorm_client import SnowstormClient
from backend.app.utils.logging import configure_logging, get_logger

log = get_logger("api")

DESCRIPTION = """
Version-aware LOINC / SNOMED CT terminology standardization core.

**What it does.** It validates *existing* clinical mappings against the
terminology release that is current right now: it detects LOINC status and
metadata changes, resolves official MapTo replacements, detects inactive
SNOMED CT concepts and their historical associations, and preserves complete
mapping provenance.

**What it does not do.** It does not predict LOINC or SNOMED codes from raw
clinical text. No ML, no LLM, no fuzzy matching -- only official terminology
fields and documented replacement relationships.

**Safety contract.** An audit may *suggest*; only
`POST /api/v1/mappings/{id}/approve-replacement` may commit. Ambiguous cases
always abstain to `MANUAL_REVIEW`.
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging(settings.log_level)
    log.info("starting %s", settings.app_name)
    log.info("database: %s", engine.url.render_as_string(hide_password=True))
    log.info("snowstorm: %s", settings.snowstorm_base_url)
    yield
    log.info("shutting down")


app = FastAPI(
    title=settings.app_name,
    description=DESCRIPTION,
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(releases.router)
app.include_router(loinc.router)
app.include_router(snomed.router)
app.include_router(mappings.router)
app.include_router(audits.router)
app.include_router(standardized_results.router)


@app.get("/health", tags=["system"], summary="Database, Snowstorm and release status")
def health() -> dict:
    """Reports every dependency explicitly rather than returning a bare 'ok'.

    Master Instruction 7: the backend must fail *clearly* when Snowstorm is
    unavailable -- so its state is always visible here.
    """
    database_ok = True
    database_detail: str | None = None
    releases_payload: dict = {}
    try:
        with SessionLocal() as session:
            session.execute(text("SELECT 1"))
            releases_payload = release_service.current_versions(session)
    except Exception as exc:  # noqa: BLE001 - reported, not hidden
        database_ok = False
        database_detail = f"{type(exc).__name__}: {exc}"
        log.error("health check: database unreachable: %s", database_detail)

    with SnowstormClient() as client:
        snowstorm = client.health().as_dict()

    return {
        "status": "ok" if database_ok else "degraded",
        "database": database_ok,
        "database_detail": database_detail,
        "snowstorm": snowstorm,
        "releases": releases_payload,
    }


@app.get("/api", tags=["system"], include_in_schema=False)
def api_root() -> dict:
    return {
        "name": settings.app_name,
        "console": "/ui/",
        "docs": "/docs",
        "health": "/health",
        "api": "/api/v1",
    }


@app.get("/", tags=["system"], include_in_schema=False)
def root() -> RedirectResponse:
    """Land on the console rather than a JSON blob."""
    return RedirectResponse(url="/ui/")


# The console is plain HTML/CSS/JS served from this app: no CDN, no build step,
# so it works on a machine with the network cable out -- the same reason nothing
# else in this project phones home.
_STATIC_DIR = Path(__file__).resolve().parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="console")
else:  # pragma: no cover - only if the package was installed without its assets
    log.warning("console assets missing at %s; /ui will 404", _STATIC_DIR)
