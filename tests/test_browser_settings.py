import json
import tempfile
import unittest
from pathlib import Path

from crawler.amazon import BrowserSettings
from run_daily import build_parser


class BrowserSettingsTests(unittest.TestCase):
    def test_default_collection_config_is_shipped(self):
        path = build_parser().parse_args([]).browser_config
        self.assertTrue(path.is_file(), "新品榜必需的共享浏览器配置不可删除")
        settings = BrowserSettings.from_file(path)
        self.assertEqual(settings.channel, "chromium")
        self.assertTrue(settings.headless)

    def test_missing_config_matches_documented_server_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = BrowserSettings.from_file(Path(tmp) / "missing.json")
        self.assertEqual(settings.channel, "chromium")
        self.assertTrue(settings.headless)

    def test_explicit_browser_choice_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "browser.json"
            path.write_text(json.dumps({"channel": "msedge", "headless": False}))
            settings = BrowserSettings.from_file(path)
        self.assertEqual(settings.channel, "msedge")
        self.assertFalse(settings.headless)
