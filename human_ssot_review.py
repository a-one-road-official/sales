from __future__ import annotations

import concurrent.futures
import json
from datetime import datetime, timedelta, timezone
from typing import Any

SSOT_DEFAULT = "営業リスト＿Factory/BPO"
JST = timezone(timedelta(hours=9))
CATEGORIES = {
    "Factory", "Factory/BPO", "EC/リテール", "金融/コンプライアンス",
    "ヘルスケア/バイオ", "広告/マーテック", "セキュリティ", "営業/GTM",
    "採用/HR", "省人化", "物流/配送", "整備/アフター", "その他",
}
LOGISTICS = (
    "logistics", "supply chain", "scm", "warehouse", "fleet", "transport",
    "freight", "delivery", "shipping", "procurement", "inventory",
    "物流", "配送", "倉庫", "輸送", "貨物", "調達", "在庫",
)
CONSUMER_EDUCATION = (
    "childcare", "child care", "daycare", "early learning", "preschool",
    "children", "kids", "k-12", "k12", "student", "students", "parents",
    "families", "tutoring", "school", "homework", "teen", "youth",
    "幼児", "子ども", "児童", "学生", "保護者", "家庭", "塾", "学校",
)
INDUSTRIAL_EDUCATION = (
    "workforce", "employee", "employees", "operator training",
    "industrial training", "manufacturing training", "factory training",
    "safety training", "upskilling", "reskilling", "職業訓練", "従業員",
    "技能", "現場教育", "製造教育",
)
REVIEW_RULE_VERSION = "2026-09-14-v2"

PROMPT = """
You are A-one road's one-company fresh research worker.
Research exactly ONE company using web_search. Confirm the exact first-party corporate
website by checking the entity name and official About, Products, Solutions,
Technology, Applications, or Contact pages. Do not use memory. Do not treat LinkedIn,
directories, marketplaces, distributors, news articles, or social profiles as the
official company website.

A-one road is building a broad industrial company universe. Keep a company in Factory
when its technology, product, service, channel, or infrastructure can plausibly connect
to manufacturing, factories, machinery, quality, inspection, materials, components,
energy, construction, mobility, marine, aerospace, semiconductors, electronics,
B2B infrastructure, logistics, supply chain, warehouse, fleet, transportation,
procurement, inventory, or delivery. Logistics must stay in Factory in this project.
Do not fail for country, funding stage, unknown funding, size, software, SaaS,
marketplace, distributor, consulting, energy, construction, or logistics.

Move out of Factory only when official evidence shows a clearly consumer/non-industrial
business with no credible industrial or supply-chain connection. Personal education
and childcare stay outside Factory unless the official site shows industrial training,
workforce development, or factory use. Preserve every row in the SSOT. Eligibility and
priority are separate.

Return ONLY JSON:
{
  "official_url": "https://...",
  "evidence_urls": ["https://..."],
  "hq_country": "",
  "what_it_solves": "specific factual product or technology description",
  "vertical_terms": ["factual terms"],
  "customer_types": ["factual customer types"],
  "industrial_connection": "specific industrial or supply-chain connection",
  "keep_in_factory": true,
  "category": "Factory",
  "subcategory": "Robotics / Automation",
  "eligibility": "PASS|FAIL",
  "reason": "specific factual classification reason",
  "japan_status": "PRESENT|NOT_FOUND|UNKNOWN",
  "japan_distributor_status": "specific evidence or UNKNOWN",
  "japan_evidence_url": "",
  "confidence": "High|Medium|Low"
}

Rules:
- Use official first-party evidence URLs and do not invent facts.
- If the official website cannot be confirmed, set official_url="" and confidence="Low".
- For logistics, SCM, warehouse, fleet, transport, procurement, inventory, or delivery,
  set keep_in_factory=true, category="Factory", and subcategory="Industrial Logistics / SCM".
- Missing information alone is not a FAIL reason.
- category must be one of the categories in the company context.

COMPANY CONTEXT:
"""

def _text(value: Any) -> str:
    return str(value or "").strip()


def _url(value: Any) -> str:
    value = _text(value)
    return value if value.startswith(("http://", "https://")) else ""


def _is_logistics(result: dict) -> bool:
    terms = " ".join(
        _text(result.get(key))
        for key in ("what_it_solves", "industrial_connection", "subcategory", "category")
    )
    terms += " " + " ".join(_text(x) for x in result.get("vertical_terms", []) or [])
    low = terms.lower()
    return any(term in low for term in LOGISTICS)


def _is_consumer_education(result: dict) -> bool:
    terms = " ".join(
        _text(result.get(key))
        for key in (
            "what_it_solves", "industrial_connection", "subcategory",
            "category", "reason", "japan_distributor_status",
        )
    )
    terms += " " + " ".join(
        _text(x)
        for key in ("vertical_terms", "customer_types")
        for x in (result.get(key, []) or [])
    )
    low = terms.lower()
    consumer = any(term in low for term in CONSUMER_EDUCATION)
    industrial = any(term in low for term in INDUSTRIAL_EDUCATION)
    return consumer and not industrial and not _is_logistics(result)


