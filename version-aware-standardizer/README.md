# Version-Aware Clinical Terminology Standardizer

A terminology standardization core that knows **which release its mappings were made against**,
and can tell you, at any later date, which of those mappings have gone stale.

---

## What this project does

It validates **existing** LOINC and SNOMED CT mappings against the terminology release that is
current right now. For every stored mapping it answers:

- is the target code still valid in the current release?
- if not, does the terminology itself declare an official successor?
- if it does, is that successor unambiguous enough to act on?
- and what was the state of the world when the mapping was originally made?

## What it does **not** do

It does **not** predict LOINC or SNOMED codes from raw clinical text. There is no machine
learning, no LLM, no BioBERT/SapBERT, no fuzzy string matching and no Gram-stain NLP in this
milestone. Every answer comes from official terminology fields and documented replacement
relationships. Those layers belong to later thesis stages, and they will sit **on top of** this
foundation.

---

## The problem, concretely

```
Today                          Six months later
-----                          ----------------
"HBsAg"  ->  LOINC 5195-3      LOINC ships a new release
             (ACTIVE)          the code becomes DISCOURAGED
                               MapTo names a successor
                               ...and your database still says ACTIVE
```

A static mapping table is silently wrong the day after the next release. LOINC 2.82 alone brought
over a thousand new concepts and well over a thousand edits; SNOMED CT International publishes
monthly. Nothing warns you.

This project is the warning.

```
  Existing local clinical mapping
              |
        LOINC / SNOMED code
              |
   Check against CURRENT release
              |
        Is it still valid?
              |
   +----------+-------------------+
   |          |                   |
 Active   Deprecated           Inactive
   |          |                   |
 KEEP    Find replacement   Find historical
   |          |               association
   |          v                   v
   |     SUGGEST / REVIEW    SUGGEST / REVIEW
   |          |                   |
   +----------+---------+---------+
                        |
                Preserve history
```

---

## Design commitments

| Commitment | How it is enforced |
|---|---|
| **Version-aware** | Every mapping stores `mapped_against_version`; every audit result stores the release it was judged against. No release identifier is hard-coded anywhere. |
| **Reproducible** | Every release carries a SHA-256; every audit run stamps the LOINC and SNOMED versions in force. A number in a paper can be traced back to exact files. |
| **Auditable** | `mapping_revision` is append-only. A mapping that moved A → B → C keeps both hops, with reviewer, date, reason and terminology version. |
| **Conservative** | A replacement is only ever *suggested*. Committing one requires `POST /api/v1/mappings/{id}/approve-replacement` with a named reviewer. |
| **Release-independent** | Files are located inside archives by basename/pattern; columns are resolved through alias tables; a column an older release lacks becomes `NULL` rather than a crash. |
| **Honest about doubt** | Ambiguity abstains to `MANUAL_REVIEW`, and the audit reports its **abstention rate** as a first-class metric. |
| **Readable offline** | Fully specified names and preferred terms are resolved from the release files, so a report says *Escherichia coli*, not `112283007`, with no server running. |

### Why abstention is the design, not a shortcut

Three findings from the literature shape the decision tables directly:

- Lin et al. (2011) manually reviewed voluntary LOINC mappings at three large institutions and
  found errors in roughly **4.5%** of sampled tests, in four systematic categories. Existing
  mappings — including MIMIC's — are therefore treated as *claims to audit*, never as gold labels.
- A 2026 comparison of ChatGPT-4.0, Gemini and Perplexity on LOINC assignment in laboratory
  medicine found only **22.7%** of test items were mapped consistently by all three models and the
  human experts. Expert validation stays mandatory; an LLM cannot own this decision.
- Swaminathan et al. (JAMIA 2024) showed that **selective prediction** — letting a system decline
  and route hard cases to a human — beats forcing an answer on every row.

So the engine suggests exactly one thing, exactly when the terminology itself is unambiguous, and
otherwise says so.

---

## Architecture

