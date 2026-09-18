"""Read-only real research + local inference preview; no sender or CRM imports."""
import argparse
import json
from pathlib import Path
from japan_research import research_company
from outreach_master import generate_email, validate_email
from sacrifice_web_research import inspect_official_site


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", required=True, help="JSON list of company_name and website")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    candidates = json.loads(Path(args.candidates).read_text())
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 5:
        raise ValueError("PREVIEW_REQUIRES_ONE_TO_FIVE_COMPANIES")
    results = []
    for candidate in candidates:
        result = {"company_name":candidate["company_name"], "sent":False}
        try:
            site = inspect_official_site(candidate["website"], max_pages=3,
                                         expected_company=candidate["company_name"])
            packet = research_company(candidate, site)
            result["research"] = packet
            # Ignore any supplied cached research: exercise the complete live path.
            draft = generate_email({**candidate, "japan_research":packet}, site)
            result.update(status="PREVIEW_READY", words=validate_email(draft), draft=draft)
        except Exception as exc:
            result.update(status="PREVIEW_FAILED", error=f"{type(exc).__name__}: {exc}")
        results.append(result)
        print(json.dumps(result, ensure_ascii=False), flush=True)
        Path(args.output).write_text(json.dumps(results, ensure_ascii=False, indent=2))
    if any(r["status"] != "PREVIEW_READY" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
