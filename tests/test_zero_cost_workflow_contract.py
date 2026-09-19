from pathlib import Path
import ast

ROOT = Path(__file__).parents[1]


def test_stop_cannot_trigger_from_push_or_schedule():
    for name in ('bpo_autopilot.yml', 'emergency-cost-stop.yml'):
        text = (ROOT / '.github/workflows' / name).read_text()
        assert '  push:' not in text and '  schedule:' not in text
        assert 'workflow_dispatch:' in text
        assert 'scheduler jobs delete' not in text
    stop = (ROOT / '.github/workflows/emergency-cost-stop.yml').read_text()
    assert 'concurrency:' in stop and 'cancel-in-progress: false' in stop
    assert '|| true' not in stop and 'continue-on-error' not in stop


def test_free_workflow_keeps_identity_budget_and_history_boundaries():
    text = (ROOT / '.github/workflows/sacrifice_canary.yml').read_text()
    assert text.startswith('name: 生贄 bulk outbound (Playwright/email; Vertex forbidden)')
    assert 'group: sacrifice-bulk-outbound' in text
    assert 'github.event.repository.private == false' in text
    for key in ('LEAD_FACTORY_VERTEX_ALLOWED', 'LEAD_FACTORY_PAID_AI_ALLOWED',
                'LEAD_FACTORY_PAID_CLOUD_ALLOWED', 'OUTREACH_FACTORY_SEND_ENABLED', 'OUTREACH_SSOT_SEND_ENABLED'):
        assert f'{key}: FALSE' in text
    assert 'OUTREACH_PREPARED_DRAFTS_ONLY: TRUE' in text
    assert 'OUTREACH_EMAIL_TRANSPORT: CHATGPT_CONNECTOR' in text
    assert 'OUTREACH_SACRIFICE_TARGET_COMPANIES: ALL' in text
    assert 'outreach_cycle.py plan' in text and 'outreach_cycle.py run' in text
    assert 'Plobal Apps' not in text
    assert '  schedule:' not in text
    assert 'upload-artifact@' not in text
    assert 'gcloud run deploy' not in text


def test_control_module_has_no_model_or_cloud_execution_dependencies():
    tree = ast.parse((ROOT / 'outreach_cycle.py').read_text())
    imports = [n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)]
    imports += [alias.name for n in ast.walk(tree) if isinstance(n, ast.Import) for alias in n.names]
    assert not any(name and name.startswith(('openai', 'anthropic', 'vertexai', 'google.genai')) for name in imports)
    text = (ROOT / 'outreach_cycle.py').read_text()
    assert 'http://127.0.0.1:8080/' not in text
    assert 'uvicorn' not in (ROOT / '.github/workflows/sacrifice_canary.yml').read_text()
    assert 'run_ten_sacrifice_batch' in text
    assert 'restart_uncertain_claims' in text