```
        OFFICIAL TERMINOLOGY RELEASES  (obtained by you, under licence)
                          |
          +---------------+----------------+
          |                                |
        LOINC                          SNOMED CT
   Loinc.csv                          RF2 Snapshot
   MapTo.csv                          sct2_Concept_Snapshot
   LoincChangeSnapshot.csv            der2_cRefset_Association*
          |                           der2_cRefset_AttributeValue*
          |                           sct2_Description + Language refset
          |                                |
          |                                +----> Snowstorm (search, ECL, FHIR)
          |                                |
          +---------------+----------------+
                          v
                     PostgreSQL
   release registry | concepts | MapTo | associations | inactivations | terms
                          v
              LoincResolver / SnomedResolver
                          v
                     Audit engine
                          v
        +-----------------+------------------+
        |                 |                  |
      KEEP        SUGGEST_REPLACEMENT   MANUAL_REVIEW
        |                 |                  |
        +--------> human approval <----------+
                          v
                  mapping_revision  (append-only)
```

Snowstorm is **infrastructure only**. Its source is never modified, and the version-aware logic
never depends on it: every verdict is computed from the locally parsed release files, so results
are reproducible offline and two releases can be diffed without a server. Preferred terms are
resolved from the description file and the language reference set at import time, so audit reports
read in clinical language with Snowstorm switched off — it is needed only for term search.

See [`docs/architecture.md`](docs/architecture.md), [`docs/database.md`](docs/database.md) and
[`docs/api.md`](docs/api.md). For the thesis write-up, [`docs/methodology.md`](docs/methodology.md) sets out the
problem, the positioning against prior work, the evaluation design and the threats to validity.

---

## Setup

Requirements: Windows/macOS/Linux, Python 3.11+, Docker Desktop (~8 GB RAM free for Snowstorm),
Git.

```powershell
# 1. Bootstrap: venv, dependencies, .env, Snowstorm clone, migrations
.\scripts\bootstrap.ps1

# 2. PostgreSQL
docker compose up -d

# 3. Snowstorm + Elasticsearch + SNOMED browser
cd infra\snowstorm
docker compose up -d
cd ..\..
#    browser:  http://localhost
#    API/docs: http://localhost:8080

# 4. Migrations (bootstrap already ran these; re-run any time)
.\.venv\Scripts\python.exe -m alembic upgrade head

# 5. The API
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload
#    http://localhost:8000/docs
```

If Elasticsearch refuses to start on Windows, raise the mmap limit in an **administrator** shell:

```powershell
wsl -d docker-desktop sysctl -w vm.max_map_count=262144
```

### No terminology files yet?

The whole pipeline runs on synthetic releases, in a throwaway database, with one command:

```powershell
.\.venv\Scripts\python.exe scripts\demo_end_to_end.py
```

It walks through all the demonstration cases, produces real CSV reports, and touches nothing
licensed.

---

## Data licensing

**This repository distributes no terminology content, and never will.**

| Source | How to obtain it | Where to put it |
|---|---|---|
| LOINC Complete | Free account at the official LOINC downloads page | `data/raw/loinc/` |
| SNOMED CT International RF2 | Affiliate/member licence via your institution or supervisor | `data/raw/snomed/` |
| MIMIC-III demo (`D_LABITEMS.csv`) | **open access, no licence needed** — `python scripts/fetch_mimic_demo.py` | `data/raw/validation/` |

`data/raw/` is git-ignored, as are `*.zip` and `*.gz`. Do not bypass a licence, and do not commit
a release archive. A CI job fails the build if one ever appears.

The MIMIC-III demo is the exception worth knowing about: it is published under the Open Data
Commons Open Database License (ODbL) v1.0 with an open access policy, so it can be fetched
directly. `scripts/fetch_mimic_demo.py` verifies the download against both a pinned SHA-256 and
PhysioNet's own published manifest, prints the ODbL terms, and saves `LICENSE.txt` next to the
data. ODbL is share-alike: a corrected mapping table derived from it must be attributed and
licensed the same way.

---

## Importing releases

```powershell
# LOINC -- version comes from the package or from you, never from the source code
.\.venv\Scripts\python.exe scripts\import_loinc.py `
    --file data\raw\loinc\Loinc_<version>.zip `
    --version <version> `
    --effective-date <YYYY-MM-DD>

