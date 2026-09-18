# Zero-metered-AI personalized outreach: execution contract

## Ultimate purpose
Continuously send evidence-backed, company-specific first-contact emails to all
explicitly authorized, eligible, previously uncontacted companies. Metered AI/API
inference spending is zero. Maintain quality, sender identity and history.
"All" preserves manual/industrial/SSOT, HOLD, DO_NOT_CONTACT, prior contact,
unresolved submissions, recipient and provider-rate-limit exclusions.

## Owners and boundaries
- The existing hourly ChatGPT task is the single cycle owner: research synthesis,
  draft generation, connector send, receipt reconciliation and cohort replenishment.
- Python handles identity, deduplication, queue checks, current policy, validation,
  reservation, serialization and outcome counters. No paid API or hosted fallback.
- `sacrifice_canary.yml` is the only reservation/execution workflow. Its name and
  concurrency group are part of the existing authorization contract. Do not create
  another sender cron. The task must finish/reconcile its handoff before new work.
- Use prepared ChatGPT drafts. The rejected small local model is not a fallback.
  The ChatGPT plan has usage limits; zero metered API spending is the constraint.
- `emergency-cost-stop.yml` is manual-only and disables/verifies only the two Google
  AI APIs. Its independent stop does not mutate outreach, Sheets, Scheduler or Run.
  The legacy BPO control is retired. Existing paid-cloud shutdown remains intact.
  API-stop verification does not establish that all project billing is zero.

## Execute a cycle
1. Read this file and the latest `prompts/japan_outreach_master.txt`; use the current
   prompt hash. Read the latest workflow summary and private canonical claims first.
   Do not repeatedly rediscover the repository or redesign infrastructure each hour.
2. For a pending handoff, finish the SAME claim. Confirm current policy, exact
   company/domain/recipient, approved text, current prompt, sender profile and Gmail
   history. Sender must be `admin@a1-road.com`. Preserve historical claims.
   Record and read back a connector-handoff event in the existing canonical log
   before sending. Use only the existing authorized Gmail connector. Runner DWD
   returned 401 in earlier runs: do not switch transports on an assumption.
3. Confirm returned Gmail message/thread IDs and the SENT label. Append/read back
   the canonical SENT event with `first-contact:<company_id>` and the same claim;
   advance only that company's private workbook status. An API timeout, an unknown
   result, or a missing receipt remains reserved for reconciliation. Never release
   a claim solely because its lease or a timer expired. No bulk status reset.
4. Use `outreach_cycle.completed_policy(policy, receipts)` after Gmail AND canonical
   ledger verification. It retains approved records as COMPLETED with a digest of
   the private receipt. This frees active PILOT capacity without deleting history
   or changing the ten-active-account quality gate. Do not infer SENT from HOLD,
   an Action result, elapsed time or a draft. Quality certification is separate.
5. Replenish from the existing sacrifice workbook, then approved nonindustrial
   sources. Join against production SSOT READ-ONLY, all canonical outreach history,
   and Gmail. Preserve actual dropdowns and existing record identities. Register
   only explicitly covered recipients. Before adding an eleventh ACTIVE PILOT
   account, reconcile existing receipts; never bypass `contact_policy.reason`.
6. Generate only missing, expired or invalid drafts for the next cohort (up to ten
   at a time). Reuse valid records, immutable factual source excerpts and unchanged
   prompt versions. HTTP/BS4/Playwright provide evidence; ChatGPT writes the actual
   company-specific buyer/workflow/operational consequence. Do not copy another
   company's paragraph or infer "no Japanese business" from UNKNOWN. Retain the
   current 48-hour freshness and strict live-site revalidation until the cache
   migration described below has its own tests.
7. Record the existing queue schema: company_id/name/website/generated_at,
   generator=chatgpt_master_agent, research, primary evidence, review flags,
   draft subject/body and master_prompt_hash. Review flags require actual review.
   Do not write tokens, confidential CRM history or raw Gmail receipts to GitHub.
