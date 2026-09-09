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
        key = range_.split("!")[0]
        if key.startswith("Config"):
            name = range_.split("!B")[0]
            self.config[name] = str(values[0][0])

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
    status = controller.start(1500, datetime(2026, 9, 8, 14, tzinfo=timezone.utc))
    assert status["target"] == 1500
    assert status["baseline_ssot_count"] == 2
    assert factory.sheets.config["LEAD_FACTORY_GOAL_STATUS"] == "RUNNING"


def test_at_risk_requests_capacity_without_changing_gate():
    factory = FakeFactory()
    controller = QualifiedLeadProductionController(factory)
    controller.start(1500, datetime.now(timezone.utc) + timedelta(hours=1))
    status = controller.tick()
    assert factory.capacity_calls == 1
    assert status["target"] == 1500


def test_restarting_same_running_goal_preserves_baseline():
    factory = FakeFactory()
    controller = QualifiedLeadProductionController(factory)
    first = controller.start(1500, datetime.now(timezone.utc) + timedelta(hours=1))
    baseline = first["baseline_ssot_count"]
    second = controller.start(1500, datetime.now(timezone.utc) + timedelta(hours=1))
    assert second["baseline_ssot_count"] == baseline
    assert second["target"] == 1500
    assert factory.sheets.config["LEAD_FACTORY_GOAL_STATUS"] == "RUNNING"
