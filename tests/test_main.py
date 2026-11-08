import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch


def load_main():
    spec = importlib.util.spec_from_file_location(
        "tradeops_main_test", Path(__file__).resolve().parent.parent / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        sys.modules,
        {"src.browser": Mock(), "src.config": Mock(), "src.updater": Mock()},
    ):
        spec.loader.exec_module(module)
    return module


class MainTests(unittest.TestCase):
    def test_saturday_rollover_reaches_updater(self):
        main = load_main()
        main.datetime = Mock()
        main.datetime.now.return_value = datetime(2026, 11, 6, 21, tzinfo=timezone.utc)
        main.configure_logging = Mock()
        settings = Mock(timezone="Africa/Nairobi", headless=True)
        main.Settings.from_env.return_value = settings
        playwright = main.start_playwright.return_value
        browser = main.launch_browser.return_value
        page = browser.new_page.return_value

        self.assertEqual(main.main(), 0)

        main.datetime.now.assert_called_once_with(timezone.utc)
        main.launch_browser.assert_called_once_with(playwright, True)
        main.update_clients.assert_called_once_with(settings, "MONDAY", "FRIDAY", page)
        browser.close.assert_called_once_with()
        playwright.stop.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
