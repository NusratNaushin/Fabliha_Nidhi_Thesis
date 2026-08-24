# STATUS

Last verified: 2026-08-23, on Windows 11 / Python 3.12.10 / SQLAlchemy 2.0.52 / FastAPI 0.141.1.

```
pytest                        318 passed, 11 skipped   (3m58s)
pytest (PostgreSQL 16)        281 passed, 14 skipped   (2m50s)   [an earlier revision]
pytest --cov                  95.96% overall
                              loinc_resolver.py   100.00%
                              snomed_resolver.py    96.44%
scripts/check_coverage.py                gate passed (85% overall / 95% core)
scripts/check_no_hardcoded_versions.py   tree clean
scripts/demo_end_to_end.py               all demos green on synthetic releases
scripts/validate_releases.py             every target met on REAL release pairs
pytest -m validation                     5 passed, 3 skipped (LOINC real; SNOMED still blocked)
```

**Clean-clone check.** The repository was cloned to an empty directory, a virtualenv created,
`requirements.txt` installed, `alembic upgrade head` run and the suite executed, with no other
setup: **302 passed, 9 skipped** (more tests ran than in the working tree because synthetic
validation archives were placed to exercise the validation suite). The README's setup instructions
were followed literally and worked.

The 11 remaining skips all need SNOMED CT International: 8 Snowstorm integration tests (no
server running) and 3 SNOMED release-comparison tests (no licensed RF2 archives). The LOINC
validation tests no longer skip -- they run against three real official releases. Every skip
prints the exact instructions to enable it; none can pass vacuously.

---

## REAL-DATA RESULTS

Three official LOINC releases (2.81, 2.82, 2.83), the LOINC Ontology RF2 package and MIMIC-III's
complete lab dictionary and result table are now in hand. Everything below was produced by
running the engine on them, not on fixtures.

### The primary correctness evidence: our diff versus LOINC's own change log

| release pair | codes before → after | new codes | field changes | official changes | **missed** | unexpected |
|---|---|---:|---:|---:|---:|---:|
| 2.81 → 2.82 | 108,248 → 109,325 | 1,077 | 3,229 | 3,229 | **0** | 0 |
| 2.82 → 2.83 | 109,325 → 112,405 | 3,080 | 11,580 | 11,580 | **0** | 0 |

**14,809 officially declared changes across two release pairs, every one reproduced, none missed,
none invented.** No code was absent from a later release in either pair, exactly as LOINC's
never-delete policy implies. Thirteen Change Snapshot properties outside the ten modelled fields
are reported by name rather than silently dropped.

Status transitions found, 2.82 → 2.83: 641 ACTIVE→DISCOURAGED, 17 ACTIVE→DEPRECATED, 3
TRIAL→ACTIVE. And 2.81 → 2.82 shows the traffic runs both ways: 31 ACTIVE→DEPRECATED, 22
TRIAL→ACTIVE, 4 DISCOURAGED→ACTIVE, 1 DEPRECATED→ACTIVE.

### Simulated historical mappings

All 658 codes that went ACTIVE → obsolete between 2.82 and 2.83 were turned into mappings made
against 2.82 and audited against 2.83: **658 of 658 detected as no longer valid, 0 reported as
still valid, 0 replacements suggested that MapTo does not license.**

Across the whole of LOINC 2.82, 3,892 of its 6,574 DISCOURAGED/DEPRECATED codes have exactly one
official replacement the engine can offer; the rest abstain.

### The MIMIC-III audit — real historical mappings, real current release

585 LOINC mappings assigned by people years ago, judged against LOINC 2.83:

| | mappings | |
|---|---:|---|
| still valid | 561 | 95.9% |
| DISCOURAGED | 11 | |
| DEPRECATED | 13 | |
| **stale** | **24** | **4.1%** |
| with one official replacement | 15 | `SUGGEST_REPLACEMENT` |
| needing a human | 9 | `MANUAL_REVIEW` |
| unknown codes | 0 | LOINC never deletes |

Abstention rate 1.5%. Report: `data/reports/mimic_loinc_audit.csv`.

### Weighted by how much data actually rests on each mapping

Joining those verdicts to the 76,074 observed laboratory results turns a code count into an
impact number:

