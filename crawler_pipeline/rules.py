"""Stable rule version and input contracts for deterministic intake."""

RULE_VERSION = "deterministic-2026-09-15-v1"
MAX_PAGES_PER_COMPANY = 5
MAX_CONCURRENT_HTTP = 10
MAX_CONCURRENT_BROWSER = 2
MAX_DAILY_COMPANIES = 1500

# Customer-facing actions are outside this package by design.
EXTERNAL_WRITE_ALLOWED = False
VERTEX_ALLOWED = False
