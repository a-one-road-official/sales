# Raw-material pipeline — normal ChatGPT Scheduled Tasks

## Purpose and boundaries
Python extracts public exhibition listings into the existing private SSOT workbook's
single visible `原料_raw_material` tab (sheetId 1909186001). Normal ChatGPT Scheduled
Tasks read the material, inspect the company and Japan application, and write a
company-specific draft into that SAME record. No model API, Work, Codex automation,
local-model fallback, public data artifact or cloud deployment is used by this intake.

This is the intake/refinement slice. `EMAIL_READY` means an internally prepared
email, not send permission or delivery. Every record starts `NOT_AUTHORIZED` for
sending. Existing manual/industrial/SSOT and previously contacted-account protections
remain. The old sender and CRM jobs are not modified by this feature.

## Human-visible store
Workbook ID: 1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo
Tab: 原料_raw_material; row 6 is the header; records begin on row 7.
- B2: approved source URL; E2: next page; H2: START/STOP for this producer only.
- B3: JSON array of explicitly excluded company names, initially empty.
- H3: pages per run (1–4), initially 2.
- E3: last successful page; E4: last error. Cursor advances only after readback.
- B4: editable prompt Doc ID; B5: target gate Doc ID.
- A:I: append-only material owned by the producer. J:X: refinement output owned
  by the ChatGPT task. Y:Z: future sender-owned status/receipt, protected here.

MAKTEK 2026 is an upcoming event. Its listing establishes scheduled exhibition
participation only; it is not proof of a completed past event, payment capacity,
actual HQ location or authorized first-contact recipient. Listing country is a
candidate field. Japanese text is not silently added as an eligibility condition.
The new relaxed raw intake does not overwrite the formal Gate Doc or certify
G1/G3/G6/GX. Explicit policy conflicts must be recorded, not silently reconciled.

## Python producer
`.github/workflows/raw-material-intake.yml` invokes `python -m refinery.worker`
on the standard public GitHub runner at :13 and :43. The first bounded run can
be explicitly requested by changing `refinery/.run` on main. Ordinary code edits
and this document do not start sends or deployment. No artifacts are uploaded.
The run checks robots.txt, performs bounded same-source GETs, and parses two
pages using BeautifulSoup. It excludes company-name/known-domain duplicates
against both existing sales workbooks and the raw tab. A source HTTP/robots/HTML
failure preserves the cursor and all previous material. Parse failures are errors,
not zero-success runs. One writer owns this tab; do not add a competing cron.

## Normal ChatGPT refinery
Read the current human-editable Docs by IDs in B4/B5 and the actual source pages.
New company => new company-specific reasoning and copy using the real prompt.
Do not reuse another company's Japan argument or reconstruct paragraphs in Python.
Read a small bounded RAW cohort; record its raw_id before writes. Skip rows another
run has claimed. Recheck the same raw_id immediately before narrow J:X writes.
Use one schedule/owner for refinement; a stale PROCESSING row is reviewed and
reconciled, never blindly freed while another task may still be active.

Store actual prompt_doc_id, prompt_revision, prompt_sha256, generated_at,
input_sha256, email_sha256 and an honest generation_run_id. Never invent a provider
model response ID. `prompt_hash()` normalizes only BOM and line endings. The input
hash is SHA256 of the sorted UTF-8 JSON object of A:I header keys/values with
ensure_ascii=False. The email hash is SHA256(subject + '\n' + body).
`validate_refinement()` tests integrity only. Meaning, source correctness and AI
provenance are not established by a self-written boolean or hash.

GO records require official and Japan evidence, each with URL, exact excerpt and
a company-specific relevance explanation. NO_GO records keep an explicit reason.
Unresolved source/prompt issues may be HOLD; this is an operational state and not
an invented third formal Gate decision. Do not count ROW, RAW or EMAIL_READY as
new qualified leads or SENT. Preserve raw source values and previous sales history.

## Deployment acceptance
1. Focused offline tests pass; full repo CI passes against this PR.
2. New visible tab and controls are read back.
3. Real Python runner appends raw rows and verifies them; no existing sales status
   writes, no customer sends and no inference calls.
4. A real company is refined in this chat and its draft is saved/read back.
5. A normal Scheduled Task repeats the operation at 06:00 JST. Record this separately
   from the interactive test: creation of a task does not prove unattended execution.
6. Sending/reply interpretation integration is a subsequent tested slice. Do not
   claim full continuous personalized delivery based on these preparation tests.
