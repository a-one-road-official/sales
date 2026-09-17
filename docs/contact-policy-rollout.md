# Account-level contact policy: staged implementation

## Business intent

Keep SSOT and sacrifice lists separate during stabilization. Industry is an
attribute; account ownership and explicit campaign permission govern contact.
Evaluate opportunities by upfront cash net of initial delivery/third-party
costs, time to payment, delivery capacity and repeatability. Eight products are confirmed in data/service_catalog.json. Billing period and
quantity limits remain unspecified; never invent them in customer-facing quotes.

## Modes

- PERSONAL: owner-led sales; no bulk permission.
- BULK_ALLOWED: reviewed company + official site + lane + campaign + expiry.
- HOLD: unresolved identity, ownership, economics or permissions.
- DO_NOT_CONTACT: suppression; overrides campaign membership.

Default is HOLD. Moving a row, changing industry, or joining SSOT must never
grant permission. Existing manufacturing safeguards remain during migration.
SSOT history and unresolved attempts retain their current blocking behavior.

## Implemented in this branch

contact_policy.json defaults to disabled with zero allowed accounts. A pure,
standard-library gate verifies operator evidence, exact company ID/name/host,
lane, expiry and campaign quality approval. It runs before reservation and
again through authorized(), used by both existing email and form executors.
No Sheets schema changes, sends, model calls or production deployment.

## Requirements before activation

1. Reconcile SSOT/sacrifice identity and account ownership; protect active deals,
   replies, opt-outs and hand-managed companies across every known alias/domain.
2. Use the confirmed eight-product catalogue. Confirm delivery quantities, billing
   period and pass-through costs at quotation time; missing commercial details must
   not block research, draft creation or initial non-binding outreach.
3. Use a shared live permission/suppression authority. Checked-in policy changes
   do not stop a running process on an older checkout. This branch alone does
   not implement fleet-wide emergency stop or live inbound-mail collection.
4. Recheck live permission, suppression and exact recipient/message immediately
   before the irreversible API send/browser click, including after waits.
5. Bind approval to recipient, channel, message hash, product version and campaign.
   An account permission alone does not prove final-message approval.
6. Preserve durable company-wide claims across all channels. Existing GitHub
   serialization is a deployment constraint, not a distributed atomic lock.
7. Exercise existing regression suite and connector/form mocks; update fixtures
   to explicitly grant test-only policy. This branch's focused tests are not a
   complete end-to-end certification.
8. Record target count, preview, exact payload and receipt. On an authorized
   10-target pilot require at least 7 appropriate messages with verified receipt;
   zero unintended recipients, duplicate sends, protected-account sends or
   history loss. A wrong target/opt-out violation stops the campaign immediately.
9. Repeated verified batches may increase volume; absent/ambiguous receipts stay
   held. Never automatically retry an uncertain send.

## September integration target

Integrate identity, history, permissions and operator visibility first. Preserve
separate PERSONAL/BULK_ALLOWED execution paths and record origins. Unify physical
storage only after safety/quality gates pass; calendar date never grants sending
permission. No new spreadsheet tabs are required by this proposal.

## Production requirements — user update

- Base unit price: USD 1,000 / JPY 150,000 / EUR 850–900 (user-specified pricing, not live FX). Billing interval is unconfirmed.
- Product matching may use simple Python or AI suggestions. Industry is not a blanket exclusion. Suggestions cannot grant sending permission.
- Missing budget, buyer identity, full research, perfect wording or AI scores should produce warnings; permit research and drafts. Do not require every product field for first contact.
- Final payload must have a verified intended recipient, nonempty relevant message, correct company, no unresolved template fields, no invented claims and no unapproved binding prices/promises.
- Quality certification is a scale-up gate. An explicitly enabled PILOT with at most ten named BULK_ALLOWED accounts can collect evidence before quality approval. Never recycle account lists to evade the pilot scope; cumulative counters and payload quality checks remain rollout work.
- Account protection, opt-outs, cross-channel duplicates, uncertain prior sends and emergency stop remain mandatory. Human approval may cover a frozen campaign target list; per-company repeated approval is not required inside that scope.
- Keep standard-library/OSS implementation; no Vertex, Google AI or additional paid model dependency.
- This PR remains a production rollout item. Source changes are not proof of runtime deployment. Live shared stop/inbound checks and complete regression verification remain outstanding.
