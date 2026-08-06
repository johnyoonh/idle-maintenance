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


if __name__ == "__main__":
    unittest.main()
