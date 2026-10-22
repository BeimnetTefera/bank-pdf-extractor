# Implementation Plan

Claude Code must follow these phases in order. Stop after each phase, summarize what changed, report tests, and wait for user review before moving on.

## Phase 0 — Inspect and align
**Goal:** Understand the current repository and avoid overwriting existing work.

Tasks:
- Inspect current files and Git status.
- Read all project documents.
- Confirm Python version and environment.
- Identify unresolved decisions that block implementation.
- Do not write application code until the user agrees the plan is understood.

Acceptance:
- A concise implementation checklist and list of blocking questions are presented.
- Existing user files remain untouched.

## Phase 1 — Project skeleton and configuration
**Goal:** Establish a runnable Python project and agreed metric configuration format.

Tasks:
- Create the planned folder structure.
- Add minimal `app.py` placeholder and package/module structure.
- Add `config/metrics.yaml` and `config/mappings.yaml` with schema/example placeholders only; do not invent the team's real metric list.
- Add `.gitignore`, `requirements.txt`, and a short setup section to README.
- Add a configuration loader and tests.

Acceptance:
- Dependencies install in the agreed environment.
- Config loads and invalid config gives actionable errors.
- `pytest` passes.
- No real report data is committed.

## Phase 2 — One-PDF extraction experiment
**Goal:** Prove local extraction and source references on a representative PDF before building a complex UI.

Tasks:
- Implement document validation and hashing.
- Extract page text and initial candidate rows using local PDF libraries.
- Preserve page index, context, and raw text.
- Add synthetic tests and a documented manual test procedure.
- Test on one user-approved real PDF locally, without committing it.

Acceptance:
- Can identify text availability and page count.
- Candidate evidence includes reliable PDF page index.
- Empty/scanned-like PDFs are reported clearly.
- Candidate results are described as unverified.

## Phase 3 — Mapping and normalization
**Goal:** Turn raw candidates into metric-linked candidates using explicit rules.

Tasks:
- Implement exact alias matching first.
- Add safe numeric parsing and explicit unit normalization.
- Retain unknown/ambiguous labels for review.
- Add duplicate and conflict warnings.
- Do not infer unresolved business definitions.

Acceptance:
- Unit tests cover aliases, ambiguous matches, unknown labels, numeric parsing, units, and duplicate candidates.
- Original raw evidence is preserved.

## Phase 4 — Persistence and review model
**Goal:** Save documents, candidates, validation findings, and decisions.

Tasks:
- Create SQLite schema and repository functions.
- Implement review states and append-only decision history.
- Require reasons for correction/rejection.
- Ensure corrected values do not overwrite the original candidate.
- Add tests for restart persistence and state transitions.

Acceptance:
- Records persist across app restarts.
- Review history is retained.
- Rejected/unresolved candidates cannot leak into approved-only results.

## Phase 5 — Streamlit review interface
**Goal:** Implement the usable interface resembling the approved mockup.

Tasks:
- Add PDF upload and bank/report period metadata.
- Add metric selection.
- Add candidate list, PDF page viewing, evidence details, and validation log.
- Add approve/reject/correct actions.
- Show statuses clearly.
- Keep UI code separate from domain logic.

Acceptance:
- Analyst can complete the workflow without editing Python code for already configured metrics.
- Source page can be inspected from the candidate.
- Review actions persist.
- Errors and ambiguous results are understandable.

## Phase 6 — Excel export
**Goal:** Export reviewed results with provenance.

Tasks:
- Implement approved-only workbook.
- Add full-review workbook if feasible without delaying the core workflow.
- Include stable columns and metadata.
- Add tests for workbook sheets, statuses, evidence, and precision.

Acceptance:
- Approved-only export contains only approved/currently accepted values.
- Each output row can be traced to its source document and page.
- Export errors are shown clearly.

## Phase 7 — Validation against representative reports
**Goal:** Measure real usefulness and identify failure cases.

Tasks:
- Choose representative reports/pages with user approval.
- Manually establish expected values and source locations.
- Compare extracted candidates and mappings with expected results.
- Measure candidate coverage, exact-value accuracy, source accuracy, mapping accuracy, and review time.
- Document failures and recommended next work.

Acceptance:
- Results include the test-set size and observed metrics.
- No unsupported accuracy claims.
- OCR, alternative parsers, or other additions are recommended only when observed failures justify them.

## Phase 8 — Demo and decision
**Goal:** Decide whether to keep the script-like prototype, improve the local app, or propose a broader internal product.

Tasks:
- Document setup and demo steps.
- Summarize demonstrated capabilities, known limitations, security considerations, and effort.
- Compare the observed workflow against requirements.
- Present a go/no-go recommendation for the next phase.

Acceptance:
- A reviewer can reproduce the demo.
- Known limitations and remaining risks are explicit.
- No production-readiness claim is made without the corresponding controls and testing.

## Working rule

Do not jump phases. If a later-phase need appears early, note it and implement only what is necessary for the current phase unless the user approves a change.
