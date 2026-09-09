# Outreach runtime validation plan

## Target and boundary

- Development repository: `a-one-road-official/sales`
- Legacy read source: `A1-Road` repositories, read-only
- Test cohort: isolated EC/retail Vendor records
- Excluded: every Factory/BPO record and the production SSOT execution path
- External effect: prohibited; this phase stops at `READY_FOR_APPROVAL`

The current `/prep/tick` entrypoint remains the preparation boundary. It may discover a first-party contact channel, verify evidence, generate a message draft, and persist a human-review artifact. It may not send email or submit a form.

## Runtime sequence

1. Select an explicit test cohort identifier. Never derive the cohort by taking “all non-Factory/BPO rows”.
2. Assert the no-send scope before reading or writing any outreach state.
3. Run contact discovery and message preparation against the isolated cohort.
4. Persist evidence for each candidate: official domain, source URL, channel, domain match, confidence, and rejection reason.
5. Store the generated message and its content hash in the approval artifact.
6. Record every failure with a stable fingerprint and the captured phase.
7. Convert each observed failure into a fixture and regression test.
8. Re-run the repaired flow in Cloud Smoke with the same no-send gate.
9. Only a later, separately approved phase may discuss execution; it is not part of this validation.

## Required outcome states

`CONTACT_FOUND`, `NO_CHANNEL_FOUND`, `MANUAL_REQUIRED`, `MESSAGE_READY`, `READY_FOR_APPROVAL`, `BLOCKED`, `FAILED`, `UNCERTAIN`.

No result may be collapsed into `success=True/False`.

## Failure loop

`runtime evidence -> fingerprint -> known/unknown classification -> fixture -> regression test -> patch -> local CI -> Cloud Smoke`

Before a legacy system is run, its known issues remain hypotheses. A failure is promoted to the verified corpus only with runtime evidence from the run.

## Promotion gate

Do not connect this module to Factory/BPO execution until all are true:

- duplicate external action: 0
- customer-facing send: 0 during this phase
- Factory/BPO contamination: 0
- wrong-company recipient: 0
- approval bypass: 0
- every contact-discovery failure has a reason
- every form result is classified
- retry does not duplicate a write
- Cloud Smoke passes with the no-send gate active
