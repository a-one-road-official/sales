# Repository Boundary: Legacy Intake vs Current Development

This repository is the development target for the current SSOT-to-outreach-preparation workflow.

## Fixed ownership boundary

| Area | Account / repository | Role | Allowed access |
|---|---|---|---|
| Legacy read source | `A1-Road` (for example `A1-Road/python`) | Read old LeMans and related legacy assets, runtime behavior, failures, tests, and history | READ ONLY |
| Current development target | `a-one-road-official/sales` | Implement and validate the new cloud-native outreach preparation module | WRITE on feature branches |
| Customer-facing execution | External recipients / Factory / BPO production targets | Out of scope for this validation | PROHIBITED |

## Non-negotiable rules

1. Never use `a-one-road-official` as the source of old LeMans or Outreach-engine code.
2. Never write, commit, branch, open a PR, delete, or otherwise mutate an `A1-Road` repository.
3. Legacy material is converted into provenance-tagged capabilities, behavior specifications, failure hypotheses, fixtures, and regression tests before reuse.
4. The new module starts from the current SSOT and stops at `READY_FOR_APPROVAL`.
5. Customer-facing email or form submission is disabled for this work. No Factory or BPO row may enter an execution queue.
6. Test cohorts must be explicitly isolated from the production Factory/BPO cohort, with EC/retail used for discovery and draft-preparation validation.
7. A runtime failure is not considered verified until the legacy or new runner produces evidence. Before runtime, records are hypotheses only.

## Current development branch

`codex/outreach-rebuild-intake-20260909`

This boundary applies to all intake jobs, benchmark notes, runtime validation, test fixtures, and future Outreach Module changes.