| | result rows | share |
|---|---:|---:|
| map to a currently valid code | 70,530 | 92.71% |
| **rest on a stale mapping** | **2,454** | **3.23%** |
| — of which the engine can suggest a replacement | 1,688 | 2.22% |
| — of which need a human | 766 | 1.01% |
| itemid carries no LOINC code at all | 3,090 | 4.06% |

The single largest contributor is **INR(PT)** — `5895-7`, DEPRECATED, 1,378 result rows, with one
clean official replacement `6301-6`. Then Lactate Dehydrogenase (423 rows, two candidates, so the
engine abstains), CK-MB (319 rows, DEPRECATED with no replacement at all) and estimated GFR
(207 rows). Report: `data/reports/mimic_impact.md`.

That is the argument of the whole project, measured on somebody else's published data: a mapping
table that looks 96% correct is carrying drift on one in thirty of the results that matter, and
nothing in the data would have told anyone.

### The SNOMED path, on real RF2

SNOMED CT International remains licence-blocked, but the **LOINC Ontology** ships as a SNOMED RF2
extension, so the RF2 half of the engine has now been run on genuine release files:

- all five file patterns matched in a real package, zero warnings — including correctly *not*
  matching `sct2_TextDefinition_Snapshot`;
- 46,057 concepts, 4 association members, 38 inactivation members parsed in 6.5 s;
- **46,056 preferred terms resolved offline**, e.g. `Bilirubin.conjugated [Mass/volume] in Serum
  or Plasma` as the PT against the SNOMED-style FSN `Mass concentration of bilirubin glucuronide
  in serum or plasma at point in time (observable entity)`;
- the two `POSSIBLY EQUIVALENT TO` rows present are **inactive members**, and were correctly
  ignored when suggesting a replacement;
- two association rows reference *description* ids rather than concepts (`REFERS TO`); the
  resolver abstained rather than inventing a concept;
- all 33 newly-inactive concepts resolved to `MANUAL_REVIEW / NO_HISTORICAL_ASSOCIATION`, each
  carrying its real inactivation reason.

What this does **not** exercise: the extension contains no active `REPLACED BY` or `SAME AS`, so
the auto-suggest branch of the SNOMED table is still only proven on synthetic fixtures.

### Faults only real data exposed

1. **`LoincChangeSnapshot.csv` does not use SCREAMING_SNAKE names.** Its real header is
   `VersionEffective, LOINC_NUM, Property, ValuePrior, ValueCurrent, ChangeReason`, so aliases
   spelled `VALUE_PRIOR` matched nothing and three columns imported as NULL on every real
   release. The importer warned rather than failing silently, which is how it surfaced. Fixed,
   with a regression test that asserts full column coverage against the real archives.
2. **Archive discovery was not recursive.** Releases filed into `validation/loinc/` were
   invisible, so the validation suite skipped while looking satisfied. Both the suite and
   `validate_releases.py` now search recursively.
3. **Confirmed by real data:** LOINC ships `Loinc.csv` twice — `LoincTable/Loinc.csv` and
   `AccessoryFiles/PanelsAndForms/Loinc.csv`. The shallowest-path rule picks the canonical full
   table. A test now pins this, because importing the panels subset would corrupt everything
   downstream while looking entirely healthy.

---

## DONE

### Infrastructure
- [x] PostgreSQL service in `docker-compose.yml`, credentials via `.env` (never committed)
- [x] **PostgreSQL 16 exercised for real**: migration up, down and up again, then the identical
      281-test suite green against it as well as against SQLite
- [x] Alembic configured; two migrations, both applied on both backends
- [x] Snowstorm cloned as infrastructure by the bootstrap scripts; **its source is never modified**
- [x] `/health` reports database, Snowstorm and release state separately
- [x] SQLite fallback so a fresh clone runs the whole suite before Docker is up
- [x] `scripts/bootstrap.sh` for macOS/Linux alongside `bootstrap.ps1`; `Makefile` with 25 targets
- [x] GitHub Actions CI: the suite on Python 3.11/3.12 × SQLite, plus a PostgreSQL 16 leg
- [x] Licence-guard CI job: fails the build on a committed archive, a committed `.env`, or a
      hard-coded release identifier in executable code