# SNOMED CT -- parses RF2 locally, then pushes the archive to Snowstorm
.\.venv\Scripts\python.exe scripts\import_snomed.py `
    --file data\raw\snomed\SnomedCT_InternationalRF2_PRODUCTION_<date>T<time>Z.zip `
    --version <YYYYMMDD>

# ...or parse locally only (audits work; term search does not)
.\.venv\Scripts\python.exe scripts\import_snomed.py --file <...> --version <...> --skip-snowstorm

# Load an OLDER release for the validation experiment without making it current
.\.venv\Scripts\python.exe scripts\import_loinc.py --file <older>.zip --version <older> --not-current
```

Importing the same archive twice is a no-op: identity is the SHA-256, so renaming the file does
not create a second release. A full SNOMED import into Snowstorm commonly takes 30–60 minutes.

---

## The console

Start the API and open **<http://localhost:8000/>** — the bare URL lands on a web console rather
than a JSON blob.

```powershell
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload
```

| Page | What it is for |
|---|---|
| **Dashboard** | Which release each terminology is speaking right now, and the last audit's headline numbers |
| **What happened to the data** | The result pipeline as a five-step funnel, with every issue explained in plain English |
| **Browse results** | One result at a time, showing what the hospital recorded next to what it became |
| **Look up a code** | Paste a LOINC code or SNOMED concept id and get the verdict, with the release it was judged against |
| **Mappings** | Browse and filter the local mappings; open one for its full revision history |
| **Audit** | Run an audit over any scope, then read the results filtered by decision |
| **Needs your decision** | The human half of the loop — the cases the engine declined to decide, with the approval action |
| **Compare releases** | Two releases side by side, including our diff checked against the release's own change log |
| **What the words mean** | A worked example, then every machine word the console can show with the sentence that explains it |

The interface is written for someone who has never heard of LOINC: the dashboard opens with a
four-line explanation of the whole idea, every verdict is a sentence rather than an enum, and the
glossary starts with a worked example instead of a table.

Three things the console does on purpose:

- **No verdict is shown without its release.** A code is not valid or invalid in the abstract; it
  is valid in a *named* release, so every card carries that name. Hiding it would reintroduce the
  exact ambiguity this project exists to remove.
- **The review queue shows the hospital's own test name and specimen**, not just a code. When the
  engine abstains because "the correct replacement depends on local test context", that context is
  precisely what the reviewer is being asked to supply.
- **Approving asks for a name and shows what will be recorded** before it writes anything. The
  console cannot bypass the safety contract — it calls the same endpoint, with the same checks.

It is plain HTML, CSS and JavaScript served from the app itself: no CDN, no build step, no
`npm install`. It works with the network cable out, which is the same reason nothing else in this
project phones home. A test asserts that the page pulls nothing from a third-party origin.

---

## Everyday use

```powershell
# What am I speaking right now?
curl http://localhost:8000/api/v1/releases/current

# One code
curl http://localhost:8000/api/v1/loinc/<code>/resolve
curl http://localhost:8000/api/v1/snomed/<conceptId>/resolve

# Import real historical mappings to audit
.\.venv\Scripts\python.exe scripts\import_mimic_labitems.py --file data\raw\validation\D_LABITEMS.csv

# Audit everything
.\.venv\Scripts\python.exe scripts\audit_mappings.py
.\.venv\Scripts\python.exe scripts\audit_mappings.py --source-dataset MIMIC_III --report-name mimic_loinc_audit.csv

# Compare two releases
.\.venv\Scripts\python.exe scripts\compare_releases.py --system LOINC     --old <A> --new <B>
.\.venv\Scripts\python.exe scripts\compare_releases.py --system SNOMED_CT --old <A> --new <B>

# Run the whole validation experiment and write one report
.\.venv\Scripts\python.exe scripts\validate_releases.py

# Check the database and the coverage floors
.\.venv\Scripts\python.exe scripts\check_database.py
.\.venv\Scripts\python.exe scripts\check_coverage.py --min-overall 85 --min-core 95
```

Reports land in `data/reports/`.

### The review loop

An audit produces decisions; a person makes them. That handover is a CSV round trip, so it stays
attributable and re-readable months later:

```powershell
# 1. every mapping needing a decision. approve_target_code is blank on EVERY row;
#    what the engine would propose sits beside it in engine_suggested_code
.\.venv\Scripts\python.exe scripts\review_queue.py export --latest

