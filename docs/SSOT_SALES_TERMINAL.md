# SSOT sales terminal v1 — operating contract

## Goal and owners
Feed Kazuma qualified meetings and actionable human exceptions. New work starts in the existing SSOT before a draft is generated. Preserve working Python collection and the authenticated Gmail connector; add no cron, service, paid model, YepCode execution or public customer data.

The three existing ChatGPT tasks are the only automatic owners:
- Producer: `6aad4e538c908191b78c5a87d6822a49` — hourly inventory replenishment, screening, SSOT registration, research and original copy. No sends.
- Sender: `6aac928c0e28819181358fba56176102` — hourly recipient-local waves, same-company send/receipt reconciliation and new replies. No copy generation or pipeline code edits.
- CRM: `6aac867e396c8191b46cb014bf88d07f` — existing daily full reconciliation, Calendar/meeting/opportunity linkage and existing owner-only Sales OS update. No customer messages or calendar mutations.

This contract supersedes old sacrifice-only sourcing / SSOT-read-only instructions for NEW explicitly authorized mature-campaign work. Existing sacrifice reservations, opt-outs, sent history, manual holds and unresolved attempts remain protected. Finish/reconcile an existing connector handoff before starting another automatic delivery. Never dispatch two independent senders.

## Canonical locations
Spreadsheet `1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo`.
- `営業リスト＿Factory/BPO`, sheetId 515643202: one current company row. Header row 1; Status is B. Resolve columns from fresh headers, not a copied numeric offset.
- `SalesOS_Action_Events`, sheetId 1373888845: immutable, idempotent events. Preserve existing A:X headers and records. Real Gmail IDs unify manual and automatic sends.
- `案件管理`, sheetId 1900000001: opportunity headers at row 8, data from row 11. Existing manual commercial fields are authoritative.
- `Sales Control` A105:B140: visible terminal and configuration. Do not modify earlier owned sections. Filters for failures, pending copy and unanswered human replies are on the primary sheet.
- `原料_raw_material`: discovery provenance only. Existing generated copy is retained during migration, never counted as sent. Resolve its raw_id to the canonical company row and preserve that mapping.

Reuse DI:DP for recipient, subject, body, evidence, generation time, machine state, approval and send permission. Preserve DQ:DV history and actual outbound receipt fields. DW:EF adds attempt time, failure stage/reason, next action, human ownership, local-wave schedule, execution JSON, last event ID/time and human-reply handling.

`AI担当状態`: AI / 手動対応中 / 手動完了 / 停止. Empty does not authorize sending. Do not reset human choices.
`Status`: initial-contact sent/failed/unknown use AI送信済み / AI送信失敗 / AI送信結果不明. Reminders, replies, live negotiations, contracts and won states are not downgraded on a later failed attempt.

## Execute the tested Python contract locally
Fetch `ssot_terminal.py` and its existing dependency `outreach_master.py` from the same current main revision; for copy validation also fetch `prompts/japan_outreach_master.txt`. Execute in the current ChatGPT Python/container environment, with private connected-tool data. Do not send input JSON to a hosted executor or publish it to GitHub.

`python ssot_terminal.py PRIVATE_INPUT.json PRIVATE_OUTPUT.json`

Operations:
- `promote`: `{operation,candidate,existing_rows,now}`. Candidate contains company_name, verified website, hq_country, real_company, screening=GO/UNKNOWN, evidence_url, raw_id, maturity and campaign. Reconcile all existing names/domains/IDs before any append. EXISTING returns its existing ID/row. APPEND returns the new row; append using server-allocated appendCells, preserving formats/validation and read back the ID. Never use a guessed next row or replace an existing company row.
- `event`: `{operation,row,event,now,headers,event_headers,existing_event_ids}`. Row is a fresh complete mapped company row with row_number. Event includes event_id, kind, company_id, company_name, website, occurred_at and the type-specific evidence. Result is narrow Sheets batchUpdate requests plus readback expectations. Submit those requests with the connected Sheets action. Search event IDs and re-read the company row after an ambiguous response before any repeat write.
- `readback`: `{operation,expected,row,event_ids}`. Use returned readback expectations, actual re-read row and actual canonical event IDs. Count completed persistence only after this passes.
- `preflight`: `{operation,row,proof,now}`; `wave`: `{operation,now,timezone}`; `summary`: `{operation,rows}`; `opportunity`: `{operation,row,existing_rows,now}`.

The module does not itself call external connectors. The task must perform the actual reads/writes/send, not stop after JSON plans. A prepared plan has external_sends=0 and spreadsheet_writes=0. No fake Gmail IDs, source quotations, review flags, execution IDs, maturity, budget or held meetings.

