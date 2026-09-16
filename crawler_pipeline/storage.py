from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS companies (
  company_key TEXT PRIMARY KEY,
  company_name TEXT NOT NULL,
  canonical_url TEXT,
  category TEXT,
  decision TEXT,
  content_hash TEXT,
  fetched_at TEXT,
  evidence_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_companies_domain ON companies(canonical_url);
CREATE INDEX IF NOT EXISTS idx_companies_hash ON companies(content_hash);
"""


class CrawlStore:
    """Small local cache; Google Sheets remains the business SSOT."""

    def __init__(self, path: str = "artifacts/crawl.sqlite3"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def unchanged(self, canonical_url: str, content_hash: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM companies WHERE canonical_url=? AND content_hash=? LIMIT 1",
            (canonical_url, content_hash),
        ).fetchone()
        return row is not None

    def upsert(self, record: dict, updated_at: str) -> None:
        evidence = record.get("evidence", {})
        canonical_url = str(evidence.get("canonical_url") or evidence.get("url") or "")
        company_key = canonical_url or str(evidence.get("company_name") or "").casefold()
        self.conn.execute(
            """INSERT INTO companies
            (company_key, company_name, canonical_url, category, decision, content_hash,
             fetched_at, evidence_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_key) DO UPDATE SET
              company_name=excluded.company_name,
              canonical_url=excluded.canonical_url,
              category=excluded.category,
              decision=excluded.decision,
              content_hash=excluded.content_hash,
              fetched_at=excluded.fetched_at,
              evidence_json=excluded.evidence_json,
              updated_at=excluded.updated_at""",
            (
                company_key,
                str(evidence.get("company_name") or ""),
                canonical_url,
                str(record.get("decision", {}).get("category") or ""),
                str(record.get("decision", {}).get("decision") or "REVIEW"),
                str(evidence.get("content_hash") or ""),
                str(evidence.get("fetched_at") or ""),
                json.dumps(evidence, ensure_ascii=False, sort_keys=True),
                updated_at,
            ),
        )
        self.conn.commit()

    def upsert_many(self, records: Iterable[dict], updated_at: str) -> int:
        count = 0
        for record in records:
            self.upsert(record, updated_at)
            count += 1
        return count
