# Architecture

## System diagram

```mermaid
flowchart TD
    subgraph official["OFFICIAL RELEASES — you obtain these, never distributed"]
        LZ["LOINC Complete ZIP<br/>Loinc.csv · MapTo.csv<br/>LoincChangeSnapshot.csv"]
        SZ["SNOMED CT International RF2 ZIP<br/>sct2_Concept_Snapshot<br/>der2_cRefset_Association*<br/>der2_cRefset_AttributeValue*<br/>sct2_Description + Language refset"]
    end

    LZ --> LI["loinc_ingest.py<br/><i>basename lookup · column aliasing</i>"]
    SZ --> SP["snomed_rf2_parser.py<br/><i>filename patterns · header-driven columns</i>"]
    SZ -.->|"same ZIP, unchanged"| SS["Snowstorm<br/><i>search · ECL · FHIR</i>"]

    LI --> DB[("PostgreSQL<br/>release registry · release content<br/>local mappings · audit trail")]
    SP --> DB

    DB --> LR["LoincResolver<br/><i>status table · MapTo chain<br/>metadata drift</i>"]
    DB --> SR["SnomedResolver<br/><i>active flag · association type<br/>successor chain</i>"]
    SS -.->|"display terms only"| SR

    LR --> AUD["audit_service<br/><i>batch preload · verdict per mapping<br/>summary · CSV</i>"]
    SR --> AUD

    AUD --> K["KEEP<br/><i>nothing to do</i>"]
    AUD --> S["SUGGEST_REPLACEMENT<br/><i>inert suggestion</i>"]
    AUD --> M["MANUAL_REVIEW<br/><i>routed to a human</i>"]

    K --> H
    S --> AP["POST /mappings/{id}/approve-replacement<br/><b>named reviewer required</b>"]
    M --> AP
    AP --> H[("mapping_revision<br/><i>append-only</i>")]

    classDef store fill:#e8f0fe,stroke:#4c6ef5,color:#1a1a2e
    classDef danger fill:#fff4e6,stroke:#f08c00,color:#1a1a2e
    classDef safe fill:#ebfbee,stroke:#2f9e44,color:#1a1a2e
    class DB,H store
    class M,AP danger
    class K safe
```

<details>
<summary>Same diagram as plain text (for terminals and print)</summary>

```
                         +-------------------------------------------+
                         |            OFFICIAL RELEASES              |
                         |   (you obtain these; never distributed)   |
                         +---------------------+---------------------+
                                               |
              +--------------------------------+--------------------------------+
              |                                                                 |
   LOINC Complete ZIP                                            SNOMED CT International RF2 ZIP
   Loinc.csv                                                     sct2_Concept_Snapshot*.txt
   MapTo.csv                                                     der2_cRefset_Association*Snapshot*.txt
   LoincChangeSnapshot.csv                                       der2_cRefset_AttributeValue*Snapshot*.txt
              |                                                                 |
              v                                                                 v
   +----------------------+                                   +--------------------------------+
   |   loinc_ingest.py    |                                   |     snomed_rf2_parser.py       |
   |  basename lookup     |                                   |  filename-pattern lookup       |
   |  column aliasing     |                                   |  header-driven columns         |
   +----------+-----------+                                   +----------------+---------------+
              |                                                                |
              |                                       (the same ZIP, unchanged)|
              |                                                                +---------> Snowstorm
              |                                                                |            (search,
              v                                                                v            preferred
   +-------------------------------------------------------------------------------+       terms, FHIR)
   |                              PostgreSQL                                       |
   |  terminology_release | loinc_concept_version | loinc_map_to | loinc_change     |
   |  snomed_concept_version | snomed_historical_association | snomed_inactivation  |
   |  local_mapping | mapping_revision | audit_run | audit_result                   |
   +----------------------------------+--------------------------------------------+
                                      |
                +---------------------+----------------------+
                |                                            |
      +---------v---------+                        +---------v---------+
      |  LoincResolver    |                        |  SnomedResolver   |
      |  status table     |                        |  active flag      |
      |  MapTo chain      |                        |  association type |
      |  metadata drift   |                        |  successor chain  |
      +---------+---------+                        +---------+---------+
                |                                            |
                +---------------------+----------------------+
                                      |
                          +-----------v-----------+
                          |     audit_service     |
                          |  batch preload        |
                          |  per-mapping verdict  |
                          |  summary + CSV        |
                          +-----------+-----------+
                                      |
        +-----------------------------+-----------------------------+
        |                             |                             |
      KEEP                 SUGGEST_REPLACEMENT               MANUAL_REVIEW
   (nothing to do)          (inert suggestion)          (routed to a human)
        |                             |                             |
        +-----------------------------+-----------------------------+
                                      |
                        POST /mappings/{id}/approve-replacement
                                      |
                          +-----------v-----------+
                          |   mapping_revision    |
                          |     append-only       |
                          +-----------------------+
```

