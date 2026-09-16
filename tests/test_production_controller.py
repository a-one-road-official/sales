from datetime import datetime, timedelta, timezone

from production_controller import QualifiedLeadProductionController


class FakeSheets:
    def __init__(self):
        self.config = {}

    def get_config(self):
        return dict(self.config)

    def read(self, range_):
        if range_.startswith("Config!"):
            return [[k, v] for k, v in self.config.items()]
        if "営業リスト" in range_:
            return [["existing"], ["existing-2"]]
        if range_.startswith("LeadFactory_Raw"):
            return []
        return []

    def update_range(self, range_, values):
        return None

    def append(self, sheet, values):
        if sheet == "Config":
            self.config[str(values[0])] = str(values[1])

    def list_sources(self):
        return []


class FakeFactory:
    def __init__(self):
        self.sheets = FakeSheets()
        self.capacity_calls = 0
        self.notifier = None

    def capacity_tick(self, status):
        self.capacity_calls += 1
        return {"status": "CALLED"}


def test_start_persists_goal_and_baseline():
    factory = FakeFactory()
    controller = QualifiedLeadProductionController(factory)
    status = controller.start(1500, datetime.now(timezone.utc) + timedelta(days=1))
    assert status["target"] == 1500
    assert status["baseline_ssot_count"] == 2
    assert factory.sheets.config["LEAD_FACTORY_GOAL_STATUS"] == "RUNNING"


def test_at_risk_does_not_expand_capacity_by_default(monkeypatch):
    monkeypatch.setenv("LEAD_FACTORY_PAID_CLOUD_ALLOWED", "FALSE")
    monkeypatch.setenv("LEAD_FACTORY_AUTONOMOUS_CAPACITY_EXPANSION", "FALSE")
    factory = FakeFactory()
    controller = QualifiedLeadProductionController(factory)
    controller.start(1500, datetime.now(timezone.utc) + timedelta(hours=1))
    status = controller.tick()
    assert factory.capacity_calls == 0
    assert status["capacity_action"]["status"] == "BLOCKED_BY_BUDGET"
    assert status["status"] == "AT_RISK_BUDGET_BLOCKED"


def test_capacity_expansion_requires_two_explicit_opt_ins(monkeypatch):
    monkeypatch.setenv("LEAD_FACTORY_PAID_CLOUD_ALLOWED", "TRUE")
    monkeypatch.setenv("LEAD_FACTORY_AUTONOMOUS_CAPACITY_EXPANSION", "TRUE")
    factory = FakeFactory()
    controller = QualifiedLeadProductionController(factory)
    controller.start(1500, datetime.now(timezone.utc) + timedelta(hours=1))
    status = controller.tick()
    assert factory.capacity_calls == 1
    assert status["capacity_action"]["status"] == "CALLED"


def test_restarting_same_running_goal_preserves_baseline():
    factory = FakeFactory()
    controller = QualifiedLeadProductionController(factory)
    first = controller.start(1500, datetime.now(timezone.utc) + timedelta(hours=1))
    baseline = first["baseline_ssot_count"]
    second = controller.start(1500, datetime.now(timezone.utc) + timedelta(hours=1))
    assert second["baseline_ssot_count"] == baseline
    assert second["target"] == 1500
    assert factory.sheets.config["LEAD_FACTORY_GOAL_STATUS"] == "RUNNING"


def test_terminal_goal_does_not_rollover_by_default(monkeypatch):
    monkeypatch.setenv("LEAD_FACTORY_PAID_CLOUD_ALLOWED", "FALSE")
    monkeypatch.setenv("LEAD_FACTORY_AUTO_ROLLOVER_DAILY_GOAL", "FALSE")
    factory = FakeFactory()
    controller = QualifiedLeadProductionController(factory)
    factory.sheets.config.update({
        "LEAD_FACTORY_GOAL_TARGET": "1500",
        "LEAD_FACTORY_GOAL_START_AT": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
        "LEAD_FACTORY_GOAL_DEADLINE": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
        "LEAD_FACTORY_GOAL_BASELINE_SSOT": "2",
        "LEAD_FACTORY_GOAL_STATUS": "DEADLINE_REACHED",
        "LEAD_FACTORY_GOAL_REPORT_SENT": "TRUE",
    })
    status = controller.tick()
    assert status["status"] == "DEADLINE_REACHED"
    assert status["rollover"] == "DISABLED_BY_BUDGET"
