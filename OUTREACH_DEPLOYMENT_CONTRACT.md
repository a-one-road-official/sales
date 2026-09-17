# Outbound deployment contract

User instruction, 2026-09-17: API budget ceiling JPY 10,000; already exhausted.
Additional paid AI API budget: JPY 0. This is a user-declared budget constraint,
not a claim that the application has audited the billing account.

* Vertex, Google AI, paid inference, paid search and automatic paid-model fallbacks
  are prohibited in this release, including message generation.
* Research and extraction use Python, Beautiful Soup and Playwright. Drafts use
  verified first-party information and deterministic templates. Selection,
  form mapping, duplicate suppression, outcome verification and quality scoring
  use explicit Python rules.
* CI, deployment preflight, container build, container entrypoint and application
  startup enforce `cost_guard.py`. Paid-model client construction and generation
  are blocked before credentials or network calls. Environment and sheet settings
  cannot reopen the budget. No automatic monthly/daily budget reset exists.
* Updating a workflow must not itself launch a paid Cloud Run deployment.
  The paused repair deployment is manual-only. Existing cloud execution remains
  paused; no paid fallback is activated by form or research failures.
* Quality acceptance requires one fixed ten-company batch: at least seven full
  messages accepted by the recipients' forms, all ten outcomes read back from
  the existing ledger, and the full subject/body/outcome visible in Dashboard.
  Clicks, previews, CI success and email delivery are not form receipts.
* SSOT Status/owner/deadlines remain human-owned. Manufacturing is manual. Unknown
  historic submission outcomes stay blocked from duplicate outreach.

Any future paid AI use, including drafting-only use, requires a newly authorized
budget and a reviewed change to this contract and its code-level enforcement.
