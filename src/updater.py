"""TradeOps login, client traversal, and hedge-result updates."""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from src.config import Settings

logger = logging.getLogger(__name__)
NAV_TIMEOUT_MS = 60_000
CARD_WAIT_ATTEMPTS = 3
FALLBACK_PORTAL_URL = "https://www.tradeopss.com/trader/Gideon%20Oruma"
ACTIVE_STATUS = re.compile(r"in progress|pending|hit\s*tp|hit\s*sl", re.IGNORECASE)
HEDGE_HEADER = re.compile(r"^HEDGE\s*RESULT(?:\s*(\d+))?$", re.IGNORECASE)


@dataclass(frozen=True)
class Client:
    name: str
    url: str


def _goto_with_retry(page: Page, url: str, attempts: int = 3) -> None:
    for attempt in range(1, attempts + 1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return
        except PlaywrightTimeoutError:
            logger.warning("Navigation to %s timed out (%d/%d)", url, attempt, attempts)
            if attempt == attempts:
                raise
            page.wait_for_timeout(2000 * attempt)


def _wait_for_client_cards(page: Page, portal_url: str) -> None:
    """Wait for client cards, reloading the portal with longer waits if it lags."""
    for attempt in range(1, CARD_WAIT_ATTEMPTS + 1):
        timeout_ms = NAV_TIMEOUT_MS * attempt
        try:
            page.wait_for_selector("a.client-card", state="attached", timeout=timeout_ms)
            return
        except PlaywrightTimeoutError:
            logger.warning(
                "Client cards not rendered after %ds (attempt %d/%d)",
                timeout_ms // 1000,
                attempt,
                CARD_WAIT_ATTEMPTS,
            )
            if attempt == CARD_WAIT_ATTEMPTS:
                raise
            _goto_with_retry(page, portal_url)


def _login(page: Page, settings: Settings) -> None:
    timeout_ms = settings.action_timeout_ms
    _goto_with_retry(page, settings.login_url)

    email_input = page.locator(
        'input[type="email"], input[name*="email" i], '
        'input[autocomplete="username"], input[placeholder*="email" i], '
        'input#email, input[type="text"]'
    ).first
    email_input.wait_for(state="attached", timeout=timeout_ms)
    email_input.fill(settings.email)

    # Some login forms reveal the password only after a Next/Continue step.
    next_btn = page.get_by_role(
        "button", name=re.compile(r"^(next|continue)$", re.IGNORECASE)
    )
    if next_btn.count() and next_btn.first.is_visible():
        next_btn.first.click()
        page.wait_for_timeout(1000)

    password_input = page.locator('input#password, input[type="password"]').first
    password_input.wait_for(state="attached", timeout=timeout_ms)
    try:
        password_input.wait_for(state="visible", timeout=3000)
        password_input.fill(settings.password)
    except PlaywrightTimeoutError:
        # Styled inputs can be classified as hidden; set the value directly.
        page.evaluate(
            """(pwd) => {
                const el = document.querySelector(
                    'input#password, input[type="password"]');
                if (el) {
                    el.value = pwd;
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }""",
            settings.password,
        )

    submit = page.get_by_role(
        "button", name=re.compile(r"log\s*in|sign\s*in|submit", re.IGNORECASE)
    )
    if submit.count() and submit.first.is_visible():
        submit.first.click()
    else:
        password_input.press("Enter")
    # networkidle never resolves here (persistent connections); give the
    # session cookies/tokens a moment to register instead.
    page.wait_for_timeout(4000)

    target_portal = (
        getattr(settings, "portal_url", None)
        or getattr(settings, "dashboard_url", None)
        or FALLBACK_PORTAL_URL
    )
    if "trader" not in page.url:
        logger.info("Navigating to Trader Portal: %s", target_portal)
        _goto_with_retry(page, target_portal)

    logger.info("Awaiting client cards to attach to the DOM...")
    _wait_for_client_cards(page, target_portal)
    page.wait_for_timeout(2000)

    if page.locator('input[type="password"]').first.is_visible():
        raise RuntimeError(
            "TradeOps login did not complete; password field remains visible."
        )
    logger.info("Authenticated successfully and opened Trader Portal.")


def _client_links(page: Page) -> list[Client]:
    cards = page.locator("a.client-card")
    clients: list[Client] = []
    seen_urls: set[str] = set()

    for index in range(cards.count()):
        card = cards.nth(index)
        href = card.get_attribute("href")
        if not href:
            continue
        absolute_url = urljoin(page.url, href)
        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)
        name = unquote(absolute_url.rstrip("/").rsplit("/", 1)[-1])
        clients.append(Client(name=name, url=absolute_url))

    if not clients:
        raise RuntimeError("No a.client-card links were found on the Trader Portal")
    logger.info("Discovered %d client accounts to process.", len(clients))
    return clients


def _open_evaluations(page: Page, timeout_ms: int) -> bool:
    tab = page.get_by_role("tab", name=re.compile("evaluations", re.I))
    if not tab.count():
        tab = page.get_by_role("link", name=re.compile("evaluations", re.I))
    if not tab.count():
        tab = page.get_by_role("button", name=re.compile("evaluations", re.I))
    if not tab.count():
        tab = page.locator('[data-tab="evaluations"]')
    if tab.count() and tab.first.is_visible():
        tab.first.click()
        page.wait_for_timeout(200)
    try:
        page.locator("table").first.wait_for(state="visible", timeout=timeout_ms)
    except PlaywrightTimeoutError:
        body_text = page.locator("body").inner_text().lower()
        if re.search(r"no evaluations|no accounts|no data|empty", body_text):
            return False
        raise RuntimeError("Evaluations table did not appear")
    return True


