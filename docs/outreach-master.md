# Shared outreach master

The goal is one order by 2026-10-02 and continued sales thereafter. The approved offer is one year of paid Japan market development, with company-specific introduction and expansion support, no fee amounts, no retreat copy and no hypothesis-permission CTA.

## Generation and handoff

The hourly ChatGPT automation reads `prompts/japan_outreach_master.txt` afresh for each company. It researches the official product, narrow Japanese buyer/workflow, Japan activity, and one primary-source Japan fact. It writes an independently generated record to `data/outreach_queue/<company-id-suffix>.json` using the schema enforced by `outreach_queue.py`. This is a generic queue: adding a company does not add Python branches or a company-specific email template.

Records contain the full email, current master SHA256, UTC generation time, company identity, source evidence, maturity searches, fact locator/excerpt, buyer/workflow and completed review checks. The agent must actually inspect evidence before marking checks true. The sender rejects records older than 48 hours, changed master revisions, mismatched companies/domains, unreviewed facts and malformed copy. The body must be 110–120 words with the exact approved link/signature. Never refresh a timestamp without fresh review/generation.

`OUTREACH_PREPARED_DRAFTS_ONLY=TRUE` uses this handoff; missing records fail preparation. No local-model or paid-API fallback occurs. Local Qwen generation remains an experimental module and is not the active production copy source: real verification found broad segments despite repeated repairs.

## Execution

The serialized `sacrifice_canary.yml` workflow re-reads official sites/contact pages, prefers a published commercial email when verified, and otherwise uses eligible public forms. It still requires contact policy, current workbook eligibility, all-history duplicate protection, and a durable reservation immediately before external action. Manufacturing/manual/SSOT protections remain. Preparation failures never consume recipients. Unknown past submissions must not be retried as new contacts.

The public standard runner executes locally with paid AI, Vertex and paid cloud flags false. The authenticated loopback exception only permits the exact sacrifice endpoint in the expected repository/workflow outside Cloud Run. All other endpoints retain the cloud-budget quarantine.

## Current evidence (2026-09-18)

- PR44: shared master and local inference prototype.
- PR45: concrete buyer/workflow checks and scoped free-runner middleware; 335 tests passed.
- PR46: scheduled-agent handoff, freshness/provenance checks, first-party contact enrichment and verified email preference; CI passed.
- Pilot 35294497856: budget-quarantine acknowledgement, zero company processing/submissions.
- Pilot 35295267817: Searchanise and Boost Commerce both failed DDGS discovery before reservation; zero confirmed submissions, both send_reserved=false.
- Fresh agent-generated drafts for these two companies each pass 110-word and handoff validation. New send run is being initiated after PR46.

No stable-volume claim is established yet. The existing 7 confirmed submissions out of 10 quality threshold remains; a two-company pilot cannot meet it. Report actual message IDs/form receipts separately from this threshold. Hourly automation checks active runs before starting at most one bounded batch and may replenish genuinely new nonindustrial targets under the user's prior explicit authorization.
