import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class AppActionPackagingTests(unittest.TestCase):
    def test_script_deployments_include_worker(self):
        deploy_core = (ROOT / "deploy_core.sh").read_text(encoding="utf-8")
        deploy = (ROOT / "deploy.sh").read_text(encoding="utf-8")
        self.assertIn('cp app_actions.py "$DEST/"', deploy_core)
        self.assertIn("app_actions.py", deploy)

    def test_app_bundle_includes_worker(self):
        build = (ROOT / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn("activity_intelligence.py,app_actions.py,maintenance_core.py", build)

    def test_prompt_helper_is_compiled_for_both_deployments(self):
        deploy_core = (ROOT / "deploy_core.sh").read_text(encoding="utf-8")
        build = (ROOT / "build_app.sh").read_text(encoding="utf-8")
        self.assertIn('swiftc -O -framework AppKit prompt.swift', deploy_core)
        self.assertIn('IdleMaintenancePrompt', deploy_core)
        self.assertIn('swiftc -O -framework AppKit "$SRC_DIR/prompt.swift"', build)
        self.assertIn('IdleMaintenancePrompt', build)


if __name__ == "__main__":
    unittest.main()
