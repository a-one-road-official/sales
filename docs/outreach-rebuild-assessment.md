# A-one Outreach Module rebuild assessment

- Assessment date: 2026-09-09
- Target repository: a-one-road-official/sales
- Target branch: codex/outreach-rebuild-intake-20260909
- External execution performed: none
- Legacy repositories modified: none

## 1. Executive result

The rebuild should reuse the behavioral knowledge of the legacy systems while replacing their execution boundary.

The current sales repository already provides the cloud-native Lead Factory control plane:

`source -> official-domain resolution -> Gate -> SSOT`

It also declares the required customer-facing boundary:

`READY -> HUMAN APPROVAL`

The repository tree at `f986f43f5c7caebf9bec7ad05682c8228a714cb9` does not yet contain a dedicated `outreach/` module, contact-research module, form executor, reply webhook receiver, or execution worker. The new module must therefore be added as a downstream subsystem without changing source discovery, Gate, or qualified-lead promotion.

## 2. Legacy Source A:営業ルマン

Repository: `A1-Road/python`
Branch: `main`
Pinned commit: `ea092e809fc455663bdab4c708e104f95dbf39b6`
Primary root: `lemans/eigyo-lemans-app/**`

The inspected implementation contains:

- orchestrator-driven company processing
- browser-backed initial site access
- robots.txt retrieval and filtering
- broad URL discovery and URL categorization
- compliance-page analysis
- main/About content collection
- email discovery with invalid-address filters and obfuscation handling
- form discovery
- Gemini-backed message generation
- Gmail API sending
- form execution through Playwright
- bounded concurrency and retry wrappers
- Supabase persistence and result recording
- tracking and Gmail checker integrations documented in the wider lemans system

The strongest reusable insight is the decomposition of discovery into independent evidence-producing stages. The most important architectural defect is that preparation and external execution remain too close: the legacy orchestrator reaches EmailPhase and FormPhase, and those phases can call send/submit strategies directly.

## 3. Legacy Source B: Outreach-engine

The requested legacy paths were not found in the connected A1-Road repository inventory or in the pinned `A1-Road/python` tree:

- `src/outreach/form_submission/browser.py`
- `src/outreach/form_submission/detector.py`
- `src/outreach/enterprise_scale.py`
- `src/outreach/triage_report.py`
- `python -m outreach.cli`
- `target_list_abc.csv`
- `a1road_gtm`

No repository name or commit SHA will be inferred. The source is recorded as unresolved in `knowledge/legacy/outreach_engine/SOURCE.json`. The reported real-site behaviors are preserved as a provisional failure corpus and must be verified against the actual pinned repository when it becomes accessible.

## 4. Current sales repository

Current main commit: `f986f43f5c7caebf9bec7ad05682c8228a714cb9`

Existing strengths:

- Cloud Run deployment path
- Cloud Tasks as independent work queue
- internal runtime token guard
- Secret Manager resolution for runtime credentials
- first-party official-site requirement
- deterministic Gate execution from Drive policy
- Google Sheets CRM/state persistence
- no customer-facing send path in the current runtime
- retry/repair and cloud-smoke concepts for source adapters
- production switch and start/stop control endpoints

Current gaps:

- no dedicated Outreach Module tree
- no explicit outreach state machine
- no recipient verification contract
- no contact-discovery evidence schema
- no form semantic parser/executor boundary
- no approval-bound execution worker contract
- no outreach idempotency key
- no reply/bounce webhook receiver
- no sender-health circuit breaker
- no regression fixtures from legacy form failures
- no legacy intake workflow
- no outreach-specific CI gates

## 5. Benchmark patterns adopted

### Instantly

The official `Instantly-ai/instantly-starter-kit` at tree SHA `7649e6e4f820ad18cc563d5075f34b1a8daceb36` demonstrates:

- campaign creation as Draft
- separate explicit activation
- read-only preflight before launch
- recipient verification before sending
- asynchronous verification with polling/webhook
- webhook-driven reply/bounce/event handling
- idempotent webhook consumption requirements
- background-job polling
- stop-on-reply
- sender/deliverability checks

