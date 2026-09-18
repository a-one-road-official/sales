from __future__ import annotations

import os
from typing import Mapping


FALSE_VALUES = {"", "0", "FALSE", "NO", "OFF", "DISABLED"}
TRUE_VALUES = {"1", "TRUE", "YES", "ON", "ENABLED"}

# User-declared budget, exhausted on 2026-09-17. Never reset from the clock,
# environment, Sheets settings, a deployment, or an automatic repair.
AI_API_BUDGET_LIMIT_JPY = 10000
AI_API_REMAINING_BUDGET_JPY = 0
PAID_AI_FORBIDDEN = True

# Step 1 of cost retirement: freeze only future executions of these old deploy
# workflows. Their existing gate runs before Google auth/build/push/mutations.
# This does not stop a running service, crawler, sender, or shared authentication.
RETIRED_CLOUD_DEPLOY_WORKFLOWS = {
    "deploy.yml": "Deploy Lead Factory",
    "deploy_paused_repair.yml": "Deploy paused SSOT history repair",
}


def assert_no_legacy_cloud_deployment(env: Mapping[str, object] | None = None) -> None:
    """Block the two retired deploy entrypoints; preserve ordinary free jobs.

    Scope: current code invoking this CLI gate. This is not an IAM deny and does
    not revoke older commits, other repositories, or already-running jobs.
    Re-enabling a retired entrypoint requires a reviewed code change, not a flag.
    """
    source = os.environ if env is None else env
    workflow = str(source.get("GITHUB_WORKFLOW", ""))
    workflow_ref = str(source.get("GITHUB_WORKFLOW_REF", "")).split("@", 1)[0]
    prefix = "a-one-road-official/sales/.github/workflows/"
    for filename, name in RETIRED_CLOUD_DEPLOY_WORKFLOWS.items():
        if workflow == name or workflow_ref == prefix + filename:
            raise RuntimeError("legacy_cloud_deployment_retired:" + filename)


def assert_zero_ai_budget(config: Mapping[str, object] | None = None) -> None:
    """Reject a deployment/startup attempting to reopen paid inference."""
    sources = [os.environ, config or {}]
    flags = ("LEAD_FACTORY_PAID_AI_ALLOWED", "LEAD_FACTORY_VERTEX_ALLOWED", "PAID_AI_ENABLED")
    for source in sources:
        for key in flags:
            if str(source.get(key, "FALSE")).strip().upper() not in FALSE_VALUES:
                raise RuntimeError("paid_ai_budget_exhausted:" + key)
    if not PAID_AI_FORBIDDEN or AI_API_REMAINING_BUDGET_JPY != 0:
        raise RuntimeError("deployment_requires_zero_paid_ai_budget")


def require_paid_ai(action: str) -> None:
    raise RuntimeError("paid_ai_budget_exhausted:" + str(action or "unknown"))


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
    """Budget is exhausted; environment opt-ins cannot authorize more spend."""
    return False


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
        "ai_api_budget_limit_jpy": AI_API_BUDGET_LIMIT_JPY,
        "ai_api_remaining_budget_jpy": AI_API_REMAINING_BUDGET_JPY,
        "ai_budget_basis": "user_declared_exhausted_2026-09-17",
        "autonomous_capacity_expansion": autonomous_capacity_allowed(),
        "auto_rollover_daily_goal": daily_rollover_allowed(),
        "http_self_fallback": http_self_fallback_allowed(),
        "cloud_run": is_cloud_run(),
    }


if __name__ == "__main__":
    import json
    assert_no_legacy_cloud_deployment()
    assert_zero_ai_budget()
    print(json.dumps(budget_snapshot(), sort_keys=True))
