"""Scrape purchase history from kroger.com with Playwright.

Why scraping rather than email: the kroger.com receipt pages carry `UPC:`
markers, and a 13-digit productId is exactly what the Cart API needs. Emailed
receipts usually do not include them. So this is the *better* source, at the
cost of being more fragile.

Design constraints, all learned from what breaks:

* Headed browser only. Kroger rejects headless sessions, and headless_shell is
  trivially fingerprinted. There is no headless fallback on purpose.
* The human logs in, by hand, in a real window. We never touch their password.
* Human-paced: randomised delays and scrolling. This is also just polite -- a
  session that hammers the account is a session that gets the account throttled,
  which would defeat the entire purpose.
* Resumable. Every order is checkpointed to disk as it completes, so an
  interrupted run resumes instead of starting over.
* Raw capture. The exact receipt layout is not something to guess at, so we save
  the raw text and write the parser against the real thing.

This module is deliberately the only part that touches a browser. Everything
downstream reads the JSON this produces.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path

from . import config

logger = logging.getLogger(__name__)

BASE = "https://www.kroger.com"

# Kroger-family domains, so this works if you shop at a banner other than Kroger.
ACCEPTED_HOSTS = (
    "kroger.com",
    "fredmeyer.com",
    "ralphs.com",
    "kingsoopers.com",
    "qfc.com",
    "dillons.com",
    "food4less.com",
    "smithsfoodanddrug.com",
    "marketplacebygrocer.com",
)

DELAY_MIN, DELAY_MAX = 2.0, 8.0
NAV_TIMEOUT_MS = 45_000


class ScrapePaused(RuntimeError):
    """Kroger is throttling us. Stop and let the account cool down."""


@dataclass
class RawOrder:
    """One order, exactly as seen. Parsing happens later, offline."""

    detail_url: str
    receipt_url: str | None = None
    order_number: str | None = None
    order_date: str | None = None
    order_type: str | None = None
    total: str | None = None
    raw_text: str = ""
    page_text: str = ""
    scraped_at: str = field(
        default_factory=lambda: datetime.now().isoformat(timespec="seconds")
    )

    def to_json(self) -> dict:
        return asdict(self)


def scrape_dir() -> Path:
    d = config.data_dir() / "scrape"
    d.mkdir(parents=True, exist_ok=True)
    return d


def raw_dir() -> Path:
    d = scrape_dir() / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_path() -> Path:
    return scrape_dir() / "state.json"


def profile_dir() -> Path:
    d = scrape_dir() / "profile"
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_state() -> dict:
    p = state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            logger.warning("state.json is corrupt; starting fresh")
    return {"order_urls": [], "done": []}


def save_state(state: dict) -> None:
    p = state_path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(p)


def _pause(low: float = DELAY_MIN, high: float = DELAY_MAX) -> None:
    time.sleep(random.uniform(low, high))


def _human_scroll(page) -> None:
    """Scroll in irregular increments, like a person reading a page."""
    for _ in range(random.randint(2, 5)):
        page.mouse.wheel(0, random.randint(220, 700))
        time.sleep(random.uniform(0.25, 0.9))
    page.mouse.wheel(0, -random.randint(200, 500))
    time.sleep(random.uniform(0.2, 0.6))


def _looks_blocked(page) -> bool:
    text = (page.title() or "") + " " + (page.inner_text("body")[:800] or "")
    markers = (
        "unable to retrieve",
        "access denied",
        "are you a robot",
        "verify you are human",
        "temporarily blocked",
        "security check",
    )
    low = text.lower()
    return any(m in low for m in markers)


def open_browser(pw, headless: bool = False):
    """Persistent context, so a successful login sticks for future runs."""
    if headless:
        raise ValueError(
            "headless is intentionally unsupported: Kroger rejects headless sessions"
        )
    ctx = pw.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir()),
        headless=False,
        viewport={"width": 1280, "height": 900},
        args=["--disable-blink-features=AutomationControlled"],
    )
    ctx.set_default_timeout(NAV_TIMEOUT_MS)
    return ctx


def wait_for_login(page, timeout_s: int = 300) -> None:
    """Block until the human has finished logging in."""
    print("A browser window is open. Log in to Kroger there, then come back here.")
    print(f"Waiting up to {timeout_s}s...")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if "/mypurchases" in page.url or "/signin" not in page.url:
                links = page.query_selector_all('a[href*="/mypurchases"]')
                if links:
                    print("Logged in.")
                    return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError("Login not detected within timeout. Re-run and log in sooner.")


def collect_order_urls(page, max_orders: int | None = None) -> list[str]:
    """Paginate /mypurchases and gather every order detail link."""
    print("Collecting order URLs from /mypurchases ...")
    page.goto(f"{BASE}/mypurchases", wait_until="domcontentloaded")
    _pause(1.5, 3.0)
    if _looks_blocked(page):
        raise ScrapePaused("Kroger returned a bot-check page on /mypurchases")

    found: list[str] = []
    seen: set[str] = set()
    stagnant = 0

    while True:
        hrefs = page.eval_on_selector_all(
            'a[href*="/mypurchases/"]', "els => els.map(e => e.href)"
        )
        before = len(found)
        for href in hrefs:
            if href in seen:
                continue
            seen.add(href)
            # Fuel Centre receipts are fuel, not groceries. Not food.
            if "fuel" in href.lower():
                continue
            found.append(href)

        if max_orders and len(found) >= max_orders:
            break
        if len(found) == before:
            stagnant += 1
            if stagnant >= 3:
                break
        else:
            stagnant = 0

        _human_scroll(page)
        # Load-more, infinite scroll, and pagination all show up in the wild.
        for sel in (
            'button:has-text("Load more")',
            'button:has-text("Show more")',
            '[aria-label*="Next"]',
            'a[rel="next"]',
        ):
            try:
                btn = page.query_selector(sel)
                if btn and btn.is_visible():
                    btn.click()
                    _pause(1.5, 3.5)
                    break
            except Exception:
                pass
        else:
            try:
                page.evaluate("window.scrollBy(0, document.body.scrollHeight)")
            except Exception:
                break
        _pause()

    found = found[:max_orders] if max_orders else found
    print(f"  found {len(found)} order URL(s)")
    return found


def scrape_one(page, detail_url: str) -> RawOrder:
    """Load an order page and capture the receipt as raw text."""
    order = RawOrder(detail_url=detail_url)
    page.goto(detail_url, wait_until="domcontentloaded")
    _pause(1.0, 2.5)
    if _looks_blocked(page):
        raise ScrapePaused(f"bot-check page on {detail_url}")

    _human_scroll(page)

    try:
        order.page_text = page.inner_text("body")
    except Exception:
        order.page_text = ""

    # Prefer the dedicated receipt view when the page links to one.
    for sel in (
        'a[href*="/mypurchases/image/"]',
        'a:has-text("View receipt")',
        'a:has-text("See receipt")',
        'button:has-text("View receipt")',
    ):
        try:
            el = page.query_selector(sel)
            if el:
                href = el.get_attribute("href")
                if href:
                    order.receipt_url = href if href.startswith("http") else BASE + href
                else:
                    order.receipt_url = page.url
                break
        except Exception:
            pass

    if order.receipt_url and order.receipt_url != detail_url:
        try:
            page.goto(order.receipt_url, wait_until="domcontentloaded")
            _pause(0.8, 2.0)
            order.raw_text = page.inner_text("body")
        except Exception as exc:
            logger.warning("receipt view failed for %s: %s", detail_url, exc)
    if not order.raw_text:
        order.raw_text = order.page_text

    blob = order.raw_text
    m = re.search(r"\b(\d{12,19})\b", blob)
    if m and "order" in blob.lower():
        order.order_number = m.group(1)
    for pat, attr in (
        (r"(?:order\s*(?:date|placed|#)\s*[:.]?\s*)([A-Z][a-z]{2,8}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})", "order_date"),
        (r"\b(Delivery|Pickup|CURBSIDE|In-Store|Ship to Home)\b", "order_type"),
        (r"(?:order\s*total|total)\s*\$?\s*([\d,]+\.\d{2})", "total"),
    ):
        mm = re.search(pat, blob, re.IGNORECASE)
        if mm:
            setattr(order, attr, mm.group(1).strip())
    return order


def run_scrape(
    max_orders: int | None = 10,
    dry_run: bool = True,
    skip_done: bool = True,
) -> list[RawOrder]:
    """Collect and scrape. Writes raw JSON to data/scrape/raw/."""
    from playwright.sync_api import sync_playwright

    state = load_state()
    results: list[RawOrder] = []
    mode = "DRY RUN (nothing written to the database)" if dry_run else "LIVE"

    with sync_playwright() as pw:
        ctx = open_browser(pw, headless=False)
        page = ctx.new_page()
        try:
            page.goto(f"{BASE}/", wait_until="domcontentloaded")
            _pause(1.0, 2.0)
            if "/signin" in page.url or not page.query_selector('a[href*="/mypurchases"]'):
                wait_for_login(page)

            urls = state.get("order_urls") or collect_order_urls(page, max_orders)
            state["order_urls"] = urls
            save_state(state)

            done = set(state.get("done", []))
            targets = [u for u in urls if not (skip_done and u in done)]
            if not targets:
                print("Nothing left to scrape. All known orders already captured.")
                return []

            print(f"Scraping {len(targets)} order(s) [{mode}] ...")
            for i, url in enumerate(targets, 1):
                print(f"  [{i}/{len(targets)}] {url.rsplit('/', 1)[-1][:40]}")
                try:
                    order = scrape_one(page, url)
                except ScrapePaused:
                    print("\nKroger started blocking. Stopping. Wait before retrying.")
                    save_state(state)
                    raise
                results.append(order)

                # Checkpoint every order so a crash costs one order, not all of them.
                slug = url.rsplit("/", 1)[-1][:60].replace("?", "_") or f"order{i}"
                out = raw_dir() / f"{slug}.json"
                out.write_text(json.dumps(order.to_json(), indent=2))
                state["done"] = sorted(done | {url})
                save_state(state)
                _pause(2.5, 6.0)

            (scrape_dir() / "last_run.json").write_text(
                json.dumps(
                    {
                        "mode": mode,
                        "count": len(results),
                        "at": datetime.now().isoformat(timespec="seconds"),
                    },
                    indent=2,
                )
            )
            print(f"\nCaptured {len(results)} order(s) -> {raw_dir()}")
        finally:
            ctx.close()
    return results


def load_raw() -> list[dict]:
    """Every previously captured raw order."""
    out = []
    for p in sorted(raw_dir().glob("*.json")):
        try:
            out.append(json.loads(p.read_text()))
        except json.JSONDecodeError:
            logger.warning("skipping unparseable %s", p.name)
    return out