</details>

## The decision path, end to end

```mermaid
flowchart TD
    START["existing local mapping<br/>+ mapped_against_version"] --> LOOK{"code present in the<br/>CURRENT release?"}
    LOOK -->|no| UNK["UNKNOWN_CODE<br/><i>never guessed</i>"]
    LOOK -->|yes| WHICH{terminology}

    WHICH -->|LOINC| LST{STATUS}
    LST -->|ACTIVE| LMETA{"metadata<br/>changed?"}
    LMETA -->|no| LKEEP["KEEP"]
    LMETA -->|yes| LKEEP2["KEEP<br/>metadata_changed = true<br/><i>the code does not move</i>"]
    LST -->|TRIAL| LTRIAL["KEEP_WITH_WARNING"]
    LST -->|"DISCOURAGED<br/>DEPRECATED"| LMAP{"official MapTo<br/>targets"}
    LMAP -->|none| LNONE["MANUAL_REVIEW<br/>NO_OFFICIAL_REPLACEMENT"]
    LMAP -->|"more than one"| LMANY["MANUAL_REVIEW<br/>MULTIPLE_REPLACEMENTS"]
    LMAP -->|"exactly one"| LCHAIN{"chase the chain:<br/>usable target?"}
    LCHAIN -->|"yes"| LSUG["SUGGEST_REPLACEMENT"]
    LCHAIN -->|"fork / dead end /<br/>cycle / too deep"| LSTOP["MANUAL_REVIEW<br/><i>reason names which</i>"]

    WHICH -->|SNOMED| SACT{"active?"}
    SACT -->|"1"| SKEEP["KEEP"]
    SACT -->|"0"| SASSOC{"active historical<br/>associations"}
    SASSOC -->|none| SNONE["MANUAL_REVIEW<br/>NO_HISTORICAL_ASSOCIATION"]
    SASSOC -->|"more than one"| SMANY["MANUAL_REVIEW<br/>MULTIPLE_REPLACEMENTS"]
    SASSOC -->|"exactly one"| STYPE{"association type"}
    STYPE -->|"REPLACED BY<br/>SAME AS"| SCHAIN{"target active<br/>in this release?"}
    STYPE -->|"POSSIBLY EQUIVALENT TO<br/>WAS A · ALTERNATIVE"| SAMB["MANUAL_REVIEW<br/>AMBIGUOUS_ASSOCIATION_TYPE"]
    STYPE -->|"MOVED TO"| SMOVE["MANUAL_REVIEW<br/>MOVED_TO_OTHER_NAMESPACE"]
    SCHAIN -->|yes| SSUG["SUGGEST_REPLACEMENT"]
    SCHAIN -->|"no / cycle / too deep"| SSTOP["MANUAL_REVIEW"]

    classDef keep fill:#ebfbee,stroke:#2f9e44,color:#1a1a2e
    classDef suggest fill:#e7f5ff,stroke:#1971c2,color:#1a1a2e
    classDef review fill:#fff4e6,stroke:#f08c00,color:#1a1a2e
    class LKEEP,LKEEP2,LTRIAL,SKEEP keep
    class LSUG,SSUG suggest
    class UNK,LNONE,LMANY,LSTOP,SNONE,SMANY,SAMB,SMOVE,SSTOP review
```

