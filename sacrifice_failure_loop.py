"""Failure-driven loop for the ten-company sales_leads sacrifice canary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


FAILURE_RULES = (
    ("NO_CHANNEL_FOUND", ("メールアドレスも問い合わせフォームも見つからなかった", "no_channel_found", "no_email_or_public_form")),
    ("BOT_DEFENSE", ("bot対策", "captcha", "recaptcha", "turnstile", "access_denied", "http_403_blocked")),
    ("IFRAME_UNSUPPORTED", ("iframe", "iframe_form_skip")),\n    ("FORM_MAPPING", ("form_not_found", "form_action_host_unverified", "form_host_unverified", "required_unmapped")),
    ("GENERATION_FAILED", ("AI生成", "generation_failed", "生テンプレート")),
    ("SUBMIT_UNCONFIRMED", ("送信ボタンのクリック", "successの確認", "send_failed", "送信失敗")),
    ("TIMEOUT", ("75秒", "ハング", "timeout")),
    ("DUPLICATE", ("重複", "duplicate")),
    ("CIRCUIT_BREAKER", ("サーキットブレーカー", "circuit_breaker")),
    ("POLICY_EXCLUDED", ("営業禁止", "do_not_contact", "プライム大企業")),
)


@dataclass(frozen=True)
class Failure:
    code: str
    raw_status: str
    raw_stage: str
    raw_error: str
    repair: str


def classify_failure(row: dict) -> Failure:
    raw = " ".join(str(row.get(key) or "") for key in ("status", "stage", "error_message"))
    lowered = raw.lower()
    for code, needles in FAILURE_RULES:
        if any(needle.lower() in lowered for needle in needles):
            repair = {
                "NO_CHANNEL_FOUND": "独自ドメイン再調査→公式メール/フォーム候補を証拠付きで再取得",
                "BOT_DEFENSE": "自動突破せずMANUAL_REQUIREDへ分類し、送信成功率から除外",
                "IFRAME_UNSUPPORTED": "same-origin/外部providerを判定し、非対応はMANUAL_REQUIRED",\n                "FORM_MAPPING": "フォームの必須項目マッピングを再評価し、未確定なら送信せず記録",
                "GENERATION_FAILED": "live prompt適用と会社固有事実検査に通らない本文を破棄",
                "SUBMIT_UNCONFIRMED": "最終送信・完了シグナルを複数観測し、確認画面を成功扱いしない",
                "TIMEOUT": "エラーfingerprintを保存し、同一対象の無限再試行を止める",
                "DUPLICATE": "会社・宛先・本文hashのidempotencyを実行前に確認",
                "CIRCUIT_BREAKER": "停止理由を復旧可能なテスト設定へ分離し、対象レーンだけ再開",
                "POLICY_EXCLUDED": "対象選定時に除外し、送信試行しない",
            }[code]
            return Failure(code, str(row.get("status") or ""), str(row.get("stage") or ""), str(row.get("error_message") or ""), repair)
    return Failure("UNKNOWN", str(row.get("status") or ""), str(row.get("stage") or ""), str(row.get("error_message") or ""), "DOM/network snapshotを保存してfixture化")


def classify_batch(rows: Iterable[dict]) -> dict:
    failures = [classify_failure(row) for row in rows]
    counts: dict[str, int] = {}
    for failure in failures:
        counts[failure.code] = counts.get(failure.code, 0) + 1
    return {"attempted": len(failures), "failure_counts": counts, "failures": [f.__dict__ for f in failures]}


def batch_gate(results: Iterable[dict], required_successes: int = 5) -> dict:
    results = list(results)
    critical = []
    successes = 0
    for result in results:
        status = str(result.get("status") or "").strip().upper()
        if result.get("semantic_success") is True or status in {"SENT", "FORM_SENT"}:
            successes += 1
        critical.extend(str(value) for value in result.get("critical_errors", []) if value)
        preflight = result.get("preflight") or {}
        critical.extend(str(value) for value in preflight.get("critical_errors", []) if value)
    passed = len(results) == 10 and successes >= required_successes and not critical
    return {
        "attempted": len(results),
        "semantic_success": successes,
        "critical_errors": sorted(set(critical)),
        "batch_status": "PASS" if passed else "FAIL",
        "next_action": "PROMOTE_TO_NEXT_CANARY" if passed else "CLASSIFY_FAILURES_AND_PATCH",
    }

