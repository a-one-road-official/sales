from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable


class MetaGateBlocked(RuntimeError):
    pass


@dataclass
class MetaDecision:
    requested_scope_ok: bool
    target_exists_checked: bool
    destructive: bool
    external_effect: bool
    test_passed: bool | None
    simpler_option_checked: bool
    concept_boundary_ok: bool
    fact_or_inference: str
    decision: str
    reason: str


class MetaSupervisor:
    """A deterministic 'てめえ大丈夫？' gate.

    It runs before important actions and can heartbeat every 120 seconds during long jobs.
    It is intentionally boring: safety here is enforced by code, not model mood.
    """

    def __init__(self, logger: Callable[[dict], None], interval_seconds: int = 120):
        self.logger = logger
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._run_id = ""
        self._stage = ""
        self._worker_id = str(uuid.uuid4())
        self._last_action_ts = time.time()

    def check(
        self,
        *,
        run_id: str,
        stage: str,
        action: str,
        target: str,
        requested_scope_ok: bool,
        target_exists_checked: bool,
        destructive: bool = False,
        external_effect: bool = False,
        test_passed: bool | None = None,
        simpler_option_checked: bool = True,
        concept_boundary_ok: bool = True,
        fact_or_inference: str = "FACT",
        reason: str = "",
    ) -> MetaDecision:
        blocked = []
        if not requested_scope_ok:
            blocked.append("outside_requested_scope")
        if destructive and not target_exists_checked:
            blocked.append("destructive_without_target_recheck")
        if destructive:
            blocked.append("autonomous_delete_disabled")
        if external_effect:
            blocked.append("cross_org_boundary_requires_human_interlock")
        if test_passed is False:
            blocked.append("previous_stage_test_failed")
        if not concept_boundary_ok:
            blocked.append("concept_boundary_confusion")

        decision = "BLOCK" if blocked else "ALLOW"
        final_reason = reason or (",".join(blocked) if blocked else "meta_gate_pass")
        row = {
            "meta_id": f"meta-{uuid.uuid4()}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": run_id,
            "stage": stage,
            "action": action,
            "target": target,
            "requested_scope_ok": requested_scope_ok,
            "target_exists_checked": target_exists_checked,
            "destructive": destructive,
            "external_effect": external_effect,
            "test_passed": "" if test_passed is None else test_passed,
            "simpler_option_checked": simpler_option_checked,
            "concept_boundary_ok": concept_boundary_ok,
            "fact_or_inference": fact_or_inference,
            "decision": decision,
            "reason": final_reason,
            "heartbeat_age_sec": 0,
            "worker_id": self._worker_id,
        }
        self.logger(row)
        self._last_action_ts = time.time()
        if blocked:
            raise MetaGateBlocked(final_reason)
        return MetaDecision(
            requested_scope_ok=requested_scope_ok,
            target_exists_checked=target_exists_checked,
            destructive=destructive,
            external_effect=external_effect,
            test_passed=test_passed,
            simpler_option_checked=simpler_option_checked,
            concept_boundary_ok=concept_boundary_ok,
            fact_or_inference=fact_or_inference,
            decision=decision,
            reason=final_reason,
        )

    def start_heartbeat(self, run_id: str, stage: str) -> None:
        self._run_id = run_id
        self._stage = stage
        if self._thread and self._thread.is_alive():
            return

        def loop() -> None:
            while not self._stop.wait(self.interval_seconds):
                age = int(time.time() - self._last_action_ts)
                self.logger({
                    "meta_id": f"meta-{uuid.uuid4()}",
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "run_id": self._run_id,
                    "stage": self._stage,
                    "action": "HEARTBEAT_META_REVIEW",
                    "target": "CURRENT_RUN",
                    "requested_scope_ok": True,
                    "target_exists_checked": True,
                    "destructive": False,
                    "external_effect": False,
                    "test_passed": "",
                    "simpler_option_checked": True,
                    "concept_boundary_ok": True,
                    "fact_or_inference": "FACT",
                    "decision": "ALLOW",
                    "reason": "120s meta heartbeat: no destructive/external action permitted",
                    "heartbeat_age_sec": age,
                    "worker_id": self._worker_id,
                })

        self._stop.clear()
        self._thread = threading.Thread(target=loop, daemon=True)
        self._thread.start()

    def stop_heartbeat(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
