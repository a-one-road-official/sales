from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any


UTC = timezone.utc
JST = timezone(timedelta(hours=9))
THROUGHPUT_MIN_PER_MINUTE = 30
THROUGHPUT_TARGET_PER_MINUTE = 60
THROUGHPUT_WINDOW_MINUTES = 5
THROUGHPUT_LONG_WINDOW_MINUTES = 10
GOAL_KEYS = {
    "target": "LEAD_FACTORY_GOAL_TARGET",
    "start_at": "LEAD_FACTORY_GOAL_START_AT",
    "deadline": "LEAD_FACTORY_GOAL_DEADLINE",
    "baseline": "LEAD_FACTORY_GOAL_BASELINE_SSOT",
    "status": "LEAD_FACTORY_GOAL_STATUS",
    "report_sent": "LEAD_FACTORY_GOAL_REPORT_SENT",
}


def _dt(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)
    except (TypeError, ValueError):
        return None


class QualifiedLeadProductionController:
    """Persistent daily Qualified-Lead SLO controller.

    Goal, baseline and report state live in Config so a Cloud Run restart cannot
    reset the production requirement. Gate evaluation is delegated unchanged to
    the factory.
    """

    def __init__(self, factory):
        self.factory = factory
        self.sheets = factory.sheets
        self.notifier = getattr(factory, "notifier", None)

    def _config(self) -> dict[str, str]:
        return self.sheets.get_config()

    def _set_config(self, values: dict[str, Any]) -> None:
        rows = self.sheets.read("Config!A2:B1000")
        positions = {str(r[0]): i for i, r in enumerate(rows, start=2) if len(r) >= 1 and r[0]}
        for key, value in values.items():
            rendered = str(value)
            if key in positions:
                self.sheets.update_range(f"Config!B{positions[key]}", [[rendered]])
            else:
                self.sheets.append("Config", [key, rendered])

    def _ssot_count(self) -> int:
        counter = getattr(self.sheets, "count_valid_qualified_ssot", None)
        if callable(counter):
            return int(counter())
        cfg = self._config()
        sheet = cfg.get("LEAD_FACTORY_HUMAN_SSOT_SHEET", "営業リスト＿Factory/BPO")
        rows = self.sheets.read(f"'{sheet}'!A2:A")
        return sum(
            1
            for row in rows
            if row and str(row[0]).strip() and str(row[0]).strip().upper() not in {"#N/A", "#REF!", "#VALUE!"}
        )

    def _queue_counts(self) -> dict[str, int]:
        def count(range_name: str, predicate=None) -> int:
            rows = self.sheets.read(range_name)
            if predicate is None:
                return sum(1 for r in rows if r and any(str(x).strip() for x in r))
            return sum(1 for r in rows if predicate(r))
        raw = self.sheets.read("LeadFactory_Raw!A2:R")
        domain = sum(1 for r in raw if len(r) > 17 and str(r[17]).upper() == "NEEDS_DOMAIN")
        gate = sum(1 for r in raw if len(r) > 17 and str(r[17]).upper() in {"READY_FOR_GATE", "READY_FOR_MITTELSTAND_GATE"})
        return {
            "raw_backlog": len([r for r in raw if r and str(r[0]).strip()]),
            "domain_backlog": domain,
            "gate_backlog": gate,
            "active_sources": len([s for s in self.sheets.list_sources() if str(s.crawl_status).upper() not in {"ERROR", "CIRCUIT_OPEN", "AUTH_REQUIRED"}]),
        }

    def _throughput_metrics(self) -> dict[str, Any]:
        """Measure qualified SSOT promotions over short control windows."""
        now = datetime.now(UTC)
        windows = {5: 0, 10: 0}
        latest_finished = None
        try:
            rows = self.sheets.read("LeadFactory_RunLog!B2:L")
        except Exception as exc:
            return {"throughput_status": "METRICS_UNAVAILABLE", "throughput_error": str(exc)[:300]}
        for row in rows:
            padded = row + [""] * (11 - len(row))
            finished = _dt(padded[1])
            if not finished:
                continue
            if latest_finished is None or finished > latest_finished:
                latest_finished = finished
            try:
                promoted = int(float(str(padded[10] or "0")))
            except (TypeError, ValueError):
                promoted = 0
            age_min = (now - finished).total_seconds() / 60.0
            for minutes in windows:
                if 0 <= age_min <= minutes:
                    windows[minutes] += promoted
        five = windows[5]
        ten = windows[10]
        five_rate = five / 5.0
        ten_rate = ten / 10.0
        breach = five < THROUGHPUT_MIN_PER_MINUTE * 5 or ten < 100
        return {
            "promoted_last_5m": five,
            "promoted_last_10m": ten,
            "qualified_per_minute_5m": round(five_rate, 2),
            "qualified_per_minute_10m": round(ten_rate, 2),
            "throughput_minimum_5m": THROUGHPUT_MIN_PER_MINUTE * 5,
            "throughput_minimum_10m": 100,
            "throughput_target_5m": THROUGHPUT_TARGET_PER_MINUTE * 5,
            "throughput_status": "THROUGHPUT_BREACH" if breach else ("ON_TARGET" if five >= THROUGHPUT_TARGET_PER_MINUTE * 5 else "ABOVE_MINIMUM"),
            "latest_promotion_at": latest_finished.isoformat() if latest_finished else "",
        }

    def status(self) -> dict:
        cfg = self._config()
        target = int(cfg.get(GOAL_KEYS["target"], "0") or 0)
        baseline = int(cfg.get(GOAL_KEYS["baseline"], "0") or 0)
        current = self._ssot_count()
        start = _dt(cfg.get(GOAL_KEYS["start_at"], ""))
        deadline = _dt(cfg.get(GOAL_KEYS["deadline"], ""))
        now = datetime.now(UTC)
        elapsed_h = max(0.0, (now - start).total_seconds() / 3600) if start else 0.0
        remaining_h = max(0.0, (deadline - now).total_seconds() / 3600) if deadline else 0.0
        accounting = {}
        snapshot = getattr(self.sheets, "promotion_accounting_snapshot", None)
        if callable(snapshot):
            try:
                accounting = snapshot(baseline, cfg.get(GOAL_KEYS["start_at"], ""))
                current = int(accounting.get("current_qualified_ssot", current) or current)
                added = int(accounting.get("daily_added", 0) or 0)
            except Exception as exc:
                accounting = {
                    "accounting_status": "UNAVAILABLE",
                    "accounting_error": f"{type(exc).__name__}:{exc}",
                }
                added = max(0, current - baseline)
        else:
            added = max(0, current - baseline)
        accounted = int(accounting.get("accounted_qualified_ssot", baseline + added) or (baseline + added))
        drift = int(accounting.get("accounting_drift", current - accounted) or (current - accounted))
        velocity = added / elapsed_h if elapsed_h > 0 else 0.0
        required = max(0, target - added) / remaining_h if remaining_h > 0 else 0.0
        forecast = added + velocity * remaining_h
        metrics = self._queue_counts()
        throughput = self._throughput_metrics()
        return {
            "status": cfg.get(GOAL_KEYS["status"], "NO_GOAL"),
            "target": target,
            "baseline_ssot_count": baseline,
            "current_ssot_count": current,
            "current_qualified_ssot_count": current,
            "accounted_qualified_ssot_count": accounted,
            "accounting_drift": drift,
            "added": added,
            "daily_added": added,
            "remaining": max(0, target - added),
            "elapsed_hours": round(elapsed_h, 3),
            "hours_remaining": round(remaining_h, 3),
            "actual_velocity_per_hour": round(velocity, 3),
            "required_velocity_per_hour": round(required, 3),
            "eod_forecast": round(forecast, 3),
            **accounting,
            **metrics,
            **throughput,
        }

    def start(self, target: int, deadline: datetime | None = None, force_reset: bool = False) -> dict:
        if int(target) <= 0:
            raise ValueError("goal_target_must_be_positive")
        existing = self._config()
        now = datetime.now(UTC)
        end = deadline or now.astimezone(JST).replace(hour=23, minute=59, second=59, microsecond=0).astimezone(UTC)
        if deadline and deadline <= now:
            raise ValueError("goal_deadline_must_be_future")

        # A normal redeploy preserves an active goal; only an explicit one-time
        # force_reset starts a fresh accounting window.
        existing_target = int(existing.get(GOAL_KEYS["target"], "0") or 0)
        existing_status = str(existing.get(GOAL_KEYS["status"], "")).upper()
        existing_deadline = _dt(existing.get(GOAL_KEYS["deadline"], ""))
        if (
            not force_reset
            and existing_target == int(target)
            and existing_status in {"RUNNING", "AT_RISK"}
            and existing_deadline
            and existing_deadline > now
        ):
            return self.status()

        baseline = self._ssot_count()
        self._set_config({
            GOAL_KEYS["target"]: int(target),
            GOAL_KEYS["start_at"]: now.isoformat(),
            GOAL_KEYS["deadline"]: end.isoformat(),
            GOAL_KEYS["baseline"]: baseline,
            GOAL_KEYS["status"]: "RUNNING",
            GOAL_KEYS["report_sent"]: "FALSE",
        })
        return self.status()

    def stop(self) -> dict:
        self._set_config({GOAL_KEYS["status"]: "USER_STOP"})
        return self.status()

    def _report(self, status: dict) -> dict:
        if str(self._config().get(GOAL_KEYS["report_sent"], "FALSE")).upper() == "TRUE":
            return {"status": "ALREADY_SENT"}
        subject = f"A-one Lead Factory Daily Result — {status['added']} / {status['target']}"
        body = "\n".join(f"{key}: {value}" for key, value in status.items())
        result = self.notifier.notify(subject, body) if self.notifier else {"status": "SKIPPED", "reason": "notifier_unavailable"}
        if result.get("status") in {"SENT", "SKIPPED"}:
            self._set_config({GOAL_KEYS["report_sent"]: "TRUE"})
        return result

    def _rollover_daily_goal(self, previous: dict) -> dict:
        """Start the next JST production day after reporting the prior day."""
        previous_report = self._report(previous)
        now = datetime.now(UTC)
        jst_now = now.astimezone(JST)
        next_deadline = jst_now.replace(
            hour=23, minute=59, second=59, microsecond=0
        ).astimezone(UTC)
        self._set_config({
            GOAL_KEYS["start_at"]: now.isoformat(),
            GOAL_KEYS["deadline"]: next_deadline.isoformat(),
            GOAL_KEYS["baseline"]: self._ssot_count(),
            GOAL_KEYS["status"]: "RUNNING",
            GOAL_KEYS["report_sent"]: "FALSE",
        })
        current = self.status()
        current["previous_day"] = {
            "status": previous.get("status"),
            "target": previous.get("target"),
            "added": previous.get("added"),
            "eod_forecast": previous.get("eod_forecast"),
            "report": previous_report,
        }
        return current

    def tick(self) -> dict:
        status = self.status()
        goal_status = status["status"]
        if goal_status in {"TARGET_ACHIEVED", "DEADLINE_REACHED"}:
            deadline = _dt(self._config().get(GOAL_KEYS["deadline"], ""))
            if deadline and datetime.now(UTC) >= deadline:
                return self._rollover_daily_goal(status)
        if goal_status in {"NO_GOAL", "USER_STOP", "REPORT_SENT"}:
            return status
        if status["remaining"] > 0 and status["hours_remaining"] > 0:
            # Capacity expansion is deliberately isolated from the Gate. The
            # factory may add sources/workers, while its authoritative Gate stays fixed.
            throughput_breach = status.get("throughput_status") == "THROUGHPUT_BREACH"
            if status["eod_forecast"] < status["target"] or throughput_breach:
                action = self.factory.capacity_tick(status)
                status["capacity_action"] = action
            status["status"] = "AT_RISK" if status.get("capacity_action") else "RUNNING"
            return status
        final_status = "TARGET_ACHIEVED" if status["remaining"] == 0 else "DEADLINE_REACHED"
        self._set_config({GOAL_KEYS["status"]: final_status})
        status["status"] = final_status
        status["report"] = self._report(status)
        return status



from single_sheet_controller_mode import install as _install_single_sheet_controller
_install_single_sheet_controller(QualifiedLeadProductionController)
