# Customer-first SSOT operations

## Purpose and activation
Every recipient is a real prospective customer. Protect their time and the user's credibility. Supply the user with actionable replies and prepared meetings, and leave recoverable work for every failed company. Throughput never relaxes any recipient, copy, provenance or delivery check.

This is the execution contract for the three EXISTING ChatGPT tasks. It supersedes their old raw-only output and duplicate-copy-generation responsibilities. No new timer, paid model, external execution service, public queue or Cloud deployment is introduced. The existing Python collector continues. Existing outbound handoffs must be reconciled before any new sender claim.

SSOT: `1SSg8qB_N1wUESnAyCwTaEB5hgvS6jDJB8Bh2ryoO9mo`, `営業リスト＿Factory/BPO`, sheetId `515643202`. Read current headers and bounds every run. Use the existing `SalesOS_Action_Events` (sheetId `1373888845`) and `案件管理` (sheetId `1900000001`). No new management sheets. Row numbers are temporary locations, never identities.

## One existing company record
A company is registered/deduplicated in SSOT before draft generation or sending. Retain the raw source and original evidence. Existing rows retain human Status, history, identity, opportunities and manual fields. Confirm actual company/domain before linking. Conflicting identity remains reviewable, never silently merged.

Initial qualification checks real company, HQ, business and duplicates. Exclude Japanese HQ, fictitious/ceased entities and complete duplicates; missing information remains visible. Priority follows the user's latest Buyer Universe: a past exhibition outside the home country; Japan branch/subsidiary/own direct sales tracked independently. Partners/distributors/customers are not a Japan-branch disqualification. Future exhibition listing is not past participation. Absence of search results stays UNKNOWN. Mature businesses are prioritized using cited history/ownership/scale, not a universal VC or employee hard gate. Never revive the superseded blanket US exclusion.

Existing fields:
- B `Status`: initial verified AI send -> `AI送信済み`; definite initial failure -> `AI送信失敗`; uncertain -> `AI送信結果不明`. Advanced reply/meeting/reminder statuses survive technical failures.
- DH:DV: qualification, exact recipient/subject/body/evidence/generated_at, email processing state/approval, complete sales history and actual outbound IDs/times.
- DW:EF: `AI最終試行日時`, `AI失敗工程`, `AI失敗理由`, `AI次アクション`, `AI担当状態`, `AI送信予定日時`, `AI実行JSON`, `AI最終イベントID`, `AI更新日時`, `AI返信対応`.
- Reuse `AI実行JSON.customer_first` for company_id, raw source link, campaign, verified timezone, immutable quality packet and claim. Preserve other JSON keys. Never add competing underscore-prefixed columns.
- `AI担当状態`: AI / 手動対応中 / 手動完了 / 停止. Any handover or stop excludes autonomous sending until receipt reconciliation.
- `AI返信対応`: 未対応 / 対応中 / 対応済み / 不要. Read the original reply before marking processed.

## Shared deterministic writer
`customer_care.py` validates identity, reviewed copy and event transitions. `customer_sheet.py` maps those transitions to the live columns. SDK access can use CustomerSheet.apply. ChatGPT connector access uses the SAME pure plan:
1. Read the full current target row, current headers and both sheet IDs. Build the row dictionary from headers.
2. Build a factual event using customer_event. For a sent message use real Gmail metadata and decoded plaintext, never an imagined response ID.
3. Call plan_event with the row, its current row number, headers and ledger headers/ID. No network calls occur inside this function.
4. Re-read the target; call check_expected. If any watched field changed, re-resolve identity/replan.
5. Submit plan.requests as ONE Google_Drive.batch_update_spreadsheet request against the SSOT. The batch only updates specified fields and appends its event. Do not replace rows or whole sheets.
6. Read the same row back and call verify_readback. A lost write response requires an event_id lookup before retry. The same committed event replays without another ledger append.

Sheets does not supply compare-and-swap. There must be one sending owner, no parallel sender tasks, and no claim takeover by age. Generation never updates reserved/sending/unknown/manual rows. CRM records inbound facts and suppresses queued work; the sender rechecks Gmail immediately before the final action. A potential ownership collision stops that company's action and is reported.

