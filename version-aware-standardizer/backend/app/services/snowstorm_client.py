"""HTTP client for the official Snowstorm terminology server.

Snowstorm is *infrastructure only* -- its source is never modified and the
audit logic never depends on it (see snomed_rf2_parser).  What it gives us that
the RF2 files do not is fast term search and preferred display terms, plus the
standard FHIR terminology endpoints.

Search always defaults to ``activeFilter=true`` and ``termActive=true`` so an
inactive concept can never be offered as a *new* mapping candidate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from backend.app.config import settings
from backend.app.constants import HISTORICAL_ASSOCIATION_REFSETS
from backend.app.utils.logging import get_logger

log = get_logger("snowstorm")

SNOMED_SYSTEM_URI = "http://snomed.info/sct"
LOINC_SYSTEM_URI = "http://loinc.org"


class SnowstormError(RuntimeError):
    """Raised when Snowstorm is unreachable or answers with an error status."""


class SnowstormUnavailable(SnowstormError):
    """Raised specifically when the server cannot be contacted at all."""


@dataclass
class SnowstormHealth:
    available: bool
    base_url: str
    version: str | None = None
    branch: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict:
        return {
            "available": self.available,
            "base_url": self.base_url,
            "version": self.version,
            "branch": self.branch,
            "detail": self.detail,
        }


class SnowstormClient:
    """Thin, synchronous wrapper over the Snowstorm REST + FHIR API."""

    def __init__(
        self,
        base_url: str | None = None,
        branch: str | None = None,
        timeout: float | None = None,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = (base_url or settings.snowstorm_base_url).rstrip("/")
        self.branch = branch or settings.snowstorm_branch
        self.timeout = timeout or settings.snowstorm_timeout_seconds
        self._client = client
        self._owns_client = client is None

    # -- plumbing ----------------------------------------------------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                base_url=self.base_url,
                timeout=self.timeout,
                headers={"Accept": "application/json"},
            )
        return self._client

    def close(self) -> None:
        if self._client is not None and self._owns_client:
            self._client.close()
            self._client = None

    def __enter__(self) -> "SnowstormClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self.client.get(path, params=params)
        except httpx.HTTPError as exc:
            # Hard Rule 15: surface the failure, never swallow it.
            raise SnowstormUnavailable(
                f"Snowstorm at {self.base_url} could not be reached: {exc}"
            ) from exc
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise SnowstormError(
                f"Snowstorm {path} returned HTTP {response.status_code}: "
                f"{response.text[:400]}"
            )
        return response.json()

    # -- health ------------------------------------------------------------
    def health(self) -> SnowstormHealth:
        """Never raises: the API exposes this as a status, not an exception."""
        try:
            payload = self._get("/version")
            version = None
            if isinstance(payload, dict):
                version = payload.get("version") or payload.get("buildVersion")
            return SnowstormHealth(
                available=True,
                base_url=self.base_url,
                version=version,
                branch=self.branch,
            )
        except SnowstormError as exc:
            # The branch is still reported: knowing which branch we *would*
            # have queried is part of diagnosing why the server is missing.
            return SnowstormHealth(
                available=False,
                base_url=self.base_url,
                branch=self.branch,
                detail=str(exc),
            )

    def require_available(self) -> SnowstormHealth:
        """Fail clearly when Snowstorm is down (Master Instruction 7)."""
        health = self.health()
        if not health.available:
            raise SnowstormUnavailable(
                f"Snowstorm is not available at {self.base_url}. "
                f"Start it with: cd infra/snowstorm && docker compose up -d. "
                f"Detail: {health.detail}"
            )
        return health

    # -- native SNOMED API -------------------------------------------------
    def get_concept(self, concept_id: str, branch: str | None = None) -> dict | None:
        """Native concept lookup; ``None`` when the id is unknown."""
        branch = branch or self.branch
        return self._get(f"/{branch}/concepts/{concept_id}")

    def search_concepts(
        self,
        term: str,
        *,
        branch: str | None = None,
        limit: int = 20,
        active_only: bool = True,
        ecl: str | None = None,
    ) -> list[dict]:
        """Term search. Active-only by default (Master Instruction 12/18)."""
        branch = branch or self.branch
        params: dict[str, Any] = {"term": term, "limit": limit}
        if active_only:
            params["activeFilter"] = "true"
            params["termActive"] = "true"
        if ecl:
            params["ecl"] = ecl
        payload = self._get(f"/{branch}/concepts", params=params)
        if not payload:
            return []
        return payload.get("items", [])

    def preferred_term(self, concept_id: str, branch: str | None = None) -> str | None:
        concept = self.get_concept(concept_id, branch=branch)
        if not concept:
            return None
        pt = concept.get("pt") or {}
        fsn = concept.get("fsn") or {}
        return pt.get("term") or fsn.get("term")

    # -- FHIR --------------------------------------------------------------
    def fhir_lookup(
        self, system: str, code: str, version: str | None = None
    ) -> dict | None:
        """``CodeSystem/$lookup`` -- used to verify the LOINC import."""
        params: dict[str, Any] = {"system": system, "code": code}
        if version:
            params["version"] = version
        return self._get("/fhir/CodeSystem/$lookup", params=params)

    def lookup_loinc(self, code: str) -> dict | None:
        return self.fhir_lookup(LOINC_SYSTEM_URI, code)

    def translate_historical(
        self, concept_id: str, refset_id: str = "900000000000526001"
    ) -> dict | None:
        """``ConceptMap/$translate`` against a SNOMED historical association
        refset. Defaults to REPLACED BY."""
        if refset_id not in HISTORICAL_ASSOCIATION_REFSETS:
            raise ValueError(
                f"{refset_id} is not a known historical association reference set."
            )
        params = {
            "url": f"{SNOMED_SYSTEM_URI}?fhir_cm={refset_id}",
            "system": SNOMED_SYSTEM_URI,
            "code": concept_id,
        }
        return self._get("/fhir/ConceptMap/$translate", params=params)

    # -- imports -----------------------------------------------------------
    def create_import(
        self,
        *,
        branch_path: str | None = None,
        import_type: str = "SNAPSHOT",
        create_code_system_version: bool = True,
    ) -> str:
        """Create an import job and return its id."""
        body = {
            "branchPath": branch_path or self.branch,
            "createCodeSystemVersion": create_code_system_version,
            "type": import_type,
        }
        try:
            response = self.client.post("/imports", json=body)
        except httpx.HTTPError as exc:
            raise SnowstormUnavailable(
                f"Could not create a Snowstorm import job: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise SnowstormError(
                f"Creating the Snowstorm import job failed with HTTP "
                f"{response.status_code}: {response.text[:400]}"
            )
        location = response.headers.get("Location", "")
        import_id = location.rstrip("/").rsplit("/", 1)[-1] if location else None
        if not import_id:
            payload = response.json() if response.content else {}
            import_id = payload.get("id")
        if not import_id:
            raise SnowstormError(
                "Snowstorm accepted the import job but returned no import id."
            )
        log.info("created Snowstorm import job %s (%s)", import_id, import_type)
        return import_id

    def upload_import_archive(self, import_id: str, archive_path: str) -> None:
        """Stream the RF2 ZIP into an existing import job."""
        with open(archive_path, "rb") as fh:
            files = {"file": (archive_path.rsplit("/", 1)[-1], fh, "application/zip")}
            try:
                response = self.client.post(
                    f"/imports/{import_id}/archive",
                    files=files,
                    timeout=None,
                )
            except httpx.HTTPError as exc:
                raise SnowstormUnavailable(
                    f"Uploading the RF2 archive to Snowstorm failed: {exc}"
                ) from exc
        if response.status_code >= 400:
            raise SnowstormError(
                f"Snowstorm rejected the RF2 archive with HTTP "
                f"{response.status_code}: {response.text[:400]}"
            )
        log.info("uploaded RF2 archive to import job %s", import_id)

    def get_import_status(self, import_id: str) -> dict | None:
        return self._get(f"/imports/{import_id}")


__all__ = [
    "LOINC_SYSTEM_URI",
    "SNOMED_SYSTEM_URI",
    "SnowstormClient",
    "SnowstormError",
    "SnowstormHealth",
    "SnowstormUnavailable",
]
