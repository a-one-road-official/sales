import csv
import json
import re
import sys
import time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.maktekfuari.com/en/exhibitor-list"
COUNTRIES = [
    "Austria", "Belgium", "Bi̇rleşi̇k Arap Emi̇rli̇kleri̇", "Bulgari̇stan", "Canada",
    "China", "Czech Republic", "Finland", "France", "Germany", "Hi̇ndi̇stan",
    "Hungary", "İngi̇ltere", "Italy", "Japan", "Malaysia", "Netherlands", "Poland",
    "Portugal", "South Korea", "Spain", "Sweden", "Switzerland", "Tayvan", "Türki̇ye",
    "United States"
]


def normalize(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def split_company_country(prefix: str):
    prefix = normalize(prefix)
    country = ""
    company = prefix
    for c in sorted(COUNTRIES, key=len, reverse=True):
        suffix = " " + c
        if prefix.endswith(suffix):
            company = prefix[: -len(suffix)].strip()
            country = c
            break
    return company, country


def parse_anchor(a, page_url: str):
    text = normalize(a.get_text(" ", strip=True))
    if "Review in Detail" not in text or "Hall:" not in text or "Booth:" not in text:
        return None

    before_review, after_review = text.split("Review in Detail", 1)
    base = re.split(r"\s+Brands\s+|\s+Representatives\s+", before_review, maxsplit=1)[0]
    company, country = split_company_country(base)
    if not company:
        return None

    hall_match = re.search(r"Hall:\s*(.*?)\s+Booth:", after_review)
    booth_match = re.search(r"Booth:\s*(.*)$", after_review)
    hall = normalize(hall_match.group(1)) if hall_match else ""
    booth = normalize(booth_match.group(1)) if booth_match else ""

    href = a.get("href") or ""
    detail_url = urljoin(page_url, href) if href else page_url

    brands = ""
    reps = ""
    m = re.search(r"\sBrands\s+(.+?)(?=\sRepresentatives\s+|$)", before_review)
    if m:
        brands = normalize(m.group(1))
    m = re.search(r"\sRepresentatives\s+(.+)$", before_review)
    if m:
        reps = normalize(m.group(1))

    return {
        "company_name": company,
        "country": country,
        "hall": hall,
        "booth": booth,
        "brands": brands,
        "representatives": reps,
        "source_page_url": page_url,
        "source_record_url": detail_url,
    }


def main():
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; A-one-road-MAKTEK-export/1.0)"
    })

    rows = []
    seen = set()
    empty_streak = 0
    pages_scanned = 0

    for page in range(1, 121):
        url = BASE if page == 1 else f"{BASE}?page={page}"
        r = session.get(url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        page_rows = []
        for a in soup.find_all("a"):
            rec = parse_anchor(a, url)
            if not rec:
                continue
            key = (
                normalize(rec["company_name"]).casefold(),
                normalize(rec["hall"]).casefold(),
                normalize(rec["booth"]).casefold(),
            )
            if key in seen:
                continue
            seen.add(key)
            page_rows.append(rec)
            rows.append(rec)

        pages_scanned = page
        print(f"page={page} rows={len(page_rows)} total={len(rows)}", flush=True)

        if page_rows:
            empty_streak = 0
        else:
            empty_streak += 1
            if page > 1 and empty_streak >= 3:
                break
        time.sleep(0.15)

    if len(rows) < 500:
        raise RuntimeError(f"Too few exhibitors parsed: {len(rows)}")

    with open("maktek_2026.json", "w", encoding="utf-8") as f:
        json.dump({"count": len(rows), "pages_scanned": pages_scanned, "rows": rows}, f, ensure_ascii=False, indent=2)

    fields = ["company_name", "country", "hall", "booth", "brands", "representatives", "source_page_url", "source_record_url"]
    with open("maktek_2026.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    print(json.dumps({"count": len(rows), "pages_scanned": pages_scanned}, ensure_ascii=False))


if __name__ == "__main__":
    main()