- [x] `scripts/check_database.py` — says *which* of connection / tables / revision / invariants
      failed, against any `--database-url`
- [x] `scripts/check_coverage.py` — enforces 85% overall **and** 95% on the resolvers separately
- [x] `scripts/check_no_hardcoded_versions.py` — AST guard for Hard Rules 1-3, excluding
      docstrings and argparse help text structurally rather than by pattern

### Release management
- [x] `terminology_release` registry with SHA-256, effective date, import status, `is_current`
- [x] Re-importing the same content is a no-op — identity is the checksum, not the file name
- [x] Same version with different content is refused rather than silently overwritten
- [x] A new current release supersedes the old one; **the old release keeps every row**
- [x] `--not-current` to load an older release for the validation experiment
- [x] No LOINC or SNOMED version string appears anywhere in executable code (CI enforces it)

### LOINC
- [x] Importer locates `Loinc.csv`, `MapTo.csv`, `LoincChangeSnapshot.csv` recursively by basename
- [x] Column alias resolution; extra columns ignored; absent optional columns become `NULL`
- [x] `LoincChangeSnapshot.csv` treated as optional (older releases ship without it)
- [x] Full decision table: `ACTIVE` / `TRIAL` / `DISCOURAGED` / `DEPRECATED` / unknown
- [x] MapTo replacement resolution, including multi-hop chains
- [x] Replacement-target validation: obsolete, absent, forking, cyclic and over-deep chains all
      abstain with a specific reason
- [x] Metadata-drift detection on codes that stay `ACTIVE` (field-level prior/current diff)
- [x] Release-to-release diff, validated against the official Change Snapshot —
      **run on three real releases: 14,809 official changes, 0 missed**
- [x] `scripts/upload_loinc_to_snowstorm.py` — the HAPI FHIR CLI step, automated, with Java and
      server checks, the `-u ...|version` trap guarded, and a post-upload `$lookup` verified with a
      code taken from the release you imported

### SNOMED CT
- [x] RF2 Snapshot parsed locally: concepts, association refsets, inactivation indicator refset
- [x] **Descriptions and the language reference set parsed too** — one FSN and one preferred term
      per concept, so reports read in clinical language with Snowstorm switched off
- [x] Dialect handling per spec: ACTIVE + PREFERRED only, ACCEPTABLE never wins, US English
      outranks GB English, order configurable, FSN fallback when there is no preferred synonym
- [x] Files found by filename pattern, not by folder; provisional `xsct2_` files never match;
      ragged rows tolerated
- [x] Ten association refsets decoded, including `SIMILAR TO` and `PARTIALLY EQUIVALENT TO`
- [x] Inactive refset members ignored when suggesting a current replacement
- [x] Inactivation reasons decoded to readable labels
- [x] Only single `REPLACED BY` / `SAME AS` may be auto-suggested; everything else abstains
- [x] `MOVED TO` explicitly not interpreted as a clinical replacement
- [x] Successor chains followed with cycle and depth guards; every target verified active
- [x] Release-to-release diff with inactive-detection recall and association-extraction rate
- [x] Snowstorm client verified against the server's own source: `POST /imports` body, the
      `Location` header, the `file` multipart field, and the four-value status enum
- [x] `WAITING_FOR_FILE` guard: a failed upload fails in three minutes with a diagnosis instead of
      two hours of silence

### Mappings, audit, history
- [x] `local_mapping` with `mapped_against_version`, `local_context_json` and `map_correlation`
- [x] `mapping_revision`: append-only, never updated, never deleted
- [x] Human approval endpoint with pre-approval checks and a required named reviewer
- [x] **`scripts/review_queue.py`** — export the pending decisions to CSV with
      `approve_target_code` **blank on every row** and the engine's proposal beside it in a
      read-only `engine_suggested_code`; a person fills the approval column in; apply feeds each
      row back through the same approval path. `--dry-run` runs every check and rolls back, so the
      preview cannot disagree with the run. A rejected row rolls back alone
- [x] Audit engine over any scope; per-mapping `audit_result`; run stamped with both versions
- [x] Summary counts plus an **abstention rate**; CSV export now carries the display term
- [x] Batch preloading — 10,000 mappings audited in ~1.7 s with 8 SELECT statements

