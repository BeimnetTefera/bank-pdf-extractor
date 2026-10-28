You are helping me test the "Bank PDF Extractor" web app open in this browser tab. I am a bank-data reviewer. Do a first review of one annual report; I will check your work afterwards.

Rules
* Work only in this app. Do not open other websites.
* Never invent a number. Every value must be printed on a PDF page in the app, OR be calculated only from printed figures (show the sum in the note, e.g. "11,689,003 + 3,477,954 + 1,197,050 = 16,364,007, p47").
* Before anything else, set "Reviewer" in the sidebar to "Claude for [MY NAME]".
* Use the CONSOLIDATED statements (group) unless the metric only exists in the separate statements; say which one you used in every note.
* Check each metric's definition on the Templates page before deciding.
* Units: values in Birr'000 stay as printed with unit Birr'000 (or convert to full Birr if the app asks). Percentages become fractions (20.9% → 0.209). Record expenses, tax and dividends as positive amounts.
* If a figure is a close substitute (e.g. "deposit accounts" for "deposit account holders", "return on average equity" for ROE), approve it but write "PROXY:" in the note.
* If unsure, leave it open and explain why in the summary. Don't stop to ask me mid-run; make a call, note it, continue.
* If the app or browser tools stop responding, wait and retry twice; if still broken, stop and tell me the last completed row so I can restart.

Steps
1. Open "Extract & Review". Use [FILE NAME].pdf (upload it if it is not loaded). Choose the bank from the Bank list, set Report year to 2025/26 (format YYYY/YY), press "Save report". Approve/Correct only work after saving.
2. Go through the metric table top to bottom. For each metric compare the suggested figure with the highlighted box: right row, right year column (2026 = current year), unit matches the page heading.
   * Correct → tick "I have checked the warning(s)" if shown, then Approve.
   * Another figure is right → use All Figures search or "Mark a number" (zoom in if the box misses), assign it to the metric, give a short reason, approve.
   * Not in the report → "Not reported" with the reason (press Ctrl+Enter in the reason box before recording).
   * Any doubt → leave open.
3. Do not press "Finish extraction" and do not export.

Summary
A table with one row per metric: metric | value | unit | page | printed label | consolidated/separate | action (approved / corrected / calculated / proxy / not reported / left open) | what to double-check.
Then list:
* figures in the report that contradict each other (e.g. a ratio that doesn't match its parts);
* problems in the app itself (wrong suggestions, wrong year/unit detection, confusing screens, errors, crashes).