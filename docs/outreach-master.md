# Shared Japan outreach master

Every company reads prompts/japan_outreach_master.txt at generation time. The offer is one year of paid Japan market development, with a concrete company-specific rollout question. No price, trade-show budget comparison, permission-to-send-hypothesis CTA, or defensive withdrawal line is included.

## Runtime

The isolated public-repository GitHub Actions runner installs Ollama and Qwen2.5 7B. Inference uses loopback only; no paid API fallback is configured. Standard public-repository runners are free; no model cache or model artifact is uploaded. Private repositories cannot run these model jobs.

japan_research.py derives a buyer/workflow from the official website, performs English/Japanese Japan-presence searches, searches for relevant Japanese government evidence, and fetches primary HTML/PDF sources. Facts need an exact source quotation and a separate semantic source review. Missing evidence stops that draft. Search snippets alone do not substantiate a fact.

outreach_master.py requests fresh per-company JSON, assembles the verified fact unchanged, appends the exact calendar/signature, and validates 110–120 words, paragraph counts, annual paid offer, one final CTA, prohibited language and prompt revision. Three bounded rewrite attempts include the previous output and concrete validation failure.

The runner reserves a company only after draft validation and, for forms, a successful read-only form preview. Pre-send research failures remain local preparation evidence and do not create an outbound reservation/history event. Actual reservation and attempt records continue to block duplicate sends, including uncertain results.

## Verification and operational status (2026-09-18)

- Nine focused local tests pass; repository CI runs on each implementation commit.
- Real public search and primary-source fetch passed in run 35292357685 (research job): five primary search results, MHLW source fetched.
- Real local-model generation is being checked by the generate job of that run; this is a release check, not proof of completed outreach.
- Current live routing (run 35292076467): 571 MANUAL, 136 HOLD, 3 REVIEW, zero AUTO_RESEARCH.
- Legacy run 35181679400 kept only aggregate results and dropped company-level outreach log appends. Its 190 aggregate failures do not establish that specific companies were never submitted.
- No company permissions have been fabricated, prior-contact locks remain intact, and no recipient sends have been made by this implementation session.
- The source packet and model have not yet been validated together end-to-end for an eligible live company because no eligible company is currently available.

Kimonix is a review fixture only. Its previous submission remains unresolved; the fixture must never authorize a resend.

An execute request with no permitted accounts now exits as OUTBOUND_NOT_STARTED instead of showing a successful activation. Resolve company identity/history and configure named campaign permissions before starting an outbound batch.
