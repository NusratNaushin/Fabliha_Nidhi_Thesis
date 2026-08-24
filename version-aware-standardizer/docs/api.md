# API reference

Base URL: `http://localhost:8000`. Interactive documentation: `/docs`.

Every response that expresses a verdict carries three things together: the **status** of the code
in the current release, the **decision** the engine reached, and the **release version** it
reached it against. None of the three is meaningful without the others.

---

## System

### `GET /health`

Reports each dependency separately rather than collapsing them into `ok`.

```json
{
  "status": "ok",
  "database": true,
  "database_detail": null,
  "snowstorm": { "available": false, "base_url": "http://localhost:8080",
                 "version": null, "branch": "MAIN",
                 "detail": "Snowstorm at http://localhost:8080 could not be reached: ..." },
  "releases": {
    "LOINC":     { "version": "...", "effective_date": "...", "sha256": "...", "import_status": "COMPLETED" },
    "SNOMED_CT": { "version": "...", "effective_date": "...", "sha256": "...", "import_status": "COMPLETED" }
  }
}
```

`status` is `degraded` when the database is unreachable. Snowstorm being down does not make the
service unhealthy — audits do not need it.

---

## Releases

### `GET /api/v1/releases?system=LOINC`

Every imported release, superseded ones included. Nothing is ever removed from this list.

### `GET /api/v1/releases/current`

```json
{
  "LOINC":     { "version": "...", "effective_date": "...", "imported_at": "...",
                 "sha256": "...", "source_filename": "...", "import_status": "COMPLETED" },
  "SNOMED_CT": null
}
```

A system with nothing imported is `null`, not omitted — so a client can distinguish
"not imported" from "not asked for".

---

## LOINC

### `GET /api/v1/loinc/{code}`

Raw record from the current release: the six axes, status, names, version-first-released /
last-changed, and the MapTo rows. `404` when the code is absent, `503` when no LOINC release has
been imported.

### `GET /api/v1/loinc/{code}/resolve`

Optional query parameter `mapped_against_version` enables metadata-drift detection.

```json
{
  "code": "...",
  "system": "LOINC",
  "version": "<current release>",
  "status": "DEPRECATED",
  "decision": "SUGGEST_REPLACEMENT",
  "reason": "SINGLE_OFFICIAL_REPLACEMENT",
  "raw_status": "DEPRECATED",
  "display": "...",
  "suggested_targets": [
    { "code": "...", "status": "ACTIVE", "display": "...", "usable": true,
      "via": ["<old>", "<intermediate>", "<final>"], "note": null }
  ],
  "metadata_changed": false,
  "metadata_diff": {},
  "details": {}
}
```

`via` is the MapTo chain that was walked. `usable` is `false` for any target that is not `ACTIVE`
or `TRIAL` in the current release — an obsolete target is never presented as safe.

An unknown code is a **verdict**, not a `404`: `decision: "UNKNOWN_CODE"`.

---

## SNOMED CT

### `GET /api/v1/snomed/search?term=...&limit=20&ecl=...`

Active-only by definition: `activeFilter=true` and `termActive=true` are always sent. Requires a
running Snowstorm; returns `503` with an actionable message when it is down.

### `GET /api/v1/snomed/{conceptId}`

Raw record from the locally parsed release. `fsn`, `preferred_term` and `language_refset_id` come
from the description file and the language reference set parsed at import time, and `display` is
the preferred term falling back to the FSN — so this works with Snowstorm switched off. Snowstorm
is consulted only as a fallback, for releases imported with `--skip-descriptions`.

### `GET /api/v1/snomed/{conceptId}/resolve`

```json
{
  "concept_id": "...",
  "system": "SNOMED_CT",
  "version": "<current release>",
  "status": "INACTIVE",
  "decision": "MANUAL_REVIEW",
  "reason": "AMBIGUOUS_ASSOCIATION_TYPE",
  "active": false,
  "inactivation_reason": "AMBIGUOUS",
  "inactivation_value_id": "...",
  "historical_associations": [
    { "association_type": "POSSIBLY_EQUIVALENT_TO", "refset_id": "900000000000523009",
      "target_component_id": "...", "target_active": null }
  ],
  "suggested_targets": [
    { "concept_id": "...", "active": true, "display": "...",
      "association_type": "POSSIBLY_EQUIVALENT_TO", "usable": false,
      "via": ["...", "..."], "note": "..." }
  ],
  "details": {}
}
```

A target listed with `usable: false` is shown **for review only**. `POSSIBLY EQUIVALENT TO`,
`WAS A`, `ALTERNATIVE` and `MOVED TO` never produce a usable target, even as the only row.