def _normalize(result: dict) -> dict:
    official = _url(result.get("official_url"))
    evidence = []
    for value in [official] + list(result.get("evidence_urls", []) or []):
        value = _url(value)
        if value and value not in evidence:
            evidence.append(value)
    if not official:
        return {"status": "RESEARCH_ERROR", "error": "official_url_not_confirmed"}

    logistics = _is_logistics(result)
    consumer_education = _is_consumer_education(result)
    category = _text(result.get("category"))
    if category not in CATEGORIES:
        category = "その他"
    raw_keep = result.get("keep_in_factory")
    if isinstance(raw_keep, str):
        raw_keep = raw_keep.strip().lower() in {"true", "yes", "1"}
    keep = (bool(raw_keep) or logistics) and not consumer_education
    if keep:
        category = "Factory"
    if consumer_education:
        category = "その他"
    subcategory = _text(result.get("subcategory"))
    if logistics:
        subcategory = "Industrial Logistics / SCM"
    elif consumer_education:
        subcategory = "Education / Childcare"
    if not subcategory:
        subcategory = "Industrial Technology / B2B Infrastructure" if keep else "Non-AUMS"
    confidence = _text(result.get("confidence")).title()
    if confidence not in {"High", "Medium", "Low"}:
        confidence = "Low"
    return {
        "status": "COMPLETE",
        "official_url": official,
        "evidence_urls": evidence[:8],
        "hq_country": _text(result.get("hq_country")),
        "what_it_solves": _text(result.get("what_it_solves")),
        "industrial_connection": _text(result.get("industrial_connection")),
        "keep_in_factory": keep,
        "category": category,
        "subcategory": subcategory,
        "eligibility": "FAIL" if consumer_education else (
            _text(result.get("eligibility")).upper() or ("PASS" if keep else "FAIL")
        ),
        "reason": _text(result.get("reason")),
        "japan_status": _text(result.get("japan_status")).upper() or "UNKNOWN",
        "japan_distributor_status": _text(result.get("japan_distributor_status")) or "UNKNOWN",
        "japan_evidence_url": _url(result.get("japan_evidence_url")),
        "confidence": confidence,
    }


def _col(index: int) -> str:
    n = int(index) + 1
    out = ""
    while n:
        n, rem = divmod(n - 1, 26)
        out = chr(65 + rem) + out
    return out


def _row(headers: list[str], values: list[Any], number: int) -> dict:
    padded = list(values) + [""] * max(0, len(headers) - len(values))
    data = {str(header).strip(): padded[i] for i, header in enumerate(headers) if str(header).strip()}
    data["row_number"] = number
    return data


def _context(row: dict) -> str:
    keys = (
        "company_name", "hq_country", "funding_stage", "website",
        "what_it_solves", "japan_status", "japan_distributor_status",
        "subcategory", "Category", "ステータス理由", "source", "original_domain",
    )
    return json.dumps({key: row.get(key, "") for key in keys if key in row}, ensure_ascii=False)


def _research(factory, row: dict) -> dict:
    response = factory.llm.client.responses.create(
        model=factory.llm.model,
        tools=[{"type": "web_search"}],
        input=PROMPT + _context(row),
    )
    value = factory.llm._json(response.output_text)
    if not isinstance(value, dict):
        raise RuntimeError("human_ssot_research_not_object")
    return _normalize(value)


def _write(factory, sheet: str, headers: list[str], number: int, changes: dict) -> None:
    positions = {str(header).strip(): i for i, header in enumerate(headers) if str(header).strip()}
    data = []
    for key, value in changes.items():
        if key in positions:
            data.append({
                "range": "'{}'!{}{}".format(sheet.replace("'", "''"), _col(positions[key]), number),
                "values": [[value]],
            })
    if data:
        factory.sheets._execute_write(
            lambda: factory.sheets.svc.spreadsheets().values().batchUpdate(
                spreadsheetId=factory.sheets.spreadsheet_id,
                body={"valueInputOption": "RAW", "data": data},
            ).execute()
        )


def _reason(old: str, review_date: str, value: dict) -> str:
    detail = value["reason"] or value["industrial_connection"] or value["what_it_solves"]
    line = (
        "[全社再調査 {} rules={}] 公式URL確認: {} | {} | 分類={}/{} | Factory維持={}"
    ).format(
        review_date, REVIEW_RULE_VERSION, value["official_url"], detail, value["category"],
        value["subcategory"], "YES" if value["keep_in_factory"] else "NO",
    )
    old = _text(old)
    return "{}\n{}".format(old, line) if old else line


