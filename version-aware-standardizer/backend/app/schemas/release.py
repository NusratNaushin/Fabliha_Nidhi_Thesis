"""Pydantic schemas for terminology releases."""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field


class ReleaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    system: str
    version: str
    effective_date: date | None = None
    sha256: str
    source_filename: str
    imported_at: datetime
    is_current: bool
    import_status: str
    notes: str | None = None


class CurrentReleaseInfo(BaseModel):
    version: str
    effective_date: str | None = None
    imported_at: str | None = None
    sha256: str | None = None
    source_filename: str | None = None
    import_status: str | None = None


class CurrentReleasesOut(BaseModel):
    """``GET /api/v1/releases/current``.

    A system with no imported release is reported as ``null`` rather than
    omitted, so a client can tell "not imported" from "not asked for".
    """

    LOINC: CurrentReleaseInfo | None = None
    SNOMED_CT: CurrentReleaseInfo | None = None


class HealthOut(BaseModel):
    status: str = Field(description="ok when the API and database are reachable")
    database: bool
    snowstorm: dict
    releases: dict
