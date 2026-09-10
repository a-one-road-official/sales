from dataclasses import dataclass

from strict_factory import OfficialSiteResolver
from task_queue import TaskDispatcher


class _FakeCreds:
    pass


class _FakeClient:
    def __init__(self, credentials=None):
        self.created = None

    def queue_path(self, project, location, queue):
        return f"projects/{project}/locations/{location}/queues/{queue}"

    def task_path(self, project, location, queue, task_id):
        return f"projects/{project}/locations/{location}/queues/{queue}/tasks/{task_id}"

    def create_task(self, request):
        self.created = request
        return type("Task", (), {"name": request["task"]["name"]})()


def test_cloud_task_carries_app_internal_token(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project")
    monkeypatch.setenv("LEAD_FACTORY_SERVICE_URL", "https://service.run.app")
    monkeypatch.setenv("LEAD_FACTORY_TASKS_SERVICE_ACCOUNT", "worker@project.iam.gserviceaccount.com")
    monkeypatch.setenv("LEAD_FACTORY_INTERNAL_TOKEN", "token-v1")

    import task_queue

    client = _FakeClient()
    monkeypatch.setattr(task_queue.google.auth, "default", lambda: (_FakeCreds(), "project"))
    monkeypatch.setattr(task_queue.tasks_v2, "CloudTasksClient", lambda credentials=None: client)
    monkeypatch.setattr(task_queue.tasks_v2, "HttpMethod", type("HttpMethod", (), {"POST": "POST"}))

    dispatcher = TaskDispatcher()
    dispatcher.enqueue("/worker/domain", {"lead_id": "lead-1"}, "domain:lead-1")

    headers = client.created["task"]["http_request"]["headers"]
    assert headers["X-Aone-Internal-Token"] == "token-v1"


def test_dispatcher_fails_closed_without_internal_token(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "project")
    monkeypatch.setenv("LEAD_FACTORY_SERVICE_URL", "https://service.run.app")
    monkeypatch.setenv("LEAD_FACTORY_TASKS_SERVICE_ACCOUNT", "worker@project.iam.gserviceaccount.com")
    monkeypatch.delenv("LEAD_FACTORY_INTERNAL_TOKEN", raising=False)

    import task_queue

    monkeypatch.setattr(task_queue.google.auth, "default", lambda: (_FakeCreds(), "project"))
    try:
        TaskDispatcher()
    except RuntimeError as exc:
        assert str(exc) == "missing_LEAD_FACTORY_INTERNAL_TOKEN"
    else:
        raise AssertionError("dispatcher must not create unauthenticated tasks")


@dataclass
class _Snapshot:
    status_code: int
    final_url: str
    text: str
    external_links: list[str] | None = None


class _FakeSheets:
    def get_config(self):
        return {"LEAD_FACTORY_SOURCE_RPS": "1"}


class _FakeLLM:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def resolve_company_domain(self, company):
        self.calls += 1
        return dict(self.result)


def test_exhibition_domain_resolution_searches_company_name_not_directory(monkeypatch):
    llm = _FakeLLM({
        "official_domain": "melodyinnovations.com",
        "official_website": "https://melodyinnovations.com/",
        "confidence": "HIGH",
    })
    resolver = OfficialSiteResolver(_FakeSheets(), llm)
    fetched = []

    def fetch(url, max_requests=2):
        fetched.append(url)
        assert "factoryautomationexpo.com" not in url
        return _Snapshot(200, url, "<title>Melody Innovations Pvt Ltd</title><body>Melody Innovations India</body>")

    monkeypatch.setattr(resolver, "_fetch", fetch)
    result = resolver.resolve({
        "company_name": "Melody Innovations Pvt Ltd",
        "hq_country": "India",
        "source_type": "EXHIBITION",
        "source_name": "Factory Automation Expo 2026",
        "source_record_url": "https://www.factoryautomationexpo.com/list-of-exhibitors/",
    })

    assert llm.calls == 1
    assert result["official_domain"] == "melodyinnovations.com"
    assert fetched == ["https://melodyinnovations.com/"]


def test_name_domain_probe_requires_first_party_identity(monkeypatch):
    llm = _FakeLLM({"official_domain": "", "official_website": "", "confidence": "LOW"})
    resolver = OfficialSiteResolver(_FakeSheets(), llm)

    def fetch(url, max_requests=2):
        return _Snapshot(
            200,
            url,
            "<title>Retail Solution And Technologies</title><body>Retail Solution And Technologies India</body>",
        )

    monkeypatch.setattr(resolver, "_fetch", fetch)
    result = resolver.resolve({
        "company_name": "Retail Solution And Technologies",
        "hq_country": "India",
        "source_type": "EXHIBITION",
        "source_name": "Factory Automation Expo 2026",
    })

    assert result["official_domain"] in {
        "retailsolutionandtechnologies.in",
        "retailsolutionandtechnologies.co.in",
        "retailsolutionandtechnologies.com",
        "retail-solution-and-technologies.in",
        "retail-solution-and-technologies.co.in",
        "retail-solution-and-technologies.com",
    }


def test_company_name_search_fallback_survives_transient_http_fetch_failure(monkeypatch):
    llm = _FakeLLM({
        "official_domain": "example-industrial.com",
        "official_website": "https://example-industrial.com/",
        "confidence": "HIGH",
        "evidence": ["https://search.example/evidence/example-industrial"],
    })
    resolver = OfficialSiteResolver(_FakeSheets(), llm)

    def fetch(url, max_requests=2):
        raise RuntimeError("temporary egress failure")

    monkeypatch.setattr(resolver, "_fetch", fetch)
    result = resolver.resolve({
        "company_name": "Example Industrial GmbH",
        "hq_country": "Germany",
        "source_type": "EXHIBITION",
        "source_name": "Example Expo",
    })

    assert result["official_domain"] == "example-industrial.com"
    assert result["verification"] == "VERIFIED_BY_COMPANY_NAME_SEARCH"


def test_failure_codes_are_stable_for_operational_reporting():
    from observability import failure_code

    assert failure_code("HTTP 429 quota exceeded") == "SHEETS_QUOTA"
    assert failure_code("reCAPTCHA blocked contact form") == "RECAPTCHA_OR_BOT_DEFENSE"
    assert failure_code("no_channel_found") == "EMAIL_NOT_FOUND"