def _hedge_columns(table: Locator) -> list[tuple[int, int]]:
    headers = table.locator("thead tr").last.locator("th, td")
    columns: list[tuple[int, int]] = []
    for index in range(headers.count()):
        text = headers.nth(index).inner_text().strip()
        match = HEDGE_HEADER.fullmatch(text)
        if match:
            columns.append((int(match.group(1) or "1"), index))
    return columns


def _cell_value(cell: Locator) -> str:
    control = cell.locator("input, textarea, select, [contenteditable='true']")
    if control.count():
        first = control.first
        if first.evaluate("(element) => element.tagName.toLowerCase()") == "select":
            return first.locator("option:checked").inner_text().strip()
        if first.get_attribute("contenteditable") == "true":
            return first.inner_text().strip()
        return first.input_value().strip()
    return cell.inner_text().strip()


def _write_cell(cell: Locator, target_day: str) -> bool:
    control = cell.locator("input, textarea, select, [contenteditable='true']")
    if not control.count():
        cell.click()
        control = cell.locator("input, textarea, select, [contenteditable='true']")
    if not control.count():
        return False

    field = control.first
    tag_name = field.evaluate("(element) => element.tagName.toLowerCase()")
    if tag_name == "select":
        options = field.locator("option")
        matching = False
        for index in range(options.count()):
            option = options.nth(index)
            if option.inner_text().strip().upper() == target_day:
                matching = True
                break
        if not matching:
            return False
        field.select_option(label=target_day)
    else:
        field.fill(target_day)
    field.press("Enter")
    field.evaluate("(element) => element.blur()")
    return True


def _update_evaluations(
    page: Page,
    client: Client,
    target_day: str,
    previous_day: str,
    timeout_ms: int,
) -> int:
    if not _open_evaluations(page, timeout_ms):
        logger.info("%s has no evaluation rows; skipping", client.name)
        return 0
    tables = page.locator("table")
    if not tables.count():
        logger.info("%s has no evaluation rows; skipping", client.name)
        return 0

    table = tables.first
    columns = _hedge_columns(table)
    if not columns:
        logger.warning("%s has no HEDGE RESULT columns; skipping", client.name)
        return 0
    active_column = max(columns)[1]
    headers = table.locator("thead tr").last.locator("th, td")
    status_column: int | None = None
    for index in range(headers.count()):
        if re.search(r"status", headers.nth(index).inner_text(), re.I):
            status_column = index
            break

    rows = table.locator("tbody tr")
    updated = 0
    for row_index in range(rows.count()):
        row = rows.nth(row_index)
        cells = row.locator("td")
        if cells.count() <= active_column:
            continue
        row_text = row.inner_text().strip()
        if not row_text:
            continue
        status_text = (
            cells.nth(status_column).inner_text().strip()
            if status_column is not None and cells.count() > status_column
            else row_text
        )
        if not ACTIVE_STATUS.search(status_text):
            continue

        cell = cells.nth(active_column)
        current_value = _cell_value(cell).strip().upper()
        if current_value == target_day:
            continue
        if current_value not in {"", "-", "—", "–", previous_day}:
            logger.info(
                "%s row %d has unexpected hedge value %r; leaving it unchanged",
                client.name,
                row_index + 1,
                current_value,
            )
            continue
        if not _write_cell(cell, target_day):
            logger.warning(
                "%s row %d hedge cell is not editable; leaving it unchanged",
                client.name,
                row_index + 1,
            )
            continue

        save_button = page.get_by_role(
            "button", name=re.compile(r"^save(?: changes)?$", re.I)
        )
        if save_button.count() and save_button.first.is_visible():
            save_button.first.click()
        time.sleep(random.uniform(0.05, 0.1))
        if _cell_value(cell).strip().upper() != target_day:
            raise RuntimeError(
                f"{client.name} row {row_index + 1}: hedge value did not persist"
            )
        updated += 1
        logger.info(
            "%s row %d hedge day set to %s", client.name, row_index + 1, target_day
        )
    return updated


def _save_error_screenshot(page: Page, client_name: str) -> None:
    error_dir = Path(__file__).resolve().parents[1] / "errors"
    error_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "_", client_name).strip("_") or "client"
    screenshot_path = error_dir / f"{safe_name}_{int(time.time())}.png"
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        logger.error("Saved failure screenshot to %s", screenshot_path)
    except Exception:
        logger.exception("Could not save failure screenshot for %s", client_name)


def update_clients(
    settings: Settings, target_day: str, previous_day: str, page: Page
) -> int:
    page.set_default_timeout(settings.action_timeout_ms)
    try:
        _login(page, settings)
        clients = _client_links(page)
    except Exception:
        logger.exception("Could not sign in or load the client list")
        _save_error_screenshot(page, "portal")
        raise
    logger.info("Found %d client dashboard(s)", len(clients))

    total_updated = 0
    for client in clients:
        try:
            logger.info("Processing client: %s", client.name)
            _goto_with_retry(page, client.url)
            page.wait_for_timeout(2000)
            total_updated += _update_evaluations(
                page,
                client,
                target_day,
                previous_day,
                settings.action_timeout_ms,
            )
            time.sleep(random.uniform(0.05, 0.1))
        except Exception:
            logger.exception("Failed while processing client %s", client.name)
            _save_error_screenshot(page, client.name)
    logger.info("Updated %d hedge-result cell(s)", total_updated)
    return total_updated