## Producer cycle
1. Read actual inventory/config and next regional waves. Prioritize existing uncontacted mature-source rows, then screened new RAW. Existing source examples: FME, MAKTEK, VDMA, VDWF, UCIMU, VDW. Do not wait for 1,500 companies to be fully prepared.
2. Keep broad first-stage admission: exclude Japanese HQ, nonexistent/ceased entities and complete duplicates. Preserve unknowns. Do not use an old US exclusion or manufacturing-only technology gate. Priority is past exhibition outside HQ country, with no verified Japanese branch/subsidiary; then 51–200 employees, maturity, founder/family/private, direct-sales absence/unknown. Partners/distributors/customers alone do not disqualify. Planned exhibition listings do not prove completed overseas exhibition attendance. Mature status requires cited company-history/scale/ownership evidence; no invented fixed-age or VC-only classifier.
3. Register screened new firms into the primary SSOT immediately; existing firms retain their IDs/history. Store campaign=MATURE_GTM_2026 only after maturity is verified; uncertain companies remain in SSOT without counting toward the mature 1,500. Keep maturity_evidence, foreign_exhibition/japan_branch/japan_subsidiary/japan_direct_sales and evidence in AI実行JSON. Preserve existing keys. Confirmed company identity conflicts are review items.
4. The active commercial-copy original is the existing main `prompts/japan_outreach_master.txt`, also validated by outreach_master.py. It requires original company/buyer/workflow/Japan-source reasoning and an explicit paid offer. The old RAW Drive six-month variant must not be relabelled with the current hash. Preserve its bytes as HOLD with PROMPT_VERSION_MISMATCH until ChatGPT genuinely revises and reviews it. No template substitution or unsupported quantity claims.
5. Generate and verify the actual original copy in this ChatGPT run. Packet: company_id, website, generator=chatgpt_master_agent, draft={subject,body,master_prompt_hash}, generated_at, email_sha256=SHA256(subject+'\n'+body), evidence=[{url,excerpt,relevance}, ...], review with all actual checks from REVIEW_KEYS. Use original generated_at; an evidence-only recheck may update rechecked_at without regenerating unchanged copy.
6. Persist DRAFT_READY through event/readback; DI recipient requires official contact evidence. Keep timezone and timezone_evidence based on recipient location; do not assume one US timezone. Set DO=承認済み and DP=許可 only within the user's explicit campaign authorization and after real quality/identity/history checks, recording that basis. Existing denial/hold/manual decisions remain.
7. Work toward 300 usable future-wave drafts as an inventory target, not proof of output or a minimum batch. Work in bounded cohorts, initially up to 20 screened companies per run; continue within actual task limits only when evidence quality is maintained. One difficult firm becomes a visible HOLD; continue with others. Keep all failure stage/reason/body on the SSOT. Count produced, reused, held and admitted separately.

## Sender cycle
1. Read SSOT current machine states and human ownership; recover pending claims before new work. Inspect existing sacrifice unresolved handoffs as well. Keep original private logs; do not reopen an unresolved company because its lease expired. No new sacrifice-only cohort after migration.
2. Reconcile new Gmail replies, opt-outs, bounces and human sends from a saved successful watermark with overlap. Match exact company/domain/thread and retain ambiguity for review. REPLIED requires an actual human response; automated acknowledgement/OOO is separate. Set human reply handling to 未対応 and suppress generic new outreach. Supplier budget/paid-interest questions are priority for Kazuma; never assert a payment agreement from a booking.
3. Select mature SEND_READY/DRAFT_READY rows for the current recipient-local weekday 08–11 window. This window is an initial scheduling heuristic, not a proven universal optimum. Missing timezone remains visible. A due wave may consume multiple completed cohorts; no waiting for exactly ten firms. Preserve Gmail/provider quotas and actual errors. Pilot one cohort of up to 10; increase only after verified receipts/readbacks and health evidence. Maximum 50 firms per task run, one send at a time. These are bounded controls, not promised throughput.
4. Fetch Gmail sender identity and full relevant prior outbound/inbound evidence. Construct proof with sender, recipient, checked_at, history_query, recipient_evidence_url and actual booleans campaign_approved/identity_verified/recipient_verified/history_complete/no_prior_send/no_reply/no_opt_out/sender_verified. Five-minute freshness is enforced. Source-read failure must not become a clean history.
5. Persist SEND_READY then RESERVED, with actual run_id. Read back the claim. Immediately re-read the row/ownership, refresh proof, and persist SUBMIT_REQUESTED for the same claim. Read back before Gmail. Call the existing authenticated Gmail send connector exactly once for that request marker using unchanged subject/body/recipient. A prior SUBMIT_REQUESTED is reconciliation work, never permission to call send again.
6. Fetch actual sent message/thread and confirm SENT, sender/recipient/body. Persist SENT with receipt={message_id,thread_id,sender,recipient,label_ids,verified,sent_at,email_sha256}, claim_id and actual occurred_at. Use event/readback. Successful provider acceptance is not inbox delivery. Never report a prepared draft/claim as sent.
7. FAILED requires exact stage/reason and proof of definite rejection when a request started; otherwise UNKNOWN. Failed identity/contact/copy checks before reservation also get a same-row event. Do not modify the completed copy on transport errors. Unknown remains reserved until actual sent evidence or definitive nonsend evidence resolves it. RECONCILED_NOT_SENT requires matching claim_id and evidence.
8. Human takeover: EA=手動対応中 excludes the company from all auto delivery. UNKNOWN/SUBMITTING must be reconciled before human resend. Read the real manually sent Gmail and record MANUAL_SENT, preserving human work and suppressing duplicate automation.
9. After each cohort, publish counts/state/oldest pending/real latest progress and remaining blockers into the existing terminal area. Completed outcomes across the same cohort must partition its company count. Do not sum cumulative funnel milestones as disjoint current states. Preserve previous known results when a source fetch fails. No ordinary GitHub code/trigger updates or Cloud activation.

