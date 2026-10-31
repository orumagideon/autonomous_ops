"""Playwright browser lifecycle helpers."""

from playwright.sync_api import Browser, Playwright, sync_playwright


def launch_browser(playwright: Playwright, headless: bool) -> Browser:
    return playwright.chromium.launch(
        headless=headless,
        channel="chrome",
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )


def start_playwright() -> Playwright:
    return sync_playwright().start()