The official `Instantly-ai/n8n-nodes-instantly` at tree SHA `925f1e35fb5e127bbdd8b114a8e4609292a3b0e0` reinforces resource-level separation for campaigns, leads, accounts, analytics, and Unibox operations.

Adopted rule: create != activate; accepted != completed; HTTP 200 != business success; retry != idempotency.

### Reply.io

No official GitHub implementation repository was verified during this intake. The implementation will not depend on unofficial copies. Reply's official API/OpenAPI documentation must be added as a separate benchmark intake before implementing Reply-specific adapters.

## 6. New architecture

The new module is downstream of qualified leads:

`Qualified Lead -> Outreach PREP -> READY_FOR_APPROVAL -> Approved Execution -> Event -> Learning`

PREP may:

- build company context
- crawl official pages
- find and score contact channels
- verify email candidates
- map form fields
- generate a message
- apply compliance, blocklist, dedupe, and campaign scope
- create an approval artifact

PREP may not send email or submit a form.

Execution requires a matching:

- `approval_id`
- `approved_at`
- `content_hash`
- `recipient_hash`
- `channel`
- campaign scope

Any content or recipient change invalidates approval.

## 7. State model

Email:

`QUALIFIED -> CONTACT_DISCOVERY -> CONTACT_FOUND -> MESSAGE_READY -> READY_FOR_APPROVAL -> APPROVED -> SEND_QUEUED -> SENT -> DELIVERED`

Terminal or side states:

`BOUNCED`, `REPLIED`, `OPTED_OUT`, `NO_REPLY`, `BLOCKED`, `MANUAL_REQUIRED`, `UNCERTAIN`

Form:

`FORM_FOUND -> FORM_PARSED -> FORM_FILLABLE -> FORM_READY_FOR_APPROVAL -> APPROVED -> FORM_EXECUTION -> SUCCESS|FAILED|MANUAL_REQUIRED|UNCERTAIN`

All transitions must be persisted with evidence and timestamps. A boolean `success` is insufficient.

## 8. Migration map

| Legacy capability | New destination | Treatment |
|---|---|---|
| URL/robots discovery | `outreach/contact_discovery/crawler.py` | behavior extracted, implementation rewritten |
| email extraction/filtering | `outreach/contact_discovery/email.py` | add domain/MX/evidence scoring |
| form discovery | `outreach/contact_discovery/forms.py` | add semantic graph and staged-form handling |
| AI message generation | `outreach/message/generator.py` | load Drive policy live |
| Gmail sending | `outreach/email/sender.py` | execution worker only |
| Playwright form execution | `outreach/form/executor.py` | approval-bound, result confidence |
| retries | `outreach/safety/idempotency.py` and worker policy | fingerprinted, bounded |
| tracking/replies | `outreach/reply/webhook.py` | event-driven |
| legacy bugs | `tests/outreach/fixtures/**` | mandatory regression fixtures |
| production incidents | `knowledge/runtime_failures/**` | repair loop input |

## 9. Initial implementation order

1. Commit this assessment and provenance records.
2. Add legacy capabilities, behaviors, and provisional failure corpus.
3. Add HTML fixtures for known Japanese-form and contact-discovery failures.
4. Add contracts, models, state transitions, and idempotency tests.
5. Add PREP endpoint integration while keeping external execution absent.
6. Run Cloud Smoke with an isolated Vendor/EC test campaign only.
7. Add approval-bound execution workers.
8. Add event receivers and sender-health circuit breakers.
9. Run 50-company no-send phase, then 50-company approved execution canary.
10. Promote to Factory/BPO only after all production-promotion gates pass.

## 10. Non-negotiable boundaries

- Legacy repositories are READ ONLY.
- New code is written only under `a-one-road-official/sales`.
- No raw legacy code import.
- No Factory/BPO company is used for the canary.
- No customer-facing action occurs during PREP.
- Existing qualified-lead/Gate pipeline remains intact.
- External execution requires the A-one approval contract or an explicit, scoped policy.