## Producer task: quality inventory, SSOT first
Use the original email prompt selected by `原料_raw_material!B4`, fetch actual current body/revision. Follow its precise offer, CTA, signature, link and length. Keep approval tied to that exact prompt. If an explicit instruction and original conflict, save the original draft and the precise conflict as QUALITY_HOLD; never invent the commercial terms or change the user's original prompt.

Select existing mature-candidate SSOT companies first, then qualified new RAW candidates. Register new verified companies first. Source attribution and qualification reasons remain in that company row. An existing raw EMAIL_READY is a saved draft, not send permission.

Within the available run budget, work in small completed/read-back units, preparing the next local-time wave. Reuse genuine evidence and completed text; do not regenerate because transport failed or because a different timer fired. A 300-company ready inventory is a planning target, not a permission to degrade quality or a promised daily capacity. Record actual completed counts and oldest blocked item.

Each final packet records: company_id/name/verified website; exact recipient; exact subject/body; current prompt_sha256; company-specific buyer_workflow; offer_authority; recipient_evidence (email, source URL, supporting excerpt; verified person name only if supported); factual sources with URL, quoted excerpt, supporting source_text and relevance.

After writing, review as the recipient: does it describe THIS company's actual capability, a specific Japanese buyer/workflow and a verified reason to care, in natural English? Is paid Japan market development clearly the user's authorized service? Does it promise only authorized work, without invented clients, relationships, capabilities, meetings or outcomes? Does the greeting identify a verified person or the real company team? Check whole subject/body, exact calendar link/signature and original word/sentence limits. Do not substitute a generic template or copy another prospect's hypothesis.

Fill the six review booleans only after those checks, with actual reviewer_run_id/reviewed_at and a concrete reason. Compute packet_sha256 AFTER finalizing recipient, evidence and text. A hash detects changes; it does not prove semantic quality. Run quality_check. Apply DRAFT_SAVED only on pass. Save imperfect copy with QUALITY_HOLD and rescue_draft so the human can repair it. Never mark a held text SEND_READY.

The user has authorized implementation and outreach to the qualified mature campaign; record that authorization provenance in the existing approval field and customer_first.campaign only after current persona/identity/qualification checks. No per-company approval should be invented from industry labels alone. Preserve explicit exclusions, refusals, existing discussions and manual stops. The producer never sends.

## Sender task: one owner, no copy generation
First reconcile existing sacrifice `outreach_engine_log` claims and Gmail receipts. Preserve its history and do not repeat uncertain sends. Once reconciled, new work consumes the reviewed SSOT row, not a copied public repository queue. The old engine stays available for its existing approved handoffs; do not replenish unrelated test-company campaigns.

Use actual recipient location or verified IANA timezone; do not assign one US timezone or guess Japan business hours from country alone. Group work by the next region's local-business sending window. Run as many complete small cohorts as permitted by task runtime and provider limits; do not send one BCC blast or launch parallel workers. Timezone windows and inventory targets are configurable operational choices, not evidence of inbox placement or an optimal reply rate.

For each customer:
1. Confirm current SSOT identity, explicit campaign authorization, original prompt revision, saved packet hash and exact recipient/subject/body. No rewriting/word-budget shortcuts here.
2. Check authenticated Gmail profile `admin@a1-road.com`, prior outbound (company/domain and exact recipient), latest inbound/refusal/unsubscribe/bounce, SSOT history, and unresolved old handoffs. Inspect actual search results; failed lookups are not zero history. Do not send to previously contacted customers as a new first contact.
3. Run fresh preflight; record SEND_READY or directly SEND_RESERVED with a unique claim_id using the shared plan and exact readback. Before final send, re-read identity, manual state, packet and claim. No expiry-based claim release.
4. Invoke the existing Gmail.send_email using exactly the saved recipient/subject/body and `content_type=text/plain`. No CC/BCC unless separately authorized. The provider/platform's real permission requirements remain binding.
5. Fetch returned message ID, confirm SENT label, sender, recipient, subject and actual decoded plaintext. Normalize only transport CRLF when comparing; never hide changed words. Apply SENT with the same claim and actual Gmail time/IDs. Save and read back before the next customer.
6. Definite rejection before a send -> SEND_FAILED with stage/reason. Timeout/ambiguous provider result -> SEND_UNKNOWN. Missing recipient, failed research or invalid copy before reservation -> QUALITY_HOLD or documented definite failure, preserving all available text. Every selected company's outcome must remain visible in SSOT, including failures before claim.

