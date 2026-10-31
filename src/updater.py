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

    updates_made = 0
    hedge_fields = [f"Hedge Result {i}" for i in range(1, 6)]
    inactive_statuses = {"", "pass", "closed", "failed", "—", "-"}
    target = target_day.upper()
    previous = previous_day.upper()

    for row in rows:
        status_td = row.locator('td[data-eval-field="Status P1"]').first
        if not status_td.count():
            continue
        status_text = status_td.inner_text().strip()
        if status_text.lower() in inactive_statuses:
            continue

        for field in hedge_fields:
            hedge_td = row.locator(f'td[data-eval-field="{field}"]').first
            if not hedge_td.count():
                continue

            current_val = hedge_td.inner_text().strip()
            if target in current_val.upper():
                break
            if previous not in current_val.upper() and current_val not in {"", "—", "-"}:
                continue

            logger.info(
                "Updating row '%s' [%s] from '%s' to '%s'",
                status_text,
                field,
                current_val,
                target_day,
            )
            # Clicking the cell runs handleCellClick, which mounts the editor input.
            hedge_td.click()
            page.wait_for_timeout(200)
            input_field = hedge_td.locator("input").first
            if input_field.count():
                input_field.fill(target_day)
                page.wait_for_timeout(random.randint(50, 100))
                input_field.press("Enter")
            else:
                page.evaluate(
                    """([td, val]) => {
                        td.innerText = val;
                        td.dispatchEvent(new Event('input', { bubbles: true }));
                        td.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    [hedge_td.element_handle(), target_day],
                )
            page.wait_for_timeout(300)

            if target in hedge_td.inner_text().upper():
                updates_made += 1
            else:
                logger.warning(
                    "Row '%s' [%s] did not keep the value '%s'",
                    status_text,
                    field,
                    target_day,
                )
            break

    save_btn = page.locator(
        'button:has-text("Save"), button:has-text("Save Changes"), '
        'input[value="Save"]'
    ).first
    if save_btn.count() and save_btn.is_visible():
        save_btn.click()
        page.wait_for_timeout(1000)

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
