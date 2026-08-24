# Methodology

A thesis-ready account of what this system is, why it is built this way, and how its correctness
is to be measured. Written to be adapted into a methods chapter; every claim here is either
implemented and tested, or explicitly marked as not yet run.

---

## 1. Problem

Clinical data is standardized by mapping a local term to a code in a published terminology:
`"HBsAg"` becomes a LOINC code, `"Staphylococcus aureus isolated"` becomes a SNOMED CT concept id.
The mapping is then stored and treated as fact.

Terminologies, however, move. LOINC publishes new releases in which codes change status —
`ACTIVE`, `TRIAL`, `DISCOURAGED`, `DEPRECATED` — and names an official successor for retired codes
in its `MapTo` table. SNOMED CT International publishes monthly; a concept that stops being valid
becomes `active = 0` and is linked to its successors through historical association reference
sets. Neither terminology deletes an identifier, which means a stale mapping never fails loudly.
It simply becomes quietly wrong.

A mapping table with no record of *which release it was made against* cannot answer the only
question that matters six months later: **is this still true?**

### The gap this addresses

The literature on automatic terminology mapping concentrates almost entirely on producing a
mapping. A 2026 systematic review of automatic clinical-terminology mapping (Anik, Ahmmed and
Mondal, *Array*) examined work published 2018–2025 under PRISMA and found that studies routinely
acknowledge limitations in general terms while under-reporting validation, expert consultation and
bias — concluding that stronger evaluation protocols and standardized reporting are needed. A
multi-institutional deployment in South Korea (Kang et al., *Int J Med Inform* 2026) reports that
resolving *inactive concept issues* required expert consultation on a case-by-case basis.

Maintenance, in other words, is acknowledged as a problem and handled manually. This project
treats it as the primary engineering object.

---

## 2. Position relative to prior work

| Work | What it establishes | What this system takes from it |
|---|---|---|
| Lin, Vreeman, McDonald & Huff, *AMIA* 2010 — voluntary LOINC mapping at three large institutions | Manual review of a one-tenth sample (884 of 9,027 tests) found 4 tests mapped to unrelated codes and 36 with at least one axis error; errors fell into four systematic categories | Existing mappings are **claims to be audited**, never gold labels. This is why MIMIC-III's `LOINC_CODE` is imported as data to check, not as truth |
| Hauser, Quine & Ryder, *JAMIA* 2018 — LabRS | Retrospective standardization of 1.25 × 10⁹ laboratory records (98.9%) across 27 facilities is feasible at scale, with expert review as quality control | Scale is not the obstacle; the human-in-the-loop step is what needs designing |
| LLM LOINC-mapping comparison, *Int J Med Inform* 2026 (ChatGPT-4.0, Gemini, Perplexity) | Of 75 clinical-chemistry and haematology test items, only 17 (22.7%) were mapped consistently by all three models *and* the experts; most mismatches were method-axis differences; expert validation remains essential | An LLM cannot own this decision. The current milestone therefore uses **only** official terminology fields and documented replacement relationships |
| Swaminathan et al., *JAMIA* 2024 — selective prediction | A model permitted to abstain outperforms both non-selective classifiers and structured proxy variables; routing "easy" charts to the model and "hard" charts to human abstractors raises efficiency without losing accuracy | Abstention is a first-class output (`MANUAL_REVIEW`), and the **abstention rate** is reported as a headline metric rather than hidden |
| Sung, Park, Jung & Kang, *JMIR Med Inform* 2023 — SNOMED CT mapping guideline (South Korea) | A nine-step guideline in which step 7 is *classify mapping correlations* | Each local mapping records a `map_correlation` (exact / broader / narrower / partial / unspecified), so a later audit can distinguish a genuinely equivalent map from a deliberately broader one |
| Chapman et al., *JAMIA Open* 2022 — MicrobEx | A rule-based microbiology concept extractor reaching F1 > 0.95 on external validation | The baseline to beat in the *next* thesis stage; deliberately out of scope here |

