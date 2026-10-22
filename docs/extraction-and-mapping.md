# Extraction, Metric Mapping, and Normalization

## 1. Principle

The extractor finds evidence; the mapper interprets labels using explicit configuration; the reviewer decides whether the candidate is acceptable. These responsibilities must not be collapsed into one opaque function.

## 2. Initial extraction strategy

1. Open the PDF locally.
2. Inspect page count and text availability.
3. Extract text and table candidates with page-level context.
4. Preserve source order, row labels, column headings, and nearby unit notes when available.
5. Keep multiple candidate occurrences.
6. Return structured records even when a numeric value cannot be parsed; preserve raw text and flag the parse issue.

Start with text-based PDFs. If a report has little or no extractable text, show a clear “may be scanned / OCR not implemented” status. Do not silently pretend extraction succeeded.

## 3. Candidate selection

Candidate ranking may use evidence such as:
- Exact configured label alias.
- Relevant statement section.
- Correct reporting-period column.
- Explicit unit context.
- Whether the value is a total or a component.
- Whether the same candidate appears in a summary/highlights section.

Ranking is advisory. It must not automatically approve a candidate. Preserve alternatives and explain why a candidate was ranked higher when possible.

## 4. Metric configuration

Create a human-readable `config/metrics.yaml` containing only confirmed metrics. Example structure (illustrative; do not treat as approved business definitions):

```yaml
metrics:
  - id: total_assets
    display_name: Total assets
    definition: "Definition to be confirmed by the team."
    expected_unit: ETB
    active: true
```

Use `config/mappings.yaml` for explicit raw-label aliases and component relationships. Example labels are placeholders, not an exhaustive bank taxonomy.

```yaml
mappings:
  - metric_id: total_assets
    aliases:
      - "Total assets"
      - "Total Assets"
```

Do not invent a comprehensive list of bank metrics or deposit component rules. Populate them from the existing metric list and decisions confirmed by the user/team.

## 5. Conservative matching

- Exact normalized alias matches may map automatically as candidate suggestions.
- Fuzzy matches can be shown as suggestions but must be flagged if ambiguous.
- Never map a label to multiple competing metrics without a clear component rule.
- A reported total and its components must not be added together.
- Unknown labels must remain unmapped and visible for review.
- Record the mapping rule or matching basis used.

## 6. Numeric parsing and unit normalization

Handle only well-defined formats in the first iteration:
- commas as thousands separators when context supports it
- decimal points
- parentheses for negative numbers where financial statement conventions support it
- explicit currency/unit headings such as ETB, ETB '000, or ETB million

Preserve the original printed string. If separators or units are ambiguous, do not guess; flag the candidate.

Keep raw and normalized values separately. A normalized value should have a corresponding normalized unit and an explicit conversion basis.

## 7. Duplicate and conflicting figures

The same metric may appear in:
- audited statements
- notes to the statements
- highlights and management discussion
- charts and narrative text
- current-year and comparative-year columns

Store plausible occurrences as separate candidates with section and page evidence. A source-priority policy can rank candidates, but conflicts must be visible. Exact audited statements are often preferred for statement totals, while notes may provide detailed breakdowns; this is a policy to confirm and apply consistently, not an excuse to ignore discrepancies.

A rounded figure can corroborate an exact figure within expected rounding, but should not replace the exact value when an exact audited value is available.

## 8. Prior-year comparison

If prior-year data is available, show it as a comparison signal. A large change should create a warning, not an automatic correction. Changes may be real, affected by restatement, or caused by differences in reporting scope.

## 9. Future OCR or AI

Do not add OCR, LLMs, external APIs, or model-based extraction in the initial implementation without approval. First test whether local text/table parsing is sufficient for representative PDFs. If not, document the observed failure and compare local OCR alternatives, cost, privacy, and accuracy before choosing.
