# Instantly benchmark

Source repositories:

- `Instantly-ai/instantly-starter-kit`
- `Instantly-ai/n8n-nodes-instantly`

Observed patterns:

- campaign creation produces a Draft
- activation is a separate explicit action
- preflight is read-only
- lead verification is a send prerequisite
- background verification/jobs can be polled or delivered to a webhook
- replies, bounces, unsubscribes, account errors, and campaign events are webhook inputs
- webhook delivery must be deduplicated
- campaign-level stop-on-reply is explicit
- account/sender health is part of readiness

A-one adoption:

`create/prep != activate/send`

The module will implement the same separation with `READY_FOR_APPROVAL` and an approval-bound execution worker.
