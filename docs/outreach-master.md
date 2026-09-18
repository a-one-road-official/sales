# Annual Japan outreach master

Objective: build a continuing order pipeline; October 2 is the first order milestone. Standard offer: one year of paid Japan market development. CTA offers our help in planning and executing a company-specific Japan rollout. No permission-to-send-hypothesis CTA, meeting request, retreat copy or unapproved price.

Runtime: sales_leads_sacrifice_run._verified_site_draft -> outreach_master.generate_email. Each company rereads prompts/japan_outreach_master.txt, receives its own official-site research and Japan fact evidence, and makes a fresh AI request. No company-message lookup and no deterministic fallback. The Kimonix preview is only a test/review fixture. History and contact-policy checks are preserved.

The free upstream researcher must supply candidate.japan_research (facts with id, URL, text, retrieval date; maturity_searches) plus site.pages. This change DOES NOT yet implement autonomous Japan-side fact discovery or populate that packet for every workbook row. Missing packets produce JAPAN_RESEARCH_REQUIRED.

The runtime adapter uses an already-installed local Ollama-compatible engine at 127.0.0.1, OUTREACH_LOCAL_MODEL and optional OUTREACH_LOCAL_MODEL_PORT (11434). No paid endpoint, HTTP proxy, redirect, automatic model download or hosted fallback. Model installation and production runner provisioning remain outstanding. No real local inference has been performed in this workspace.

Checks: 110-120 body words, paragraph structure, paid annual offer, exactly one final question, forbidden phrases, exact link/signature, cited fact ID/quote, current prompt hash. Three bounded rewrite attempts. These checks cover mechanical consistency and traceable facts, not a guarantee of semantic quality. The commercial reasoning still requires real-model evaluation against multiple company types.

Tests: mocked-model unit checks cover fresh prompts across two company inputs, bad-copy regeneration, unsupported facts, missing research, no paid fallback, and pre-send prompt changes. No live sends; outbound policy is unchanged and disabled. Human review of initial examples is still pending.
