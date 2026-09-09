# Legacy営業ルマン architecture

Pinned source: `A1-Road/python@ea092e809fc455663bdab4c708e104f95dbf39b6`

The legacy flow is an orchestrator over discovery, email, and form phases.

```
company
  -> initial check / robots
  -> URL discovery and categorization
  -> compliance
  -> content collection
  -> email discovery
  -> form discovery
  -> email generation/send
  -> form submission
  -> persistence
```

The implementation uses Playwright for browser work, Gemini-backed services for page/message selection, Supabase for persistence, and Gmail API for email execution. It includes bounded retries and a concurrency semaphore.

The new system must preserve the behavioral decomposition and evidence collection while separating:

```
PREP -> READY_FOR_APPROVAL -> APPROVED EXECUTION -> EVENT
```

Raw legacy modules must not be imported into production.
