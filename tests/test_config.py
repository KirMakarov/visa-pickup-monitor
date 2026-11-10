import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from visa_pickup_monitor import infrastructure as monitor


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env_path = Path(self.temp.name) / ".env"

    def test_dotenv_preserves_leading_zeros_and_deduplicates(self):
        self.env_path.write_text('NUMBERS="000123, 123456,000123"\n', encoding="utf-8")
        with patch.dict(os.environ, {}, clear=True):
            page_url, numbers = monitor.load_config(
                monitor.read_settings(self.env_path)
            )
        self.assertEqual(page_url, monitor.DEFAULT_PAGE_URL)
        self.assertEqual(numbers, ("000123", "123456"))

    def test_environment_overrides_dotenv(self):
        self.env_path.write_text(
            "PAGE_URL=https://file.example/page\nNUMBERS=123456\n", encoding="utf-8"
        )
        settings = {"PAGE_URL": "https://env.example/page", "NUMBERS": "000123"}
        with patch.dict(os.environ, settings, clear=True):
            self.assertEqual(
                monitor.load_config(monitor.read_settings(self.env_path)),
                ("https://env.example/page", ("000123",)),
            )

    def test_missing_or_invalid_numbers_are_configuration_errors(self):
        for value in ("", "12345", "1234567", "abc123", "123456,", "１２３４５６"):
            with (
                self.subTest(value=value),
                patch.dict(os.environ, {"NUMBERS": value}, clear=True),
            ):
                with self.assertRaisesRegex(monitor.MonitorError, "NUMBERS"):
                    monitor.load_config(monitor.read_settings(self.env_path))

    def test_invalid_source_urls_are_configuration_errors(self):
        for url in (
            "file:///tmp/page.html",
            "embassy.example/page",
            "https://[invalid",
        ):
            settings = {"NUMBERS": "123456", "PAGE_URL": url}
            with self.subTest(url=url), patch.dict(os.environ, settings, clear=True):
                with self.assertRaisesRegex(monitor.MonitorError, "PAGE_URL"):
                    monitor.load_config(monitor.read_settings(self.env_path))

    def test_optional_telegram_requires_both_settings(self):
        self.assertIsNone(monitor.load_telegram({}))
        invalid_settings: tuple[dict[str, str | None], ...] = (
            {"TELEGRAM_BOT_TOKEN": "123:test"},
            {"TELEGRAM_CHAT_ID": "456"},
            {"TELEGRAM_BOT_TOKEN": "invalid", "TELEGRAM_CHAT_ID": "456"},
        )
        for settings in invalid_settings:
            with (
                self.subTest(settings=settings),
                self.assertRaises(monitor.MonitorError),
            ):
                monitor.load_telegram(settings)
        config = monitor.load_telegram(
            {
                "TELEGRAM_BOT_TOKEN": "123:test",
                "TELEGRAM_CHAT_ID": "456",
            }
        )
        assert config is not None
        self.assertEqual(config.chat_id, "456")
        self.assertNotIn("123:test", repr(config))
