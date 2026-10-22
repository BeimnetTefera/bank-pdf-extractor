# Instructions for Claude Code

## Mission

Implement the Bank PDF Extractor prototype according to this repository's Markdown specifications. The user and assistant will make product and architecture decisions together. Do not independently expand the scope or make consequential assumptions.

## Required reading order

Before writing implementation code, read:

1. `README.md`
2. `docs/requirements.md`
3. `docs/architecture.md`
4. `docs/data-model.md`
5. `docs/extraction-and-mapping.md`
6. `docs/review-and-export.md`
7. `docs/security-and-privacy.md`
8. `docs/testing-and-acceptance.md`
9. `docs/implementation-plan.md`
10. `docs/decisions.md`

## How to work

- Follow `docs/implementation-plan.md` in order. Work only on the current phase unless the user explicitly authorizes moving on.
- At the start of a phase, briefly state the goal, files to create/change, and acceptance checks. Then implement that phase.
- Prefer small, reviewable changes over a large code dump.
- Explain meaningful architectural decisions briefly in plain English. Do not overload the user with unrelated detail.
- Ask the user before making a decision that changes scope, introduces an external service, sends documents/data outside the machine, changes the storage model, or adds a major dependency.
- If a requirement is ambiguous, identify the ambiguity and propose a default; do not silently invent business definitions.
- Do not invent bank metrics, values, page references, or verified outcomes.
- Use synthetic fixtures for tests. Do not include real bank PDFs or sensitive extracted values in source control.
- Do not claim extraction is accurate until tested against manually verified examples.
- Keep metric definitions and label mappings in configuration, not hard-coded throughout application logic.
- Keep UI code thin. Put extraction, mapping, validation, persistence, and export logic in separate modules.
- Use type hints, clear names, small functions, and actionable error messages.
- Avoid unnecessary abstractions, microservices, background queues, Docker, cloud infrastructure, or frontend frameworks for this local prototype.
- Do not add OCR, LLM calls, external APIs, or telemetry without discussing it with the user first.
- Never silently guess when candidates conflict. Flag the ambiguity for human review.
- Preserve the original extracted candidate when a reviewer corrects it. Store the correction and decision as a separate, auditable record.
- Do not overwrite the existing historical dataset. Excel export is a separate deliverable.
- Do not store uploaded PDFs, SQLite files, exports, environment files, or secrets in Git.

## Development quality

- Add or update tests with each functional change.
- Run relevant tests after each phase and report the actual result.
- Keep dependencies in `requirements.txt`; do not add dependencies without a clear reason.
- Provide a concise summary of changed files, tests run, known limitations, and the next phase.
- If the current repository already contains code, inspect it before changing files. Do not overwrite existing work without asking.
- Never delete or rewrite user files as a shortcut.
- For UI work, match the approved concept: bank/report selection, metric selection, PDF evidence viewer, candidate table, review actions, validation log, and approved-only Excel export. A simple usable UI is better than decorative complexity.

## Definition of done

A phase is done only when its acceptance criteria in `docs/implementation-plan.md` are met, relevant tests pass, and known limitations are documented. Stop at the end of each phase and let the user review before continuing.
