from __future__ import annotations

import json
import os
import subprocess
import tempfile
import uuid
from pathlib import Path


class SandboxError(RuntimeError):
    pass


HARNESS = r'''
import importlib.util, json, sys
spec = importlib.util.spec_from_file_location("adapter", "/workspace/adapter.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
snapshot = json.load(open("/workspace/input.json", "r", encoding="utf-8"))
out = mod.extract(snapshot)
print(json.dumps(out, ensure_ascii=False))
'''


def _direct_run(code: str, snapshot: dict) -> dict:
    """Only for unit tests/local CI. Production should use Cloud Run sandbox launcher."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "adapter.py").write_text(code, encoding="utf-8")
        (p / "input.json").write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        harness = HARNESS.replace('/workspace/adapter.py', str(p / 'adapter.py')).replace('/workspace/input.json', str(p / 'input.json'))
        (p / "harness.py").write_text(harness, encoding="utf-8")
        cp = subprocess.run(["python", str(p / "harness.py")], capture_output=True, text=True, timeout=30)
        if cp.returncode != 0:
            raise SandboxError(cp.stderr[-4000:])
        return json.loads(cp.stdout)


def run_adapter(code: str, snapshot: dict) -> dict:
    sandbox_bin = os.getenv("CLOUD_RUN_SANDBOX_BIN", "/usr/local/gcp/bin/sandbox")
    if not os.path.exists(sandbox_bin):
        if os.getenv("ALLOW_DIRECT_ADAPTER_EXECUTION", "FALSE").upper() == "TRUE":
            return _direct_run(code, snapshot)
        raise SandboxError("cloud_run_sandbox_launcher_not_available")

    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "adapter.py").write_text(code, encoding="utf-8")
        (p / "input.json").write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
        (p / "harness.py").write_text(HARNESS, encoding="utf-8")
        sid = "lf-" + uuid.uuid4().hex[:12]
        # No --allow-egress: generated code is physically network-isolated.
        cmd = [
            sandbox_bin, "run", sid,
            "--mount", f"type=bind,source={td},destination=/workspace",
            "--workdir", "/workspace",
            "--write",
            "env", "-i", "PATH=/usr/local/bin:/usr/bin:/bin", "PYTHONPATH=/workspace",
            "python", "/workspace/harness.py",
        ]
        cp = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if cp.returncode != 0:
            raise SandboxError((cp.stderr or cp.stdout)[-6000:])
        lines = [x for x in cp.stdout.splitlines() if x.strip()]
        if not lines:
            raise SandboxError("sandbox_no_output")
        return json.loads(lines[-1])