8. Commit a complete cohort's data updates together, after an optimistic HEAD
   check. Normal data production does not edit Python, workflows or the master
   prompt. Then update `.github/ec-playwright-trigger` with a unique request ID
   and a commit message starting `RUN_SACRIFICE_BULK`. Reuse an already running
   cycle; do not launch concurrent/duplicate runs. No per-company workflow edits.
9. The runner's `outreach_cycle.py plan` reads a fresh private workbook snapshot,
   filters current permitted+prepared records, rotates ready lanes, and selects
   the cohort BEFORE Chromium startup. Invalid/absent drafts cannot occupy all
   ten execution slots. `NEEDS_GENERATION` and `NEEDS_REPLENISHMENT` are work items.
10. `outreach_cycle.py run` reports HANDOFF_PENDING separately from provider
    receipts and failures. Finish step 2 in the same task run when possible.
    Do not create another cohort while unresolved connector handoffs remain.
    A partial ready cohort may proceed. The 7/10 certification requires actual
    receipts, semantic review and readback across a complete ten-company sample.
    One ready company does not demonstrate 7/10, and need not wait for nine others.
11. Continue with the next cohort after the preceding one is reconciled, within
    task/provider runtime limits. Resume at the next hourly run from private state.
    No new permission is needed for ordinary continuation within the approved scope.

## Private storage and public reporting
Workbook: `1QBZKoN82O-SrFUnWaHBQtvflcdMT1gDp-QMPtZvLsEk`, `営業リスト_Vendor`.
Canonical ledger: `outreach_engine_log`, sheetId `751812358`, A:U (21 columns).
Append using `appendCells` and read back the same event key; `values.append` caused
column shifts and must not be used for this ledger. Preserve all previous records.
Production SSOT `1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo` is read-only here.
Public workflow output contains aggregate counts/run ID only. No raw-email,
recipient, browser-trace or runtime-log artifacts. Existing public queue JSON is
not confidential storage; move runtime data to the private workbook in P1.

## Acceptance and honest status
Track new verified prospects, authorized-ready drafts, invalid/stale drafts,
connector pending, actual submission attempts, Gmail accepted, form accepted,
unknown outcomes, failed/blocked, oldest pending age, elapsed cycle time and
provider errors independently. GitHub green means a completed processing phase;
only provider receipts plus canonical readback establish a recorded send.
A budget test passes only with no metered inference calls and no paid deployment.
Never claim inbox delivery, reply or revenue from API acceptance alone.

## Implementation sequence and definition of done
P0 (this change): retire auto-fired destructive stop; verified manual API-only stop;
ready-cohort planner; remove fixed single-company target; pending/receipt accounting;
receipt-verified permit completion helper; counts-only output; regression tests.
DoD: CI passes, unchanged safety tests pass, main contains exact tested commit,
and one live cycle demonstrates correct selection/handoff or explicit shortage.
Production throughput is unproven until actual repeated cycles finish.

P1: private ready/evidence queue using existing approved workbook columns/logs;
Python replenishment adapters and bounded evidence cache; separate prompt/source
content fingerprints from rechecked timestamps; incremental history watermark
with periodic full reconciliation. DoD: 30 valid private drafts buffered, no extra
management sheets, no repeated generation for unchanged evidence, restart survives
and source/history-read failures cannot reopen contacted companies.

P2: deterministic recipient/suppression reconciliation and connector receipt import;
when native Gmail DWD is independently verified, optional direct Python transport.
DoD: profile/send scope verified, send-timeout and crash-after-send drills do not
cause retries without reconciliation; no switch away from working connector early.

P3: 24-hour operational acceptance. DoD: repeated cycles replenish and send under
rate limits, zero duplicate sends, zero SSOT status writes, zero paid-AI calls,
complete pending/failed reasons, and measured sends/hour rather than predictions.
Do not label P1/P2/P3 complete because their interfaces or documents exist.
