# A-one Lead Factory

## Why
Keep qualified outbound inventory growing while the owner is away:

`source discovery -> company extraction -> domain/research -> Gate -> qualified SSOT -> contact research -> email draft -> READY -> HUMAN APPROVAL`

## Hard invariants
- `LEAD_FACTORY_ALLOW_DELETE=FALSE`
- `LEAD_FACTORY_ALLOW_EXTERNAL_WRITE=FALSE`
- No Gmail send path in the runtime.
- Customer-facing execution stops in `LeadFactory_ApprovalQueue` with `requires_human_approval=TRUE` and `execution_allowed=FALSE`.
- Generated scraper code has no network access; trusted fetchers own network I/O.
- Generated adapters must pass S1-S7 before runtime use.
- Gate rules are loaded from their Drive SSOT and may not be replaced by code-owned criteria.

## Production loops
Two Cloud Scheduler jobs run bounded end-to-end lanes.

### Growth
`POST /supply/growth` every 10 minutes.

Exhibition/company directories are the primary source. When a cycle yields no new source and no new raw lead, funding/news discovery supplies newly funded industrial/deeptech companies.

### Mittelstand
`POST /supply/mittelstand` every 10 minutes, offset by 2 minutes.

Mature-industrial sources rotate by least-recently-crawled order. M1-M3 `GO` is sales-ready; `UNKNOWN` remains in technical screening history for further research and is not promoted to outbound READY.

### Each lane performs
1. Discover sources.
2. Crawl a bounded source set and self-build/repair adapters.
3. Resolve official domains for a bounded backlog.
4. Run the lane's authoritative Gate in fresh context per company.
5. Promote qualified companies to `営業リスト＿Factory/BPO` with `Status=未接触` while preserving existing human Status values.
6. Research one best-fit commercial contact.
7. Load `outreach_prompt_production_v1` from Drive and generate one send-ready email.
8. Write internal records to `LeadFactory_ContactResearch`, `LeadFactory_MessageDrafts`, and `LeadFactory_ApprovalQueue`.
9. Stop at HUMAN APPROVAL.

Raw/PENDING/NO-GO companies remain in hidden LeadFactory technical sheets and are not newly surfaced into the human sales list.

## Scheduler
- Growth supply: `*/10 * * * *`
- Mittelstand supply: `2-59/10 * * * *`
- Meta watchdog: `*/2 * * * *`
- Legacy midnight `lead-factory-daily`: paused by deployment; `/tick` remains a manual catch-up/debug endpoint.

Cloud Run request timeout and Scheduler attempt deadline are 540 seconds. Per-tick source/domain/Gate/READY limits are configured in the spreadsheet `Config` sheet.

## Source of truth
- Human sales SSOT: `営業リスト＿Factory/BPO`
- Growth Gate: Drive Google Doc `target_screening_gate`
- Mittelstand Gate: Drive file `mittelstand_screening_gate.txt`
- Outreach copy: Drive Google Doc `outreach_prompt_production_v1`

## Deployment
`cloudbuild.yaml` builds the container, deploys private Cloud Run, keeps the Meta watchdog, configures the two supply schedulers, and pauses the redundant daily scheduler. The current ChatGPT session can update Drive source/config but does not have authenticated Google Cloud control-plane access; deployment must be executed from a GCP-authorized environment.
