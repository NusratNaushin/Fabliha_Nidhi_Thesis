"""Pydantic schemas for audit runs and results."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditRunIn(BaseModel):
    source_dataset: str | None = Field(
        default=None, description="restrict the run to one dataset"
    )
    target_system: str | None = Field(
        default=None, description="LOINC or SNOMED_CT; null audits both"
    )
    limit: int | None = Field(default=None, ge=1, description="cap the mapping count")
    export_csv: bool = True
    report_name: str | None = None


class AuditRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    started_at: datetime
    completed_at: datetime | None = None
    loinc_version: str | None = None
    snomed_version: str | None = None
    mapping_count: int
    status: str
    summary_json: dict[str, Any] | None = None
    scope_json: dict[str, Any] | None = None
    report_path: str | None = None
    error_message: str | None = None


class AuditResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    audit_run_id: int
    mapping_id: int | None = None
    target_system: str
    old_code: str
    current_version: str | None = None
    terminology_status: str
    decision: str
    suggested_targets_json: list[dict[str, Any]] | None = None
    reason: str | None = None
    metadata_json: dict[str, Any] | None = None
    created_at: datetime


class AuditReportOut(BaseModel):
    run: AuditRunOut
    report_text: str
