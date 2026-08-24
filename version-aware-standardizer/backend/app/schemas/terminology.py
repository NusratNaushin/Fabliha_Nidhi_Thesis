"""Pydantic schemas for LOINC / SNOMED lookup and resolution responses."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class SuggestedTargetOut(BaseModel):
    code: str
    status: str | None = None
    display: str | None = None
    usable: bool = False
    via: list[str] = Field(
        default_factory=list,
        description="the MapTo chain walked to reach this candidate",
    )
    note: str | None = None


class LoincResolveOut(BaseModel):
    code: str
    system: str
    version: str | None = None
    status: str
    decision: str
    reason: str | None = None
    raw_status: str | None = None
    display: str | None = None
    suggested_targets: list[SuggestedTargetOut] = Field(default_factory=list)
    metadata_changed: bool | None = None
    metadata_diff: dict[str, dict[str, str | None]] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)


class LoincConceptOut(BaseModel):
    code: str
    version: str | None = None
    status: str | None = None
    long_common_name: str | None = None
    short_name: str | None = None
    component: str | None = None
    property: str | None = None
    time_aspect: str | None = None
    system: str | None = None
    scale_type: str | None = None
    method_type: str | None = None
    class_name: str | None = None
    change_type: str | None = None
    version_first_released: str | None = None
    version_last_changed: str | None = None
    map_to: list[dict[str, Any]] = Field(default_factory=list)


class AssociationOut(BaseModel):
    association_type: str
    refset_id: str
    target_component_id: str
    target_active: bool | None = None


class SnomedSuggestedTargetOut(BaseModel):
    concept_id: str
    active: bool | None = None
    display: str | None = None
    association_type: str | None = None
    usable: bool = False
    via: list[str] = Field(default_factory=list)
    note: str | None = None


class SnomedConceptOut(BaseModel):
    concept_id: str
    version: str | None = None
    active: bool | None = None
    effective_time: str | None = None
    module_id: str | None = None
    definition_status_id: str | None = None
    fsn: str | None = Field(
        default=None, description="fully specified name, from the parsed release"
    )
    preferred_term: str | None = Field(
        default=None,
        description=(
            "from the parsed language reference set; falls back to Snowstorm "
            "when descriptions were not imported"
        ),
    )
    language_refset_id: str | None = Field(
        default=None, description="which dialect supplied the preferred term"
    )
    display: str | None = Field(
        default=None, description="preferred term, else the fully specified name"
    )
    inactivation_value_id: str | None = None
    inactivation_reason: str | None = None
    historical_associations: list[AssociationOut] = Field(default_factory=list)


class SnomedResolveOut(BaseModel):
    concept_id: str
    system: str
    version: str | None = None
    status: str
    decision: str
    reason: str | None = None
    active: bool | None = None
    display: str | None = None
    inactivation_reason: str | None = None
    inactivation_value_id: str | None = None
    historical_associations: list[AssociationOut] = Field(default_factory=list)
    suggested_targets: list[SnomedSuggestedTargetOut] = Field(default_factory=list)
    details: dict[str, Any] = Field(default_factory=dict)


class SnomedSearchOut(BaseModel):
    """Search always runs active-only, and says so in the payload."""

    term: str
    active_only: bool = True
    branch: str
    items: list[dict[str, Any]] = Field(default_factory=list)