# 2. a human opens data\reports\review_queue.csv and copies engine_suggested_code
#    into approve_target_code for the rows they agree with

# 3. see what would happen, then do it
.\.venv\Scripts\python.exe scripts\review_queue.py apply --file data\reports\review_queue.csv `
    --reviewer "dr name" --dry-run
.\.venv\Scripts\python.exe scripts\review_queue.py apply --file data\reports\review_queue.csv `
    --reviewer "dr name"
```

Only rows with a value in `approve_target_code` are touched; a blank means "leave it alone", and
**the export always leaves it blank** — an unedited round trip applies nothing. Pre-filling it
would mean a named clinician's approval was recorded against changes they never looked at, so
the engine's proposal lives in a separate, read-only `engine_suggested_code` column.

`--dry-run` runs every check the real run runs and rolls back instead of committing, so the
preview cannot disagree with what follows; it writes to a separate `*_dryrun_outcome.csv`.
Each applied row goes through the same approval path, so the target must be valid in the
current release and the old code plus the release it was valid in survive on a new revision.
A rejected row rolls back alone and the run reports it, rather than discarding the whole
session.

### LOINC inside Snowstorm

Snowstorm serves LOINC through the HAPI FHIR terminology loader, which is a separate step from our
own LOINC import (that one fills PostgreSQL, which is what the resolver reads):

```powershell
.\.venv\Scripts\python.exe scripts\upload_loinc_to_snowstorm.py `
    --file data\raw\loinc\Loinc_<version>.zip --download-cli
```

It checks Java 17+, checks Snowstorm is up before starting a large upload, and verifies afterwards
with a `$lookup` on a code taken from the release you imported.

### API surface

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | database, Snowstorm and release status |
| GET | `/api/v1/releases` | every imported release, superseded ones included |
| GET | `/api/v1/releases/current` | the release in force per terminology |
| GET | `/api/v1/loinc/{code}` | raw LOINC record from the current release |
| GET | `/api/v1/loinc/{code}/resolve` | version-aware verdict |
| GET | `/api/v1/snomed/search` | active-only term search (needs Snowstorm) |
| GET | `/api/v1/snomed/{id}` | raw SNOMED record |
| GET | `/api/v1/snomed/{id}/resolve` | version-aware verdict |
| POST | `/api/v1/mappings` | record an existing local mapping |
| GET | `/api/v1/mappings` | list / filter mappings |
| GET | `/api/v1/mappings/{id}` | one mapping with its revision history |
| GET | `/api/v1/mappings/{id}/history` | append-only revision history |
| POST | `/api/v1/mappings/{id}/approve-replacement` | **the only** way to change a target code |
| POST | `/api/v1/audits` | audit stored mappings against the current releases |
| GET | `/api/v1/audits` · `/{id}` · `/{id}/results` · `/{id}/report` | audit runs, results, report |

Full interactive documentation: `http://localhost:8000/docs`.

---

## Standardizing the results themselves

The terminology layer answers *is this code still the right code?* This layer answers the
question underneath it: *what did the test actually say, and can another system read it?*

```powershell
# 1. load the raw results (identifiers are pseudonymised at the door)
.\.venv\Scripts\python.exe scripts\import_mimic_labevents.py --file "<LABEVENTS source>"

# 2. standardize them
.\.venv\Scripts\python.exe scripts\standardize_mimic_results.py --seed-rules

# 3. write them as FHIR, and check them
.\.venv\Scripts\python.exe scripts\export_fhir_observations.py --validate
.\.venv\Scripts\python.exe scripts\validate_standardized_results.py
```

A raw row like `Sodium | "137" | "mEq/L" | (no flag)` becomes an approved LOINC code that is
valid today, a value typed as a number, and the UCUM unit `meq/L` — with every step recorded.

### What it will not do

These are not conservative defaults; they are the point of the module.