### Validation
- [x] **`scripts/validate_releases.py`** — the whole experiment as one command, into a disposable
      database, writing `data/reports/validation_report.md`; exit `0` all targets met, `1` a target
      missed, `2` nothing validated
- [x] Proved end to end on synthetic release pairs, **including a negative case**: a doctored pair
      declaring a change the concept table does not contain makes the run fail, so the gate is
      demonstrably not vacuous

### Real-world data
- [x] **MIMIC-III demo obtained and verified.** `scripts/fetch_mimic_demo.py` downloads
      `D_LABITEMS.csv` (ODbL v1.0, open access — no credentialing), checks it against a pinned
      SHA-256 *and* PhysioNet's published manifest, and saves `LICENSE.txt` beside it
- [x] Imported: 753 rows, 585 carrying a LOINC code, 575 distinct codes — with
      `mapped_against_version` left null, because for MIMIC-III it is genuinely unknown and
      inventing one would defeat the point
- [x] **Audited against real LOINC 2.83**: 24 of 585 stale (4.1%), 15 with a single official
      replacement, 9 needing a human
- [x] **`scripts/mimic_impact_report.py`** — weights the audit by observed result rows, turning a
      code count into "3.23% of real laboratory results rest on a stale mapping". Reads only
      `itemid`; no patient identifier, timestamp or value is examined or stored

### API and CLI
- [x] 16 REST endpoints, OpenAPI at `/docs`
- [x] 12 scripts: bootstrap (×2), import_loinc, import_snomed, import_mimic_labitems,
      fetch_mimic_demo, upload_loinc_to_snowstorm, audit_mappings, compare_releases,
      validate_releases, review_queue, demo_end_to_end, check_database, check_coverage

### Tests
- [x] 320 tests; synthetic fixtures only — no licensed content in the repository
- [x] Every LOINC and SNOMED decision branch asserted individually
- [x] Mandatory safety suite: ambiguity always abstains, nothing migrates without approval
- [x] History-preservation suite: `A -> B -> C` keeps both hops
- [x] Idempotency and checksum suites
- [x] 31 API tests via `TestClient`
- [x] 73 script tests, including the review round trip, the validation runner as a subprocess,
      and the hard-coded-version guard in both directions
- [x] A regression test for the review round trip's central promise: applying an **unedited**
      export changes nothing and writes no revision
- [x] Performance suite asserting the absence of N+1 queries
- [x] Regression cover for the two bugs found this session (see below)
- [x] Snowstorm integration suite (skips without a server)
- [x] Official-release validation suite (skips without real archives)

### Documentation
- [x] `README.md`, `docs/architecture.md`, `docs/database.md`, `docs/api.md`,
      `docs/methodology.md` (thesis-ready: problem, prior work, evaluation design, threats)
- [x] Mermaid architecture, decision-path and ER diagrams that render on GitHub, with the
      plain-text versions kept in `<details>` for terminals and print
- [x] Troubleshooting section covering the failures actually hit on this machine
- [x] Data-licensing section; `data/raw/`, `tools/`, `*.zip`, `*.gz` git-ignored

### Adversarial review of this session's code

A six-lens review, each finding independently checked by a sceptic told to
refute it, raised **39 findings of which 23 survived**. All 23 are fixed. The
ones worth naming:

- **`review_queue.py` pre-filled the approval column.** The engine's own answer
  was written into `approve_target_code` -- the same column `apply` reads as
  human consent -- so an unedited round trip would have migrated every
  single-target suggestion and written a named clinician's approval onto changes
  they never looked at, permanently, into append-only history. The proposal now
  sits in a read-only `engine_suggested_code` column and the approval column is
  exported blank on every row. A regression test applies an unedited export and
  asserts nothing moves.
- **`--dry-run` skipped both approval pre-checks**, so a preview could report
  `WOULD_APPLY` and exit 0 for rows the real run then rejected. It now takes the
  identical path and rolls back instead of committing, and writes to a separate
  `*_dryrun_outcome.csv` so it cannot clobber a real run's outcome.
- **The "association-extraction accuracy" metric was tautological.** It compared
  the database against the resolver -- our parser against our own code -- so it
  reported 100% whatever the parser did. Ground truth is now re-read from the
  release archive through a new `read_active_associations()`; the same fault in
  `tests/validation` is fixed the same way.
