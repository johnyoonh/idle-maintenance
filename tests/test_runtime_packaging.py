from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("maint.py", "shortcut_review.py", "maintenance_status_extended.py")


def test_review_runtime_is_packaged_for_app_and_script_deployments():
    app_build = (ROOT / "build_app.sh").read_text(encoding="utf-8")
    script_deploy = (ROOT / "deploy_core.sh").read_text(encoding="utf-8")
    for filename in REQUIRED:
        assert filename in app_build
        assert filename in script_deploy