| It never | Because |
|---|---|
| turns `Negative` into `0` | A negative result and a result of zero are different clinical statements, and no arithmetic downstream can tell them apart afterwards. |
| turns a missing result into `0` | An average over the column would silently include zeros that were never measured. |
| drops the `<` from `<2.0` | That turns a below-detection-limit reading into a measurement. The number and the sign are both kept. |
| guess a missing unit | LOINC's example units are *examples*, not a permitted list, so a unit cannot be inferred from the code. |
| convert a unit without an approved rule | Glucose and creatinine have different molar masses. A blanket "mg/dL to mmol/L" rule would corrupt every creatinine in the dataset. |
| invent a SNOMED code | With no licence, a recognised value gets normalised **text** and a null code, marked `TEXT_NORMALIZED_CODE_PENDING`. |
| promote a suggestion to an approval | `engine_suggested_loinc` and `approved_current_loinc` are separate columns and never assigned from one another. |
| drop a row | Input rows must equal standardized plus quarantined rows. The run fails rather than publishing a table that quietly lost something. |

### Privacy

This is the first part of the project that touches patient-level data, so identifiers stop
being identifiers at import: `SUBJECT_ID` and `HADM_ID` become keyed HMAC pseudonyms.

A plain hash would not do — a bare SHA-256 of a small integer id is recoverable by trying every
integer, which for a hundred-patient demo takes moments. The key is what makes it one-way, and
it lives in `PSEUDONYM_SECRET` in the environment, never in the repository. The scripts refuse
to run without it. A null `HADM_ID` stays null, because it means an outpatient result — a real
state, not missing data.

### What comes out

| File | What it is |
|---|---|
| `standardized_lab_results.csv` | one row per result, raw and standardized side by side |
| `standardized_lab_results.ndjson` | the same as FHIR R4 Observations |
| `result_standardization_issues.csv` | every named problem, with its explanation |
| `unmapped_lab_items.csv` | tests with no code, with their observed units and example values |
| `unit_mapping_coverage.md` | which units were recognised, and which have no rule yet |
| `result_value_mapping_coverage.md` | the same for categorical results |
| `standardization_summary.md` | the headline numbers |
| `standardization_manifest.json` | the releases, rule versions and commit the run depended on |

### Three FHIR choices worth knowing

- **`status` is `unknown`.** FHIR requires it; MIMIC does not record whether a result was
  preliminary or final. Writing `final` would assert something the source never said.
- **A censored result keeps its comparator** — `valueQuantity` with `value: 2.0` and
  `comparator: "<"`.
- **A missing value becomes `dataAbsentReason`**, never a value of zero.

---

## The decision tables

### LOINC

| Status in current release | MapTo targets | Decision |
|---|---|---|
| `ACTIVE` | — | `KEEP` |
| `TRIAL` | — | `KEEP_WITH_WARNING` |
| `DISCOURAGED` / `DEPRECATED` | exactly one, usable | `SUGGEST_REPLACEMENT` |
| `DISCOURAGED` / `DEPRECATED` | more than one | `MANUAL_REVIEW` (`MULTIPLE_REPLACEMENTS`) |
| `DISCOURAGED` / `DEPRECATED` | none | `MANUAL_REVIEW` (`NO_OFFICIAL_REPLACEMENT`) |
| code absent | — | `UNKNOWN_CODE` |

A code that stays `ACTIVE` but whose component/name/property changed is **not** moved: it returns
`KEEP` with `metadata_changed = true` and a field-level diff. A chained MapTo is followed until an
`ACTIVE`/`TRIAL` target, a fork, a dead end, a cycle, or the depth limit — an obsolete target is
never presented as safe.

### SNOMED CT

| State | Association | Decision |
|---|---|---|
| `active = 1` | — | `KEEP` |
| `active = 0` | single `REPLACED BY` | `SUGGEST_REPLACEMENT` |
| `active = 0` | single `SAME AS` | `SUGGEST_REPLACEMENT` |
| `active = 0` | `POSSIBLY EQUIVALENT TO` | `MANUAL_REVIEW` — *even as the only row* |
| `active = 0` | `WAS A` / `ALTERNATIVE` | `MANUAL_REVIEW` |
| `active = 0` | `MOVED TO` | `MANUAL_REVIEW` — a namespace move is not a clinical replacement |
| `active = 0` | more than one | `MANUAL_REVIEW` |
| `active = 0` | none | `MANUAL_REVIEW` (`NO_HISTORICAL_ASSOCIATION`) |

