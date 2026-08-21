import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PromptSwiftTests(unittest.TestCase):
    def test_prompt_typechecks(self):
        result = subprocess.run(
            ["swiftc", "-typecheck", str(ROOT / "prompt.swift")],
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_app_open_is_local_and_non_final(self):
        text = (ROOT / "prompt.swift").read_text(encoding="utf-8")
        self.assertIn("func openCurrentApp()", text)
        self.assertIn("NSWorkspace.shared.open(URL(fileURLWithPath: itemPath))", text)
        self.assertIn("Opened—choose Keep, Snooze, or Move to Trash.", text)
        self.assertIn("Could not open this application; choose Keep, Snooze, or Move to Trash.", text)
        branch = '''if action.result == "TRY" && mode != "process" {
            openCurrentApp()
            return
        }'''
        self.assertIn(branch, text)
        self.assertLess(text.index(branch), text.index("finish(action.result)", text.index("func perform")))


if __name__ == "__main__":
    unittest.main()