- **`make test-postgres` named the project's own database** in a command whose
  fixtures `DROP` every table. It now demands an explicit `DB_URL` and says why.

Plus: export refuses to silently clobber a queue a human has edited and
validates `--decisions` against the enum; `find_cli` prefers the `.cmd` launcher
on Windows; the uploader refuses paths containing characters `cmd.exe`
re-parses; `fetch_mimic_demo` deletes a refused download instead of leaving it
where the importer reads, and catches `http.client.HTTPException`;
`validate_releases` no longer creates a temp directory on `--help`, prints the
database it really used, and treats a half-specified release pair as an error;
`bootstrap.sh --help` no longer prints its own `set -euo pipefail` and is marked
executable in the index; `docs/database.md` and `docs/api.md` were stale about
`snomed_concept_term`.

### Bugs found and fixed earlier this session
- [x] `check_database.py --database-url` mutated the environment *after* the application had bound
      its engine, so the flag was silently ignored and the script cheerfully checked the wrong
      database. It now builds its own engine.
- [x] The SQLite foreign-key PRAGMA was registered on the **Engine class**, so it fired on every
      engine in the process — including a PostgreSQL one, which cannot parse `PRAGMA`. Now attached
      per engine instance, with regression cover.
- [x] `detect_version` for SNOMED matched only dates beginning `20`, which is the same
      hard-coded-century assumption the project exists to avoid. It now accepts any 8-digit run
      that parses as a real calendar date.
- [x] The CI guard against hard-coded release identifiers skipped every **string** constant, so it
      would have sailed past `VERSION = "2.82"` — the only form anyone actually writes. It now
      checks strings and numbers, and is a tested script rather than YAML-embedded Python.

---

## NOT DONE

These need licensed inputs or hardware this machine does not have. The code for all of them is
written and tested against synthetic data; only the real runs are outstanding.

- [x] ~~A real LOINC release imported~~ — **done**: 2.83 is the current release, with 2.82 and
      2.81 available for comparison. 112,405 concepts, 4,667 MapTo rows and 11,894 change rows
      imported in 3.8 seconds. Nothing in the code needed changing for the version bump, which was
      the point.
- [ ] **A real SNOMED CT International release imported.** Needs affiliate/member licensed
      access and `data/raw/snomed/SnomedCT_InternationalRF2_PRODUCTION_<date>T<time>Z.zip`.
      Partially mitigated: the LOINC Ontology RF2 extension (46,057 concepts) has been imported
      and exercises the concept, association, inactivation, description and language-refset
      parsing on genuine release files.
- [ ] **Snowstorm run end-to-end.** Docker is installed and working (PostgreSQL was brought up and
      used), but Snowstorm needs ~8 GB of free RAM and this machine had 1.4 GB free. Not a code
      problem — a hardware one. The client is verified against Snowstorm's own source instead.
- [ ] **LOINC uploaded into Snowstorm.** The script exists and its checks are tested; the upload
      itself needs both a LOINC ZIP and a running Snowstorm.
- [x] ~~The MIMIC-III audit report~~ — **done**, see the real-data section above.
- [x] ~~`data/reports/mimic_loinc_audit.csv`~~ — **produced**, plus `mimic_impact.md`.
- [x] ~~The official release-to-release LOINC validation figures~~ — **done** for both pairs.
- [ ] **The SNOMED half of the release-to-release validation.** Needs two SNOMED CT International
      RF2 releases. The LOINC Ontology extension exercised the parser and the resolver, but it
      contains no active REPLACED BY / SAME AS, so the auto-suggest branch is still synthetic-only.

---

## KNOWN LIMITATIONS

1. **LOINC is not pushed into Snowstorm by our own importer.** Our LOINC path parses the CSVs into
   PostgreSQL, which is what the resolver reads. Loading LOINC into Snowstorm for FHIR `$lookup`
   is a separate HAPI FHIR CLI step — now scripted, but still a separate step by design, because
   that is what Snowstorm's own documentation prescribes.

2. **Only the LOINC axes and status are diffed.** Change Snapshot properties outside the ten
   modelled fields are counted and reported as `unsupported_official_properties` rather than
   silently dropped — but they are not diffed.

