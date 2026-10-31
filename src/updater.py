"""TradeOps login, client traversal, and hedge-result updates."""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin

from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError

from src.config import Settings

logger = logging.getLogger(__name__)
ACTIVE_STATUS = re.compile(r"in progress|pending|hit\s*tp|hit\s*sl", re.IGNORECASE)
HEDGE_HEADER = re.compile(r"^HEDGE\s*RESULT(?:\s*(\d+))?$", re.IGNORECASE)


@dataclass(frozen=True)
class Client:
    name: str
    url: str


def _first_visible(locator: Locator) -> Locator:
    for index in range(locator.count()):
        candidate = locator.nth(index)
        if candidate.is_visible():
            return candidate
    raise ValueError("Expected form control was not found or visible")


def _login(page: Page, settings: Settings) -> None:
    page.goto(settings.login_url, wait_until="domcontentloaded")
    email = _first_visible(
        page.locator(
            'input[type="email"], input[name*="email" i], '
            'input[autocomplete="username"]'
        )
    )
    password = _first_visible(page.locator('input[type="password"]'))
    email.fill(settings.email)
    password.fill(settings.password)

    submit = page.get_by_role(
        "button", name=re.compile(r"log\s*in|sign\s*in|submit", re.IGNORECASE)
    )
    if submit.count() and submit.first.is_visible():
        submit.first.click()
    else:
        password.press("Enter")
    password.wait_for(state="hidden", timeout=settings.action_timeout_ms)

    page.goto(settings.portal_url, wait_until="domcontentloaded")
    page.get_by_text(re.compile(r"open\s+dashboard", re.I)).first.wait_for(
        state="visible", timeout=settings.action_timeout_ms
    )
    if page.locator('input[type="password"]').count():
        raise RuntimeError("TradeOps login did not complete; a password form remains")
    logger.info("Authenticated and opened the Trader Portal")


def _client_links(page: Page, portal_url: str) -> list[Client]:
    clients: list[Client] = []
    seen_urls: set[str] = set()
    portal_path = portal_url.rstrip("/")

    for index in range(page.locator("a[href]").count()):
        anchor = page.locator("a[href]").nth(index)
        href = anchor.get_attribute("href")
        if not href:
            continue
        absolute_url = urljoin(page.url, href)
        if absolute_url.split("#", 1)[0].rstrip("/") == portal_path:
            continue
        try:
            anchor_text = anchor.inner_text(timeout=1000).strip()
        except PlaywrightTimeoutError:
            anchor_text = ""
        try:
            card = anchor.locator(
                "xpath=ancestor::*[contains(translate(@class,"
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'card')][1]"
            )
            card_text = card.inner_text(timeout=1000).strip() if card.count() else ""
        except PlaywrightTimeoutError:
            card_text = ""

        combined_text = f"{anchor_text}\n{card_text}"
        if not re.search(r"open\s+dashboard|client\s+dashboard", combined_text, re.I):
            continue
        if absolute_url in seen_urls:
            continue
        seen_urls.add(absolute_url)
        name = anchor_text
        if re.search(r"open\s+dashboard|client\s+dashboard", name, re.I):
            name = next(
                (
                    line.strip()
                    for line in card_text.splitlines()
                    if line.strip()
                    and not re.search(
                        r"open\s+dashboard|client\s+dashboard", line, re.I
                    )
                ),
                "",
            )
        clients.append(
            Client(
                name=name or absolute_url.rstrip("/").rsplit("/", 1)[-1],
                url=absolute_url,
            )
        )

    if not clients:
        raise RuntimeError("No client dashboard links were found on the Trader Portal")
    return clients


def _open_evaluations(page: Page, timeout_ms: int) -> bool:
    tab = page.get_by_role("tab", name=re.compile("evaluations", re.I))
    if not tab.count():
        tab = page.get_by_role("link", name=re.compile("evaluations", re.I))
    if not tab.count():
        tab = page.get_by_role("button", name=re.compile("evaluations", re.I))
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
        clients = _client_links(page, settings.portal_url)
    except Exception:
        logger.exception("Could not sign in or load the client list")
        _save_error_screenshot(page, "portal")
        raise
    logger.info("Found %d client dashboard(s)", len(clients))

    total_updated = 0
    for client in clients:
        try:
            logger.info("Processing client: %s", client.name)
            page.goto(client.url, wait_until="domcontentloaded")
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
            page.goto(settings.portal_url, wait_until="domcontentloaded")
    logger.info("Updated %d hedge-result cell(s)", total_updated)
    return total_updated
