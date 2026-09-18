"""Exercise the actual middleware without starting background application workers."""
import ast
import asyncio
import hmac
import os
from pathlib import Path
from types import SimpleNamespace


def test_free_runner_is_scoped_and_still_authenticated(monkeypatch):
    tree = ast.parse(Path('main.py').read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == 'internal_runtime_guard')
    fn.decorator_list = []
    ns = dict(os=os, hmac=hmac, Request=object,
              paid_cloud_allowed=lambda:False,
              is_cloud_run=lambda:bool(os.getenv('K_SERVICE')),
              JSONResponse=lambda **kwargs:kwargs)
    exec(compile(ast.Module(body=[fn], type_ignores=[]), 'main.py', 'exec'), ns)
    for key,value in {'GITHUB_ACTIONS':'true','GITHUB_REPOSITORY':'a-one-road-official/sales',
                      'GITHUB_WORKFLOW':'生贄 bulk outbound (Playwright/email; Vertex forbidden)',
                      'LEAD_FACTORY_ISOLATED_SACRIFICE_RUNTIME':'TRUE',
                      'LEAD_FACTORY_INTERNAL_TOKEN':'test-token'}.items():
        monkeypatch.setenv(key,value)
    monkeypatch.delenv('K_SERVICE', raising=False)
    async def next_handler(_): return 'AUTHORIZED_LOCAL_CALL'
    request = SimpleNamespace(method='POST',url=SimpleNamespace(path='/outreach/sales-leads-sacrifice-run'),
                              client=SimpleNamespace(host='127.0.0.1'),headers={'X-Aone-Internal-Token':'test-token'})
    def run(): return asyncio.run(ns['internal_runtime_guard'](request,next_handler))
    assert run() == 'AUTHORIZED_LOCAL_CALL'
    request.headers = {}
    assert run()['status_code'] == 401
    request.headers = {'X-Aone-Internal-Token':'test-token'}
    monkeypatch.setenv('K_SERVICE','managed-cloud')
    assert run()['content']['status'] == 'BUDGET_BLOCKED_ACK'
    monkeypatch.delenv('K_SERVICE')
    request.url.path='/pipeline/tick'
    assert run()['content']['status'] == 'BUDGET_BLOCKED_ACK'
    request.url.path='/outreach/sales-leads-sacrifice-run'
    request.client.host='203.0.113.1'
    assert run()['content']['status'] == 'BUDGET_BLOCKED_ACK'
