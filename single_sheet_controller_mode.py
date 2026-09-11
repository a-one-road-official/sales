from __future__ import annotations

from datetime import datetime

from production_controller import UTC, THROUGHPUT_MIN_PER_MINUTE, THROUGHPUT_TARGET_PER_MINUTE, _dt


def install(cls):
    def _config(self):
        cfg = dict(self.sheets.get_config())
        cfg.update(getattr(self, "_runtime_goal_config", {}))
        return cfg

    def _set_config(self, values):
        current = dict(getattr(self, "_runtime_goal_config", {}))
        current.update({str(k): str(v) for k, v in values.items()})
        self._runtime_goal_config = current

    def _queue_counts(self):
        rows = self.sheets._single_ssot_rows() if hasattr(self.sheets, "_single_ssot_rows") else []
        raw = domain = gate = 0
        for row in rows:
            if not str(row.get("LF_lead_id") or "").strip():
                continue
            raw += 1
            intake = str(row.get("LF_intake_status") or "").upper()
            if intake == "NEEDS_DOMAIN":
                domain += 1
            if intake in {"READY_FOR_GATE", "READY_FOR_MITTELSTAND_GATE"}:
                gate += 1
        try:
            active_sources = len(self.sheets.list_sources())
        except Exception:
            active_sources = 0
        return {
            "raw_backlog": raw,
            "domain_backlog": domain,
            "gate_backlog": gate,
            "active_sources": active_sources,
        }

    def _throughput_metrics(self):
        now = datetime.now(UTC)
        windows = {5: 0, 10: 0}
        latest = None
        rows = self.sheets._single_ssot_rows() if hasattr(self.sheets, "_single_ssot_rows") else []
        for row in rows:
            if str(row.get("LF_screening_status") or "").upper() not in {"GO", "PASS"}:
                continue
            finished = _dt(str(row.get("added_at") or row.get("LF_last_screened_at") or ""))
            if not finished:
                continue
            latest = finished if latest is None or finished > latest else latest
            age = (now - finished).total_seconds() / 60.0
            for minutes in windows:
                if 0 <= age <= minutes:
                    windows[minutes] += 1
        five, ten = windows[5], windows[10]
        breach = five < THROUGHPUT_MIN_PER_MINUTE * 5 or ten < 100
        return {
            "promoted_last_5m": five,
            "promoted_last_10m": ten,
            "qualified_per_minute_5m": round(five / 5.0, 2),
            "qualified_per_minute_10m": round(ten / 10.0, 2),
            "throughput_minimum_5m": THROUGHPUT_MIN_PER_MINUTE * 5,
            "throughput_minimum_10m": 100,
            "throughput_target_5m": THROUGHPUT_TARGET_PER_MINUTE * 5,
            "throughput_status": "THROUGHPUT_BREACH" if breach else (
                "ON_TARGET" if five >= THROUGHPUT_TARGET_PER_MINUTE * 5 else "ABOVE_MINIMUM"
            ),
            "latest_promotion_at": latest.isoformat() if latest else "",
        }

    cls._config = _config
    cls._set_config = _set_config
    cls._queue_counts = _queue_counts
    cls._throughput_metrics = _throughput_metrics