**The contribution claimed here** is not a better mapper. It is a *version-aware, auditable
maintenance layer* underneath any mapper: it records the release each mapping was made against,
detects when that mapping has gone stale, resolves the terminology's own official successor when
one exists, abstains when it does not, and preserves the complete provenance of every change.

---

## 3. System design

### 3.1 Sources of truth

Only published terminology artefacts are consulted. Nothing is inferred.

| Terminology | Files parsed | What they license |
|---|---|---|
| LOINC | `Loinc.csv` | code status and the six axes |
| | `MapTo.csv` | official replacement candidates for retired codes |
| | `LoincChangeSnapshot.csv` | the vendor's own release-to-release change log |
| SNOMED CT | `sct2_Concept_Snapshot` | `active` flag per concept |
| | association reference sets | `REPLACED BY`, `SAME AS`, `POSSIBLY EQUIVALENT TO`, `WAS A`, `ALTERNATIVE`, `MOVED TO`, … |
| | Concept Inactivation Indicator refset | *why* a concept was inactivated |
| | description file + language reference set | fully specified name and preferred term |

Every parsed row is stamped with its release version, so two releases coexist in the database and
can be diffed offline.

### 3.2 Why not delegate to a terminology server

Snowstorm (SNOMED International's official open-source server) is used as infrastructure for term
search and the FHIR endpoints, but **not** for the version-aware logic, for three reasons:

1. its branch state is mutable, so a verdict computed from it is not reproducible from files;
2. it holds one imported state per branch, so it cannot answer "what changed between release A and
   release B";
3. it requires ~8 GB of RAM, which would otherwise make every audit conditional on a large stack
   being available.

Consequently every verdict in this system is a pure function of the release files plus the stored
mapping — which is what makes a published number re-derivable by a third party.

### 3.3 Decision tables

The engine emits exactly five decisions: `KEEP`, `KEEP_WITH_WARNING`, `SUGGEST_REPLACEMENT`,
`MANUAL_REVIEW`, `UNKNOWN_CODE`.

**LOINC.** `ACTIVE` → keep. `TRIAL` → keep with a warning (never silently replaced). `DISCOURAGED`
or `DEPRECATED` → consult `MapTo`: exactly one usable target yields `SUGGEST_REPLACEMENT`; more
than one yields `MANUAL_REVIEW` with reason `MULTIPLE_REPLACEMENTS`, because which one applies
depends on local test context; none yields `MANUAL_REVIEW` with `NO_OFFICIAL_REPLACEMENT`. A code
absent from the current release is `UNKNOWN_CODE` — never guessed.

A code that remains `ACTIVE` while its component, name, property, system, scale or method changed
is **not** moved. It returns `KEEP` with `metadata_changed = true` and a field-level diff, because
the code is still correct and only its description drifted.

**SNOMED CT.** `active = 1` → keep. `active = 0` → consult the historical associations. Only a
*single* `REPLACED BY` or `SAME AS` may be suggested. `POSSIBLY EQUIVALENT TO`, `WAS A` and
`ALTERNATIVE` abstain **even when they are the only row**, because none of them asserts
equivalence. `MOVED TO` abstains because a namespace move is not a clinical replacement. More than
one active association abstains.

**Chain following.** Where a successor is itself retired, the chain is followed — but only along
semantically strong links (`MapTo`; `REPLACED BY` / `SAME AS`), and with three guards: a visited
set (a cycle returns `REPLACEMENT_CHAIN_CYCLE`), a configurable depth limit
(`REPLACEMENT_CHAIN_TOO_DEEP`), and fork detection (`MULTIPLE_REPLACEMENTS`). Every terminal
candidate must itself be valid in the current release; an obsolete target is never presented as
safe.

### 3.4 The safety contract

The property the system is designed around, and tests as a mandatory suite:

> An audit may **suggest**. Only a named human may **commit**.

Mechanically: `run_audit` writes verdicts and may flag mappings `NEEDS_REVIEW`, but no code path in
the audit touches a target code. The single mutation path is
`mapping_service.approve_replacement`, which requires an attributable reviewer, requires the target
to be one the engine actually suggested (or an explicit override), requires the target to be valid
in the *current* release, and writes an append-only `mapping_revision` carrying the old code, the
release it was valid in, the new code, the new release, the reviewer and the timestamp. Nothing is
ever deleted or overwritten.

The human half of the loop is a CSV round trip rather than an interactive prompt, so a decision is
a file that a named person edited and that survives alongside the audit it came from.

### 3.5 Reproducibility mechanisms

- every release carries a SHA-256, so identity is content rather than filename, and re-importing
  the same archive is a no-op;
- every audit run stamps the LOINC and SNOMED versions in force;
- every audit result records the release it was judged against, separately from the release the
  mapping was made against;
- no release identifier appears in executable code — a CI job fails the build if one does.

---

## 4. Data

| Dataset | Role | Access |
|---|---|---|
| LOINC Complete, two releases | primary validation input | free account |
| SNOMED CT International RF2, two releases | primary validation input | affiliate/member licence |
| MIMIC-III demo `D_LABITEMS` | real-world audit target | **open**, ODbL v1.0, no credentialing |

No terminology content is redistributed by the repository; archives are git-ignored and a CI job
enforces it.

### 4.1 Why MIMIC-III and not MIMIC-IV

MIMIC-III's `D_LABITEMS` carries a `LOINC_CODE` column assigned by people years ago — exactly the
kind of historical mapping set this system exists to audit. The open-access demo ships the
dictionary table *complete* (753 rows; the event tables are the ones subset to 100 patients), so
the full lab-to-LOINC mapping can be studied without credentialing. 585 rows carry a code, over
575 distinct codes.

MIMIC-IV **removed** `loinc_code` at v2.0, stating that errors were found in its values and that
the column would be developed collaboratively in the MIMIC code repository instead. That removal is
itself evidence for the thesis's premise, and it is why MIMIC-IV-on-FHIR is *not* used as a LOINC
gold standard here: it primarily retains the original MIMIC terminology for laboratory
observations, so `predicted == MIMIC-IV-FHIR` would not be a valid comparison.

A concrete, documented example survives in the demo file: itemid 50960 (Magnesium, Blood) is mapped
to a substance-concentration code where the mass-concentration code is the clinically expected one.
That single row is a useful illustration of why these mappings are treated as claims.

---

## 5. Evaluation design

Three experiments. None uses a hand-written expectation as ground truth; all three compare the
engine against the terminology publishers' own files.

### Experiment 1 — release-to-release change detection (LOINC)

**Input.** Two official LOINC releases, A (older) and B (newer).
**Procedure.** Compute a diff between A and B from the stored concept rows, over ten fields
(status, long common name, short name, and the six axes plus class). Compare that diff against
`LoincChangeSnapshot.csv` shipped *inside* release B, restricted to the properties we model, and
excluding codes newly created in B (a new code has no prior state to diff).
**Metrics.** `official_changes`, `detected_changes`, `matched_changes`, `missed_changes`,
`unexpected_changes`.
**Target.** `missed_changes = 0`.
**Why it is meaningful.** The comparison is against the vendor's own change log. It cannot be
satisfied by agreeing with the author's expectations.

### Experiment 2 — simulated historical mappings (LOINC)

**Input.** The same two releases.
**Procedure.** Identify every code that went `ACTIVE` in A to `DISCOURAGED`/`DEPRECATED` in B.
Create one local mapping per code, recorded as having been made against release A. Run the auditor
against release B.
**Metrics.** (a) proportion of those mappings still reported valid — should be zero; (b) number of
suggested replacements whose first hop is not an official `MapTo` row of release B — should be
zero.
**Target.** 100% of status changes detected; 0 invented replacements.

### Experiment 3 — inactivation and successor extraction (SNOMED CT)

**Input.** Two official RF2 releases.
**Procedure.** Identify every concept that went `active = 1` to `active = 0`. For each, compare the
engine's extracted inactivation reason and historical associations against the newer release's own
Concept Inactivation Indicator refset and association refsets, row for row.
**Metrics.** inactive-detection recall; association-extraction accuracy; the distribution of
decisions; `unsafe_auto_update` count.
**Target.** recall = 100%, association accuracy = 100%, `unsafe_auto_update` = 0.

`unsafe_auto_update` is zero by construction — nothing migrates without an approval call — but it
is *measured and reported* rather than asserted, because a claim about safety that is not
instrumented is not evidence.

### Experiment 4 — real-world audit (MIMIC-III)

**Input.** 585 historical `itemid → LOINC` mappings and one current LOINC release.
**Procedure.** Audit them.
**Reported.** Counts by status (`ACTIVE`, `TRIAL`, `DISCOURAGED`, `DEPRECATED`, unknown), by
decision, single vs multiple vs no official replacement, and the abstention rate. Exported as
`data/reports/mimic_loinc_audit.csv` with one row per mapping.
**Interpretation.** A non-trivial stale fraction is the *finding*, not a defect: it is the
argument for version-aware mapping, measured on a third party's real data rather than on a
constructed example. Nothing here claims MIMIC's codes were right when assigned.

### Supporting evidence

- **Unit level.** Every branch of both decision tables is asserted individually against synthetic
  releases built in the real file formats (invented codes; no licensed content in the repository).
- **Safety.** A mandatory suite asserts that every ambiguous case abstains and that no code path
  changes a mapping without a named reviewer.
- **Provenance.** A test drives `A → B → C` and asserts both hops survive with their release
  versions, reviewers and timestamps.
- **Idempotency.** Re-importing an archive — including under a different filename — creates no
  second release and no duplicate rows.
- **Performance.** Auditing 10,000 mappings is asserted to issue fewer than 100 SQL statements, so
  the batch design is verified rather than assumed.
- **Negative control.** The validation runner is itself tested against a deliberately doctored
  release pair that declares a change the concept table does not contain; the run must **fail**.
  A gate that cannot fail is not a gate.

---

## 6. Threats to validity

1. **Ground truth is the publisher's, not clinical reality.** Experiments 1–3 measure agreement
   with LOINC's and SNOMED's own files. If the publisher's `MapTo` entry is clinically
   inappropriate for a given local test, this system will faithfully reproduce that. It is a
   maintenance layer, not a clinical adjudicator — which is precisely why a suggestion is never
   committed automatically.
2. **MIMIC-III mappings are of unknown quality.** Treating them as an audit target rather than a
   gold standard is deliberate; no accuracy claim is made about them.
3. **Coverage of change types is partial.** Only the ten modelled LOINC fields are diffed. Change
   Snapshot properties outside that set are counted and reported, not silently dropped, but they
   are not evaluated.
4. **Single-edition scope.** The International Edition only. National extensions and their
   namespaces are out of scope, which is also why `MOVED TO` always abstains.
5. **Release-pair sensitivity.** Experiments 1–3 measure whatever changed between the two chosen
   releases. A narrow pair may contain few transitions; the runner reports the counts so that a
   vacuous result is visible rather than reported as success.
6. **The abstention rate is a design output, not a quality score.** A high rate means the
   terminology was ambiguous, not that the engine performed badly. It should be read together with
   the reason distribution.

---

## 7. Reproducibility statement

- Source, tests and CI configuration are in the repository; no terminology content is.
- `python scripts/demo_end_to_end.py` reproduces the entire pipeline on synthetic releases in a
  throwaway database, with no licensed input and nothing but Python installed.
- `python scripts/validate_releases.py` reproduces Experiments 1–3 from two release pairs and
  writes `data/reports/validation_report.md`, exiting non-zero if any target is missed.
- `python scripts/fetch_mimic_demo.py` obtains the open-access MIMIC-III dictionary and verifies it
  against both a pinned SHA-256 and PhysioNet's published manifest.
- The test suite runs on SQLite and on PostgreSQL 16, on Python 3.11 and 3.12, in CI.

### Current state of execution

At the time of writing, Experiments 1–3 have been executed **only against synthetic release
pairs**, where every target is met, including the negative control. Experiment 4's data half is
complete (585 mappings loaded and verified) but the audit awaits a LOINC release. Running all four
against official archives is the immediate next step and requires only licence access, not further
code. `STATUS.md` records this distinction line by line; no result in this document should be read
as an official-data result until that file says so.
