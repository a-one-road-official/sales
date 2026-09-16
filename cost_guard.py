from __future__ import annotations

import os
from typing import Mapping


FALSE_VALUES = {"", "0", "FALSE", "NO", "OFF", "DISABLED"}
TRUE_VALUES = {"1", "TRUE", "YES", "ON", "ENABLED"}


def _truthy(value: object, default: bool = False) -> bool:
    raw = str(value or "").strip().upper()
    if raw in TRUE_VALUES:
        return True
    if raw in FALSE_VALUES:
        return False
    return bool(default)


def env_truthy(name: str, default: bool = False) -> bool:
    return _truthy(os.getenv(name, "TRUE" if default else "FALSE"), default=default)


def is_cloud_run() -> bool:
    """Return whether this process is executing inside a managed Cloud Run revision."""
    return bool(os.getenv("K_SERVICE") or os.getenv("K_REVISION") or os.getenv("K_CONFIGURATION"))


def paid_cloud_allowed() -> bool:
    """Hard budget gate. Paid autonomous cloud execution requires an explicit opt-in."""
    return env_truthy("LEAD_FACTORY_PAID_CLOUD_ALLOWED", default=False)


def paid_ai_allowed() -> bool:
    """Paid model/search usage requires both the global cloud budget gate and an AI opt-in."""
    return paid_cloud_allowed() and env_truthy("LEAD_FACTORY_PAID_AI_ALLOWED", default=False)


def autonomous_capacity_allowed() -> bool:
    return paid_cloud_allowed() and env_truthy(
        "LEAD_FACTORY_AUTONOMOUS_CAPACITY_EXPANSION", default=False
    )


def daily_rollover_allowed() -> bool:
    return paid_cloud_allowed() and env_truthy(
        "LEAD_FACTORY_AUTO_ROLLOVER_DAILY_GOAL", default=False
    )


def http_self_fallback_allowed() -> bool:
    return paid_cloud_allowed() and env_truthy(
        "LEAD_FACTORY_ALLOW_HTTP_SELF_FALLBACK", default=False
    )


def require_paid_cloud(action: str) -> None:
    if not paid_cloud_allowed():
        raise RuntimeError(f"paid_cloud_disabled:{str(action or 'unknown')}")


def bounded_int(
    config: Mapping[str, object] | None,
    key: str,
    *,
    default: int,
    hard_max: int,
    minimum: int = 0,
) -> int:
    """Read an integer control with a code-level hard ceiling.

    Sheet Config and environment values can request less work, but cannot exceed
    the ceiling without a code change. This prevents stale Config values from
    resurrecting historical fan-out after a redeploy.
    """
    config = config or {}
    raw = config.get(key, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return max(int(minimum), min(int(hard_max), value))


def budget_snapshot() -> dict[str, object]:
    return {
        "paid_cloud_allowed": paid_cloud_allowed(),
        "paid_ai_allowed": paid_ai_allowed(),
        "autonomous_capacity_expansion": autonomous_capacity_allowed(),
        "auto_rollover_daily_goal": daily_rollover_allowed(),
        "http_self_fallback": http_self_fallback_allowed(),
        "cloud_run": is_cloud_run(),
    }