---

## Mappings

### `POST /api/v1/mappings`

```json
{
  "source_dataset": "MIMIC_III",
  "local_code": "50912",
  "local_text": "Creatinine",
  "target_system": "LOINC",
  "target_code": "...",
  "local_context": { "fluid": "Blood", "category": "Chemistry" },
  "mapped_against_version": "...",
  "map_correlation": "EXACT_MATCH",
  "review_status": "UNREVIEWED"
}
```

`map_correlation` follows the SNOMED CT mapping-guideline convention: `EXACT_MATCH`,
`BROAD_TO_NARROW`, `NARROW_TO_BROAD`, `PARTIAL_OVERLAP`, `NOT_SPECIFIED`. Leave
`mapped_against_version` `null` when it is genuinely unknown — guessing would defeat the purpose.

`409` on a duplicate `(source_dataset, local_code, target_system)`; `422` on an unsupported target
system.

### `GET /api/v1/mappings` · `GET /api/v1/mappings/{id}` · `GET /api/v1/mappings/{id}/history`

Filters: `source_dataset`, `target_system`, `review_status`, `limit`, `offset`. The detail
endpoint embeds the full revision history.

### `POST /api/v1/mappings/{id}/approve-replacement`

**The only endpoint that changes a target code.**

```json
{ "target_code": "...", "reviewer": "dr-name",
  "reason": "official MapTo, reviewed against local test context",
  "audit_result_id": 42, "allow_unsuggested": false }
```

Checks, in order:

1. the code must be one the engine actually suggested for this mapping — unless
   `allow_unsuggested: true`, which records a deliberate manual decision;
2. the code must be valid in the current release (`ACTIVE`, or `TRIAL` for LOINC) — a `DEPRECATED`
   or inactive target is refused even with the override;
3. a `mapping_revision` row is written with the old code, the old release version, the new code,
   the new release version, the reviewer and the timestamp;
4. only then is the mapping updated.

`409` when a check fails, with a message naming what was expected. `404` for an unknown mapping.

---

## Audits

### `POST /api/v1/audits`

```json
{ "source_dataset": "MIMIC_III", "target_system": "LOINC",
  "limit": null, "export_csv": true, "report_name": "mimic_loinc_audit.csv" }
```

All fields are optional; an empty body audits everything. The run records verdicts and may flag
mappings `NEEDS_REVIEW`, but **never changes a target code**.

The `summary_json`:

```json
{
  "total_mappings": 0, "by_system": {},
  "valid": 0, "trial_warning": 0, "discouraged": 0, "deprecated": 0,
  "inactive_snomed": 0, "unknown": 0,
  "single_replacement": 0, "multiple_replacement": 0, "no_replacement": 0,
  "manual_review_required": 0, "metadata_changed": 0,
  "abstention_rate": 0.0,
  "decisions": {}, "reasons": {}
}
```

`abstention_rate` is `manual_review_required / total_mappings` — how often the engine declined to
answer. It is reported as a headline metric, not buried.

### `GET /api/v1/audits` · `/{id}` · `/{id}/results?decision=...` · `/{id}/report`

`/report` returns plain text, ready to paste into a supervision meeting.

---

## Vocabularies

**Decisions** — `KEEP`, `KEEP_WITH_WARNING`, `SUGGEST_REPLACEMENT`, `MANUAL_REVIEW`,
`UNKNOWN_CODE`.

**Terminology statuses** — `CURRENT_VALID`, `CURRENT_TRIAL`, `DISCOURAGED`, `DEPRECATED`,
`INACTIVE`, `UNKNOWN`.

**Reasons** — `STATUS_ACTIVE`, `STATUS_TRIAL`, `SINGLE_OFFICIAL_REPLACEMENT`,
`MULTIPLE_REPLACEMENTS`, `NO_OFFICIAL_REPLACEMENT`, `NO_HISTORICAL_ASSOCIATION`,
`AMBIGUOUS_ASSOCIATION_TYPE`, `REPLACEMENT_TARGET_NOT_CURRENT`, `REPLACEMENT_CHAIN_CYCLE`,
`REPLACEMENT_CHAIN_TOO_DEEP`, `CODE_NOT_IN_CURRENT_RELEASE`, `NO_CURRENT_RELEASE`,
`MOVED_TO_OTHER_NAMESPACE`.

**Review statuses** — `UNREVIEWED`, `NEEDS_REVIEW`, `APPROVED`, `REJECTED`.
