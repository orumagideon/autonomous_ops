"""TradeOps login, client traversal, and hedge-result updates."""

from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urljoin

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError

from src.config import Settings

logger = logging.getLogger(__name__)
NAV_TIMEOUT_MS = 60_000
CARD_WAIT_ATTEMPTS = 3
FALLBACK_PORTAL_URL = "https://www.tradeopss.com/trader/Gideon%20Oruma"


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


def _open_evaluations(page: Page, timeout_ms: int = 20_000) -> bool:
    eval_btn = page.locator('button.tab-btn:has-text("Evaluations")').first
    if eval_btn.count() and eval_btn.is_visible():
        try:
            eval_btn.click()
            page.wait_for_timeout(500)
        except PlaywrightTimeoutError:
            logger.warning("Could not click the Evaluations tab on %s", page.url)

    table = page.locator("table.wide-table").first
    try:
        table.wait_for(state="visible", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        if page.locator("table.wide-table").count() > 0:
            return True
        logger.warning("No wide-table found on %s", page.url)
        return False


DAYS_OF_WEEK = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY"]
HEDGE_FIELDS = [f"Hedge Result {i}" for i in range(1, 6)]
PLACEHOLDERS = {"", "—", "-"}

SET_CELL_JS = """({ index, field, value }) => {
    const input = document.querySelector(
        `input.cell-input[data-eval-index="${index}"][data-eval-field="${field}"]`);
    if (!input) return false;
    input.value = value;
    input.dispatchEvent(new Event('input', { bubbles: true }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
    if (typeof window.handleInputChange === 'function') {
        window.handleInputChange(index, field, value);
    }
    if (typeof window._evalPendingSave !== 'undefined') {
        window._evalPendingSave = true;
    }
    if (typeof window._flushEvalPendingSave === 'function') {
        window._flushEvalPendingSave();
    } else if (typeof window.scheduleEvalSave === 'function') {
        window.scheduleEvalSave();
    }
    return true;
}"""


def _set_cell_value(page: Page, index: str, field: str, new_value: str) -> bool:
    return bool(
        page.evaluate(SET_CELL_JS, {"index": index, "field": field, "value": new_value})
    )


def plan_hedge_changes(values: dict[str, str], target_day: str) -> dict[str, str]:
    """Return {field: new_value} so exactly one hedge cell holds target_day.

    `values` maps hedge field -> current text, for the fields present in the row.
    An existing day is replaced in place; other day cells are cleared.
    """
    target = target_day.upper()
    day_fields = [f for f, v in values.items() if v.strip().upper() in DAYS_OF_WEEK]
    if not values:
        return {}
    if not day_fields:
        first = next(iter(values))
        return {first: target_day}

    keep = next((f for f in day_fields if values[f].strip().upper() == target), day_fields[0])
    changes = {f: "" for f in day_fields if f != keep}
    if values[keep].strip().upper() != target:
        changes[keep] = target_day
    return changes


SAVE_PENDING_JS = """() => Boolean(window._evalBlurSaveTimer) ||
    window._evalPendingSave === true"""


class _WriteTracker:
    """Counts non-GET requests so we know when the backend save has finished."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.started = 0
        self.finished = 0
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_done)
        page.on("requestfailed", self._on_done)

    def _on_request(self, request) -> None:
        if request.method != "GET":
            self.started += 1

    def _on_done(self, request) -> None:
        if request.method != "GET":
            self.finished += 1

    def close(self) -> None:
        self.page.remove_listener("request", self._on_request)
        self.page.remove_listener("requestfinished", self._on_done)
        self.page.remove_listener("requestfailed", self._on_done)


def _wait_for_save(page: Page, tracker: _WriteTracker, max_wait_ms: int = 30_000) -> None:
    """Wait until the debounced save fired and its HTTP write(s) completed."""
    page.wait_for_timeout(3000)
    waited = 3000
    while waited < max_wait_ms:
        pending = page.evaluate(SAVE_PENDING_JS)
        in_flight = tracker.finished < tracker.started
        if not pending and not in_flight and tracker.started > 0:
            return
        page.wait_for_timeout(500)
        waited += 500
    logger.warning(
        "Save not confirmed after %ds (writes started=%d finished=%d)",
        max_wait_ms // 1000,
        tracker.started,
        tracker.finished,
    )


def _read_hedge_cells(row) -> tuple[str, dict[str, str]]:
    """Return the row's eval index and current value of each present hedge input."""
    values: dict[str, str] = {}
    eval_index = ""
    for field in HEDGE_FIELDS:
        cell_input = row.locator(
            f'td[data-eval-field="{field}"] input.cell-input'
        ).first
        if not cell_input.count():
            continue
        eval_index = eval_index or (cell_input.get_attribute("data-eval-index") or "")
        values[field] = cell_input.input_value().strip()
    return eval_index, values


def _update_evaluations(
    page: Page, target_day: str, previous_day: str, timeout_ms: int = 15_000
) -> int:
    if not _open_evaluations(page, timeout_ms):
        logger.info("Skipping client on %s: Evaluations table not present.", page.url)
        return 0

    rows = page.locator("table.wide-table tbody tr").all()
    if not rows:
        logger.info("No evaluation rows found (empty account).")
        return 0

    inactive_statuses = {"", "pass", "closed", "failed", "—", "-"}
    updates_made = 0

    tracker = _WriteTracker(page)
    try:
        for row in rows:
            status_td = row.locator('td[data-eval-field="Status P1"]').first
            if not status_td.count():
                continue
            status_text = status_td.inner_text().strip()
            if status_text.lower() in inactive_statuses:
                continue

            eval_index, values = _read_hedge_cells(row)
            changes = plan_hedge_changes(values, target_day)
            if not changes:
                logger.info("Row index %s already set to %s.", eval_index, target_day)
                continue

            for field, new_value in changes.items():
                if _set_cell_value(page, eval_index, field, new_value):
                    logger.info(
                        "Updated row index %s [%s] '%s' -> '%s'",
                        eval_index, field, values[field], new_value,
                    )
                    updates_made += 1
                else:
                    logger.warning(
                        "Row index %s [%s] input not found for update",
                        eval_index, field,
                    )
                page.wait_for_timeout(random.randint(50, 100))

        if updates_made:
            logger.info("Persisting %d update(s) to TradeOps backend...", updates_made)
            _wait_for_save(page, tracker)
    finally:
        tracker.close()

    logger.info("Finished client dashboard with %d update(s).", updates_made)
    return updates_made


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
                page, target_day, previous_day, settings.action_timeout_ms
            )
            time.sleep(random.uniform(0.05, 0.1))
        except Exception:
            logger.exception("Failed while processing client %s", client.name)
            _save_error_screenshot(page, client.name)
    logger.info("Updated %d hedge-result cell(s)", total_updated)
    return total_updated
