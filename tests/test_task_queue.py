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
