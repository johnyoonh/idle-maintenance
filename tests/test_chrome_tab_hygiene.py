import importlib.util
from pathlib import Path
import unittest

SPEC = importlib.util.spec_from_file_location(
    "chrome_tab_hygiene",
    Path(__file__).parents[1] / "scripts/chrome_tab_hygiene.py",
)
mod = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mod)


class ChromeTabHygieneTests(unittest.TestCase):
    def test_classify_tab(self):
        self.assertEqual(mod.classify_tab(""), "blank")
        self.assertEqual(mod.classify_tab("about:blank"), "blank")
        self.assertEqual(mod.classify_tab("https://chatgpt.com"), "root_chatgpt")
        self.assertEqual(mod.classify_tab("https://chatgpt.com/"), "root_chatgpt")
        self.assertEqual(mod.classify_tab("https://www.chatgpt.com/"), "root_chatgpt")
        self.assertEqual(mod.classify_tab("https://chat.openai.com/"), "root_chatgpt")
        self.assertEqual(mod.classify_tab("https://chatgpt.com/?model=gpt-4"), "root_chatgpt")

        # Conversations must NOT be classified as root
        self.assertEqual(
            mod.classify_tab("https://chatgpt.com/c/6ae36e9a-4b58-83ea-be54-ddf8e4549bf6"),
            "conversation",
        )
        self.assertEqual(
            mod.classify_tab("https://chatgpt.com/g/g-p-6771d9d40b0881919ba1135f4bf880b2-opencli"),
            "conversation",
        )

        # Other websites must be user pages
        self.assertEqual(mod.classify_tab("https://github.com/johnyoonh"), "user_page")
        self.assertEqual(mod.classify_tab("https://google.com/search?q=test"), "user_page")

    def test_plan_cleanup_keeps_single_root_and_all_conversations(self):
        windows = [
            {
                "tabs": [
                    {"url": "https://chatgpt.com/", "title": "ChatGPT"},
                    {"url": "about:blank", "title": ""},
                    {"url": "https://chatgpt.com/c/123", "title": "Day 302"},
                    {"url": "https://chatgpt.com/", "title": "ChatGPT (dup 1)"},
                ]
            },
            {
                "tabs": [
                    {"url": "https://chatgpt.com/", "title": "ChatGPT (dup 2)"},
                    {"url": "https://github.com/test", "title": "GitHub"},
                    {"url": "https://chatgpt.com/c/456", "title": "Day 303"},
                ]
            },
        ]

        kept, closed = mod.plan_cleanup(windows)

        # Kept:
        # - 1st root ChatGPT tab
        # - Day 302 conversation
        # - GitHub user page
        # - Day 303 conversation
        self.assertEqual(len(kept), 4)
        kept_urls = [t["url"] for t in kept]
        self.assertIn("https://chatgpt.com/", kept_urls)
        self.assertIn("https://chatgpt.com/c/123", kept_urls)
        self.assertIn("https://github.com/test", kept_urls)
        self.assertIn("https://chatgpt.com/c/456", kept_urls)

        # Closed:
        # - about:blank
        # - ChatGPT dup 1
        # - ChatGPT dup 2
        self.assertEqual(len(closed), 3)
        reasons = [t["reason"] for t in closed]
        self.assertEqual(reasons.count("blank"), 1)
        self.assertEqual(reasons.count("duplicate_root_chatgpt"), 2)


if __name__ == "__main__":
    unittest.main()
