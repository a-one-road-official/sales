"""Offline regression tests for the narrow old-deployment retirement gate."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("retirement_cost_guard", ROOT / "cost_guard.py")
GUARD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GUARD)


class RetiredDeployGateTests(unittest.TestCase):
    def test_both_legacy_names_are_blocked(self):
        for name in GUARD.RETIRED_CLOUD_DEPLOY_WORKFLOWS.values():
            with self.subTest(name=name), self.assertRaisesRegex(RuntimeError, "legacy_cloud_deployment_retired"):
                GUARD.assert_no_legacy_cloud_deployment({"GITHUB_WORKFLOW": name})

    def test_file_ref_blocks_even_with_renamed_title(self):
        for filename in GUARD.RETIRED_CLOUD_DEPLOY_WORKFLOWS:
            with self.subTest(filename=filename), self.assertRaises(RuntimeError):
                GUARD.assert_no_legacy_cloud_deployment({
                    "GITHUB_WORKFLOW": "Renamed title",
                    "GITHUB_WORKFLOW_REF": "a-one-road-official/sales/.github/workflows/" + filename + "@refs/heads/main"})

    def test_free_crawlers_sender_ci_and_stop_remain_allowed(self):
        for filename, name in (
            ("raw-material-intake.yml", "Raw material intake (Python only)"),
            ("non-ai-lead-intake.yml", "Non-AI lead intake"),
            ("deterministic-crawl.yml", "Deterministic crawl"),
            ("sacrifice_canary.yml", "生贄 bulk outbound (Playwright/email; Vertex forbidden)"),
            ("ci.yml", "CI"),
            ("emergency-cost-stop.yml", "Explicit paid AI cost stop"),
        ):
            with self.subTest(filename=filename):
                GUARD.assert_no_legacy_cloud_deployment({"GITHUB_WORKFLOW": name,
                    "GITHUB_WORKFLOW_REF": "a-one-road-official/sales/.github/workflows/" + filename + "@refs/heads/main"})

    def test_paid_flags_cannot_reopen_retired_deploy(self):
        with self.assertRaises(RuntimeError):
            GUARD.assert_no_legacy_cloud_deployment({"GITHUB_WORKFLOW": "Deploy Lead Factory",
                "LEAD_FACTORY_PAID_CLOUD_ALLOWED": "TRUE", "CONFIRM_PAID_CLOUD": "TRUE"})

    def test_existing_runtime_environment_is_untouched(self):
        env = {"K_SERVICE": "aone-lead-factory", "OUTREACH_SALES_GTM_SEND_ENABLED": "TRUE",
            "GOOGLE_APPLICATION_CREDENTIALS": "/existing/credentials", "LEAD_FACTORY_PAID_CLOUD_ALLOWED": "FALSE"}
        before = env.copy()
        GUARD.assert_no_legacy_cloud_deployment(env)
        self.assertEqual(env, before)
        GUARD.assert_no_legacy_cloud_deployment({})

    def _cli(self, workflow):
        env = {key: value for key, value in os.environ.items() if not key.startswith(("GITHUB_", "LEAD_FACTORY_", "PAID_AI_"))}
        env["GITHUB_WORKFLOW"] = workflow
        return subprocess.run([sys.executable, "-I", str(ROOT / "cost_guard.py")],
            env=env, text=True, capture_output=True, timeout=10, check=False)

    def test_cli_fails_before_legacy_deploy_can_continue(self):
        result = self._cli("Deploy Lead Factory")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("legacy_cloud_deployment_retired", result.stderr)
        self.assertEqual(result.stdout, "")

    def test_sender_cli_still_passes_with_zero_paid_ai(self):
        result = self._cli("生贄 bulk outbound (Playwright/email; Vertex forbidden)")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIs(json.loads(result.stdout)["paid_ai_allowed"], False)


if __name__ == "__main__":
    unittest.main()