3. **`MOVED TO` is never resolved.** Following it would mean reasoning about namespaces and
   extension modules that may not be imported. It abstains by design.

4. **Description parsing costs a couple of minutes and a few hundred MB of RAM** on a full
   International Edition (~1.4M description rows, ~2.8M language rows). It is on by default
   because readable reports are worth it; `--skip-descriptions` turns it off.

5. **Only US and GB English are read.** Other dialects in the language reference set are ignored.
   `--language-refsets` is exposed on the ingest function but not yet on the CLI.

6. **The SNOMED diff resolves newly-inactive concepts one at a time after a batch preload.** For a
   release pair with tens of thousands of inactivations, use `--limit`. Not a correctness issue.

7. **`map_correlation` is recorded, not enforced.** Nothing checks that a mapping labelled
   `EXACT_MATCH` really is one; that is a human judgement, and the field exists so a later audit
   can take it into account.

8. **No authentication on the API.** It is a local research service. Do not expose it.

9. **`scripts/upload_loinc_to_snowstorm.py` refuses LOINC paths containing `& | ^ < > ( ) " % !`**
   when the resolved launcher is a Windows `.cmd`, because `cmd.exe` re-parses the command line
   and would silently drop later flags. Move the archive, or pass a non-batch launcher.

10. **The MIMIC-III `LOINC_CODE` column is not ground truth**, and the code never treats it as such.
   Lin et al. found errors in ~4.5% of sampled voluntary LOINC mappings. Concretely: the demo file
   maps itemid 50960 (Magnesium, Blood) to `2601-3`, a substance-concentration code, where the
   mass-concentration code is the clinically expected one — this is a documented error in the MIMIC
   code repository, and it is sitting in the 585 mappings now loaded. Separately, `loinc_code` was
   **removed** from MIMIC-IV at v2.0 precisely because such errors were found, which is why
   MIMIC-III is used here and MIMIC-IV-on-FHIR is explicitly not used as a LOINC gold standard.

---

## NEXT THESIS STEP

In order, with nothing skipped:

1. **Get the licence.** SNOMED CT affiliate access through your institution or supervisor, and a
   free LOINC account. This is the only genuine blocker on the remaining checklist.
2. **Import both, for real.**
   ```powershell
   .\.venv\Scripts\python.exe scripts\import_loinc.py  --file data\raw\loinc\<file>.zip  --version <version>
   .\.venv\Scripts\python.exe scripts\import_snomed.py --file data\raw\snomed\<file>.zip --skip-snowstorm
   ```
3. **Run the MIMIC-III audit.** The 585 mappings are already loaded:
   ```powershell
   .\.venv\Scripts\python.exe scripts\audit_mappings.py --source-dataset MIMIC_III --report-name mimic_loinc_audit.csv
   ```
   Expect a non-trivial fraction to be stale. **That is the finding, not a bug** — it is the whole
   argument for version-aware mapping, measured on somebody else's real data.
4. **Run the strongest experiment.** Two LOINC releases and two SNOMED releases in
   `data/raw/validation/`, then `python scripts/validate_releases.py`. The four numbers to show
   your supervisor: missed official changes (target 0), inactive-detection recall,
   association-extraction accuracy, and unsafe automatic updates (target 0).
5. **Work the review queue once, by hand.** Export it, decide a dozen cases, apply them, and show
   the history that results. It demonstrates the safety contract better than any description of it.
6. **Then, and only then, add the next layer.** LOINC Ontology (SNOMED RF2 format, aligned to a
   specific LOINC and SNOMED release) for LOINC–SNOMED interoperability, and CompLOINC for
   OWL/graph artefacts. Both consume the release files this core already manages.
7. **After that, the microbiology mapper.** Gram-stain and culture concept extraction (MicrobEx is
   the rule-based baseline to beat), candidate retrieval over active-only SNOMED, and a selective
   predictor that abstains. It plugs into the existing `MANUAL_REVIEW` decision and the abstention
   metric — the safety contract does not need to change.

Deliberately still out of scope, exactly as specified for this milestone: raw text → LOINC/SNOMED
prediction, Jaro-Winkler, BioBERT, SapBERT, LLMs, fungal NLP, cross-hospital models, RAG, vector
databases.