Every orange box is an **abstention**: the engine declines and hands the case to a person. The
audit reports how often that happened as its abstention rate.

## Why the RF2 files are parsed locally as well as pushed to Snowstorm

Snowstorm is excellent at what it is for: term search, ECL, preferred terms, a browser, and the
standard FHIR terminology endpoints. It is not the right dependency for version-aware reasoning:

- its branch state is mutable, so a verdict computed from it is not reproducible from files alone;
- it holds *one* imported state per branch, so it cannot answer "what changed between release A
  and release B" offline;
- it is a large stack (Elasticsearch, ~8 GB RAM) whose absence would otherwise block every audit.

So the auditor reads the locally parsed tables, and Snowstorm only enriches display terms and
serves search. `SnowstormClient.health()` never raises; the API surfaces its state at `/health`,
and `/api/v1/snomed/search` — the one endpoint that genuinely needs it — returns a clear 503 when
it is down.

## Data flow of a single audit

1. `run_audit` reads the current release rows for LOINC and SNOMED from `terminology_release`.
2. It creates an `audit_run` stamped with both versions, plus the scope filter.
3. It selects the mappings in scope and **batch-preloads** everything the resolvers will need:
   concepts, MapTo rows, associations, inactivations, and the older-release baselines used for
   metadata-drift detection.
4. Each mapping is resolved from cached data. Chain following (MapTo / historical association)
   may fetch a small number of extra rows, cached per resolver instance.
5. Each verdict is written as an `audit_result`, including the suggested targets as JSON, the
   reason code, and the metadata used to reach the decision.
6. Mappings that need attention are flagged `NEEDS_REVIEW`. **No target code is touched.**
7. A summary and a CSV report are written.

The batch preload is what keeps a 10,000-mapping audit at single-digit SQL statements; the
performance test asserts this rather than trusting it.

## Chain following

Both resolvers follow a successor chain with the same three guards:

- **visited set** — a cycle returns `REPLACEMENT_CHAIN_CYCLE`, never an infinite loop;
- **depth limit** — configurable via `MAX_REPLACEMENT_CHAIN_DEPTH`, returns
  `REPLACEMENT_CHAIN_TOO_DEEP`;
- **fork detection** — a hop with more than one successor stops with `MULTIPLE_REPLACEMENTS`.

A chain is only followed along semantically strong links: LOINC `MapTo`, and SNOMED
`REPLACED BY` / `SAME AS`. The engine never walks a `POSSIBLY EQUIVALENT TO` or a `WAS A` edge in
search of something to suggest.

## Deployment shapes

| Shape | What runs | What works | What does not |
|---|---|---|---|
| **Zero-config** | Python only, SQLite | every audit, both diffs, all reports, the full API | SNOMED term search |
| **Standard** | + PostgreSQL (`docker compose up -d`) | the above, at production scale | SNOMED term search |
| **Full** | + Snowstorm/Elasticsearch (~8 GB RAM) | everything, including search and FHIR `$lookup` | — |

The zero-config shape is deliberate: a supervisor should be able to clone the repository and see
the whole pipeline run on synthetic releases without installing anything but Python.

## Extension points for later thesis stages

The layers a microbiology mapper will add sit *above* this core and consume its API:

- **candidate retrieval** — `GET /api/v1/snomed/search` already returns active-only candidates;
- **a proposed mapping** — `POST /api/v1/mappings` with `map_correlation` and a
  `mapped_against_version`, so a model's output is version-stamped from birth;
- **uncertainty** — a model that abstains maps naturally onto the existing
  `MANUAL_REVIEW` decision and the abstention-rate metric;
- **staying fresh** — the audit engine already tells the model's output when it has gone stale.

Nothing in the later stages requires changing the decision tables here.