## CRM and meeting handoff
Reuse the existing daily Calendar/CRM/Sales OS work. Collect actual Calendar event details, attendees and cancellations. Map to SSOT company, emit MEETING_BOOKED/CANCELLED; held requires held_evidence. On first migration of an existing company, normalize prior event IDs and use meeting_history_complete/existing_meetings; otherwise preserve legacy count and mark reconciliation pending.

Run opportunity_plan against live 案件管理 rows. Reuse an existing OPP_ID and preserve amount/probability/Yomi/status. For a new verified booking append one row with the deterministic company opportunity ID and current calendar evidence. Read back and link the same OPP_ID on the company. No guessed budgets, extra calendar UI, recreated Sites or new management tabs. Keep next meeting separate from expected close and actual held count separate from bookings. Attach initial email, genuine reply, next questions and the paid-service understanding to meeting preparation. Proposal/contract/payment states require corresponding source evidence.

Human surface: primary-sheet filter views AI｜失敗・保留・結果不明, AI｜返信未対応, AI｜送信待ち・文面完成; terminal at Sales Control A105. Failures retain DI/DJ/DK for manual recovery. DW/DX/DY/DZ explain when/where/why/next. Existing owner-only Sales OS may display these same records; it must never independently invent counts.

## Limits, evidence and acceptance
No distributed compare-and-swap is claimed for Sheets. One automatic sender owns its claim; fresh re-reads plus append-only events handle conflicts and uncertain writes. Human takeover during an already-issued Gmail call cannot cancel that call; reconcile before any resend. Keep hidden background processes absent.

CI + schema + task configuration proves implementation, not 24-hour capacity or 1,500 real contacts. Acceptance also requires real scheduled cycles, same-row preclaim-failure recovery, Gmail receipts, matching ledger/UI counts and actual reply/meeting linkage. Report unverified elements explicitly.


## Customer-specific quality hardening (2026-09-20)
The canonical workflow/state/writer remains ssot_terminal.py. customer_care.py supplies only stateless customer-quality checks; do not add another queue, projection or timer.

Every SSOT packet must include recipient_evidence={email,source_url,source_excerpt}; a person's first_name requires person_name_source. Each company/Japan evidence entry retains the actual captured source_text, its exact excerpt and relevance. Fetch the real source first; never manufacture source_text by copying a claimed quotation into it. Keep excerpts bounded and retain source URLs. A short company alias requires company_alias_evidence.

After finalizing the recipient, subject, complete body and supporting evidence, the ChatGPT producer must reread as the recipient. Check sender_identity, recipient_fit, facts_supported, authorized_offer, natural_language and individualized. Record actual booleans, reviewer_run_id, reviewed_at and a concrete reason in customer_review. Compute customer_review.packet_sha256 with customer_care.customer_packet_hash(row,packet). For a DRAFT_READY event with a new recipient, hash a row containing that exact proposed recipient. Do not invent review provenance. The fingerprint does not prove semantic truth: actual source and commercial-scope review remain required.

ssot_terminal.verify_draft runs these checks during draft saving, preflight and final submit request. An edited recipient, name, source or message invalidates the approval. Missing review is HOLD/COPY_REVIEW; retain original text and genuinely re-review it. Reuse unchanged text; no transport-triggered regeneration. The sender reads the full final subject/body and critical evidence too, may hold a defective draft, and never silently rewrites it.

For a failed first draft, pass rescue_draft={subject,body} and rescue_recipient with HOLD/FAILED. The existing reducer saves available bytes in the same SSOT row without marking them ready or overwriting an existing draft. Keep definite failure, uncertain submission, human ownership and advanced sales stage rules unchanged.

Forms verify actual DOM first/last/full names, company and email before every Next/final submit: Kazuma / Tamura, Kazuma Tamura, A-one road Co., Ltd., admin@a1-road.com. Ambiguous names or page-side mutation block the action. The local regression includes a page rewriting names to A1/A1 and proves no POST occurs in that case.

Canonical real-send events use OUTBOUND_SENT, human replies REPLY_RECEIVED, held meetings MEETING_COMPLETED and source EVIDENCE_RECONCILE so current Sales Control formulas include them. canonical_action_id uses the actual Gmail ID without an extra prefix, deduplicating the existing CRM imports. For Calendar backfills, reuse a previously matched canonical_action_id; never invent a second meeting identity. Preparation/errors remain source ssot-terminal-v1 and count as zero sends.