def _changes(row: dict, value: dict, review_date: str) -> dict:
    return {
        "Category": value["category"],
        "hq_country": value["hq_country"] or row.get("hq_country", ""),
        "website": value["official_url"],
        "ステータス理由": _reason(row.get("ステータス理由", ""), review_date, value),
        "what_it_solves": value["what_it_solves"] or row.get("what_it_solves", ""),
        "japan_status": value["japan_status"],
        "japan_distributor_status": value["japan_distributor_status"],
        "japan_evidence_url": value["japan_evidence_url"],
        "japan_checked_at": review_date,
        "subcategory": value["subcategory"],
        "classification_confidence": value["confidence"],
        "selection_reason": "{}: {}".format(
            "PASS" if value["keep_in_factory"] else "FAIL",
            value["reason"] or value["industrial_connection"] or value["what_it_solves"],
        ),
        "research_sources": " | ".join(value["evidence_urls"]),
        "reviewed_at": review_date,
    }


def _process_one(factory, sheet: str, headers: list[str], row: dict, review_date: str) -> dict:
    number = int(row["row_number"])
    previous = _text(row.get("reviewed_at", ""))
    _write(factory, sheet, headers, number, {"reviewed_at": "IN_PROGRESS:{}:{}".format(review_date, number)})
    try:
        value = _research(factory, row)
        if value.get("status") != "COMPLETE":
            raise RuntimeError(value.get("error") or "research_incomplete")
        _write(factory, sheet, headers, number, _changes(row, value, review_date))
        return {"row_number": number, "company_name": _text(row.get("company_name")), "status": "COMPLETE", "category": value["category"]}
    except Exception as exc:
        _write(factory, sheet, headers, number, {"reviewed_at": previous})
        print(
            "human-ssot-review:error row={} company={} error={}:{}".format(
                number, _text(row.get("company_name")), type(exc).__name__, exc
            ),
            flush=True,
        )
        return {"row_number": number, "company_name": _text(row.get("company_name")), "status": "RESEARCH_ERROR", "error": "{}:{}".format(type(exc).__name__, exc)}


def run_human_ssot_review_tick(factory, limit: int | None = None) -> dict:
    cfg = factory._config()
    if _text(cfg.get("LEAD_FACTORY_HUMAN_REVIEW_ENABLED", "TRUE")).upper() != "TRUE":
        return {"status": "DISABLED", "execution": "HUMAN_SSOT_REVIEW"}
    sheet = _text(cfg.get("LEAD_FACTORY_HUMAN_SSOT_SHEET")) or SSOT_DEFAULT
    review_date = datetime.now(JST).date().isoformat()
    try:
        requested = max(1, min(20, int(limit or cfg.get("LEAD_FACTORY_HUMAN_REVIEW_BATCH_SIZE", "6") or 6)))
    except (TypeError, ValueError):
        requested = 6

    raw = factory.sheets.read("'{}'!A1:V".format(sheet))
    if not raw:
        return {"status": "ERROR", "execution": "HUMAN_SSOT_REVIEW", "error": "empty_ssot"}
    headers = [str(value or "").strip() for value in raw[0]]
    rows = [_row(headers, value, number) for number, value in enumerate(raw[1:], start=2)]
    candidates = [
        value for value in rows
        if _text(value.get("company_name"))
        and (
            _text(value.get("reviewed_at")) != review_date
            or REVIEW_RULE_VERSION not in _text(value.get("ステータス理由"))
        )
        and not _text(value.get("reviewed_at")).startswith("IN_PROGRESS:")
    ][:requested]
    if not candidates:
        return {
            "status": "EXHAUSTED",
            "execution": "HUMAN_SSOT_REVIEW",
            "review_date": review_date,
            "requested": requested,
            "candidates": 0,
            "completed": 0,
            "research_errors": 0,
            "remaining_unreviewed": 0,
        }

    workers = max(1, min(8, len(candidates)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        results = [future.result() for future in [
            pool.submit(_process_one, factory, sheet, headers, value, review_date)
            for value in candidates
        ]]
    remaining = 0
    for value in factory.sheets.read("'{}'!A2:V".format(sheet)):
        padded = list(value) + [""] * max(0, len(headers) - len(value))
        if (
            _text(padded[0])
            and (
                _text(padded[headers.index("reviewed_at")]) != review_date
                or REVIEW_RULE_VERSION not in _text(padded[headers.index("ステータス理由")])
            )
        ):
            remaining += 1
    return {
        "status": "PROGRESS",
        "execution": "HUMAN_SSOT_REVIEW",
        "review_date": review_date,
        "requested": requested,
        "candidates": len(candidates),
        "completed": sum(1 for value in results if value.get("status") == "COMPLETE"),
        "research_errors": sum(1 for value in results if value.get("status") == "RESEARCH_ERROR"),
        "remaining_unreviewed": remaining,
        "results": results,
    }
