# A-one Lead Factory

## Why
Keep qualified outbound inventory growing continuously while the owner is offline.

`source frontier -> scraper build/test/smoke -> mass raw capture -> official-site resolution -> live Gate -> SSOT -> contact/draft -> READY -> HUMAN APPROVAL`

The human control plane is ChatGPT / ChatGPT Work. GitHub is the code SSOT, Google Drive holds the human-editable policy/prompt SSOTs, Google Sheets holds CRM/state, and Google Cloud is the unattended runtime.

## Hard invariants
- `LEAD_FACTORY_ALLOW_DELETE=FALSE`
- `LEAD_FACTORY_ALLOW_EXTERNAL_WRITE=FALSE` and `LEAD_FACTORY_SEND_MODE=DISABLED` by default. Only the dedicated manual BPO workflow may enable the isolated BPO lane.
- Factory and SSOT execution always stops in `LeadFactory_ApprovalQueue` with `requires_human_approval=TRUE` and `execution_allowed=FALSE`. BPO is the currently approved controlled outbound lane and still requires the explicit dual gate.
- BPO execution is restricted to `OUTREACH_ALLOWED_LANES=BPO` and `OUTREACH_BPO_SEND_ENABLED=TRUE`; it still requires semantic preflight, idempotency, and the explicit approval flag.
- Generated scraper code has no network access; trusted fetchers own network I/O.
- Generated adapters pass S1-S7 and then a deployed Cloud smoke before production activation.
- A company must have a verified first-party website before Gate evaluation.
- Existing human CRM status is preserved; new qualified rows start at `未接触`.

## Outbound execution

BPO, legacy EC/retail, and future SSOT email sends use the same `OutboundEmailExecutor`: live Prompt read, official-site/contact preflight, semantic preflight, lane interlock, Sheet/Gmail idempotency lookup, Gmail send, and `LeadFactory_ExecutionLog` audit. The lane and policy flags change; the sending mechanism does not.

`.github/workflows/deploy.yml` may deploy on push, but its runtime is explicitly send-disabled. Customer-facing BPO execution is available only through `.github/workflows/bpo_outbound.yml`, which requires both a manual `execute_bpo=true` input and repository approval variables. It builds the same Docker image and calls `/outreach/bpo-run`; the temporary Cloud Run service is only an execution-isolation boundary.

Every 10-company BPO batch must return `attempted=10` and at least five successes. Non-successful decisions are persisted with their reason so a human can work only the `AI送信失敗`/blocked set. Factory and SSOT remain blocked even while BPO is enabled.

## Single sources of truth
- Code/deployment: GitHub `a-one-road-official/sales` / `main`
- Human sales SSOT: `営業リスト 最新版連携用` -> `営業リスト＿Factory/BPO`
- Selection **and discovery** policy: Google Doc `target_screening_gate`
- Outreach copy SSOT: the unique Native Google Doc titled `outreach_prompt_production_v1`
  - Document ID is resolved by exact-title lookup at runtime; humans never synchronize IDs.
  - Every draft generation performs a live Drive read; changing the Doc requires no redeploy.
  - Each draft stores `prompt_doc_title`, `prompt_doc_id`, `prompt_modified_time`, and SHA-256 `prompt_hash`.
  - Unsent drafts whose hash differs from the live Prompt are marked `STALE_PROMPT` and automatically regenerated.
  - Send preflight performs a second live-read hash comparison; mismatches block sending and return to regeneration.
  - Production invokes /prep/tick every five minutes for internal draft/revision processing; the normal deploy workflow keeps external sends disabled.
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
`.github/workflows/deploy.yml` is the normal production deployment path. It may run on `push` or `workflow_dispatch`, but its runtime send interlocks remain disabled. It compiles/tests, authenticates to Google Cloud via OIDC, builds the image, deploys Cloud Run, configures the Cloud Tasks queue and continuous Scheduler jobs, runs a deep authenticated health check, and immediately kicks discovery/dispatch. Runtime production is driven by the configured Cloud Scheduler jobs; redeploying does not reset an already-running daily SLO goal. Customer-facing BPO sends are deliberately separated into the manually gated workflow described above.

`cloudbuild.yaml` is legacy and must not be attached to a production trigger.
