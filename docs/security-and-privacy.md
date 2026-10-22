# Security and Privacy

## 1. Data sensitivity

Bank annual reports and extracted financial data are treated as sensitive for this project. The prototype should minimize unnecessary copies and avoid external data transfer.

## 2. Local-first rules

- Process PDFs on the local machine.
- Do not use external LLM, OCR, analytics, telemetry, or document APIs in v1.
- Do not make network requests as part of extraction.
- Do not log full extracted tables or financial values unnecessarily.
- Do not commit PDFs, extracted financial records, SQLite databases, or Excel exports to Git.
- Keep data paths under the application's configured local data directory.
- Use safe generated storage names rather than trusting user-provided filenames as filesystem paths.

## 3. File handling

- Accept PDF files only.
- Handle invalid, encrypted, malformed, and unusually large PDFs gracefully.
- Do not execute content from PDFs.
- Preserve the original file and calculate a hash for identity/integrity.
- Avoid accidental overwrites when filenames repeat.
- Consider size/page limits for the prototype and make them configurable.

## 4. SQLite and exports

- Use parameterized SQL.
- Keep the database and exports in ignored local directories.
- Make clear where files are saved.
- Do not expose local file paths or document content beyond what is needed in the UI.
- The app is single-user and local in v1; do not imply it has authentication or multi-user access controls.

## 5. Limitations

Local processing does not by itself guarantee security. The operating-system account, backups, synchronization folders, and exported workbooks can still expose data. Before using real reports, confirm the team's handling requirements and select an approved storage location.

## 6. Future external services

Any proposal to send document content, financial values, or metadata to an external service requires explicit discussion and approval first. Explain what data would leave the machine, the provider's handling, cost, and alternatives before implementation.
