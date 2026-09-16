# Deterministic OSS crawler

Vertex-free intake pipeline for the A-one road Factory/BPO SSOT.

## Scope

- HTTP-first fetch with `httpx`
- CSS/XPath extraction with `parsel`
- main-content extraction with `readability-lxml`
- deterministic Factory / Logistics / Other classification
- evidence URL and content hash retention
- domain-level deduplication
- no Google Search grounding, no Vertex, no Gmail, no external write

## Runtime contract

1. The caller supplies a company name and candidate URL from an approved source.
2. `crawl_company()` fetches one domain with a 12-second timeout and 1 MB response cap.
3. `classify_evidence()` returns `GO`, `NO-GO`, or `REVIEW`.
4. Only a separate SSOT writer may write `GO` records to `営業リスト＿Factory/BPO`.
5. `REVIEW` is retained for human review. It never invokes an AI fallback.

JavaScript rendering is a later, explicitly bounded fallback. It must never call Vertex and must keep the same host, page-count, timeout, and byte limits.
