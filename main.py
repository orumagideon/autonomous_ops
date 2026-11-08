"""Entrypoint for the scheduled TradeOps updater."""

import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.browser import launch_browser, start_playwright
from src.config import Settings
from src.scheduler_utils import get_previous_trading_day, get_target_day
from src.updater import update_clients


def configure_logging() -> None:
    project_root = Path(__file__).resolve().parent
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(project_root / "execution.log"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def main() -> int:
    configure_logging()
    logger = logging.getLogger(__name__)
    playwright = None
    browser = None
    try:
        settings = Settings.from_env()
        now = datetime.now(timezone.utc)
        target_day = get_target_day(now, timezone_name=settings.timezone)
        previous_day = get_previous_trading_day(now, timezone_name=settings.timezone)
        logger.info(
            "Starting TradeOps update: previous trading day=%s, target day=%s",
            previous_day,
            target_day,
        )
        playwright = start_playwright()
        browser = launch_browser(playwright, settings.headless)
        page = browser.new_page()
        update_clients(settings, target_day, previous_day, page)
        return 0
    except Exception:
        logger.exception("TradeOps update failed")
        return 1
    finally:
        if browser is not None:
            browser.close()
        if playwright is not None:
            playwright.stop()


if __name__ == "__main__":
    raise SystemExit(main())
