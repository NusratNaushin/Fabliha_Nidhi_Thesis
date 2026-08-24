"""Pydantic schemas for local mappings, revisions and approvals."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from backend.app.constants import MapCorrelation, ReviewStatus


class MappingCreate(BaseModel):
    source_dataset: str = Field(
        description="MIMIC_III | BANGLADESH_HOSPITAL_A | MANUAL_TEST | ..."
    )
    local_code: str
    local_text: str
    target_system: str = Field(description="LOINC or SNOMED_CT")
    target_code: str
    target_display: str | None = None
    source_system: str | None = None
    local_context: dict[str, Any] | None = Field(
        default=None, description="e.g. {'fluid': 'Blood', 'category': 'Chemistry'}"
    )
    mapped_against_version: str | None = Field(
        default=None,
        description=(
            "the terminology release this mapping was originally made against; "
            "leave null when genuinely unknown rather than guessing"
        ),
    )
    map_correlation: MapCorrelation = MapCorrelation.NOT_SPECIFIED
    review_status: ReviewStatus = ReviewStatus.UNREVIEWED


class MappingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    source_dataset: str
    source_system: str | None = None
    local_code: str
    local_text: str
    local_context_json: dict[str, Any] | None = None
    target_system: str
    target_code: str
    target_display: str | None = None
    mapped_against_version: str | None = None
    map_correlation: str
    review_status: str
    created_at: datetime
    updated_at: datetime


class MappingRevisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    mapping_id: int
    old_target_code: str | None = None
    old_target_version: str | None = None
    new_target_code: str | None = None
    new_target_version: str | None = None
    reason: str | None = None
    audit_result_id: int | None = None
    approved: bool
    approved_by: str | None = None
    approved_at: datetime | None = None
    created_at: datetime


class MappingDetailOut(MappingOut):
    revisions: list[MappingRevisionOut] = Field(default_factory=list)


class ApproveReplacementIn(BaseModel):
    target_code: str
    reviewer: str = Field(
        description="who approved this change; approvals must be attributable"
    )
    reason: str | None = None
    audit_result_id: int | None = None
    allow_unsuggested: bool = Field(
        default=False,
        description=(
            "set true to record a manual decision the engine never suggested; "
            "the target must still be valid in the current release"
        ),
    )
