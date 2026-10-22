# Review Workflow and Excel Export

## 1. Review UI concept

The interface should roughly follow the approved mockup (`docs/ui-mockup.png`). Its figures are example data only. Where it differs from confirmed decisions, the decisions win: one PDF at a time (D-003), local save only (D-006), reviewer name instead of login (D-016).
- Sidebar/navigation with Home, Extract & Review, Bank Reports, Templates/Metric Configuration, and Settings as appropriate.
- Bank and reporting period selector.
- PDF viewer with page navigation.
- Candidate metrics table with value, unit, and status.
- Selected candidate details with original label, source section, page, extraction method, other candidates, and validation findings.
- Approve, Reject, and Correct actions.
- Export controls.

Keep the first UI simple and usable. Do not build every navigation item as a separate complex subsystem; placeholders are acceptable until functionality exists.

## 2. Review actions

### Approve
- Requires a candidate and sufficient source evidence.
- Records reviewer label and timestamp.
- If significant ambiguity remains, the UI should clearly show it and require explicit acknowledgement or resolution before approval.

### Reject
- Requires a reason.
- Does not delete the candidate.

### Correct
- Requires replacement value, unit when applicable, and a reason.
- Preserves original raw candidate unchanged.
- Records the corrected value as a review decision.
- The corrected value must be clearly identified as human-corrected, not automatically extracted.

### Reopen/revise
If a previous decision needs to change, append a new review event rather than erasing history. The app may derive the current status from the latest valid event.

## 3. Statuses in the UI

Use clear labels:
- Needs review
- Flagged
- Approved
- Rejected
- Corrected
- Not found

Do not use a numeric “confidence” score as if it proves financial correctness. If a ranking score is later added, label it as a technical heuristic and explain its limitations.

## 4. PDF evidence

- Display the selected source page or provide a reliable way to open it.
- Clearly distinguish zero-based internal page index from the one-based PDF page number shown to the user.
- Show printed page number separately only when known.
- If text coordinates permit highlighting, it is a useful enhancement but not required for the first usable prototype.
- Do not fabricate source snippets or page references.

## 5. Export formats

### Approved-only workbook
Recommended sheets:
1. `Approved Data`: one row per approved metric/reporting period.
2. `Source Evidence`: source document, hash, PDF page, printed page if known, original label/value/unit, section/context, extraction method, and review decision.
3. `Export Metadata`: export timestamp and application version.

### Full-review workbook (optional in first version)
Can include all candidates, statuses, warnings, and review notes. It must be clearly distinguished from the approved-only output.

## 6. Export safety
- Exports must be generated from persisted records, not temporary UI state alone.
- Do not include rejected or unresolved records in the approved-only sheet.
- Do not overwrite the historical dataset.
- Use deterministic columns and preserve numeric precision.
- Show a success message only after the workbook has been created successfully.
