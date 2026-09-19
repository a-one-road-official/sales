"""One shared, evidence-backed AI email pipeline. Never calls a paid API."""
from __future__ import annotations
import hashlib
import re
from pathlib import Path

PROMPT_PATH = Path(__file__).with_name("prompts") / "japan_outreach_master.txt"
CALENDAR_URL = "https://calendar.app.google/adKEhXC4UWhQXfJp6"
SIGNATURE = "Best regards,\nKazuma Tamura\nFounder & CEO, A-one Road Co., Ltd. (Yokohama, Japan)\n\n────────────────────\nすべての「隠れた価値」の語り部に。\nUnearth All \"Hidden\" Value.\nエーワンロード株式会社 / A-one road Co., Ltd.\n代表取締役　田村 一馬 / Kazuma Tamura, Founder & CEO\n〒220-0072 神奈川県横浜市西区浅間町1丁目4-3 ウィザードビル402\n古物商許可｜第306622619455号\nWebsite｜https://a-oneroad.com/\nBlog｜https://note.com/kazumat\nE-mail｜admin@a1-road.com\nMobile｜+81 80-4870-5690\n────────────────────"

def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def read_prompt():
    text = PROMPT_PATH.read_text(encoding="utf-8")
    return text, _hash(text)

def verify_prompt_revision(draft):
    if draft.get("master_prompt_hash") != read_prompt()[1]:
        raise ValueError("MASTER_PROMPT_CHANGED_REGENERATE")

def validate_email(draft):
    parts = str(draft.get("body") or "").split("\n\n")
    if len(parts) < 6 or not re.fullmatch(r"Hi .+,", parts[0]):
        raise ValueError("EMAIL_LAYOUT")
    p = parts[1:4]
    if parts[4] != CALENDAR_URL or "\n\n".join(parts[5:]) != SIGNATURE:
        raise ValueError("CALENDAR_OR_SIGNATURE")
    n = len(" ".join(p).split())
    if not 110 <= n <= 120:
        raise ValueError(f"BODY_WORD_COUNT: {n}; target 110-120")
    for i, text in enumerate(p):
        if len(re.split(r"(?<=[.!?])\s+", text)) not in ({2} if i == 0 else {2,3}):
            raise ValueError("PARAGRAPH_SENTENCES")
    text = " ".join(p)
    lower = text.lower()
    if not re.search(r"\bpaid\b", p[2]) or not re.search(r"\b(?:a year|one year|12 months|twelve months)\b", p[2]):
        raise ValueError("ANNUAL_PAID_OFFER")
    if text.count("?") != 1 or not p[2].endswith("?"):
        raise ValueError("SINGLE_FINAL_CTA")
    prohibited = ("if this is not relevant", "if irrelevant", "won't follow up", "won’t follow up", "will not follow up",
        "sent over the first", "worth discussing", "without hiring", "your first japanese customers",
        "recurring revenue", "caddi", "i hope this email finds you well", "i came across your company",
        "revolutionary", "cutting-edge", "synergy", "book a call", "calendly", "six months",
        "we selected", "we chose", "we are inviting you", "we would like you to", "we want you to",
        "bring you customers", "find you customers", "guaranteed")
    if any(term in lower for term in prohibited) or re.search(r"\b(?:meeting|availability|minutes?)\b", text, re.I):
        raise ValueError("FORBIDDEN_COPY")
    if re.search(r"https?://", text) or re.search(r"[$€£¥￥]|\b(?:USD|EUR|GBP|JPY)\b|trade show", p[2], re.I):
        raise ValueError("BODY_LINK_OR_OFFER_PRICE")
    return n