Inactive reference-set members are ignored when suggesting a current replacement, and every
suggested target must itself be active in the current release.

---

## Testing

```powershell
.\.venv\Scripts\python.exe -m pytest -v --cov=backend/app --cov-report=term-missing --cov-report=html
```

| Selection | Command | Needs |
|---|---|---|
| Default (unit + API) | `pytest` | nothing |
| Performance | `pytest -m slow` | nothing |
| Against PostgreSQL | `VAS_TEST_DATABASE_URL=... pytest` | a **throwaway** PostgreSQL — the fixtures DROP every table |
| Snowstorm integration | `pytest -m integration` | running Snowstorm + imported SNOMED |
| Official-release validation | `pytest -m validation -s` | two real releases in `data/raw/validation/` |

`VAS_TEST_DATABASE_URL` points the identical suite at any database — the schema is only portable
if that is proved rather than asserted, so CI runs both legs on every push. The fixtures drop and
recreate every table, so never aim it at anything you care about.

Unit tests run entirely on **synthetic** releases built in `tests/fixtures/synthetic.py` — tiny
invented codes in the real file formats. No licensed content is committed, and no test needs a
100 MB package.

The `integration` and `validation` suites **skip loudly** with placement instructions when their
inputs are missing. They are never allowed to pass vacuously.

Two gates run in CI alongside the suite:

- `scripts/check_coverage.py` enforces 85% overall **and** 95% on the two resolver modules
  separately, because a single overall figure can hide a thin resolver behind fat schema files;
- a licence-guard job fails the build if a terminology archive, a `.env`, or a hard-coded release
  identifier ever reaches the repository.

On macOS or Linux, `make help` lists every command in this README as a target.

---

## Validation methodology

### 1. Official release-to-release validation (primary evidence)

One command runs the whole thing once the archives are in `data/raw/validation/`:

```powershell
.\.venv\Scripts\python.exe scripts\validate_releases.py
```

It imports the releases into a **disposable** database, runs every experiment below, writes
`data/reports/validation_report.md`, and exits `0` only when every target is met — `1` when one is
missed, and `2` when nothing was validated, so an empty run can never be mistaken for a pass.

Import two real LOINC releases. The engine computes its own diff, then checks it against the
newer release's own `LoincChangeSnapshot.csv`:

```
official_changes   -> declared by LOINC
detected_changes   -> found by us
matched_changes
missed_changes     -> target: 0
unexpected_changes
```

The same shape for SNOMED CT: take every concept that went `active = 1` → `active = 0` between two
RF2 releases, and check that our extracted inactivation reasons and historical associations equal
the official reference-set rows exactly. Reported metrics: inactive-detection recall,
association-extraction accuracy, safe-suggestion accuracy, and **unsafe automatic updates (target:
0, zero by construction — nothing migrates without an approval call)**.

This is evidence that does not depend on anybody's opinion — only on two official files.

### 2. Real-world audit (supplementary)

MIMIC-III `D_LABITEMS` carries a `LOINC_CODE` column assigned by real people years ago: exactly
the kind of historical mapping set this project exists to audit. It is imported as claims to be
audited, not as ground truth.

**MIMIC-IV-on-FHIR is deliberately not used as a LOINC gold standard** — it primarily keeps the
original MIMIC terminology for laboratory observations, so `predicted == MIMIC-IV-FHIR` would not
be a valid validation design. It remains useful later for FHIR structure and interoperability
demonstrations.

---

## Repository layout

```
version-aware-standardizer/
├── backend/app/
│   ├── main.py          config.py  database.py  constants.py
│   ├── api/             releases · loinc · snomed · mappings · audits
│   ├── models/          SQLAlchemy 2.x ORM
│   ├── schemas/         Pydantic request/response models
│   ├── services/        the engine (see below)
│   └── utils/           checksum · archive · logging
├── scripts/             bootstrap · import_* · audit_mappings · compare_releases
│                       validate_releases · review_queue · demo_end_to_end
│                       fetch_mimic_demo · upload_loinc_to_snowstorm
│                       check_database · check_coverage
├── alembic/             migrations
├── tests/               unit · integration · validation · fixtures
├── docs/                architecture · database · api
├── .github/workflows/   CI: tests on SQLite and PostgreSQL, plus the licence guard
├── data/raw/            YOUR licensed releases (git-ignored)
└── data/reports/        generated CSV + reports (git-ignored)
```

