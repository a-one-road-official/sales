# A-one Lead Factory

## Why
Keep qualified outbound inventory growing continuously while the owner is offline.

`source frontier -> scraper build/test/smoke -> mass raw capture -> official-site resolution -> live Gate -> SSOT -> contact/draft -> READY -> HUMAN APPROVAL`

The human control plane is ChatGPT / ChatGPT Work. GitHub is the code SSOT, Google Drive holds the human-editable policy/prompt SSOTs, Google Sheets holds CRM/state, and Google Cloud is the unattended runtime.

## Hard invariants
- `LEAD_FACTORY_ALLOW_DELETE=FALSE`
- `LEAD_FACTORY_ALLOW_EXTERNAL_WRITE=FALSE`
- Customer-facing execution stops in `LeadFactory_ApprovalQueue` with `requires_human_approval=TRUE` and `execution_allowed=FALSE`.
- Generated scraper code has no network access; trusted fetchers own network I/O.
- Generated adapters pass S1-S7 and then a deployed Cloud smoke before production activation.
- A company must have a verified first-party website before Gate evaluation.
- Existing human CRM status is preserved; new qualified rows start at `未接触`.

## Single sources of truth
- Code/deployment: GitHub `a-one-road-official/sales` / `main`
- Human sales SSOT: `営業リスト 最新版連携用` -> `営業リスト＿Factory/BPO`
- Selection **and discovery** policy: Google Doc `target_screening_gate`
- Outreach copy policy: Google Doc `outreach_prompt_production_v1`
- Runtime switch/state: Sheet `Config`

`target_screening_gate` is loaded live for every company. AI/web search may collect factual evidence; Python applies the Doc's G1-G6 rules deterministically. The runtime owns no independent eligibility criteria.

## Autonomous source frontier
The factory cold-starts from high-yield official industrial universes such as VDMA/VDW and large industrial exhibition directories. Every discovery cycle also uses public-web search to find adjacent official associations, exhibitions, clusters, exporter/company directories and recurring funding feeds, excluding already-known sources.

A new source follows this path automatically:

`DISCOVERED -> probe -> adapter generation -> S1-S7 -> READY_FOR_CLOUD_SMOKE -> cloud smoke -> ACTIVE -> full crawl`

Runtime failures trigger adapter repair and the same smoke gate before reactivation.

## Parallel processing
Google Cloud Tasks is the work queue. Each source/company is an independent authenticated job:

- `/worker/source` — one source crawl/build/repair
- `/worker/domain` — one company official-site resolution
- `/worker/gate` — one company fresh-context research + deterministic live-Doc Gate

Schedulers only replenish the source frontier and fan work into the queue. Cloud Run scales the independent jobs horizontally; the 540-second monolithic supply request is no longer the production bottleneck.

## Production loops
- Source discovery, Growth: every 5 minutes
- Source discovery, Mittelstand: every 5 minutes, offset by 2 minutes
- Worker dispatch, Growth: every minute
- Worker dispatch, Mittelstand: every minute
- Promotion to human SSOT: every minute
- Contact/email READY preparation: every 5 minutes
- Exhaustion controller: every 10 minutes
- Meta watchdog: every 2 minutes

Legacy bounded `/supply/growth` and `/supply/mittelstand` endpoints remain for debugging/backward compatibility; their Scheduler jobs are paused in production.

## Stop condition
The factory does not stop because a few runs promoted zero companies and it does not stop after an arbitrary target such as 200 rows.

Automatic stop requires all of the following:
1. no unresolved official-domain backlog,
2. no pending Growth Gate backlog,
3. no pending Mittelstand Gate backlog,
4. no discovered/retry/degraded source work,
5. no new Raw company or new source for the configured quiet window (default 180 minutes).

Then `LEAD_FACTORY_ENABLED` is set to `FALSE` and an internal stop notification is attempted to `admin@a1-road.com`. Customer-facing execution remains untouched.

## Deployment
`.github/workflows/deploy.yml` is the production deployment path. A push to `main` compiles/tests, authenticates to Google Cloud via OIDC, builds the image, deploys private Cloud Run, injects `OPENAI_API_KEY` from Secret Manager, configures the Cloud Tasks queue, configures continuous Scheduler jobs, runs a deep authenticated health check, and immediately kicks discovery/dispatch.

`cloudbuild.yaml` is legacy and must not be attached to a production trigger.