Forms use the already tested existing executor only: first name Kazuma, last name Tamura, full name Kazuma Tamura, company A-one road Co., Ltd., sender admin@a1-road.com. Inspect labels/autocomplete and actual DOM values before each Next/final submit. Ambiguous/malformed identity, truncation, unsupported fields, CAPTCHA or prohibited consent -> stop and preserve for human handling. Never insert A1/A1, invented financial/purchasing answers or secretly alter the message to squeeze through a form. A confirmed form receipt remains channel=FORM and counts via its actual confirmation; never invent a Gmail message_id.

## Human rescue
A failed row contains exact saved recipient (or clearly missing), subject/body, reason, failure stage and last attempt. `AI担当状態=手動対応中` takes it out of automatic work. Uncertain submissions are reconciled before offering a manual resend. After manual sending, fetch real Gmail proof and apply MANUAL_SENT; do not blindly restart the AI queue. A manual send before marking takeover is still a fact to reconcile. Never overwrite the human's revised message with an older packet.

## Reply, meeting and cash recovery task
Use the existing CRM task and original Site operations contract. Incrementally read Gmail with an overlap window and complete pagination; deduplicate by message ID. Read all required evidence before updating. Inbound autoack, OOO, bounce and calendar notification are separate from a human reply. Link by verified company/domain/address/thread, keep uncertain matches for review.

Human reply -> REPLIED, AI返信対応=未対応, meaningful reply class, source link and next action; suppress future first-contact/automatic follow-up. Human owns sending commercial replies and making commitments. A proposed reply stays a draft until approved.

Read Calendar event details/attendees/cancellations. Create/link the existing 案件管理 row for a verified booked company; show contact, next meeting, original outreach, reply, commercial question and what to learn. No speculative amounts/probabilities. Meeting-held requires meeting notes, a post-meeting email or explicit user confirmation; preserve distinct meeting count vs distinct companies. Preserve prior opportunities, human Yomi/amounts, 21-user-reported/17-verified historical meeting baseline and unresolved evidence. Do not zero unavailable sources.

Normalize canonical events to the existing Sales Control vocabulary: actual Gmail -> OUTBOUND_SENT (or REMINDER_SENT), human reply -> REPLY_RECEIVED, held meeting -> MEETING_COMPLETED; source EVIDENCE_RECONCILE and canonical_action_id equal the underlying message ID. Reuse existing meeting canonical IDs before adding a new one. Preparation/reservation/error events use source CUSTOMER_FIRST and count as zero sends. UI and Sheet read the same canonical counts.

Continue using original Site project appgprj_6aac8013c36481919c6bbae9eaf7c632 and its docs/OPERATIONS.md. Never create a replacement Site or publicly publish private data. If that app is unavailable, preserve the working Site and update the authorized SSOT; report the UI-specific gap. Show human tasks first: today's meetings, unanswered genuine replies, recoverable failures. No calendar-specific extra UI is needed.

## Acceptance
- Actual local-form test submits Kazuma/Tamura once. A page that rewrites names to A1/A1 is blocked before submission.
- Reviewed recipient/text/evidence mutation invalidates the review. Unsupported claims and commercial conflicts stay held.
- Ten prepared customers with five recorded sends and five failures remain ten identifiable SSOT rows, all drafts recoverable.
- No previous history/advanced human status is erased. Manual takeover blocks auto send; unknown sends do not become automatic retries.
- Returned Gmail receipt matches exact company, addressee, subject and body. Source failures never become successful zero counts.
- Generation, sending and recovery report separate actual counts, run IDs, last success and pending reasons. CI success, drafted copy or a scheduled task setting does not establish live throughput, inbox delivery, bookings, contracts or cash.