Every service file, in one line each:

| File | Responsibility |
|---|---|
| `release_service.py` | release registry: checksums, duplicate refusal, current-release handover |
| `loinc_ingest.py` | parse `Loinc.csv`, `MapTo.csv`, `LoincChangeSnapshot.csv` from the ZIP |
| `loinc_resolver.py` | the LOINC decision table, MapTo chain following, metadata drift |
| `loinc_diff.py` | release-to-release diff and validation against the official Change Snapshot |
| `snomed_rf2_parser.py` | parse concept / association / inactivation Snapshot files |
| `snomed_resolver.py` | the SNOMED decision table, historical-association chain following |
| `snomed_diff.py` | release-to-release diff of active state and successors |
| `snowstorm_client.py` | Snowstorm REST + FHIR client, active-only search, import jobs |
| `mapping_service.py` | local mappings and the human approval path |
| `audit_service.py` | run an audit, summarise it, export CSV and the report |

---

## Troubleshooting

**`password authentication failed for user "terminology"` on port 5432.** Something else is
already listening there — very often a natively installed PostgreSQL, which answers before the
container does. Publish the container somewhere else:

```powershell
$env:POSTGRES_PORT = "55432"
docker compose up -d
# then use   postgresql+psycopg://terminology:terminology@localhost:55432/terminology
```

**Elasticsearch exits immediately on Windows.** Raise the mmap limit in an administrator shell:
`wsl -d docker-desktop -u root sysctl -w vm.max_map_count=262144`.

**Snowstorm import sits at `WAITING_FOR_FILE`.** The archive never reached the server; nothing will
ever move the job off that state. `scripts/import_snomed.py` now gives up after three minutes with
that diagnosis rather than polling for two hours. Check Snowstorm's multipart size limits.

**SNOMED audit reports show bare concept ids.** The release was imported with
`--skip-descriptions`, so there are no local display terms. Re-import without that flag, or start
Snowstorm.

**`hapi-fhir-cli` cannot be found.** Pass `--download-cli` to fetch it into `tools/`, or point at
your own copy with `--cli`. It needs Java 17 or newer.

## Reading list behind the design

1. Lin MC, Vreeman DJ, McDonald CJ, Huff SM. *Correctness of Voluntary LOINC Mapping for
   Laboratory Tests in Three Large Institutions.* AMIA Annu Symp Proc. 2010.
2. Hauser RG, Quine DB, Ryder A. *LabRS: A Rosetta stone for retrospective standardization of
   clinical laboratory test results.* JAMIA. 2018;25(2):121–126.
3. Kang H, Park Y, Son Y, Lee HY, Shin SY. *Mapping clinical terms to standard terminology for a
   multi-institutional research platform.* Int J Med Inform. 2026.
4. *Large Language Models' performances regarding LOINC mapping in laboratory medicine: a
   comparative analysis of ChatGPT-4.0, Gemini and Perplexity.* Int J Med Inform. 2026.
5. Anik MM, Ahmmed S, Mondal MRH. *A systematic review of automatic mapping of clinical
   terminologies.* Array. 2026.
6. Sung S, Park HA, Jung H, Kang H. *A SNOMED CT Mapping Guideline for Local Terms Used to Document
   Clinical Findings and Procedures in EMRs in South Korea.* JMIR Med Inform. 2023;11:e46127.
7. Swaminathan A, et al. *Selective prediction for extracting unstructured clinical data.* JAMIA.
   2024;31(1):188–195.
8. Chapman AB, et al. *Development and validation of MicrobEx: an open-source package for
   microbiology culture concept extraction.* JAMIA Open. 2022;5(2):ooac026.

Papers 1, 4, 5, 6 and 7 shaped the decision tables and the abstention design. Paper 8 belongs to
the next thesis stage.

---

## Status

See [`STATUS.md`](STATUS.md) for what is done, what is not, the known limitations, and the next
thesis step. Unfinished work is listed there rather than hidden.
