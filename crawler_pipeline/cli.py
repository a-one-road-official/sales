from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from .pipeline import crawl_company, dedupe_records


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bounded Vertex-free company intake")
    parser.add_argument("--input", required=True, help="JSONL with company_name and url")
    parser.add_argument("--output", required=True, help="JSONL output path")
    args = parser.parse_args()
    records = []
    with open(args.input, encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            item = json.loads(line)
            try:
                records.append(crawl_company(
                    str(item["company_name"]),
                    str(item["url"]),
                    fetched_at=datetime.now(timezone.utc).isoformat(),
                ))
            except Exception as exc:
                records.append({
                    "company_name": item.get("company_name"),
                    "url": item.get("url"),
                    "decision": {"decision": "REVIEW", "error": f"{type(exc).__name__}:{exc}"},
                    "line": line_number,
                })
    records = dedupe_records(records)
    with open(args.output, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(json.dumps({"processed": len(records), "output": args.output}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
