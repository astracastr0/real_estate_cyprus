#!/usr/bin/env python3
"""Check which listings have expired (ad removed / URL dead).

Adds `is_expired` and `expired_checked_at` columns to DB if missing.
- dom.cy: simple HTTP HEAD (no Cloudflare)
- bazaraki: Playwright headed browser (Cloudflare-protected)

Usage:
    python3 check_expired.py [--source bazaraki|dom_cy|all] [--batch N] [--only-unchecked]
"""

import argparse
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import urlopen, Request

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

BASE = Path(__file__).parent
BAZARAKI_DB = BASE / "bazaraki" / "bazaraki.db"
DOMCY_DB = BASE / "dom_cy" / "dom_cy.db"
SOTHEBYS_DB = BASE / "sothebys" / "sothebys.db"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def ensure_columns(db_path: Path):
    """Add is_expired and expired_checked_at columns if they don't exist."""
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
    if "is_expired" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN is_expired INTEGER DEFAULT 0")
        log.info("Added is_expired column to %s", db_path.name)
    if "expired_checked_at" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN expired_checked_at TEXT")
        log.info("Added expired_checked_at column to %s", db_path.name)
    conn.commit()
    conn.close()


def get_urls_to_check(db_path: Path, only_unchecked: bool) -> list[tuple[str, str]]:
    """Return list of (id, url) to check."""
    conn = sqlite3.connect(db_path)
    if only_unchecked:
        rows = conn.execute(
            "SELECT id, url FROM listings WHERE expired_checked_at IS NULL AND url IS NOT NULL AND (is_expired IS NULL OR is_expired = 0)"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, url FROM listings WHERE url IS NOT NULL AND (is_expired IS NULL OR is_expired = 0)"
        ).fetchall()
    conn.close()
    return rows


def mark_expired(db_path: Path, listing_id: str, is_expired: bool):
    """Update a single listing's expired status."""
    conn = sqlite3.connect(db_path)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE listings SET is_expired = ?, expired_checked_at = ? WHERE id = ?",
        (1 if is_expired else 0, now, listing_id),
    )
    conn.commit()
    conn.close()


# ── dom.cy: simple HTTP check ────────────────────────────────────────────────

def check_domcy_url(url: str) -> bool:
    """Return True if expired (404 or error)."""
    try:
        req = Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=15) as resp:
            return resp.status == 404
    except Exception as e:
        err = str(e)
        if "404" in err:
            return True
        if "SSL" in err or "certificate" in err:
            # Retry without SSL verification
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            try:
                req = Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
                with urlopen(req, timeout=15, context=ctx) as resp:
                    return resp.status == 404
            except Exception as e2:
                if "404" in str(e2):
                    return True
                log.debug("SSL retry failed for %s: %s", url, e2)
                return False
        log.debug("Error checking %s: %s", url, e)
        return False


def check_domcy(only_unchecked: bool, batch_size: int):
    """Check all dom.cy listings for expiry using HTTP HEAD."""
    if not DOMCY_DB.exists():
        log.warning("Dom.cy DB not found")
        return

    ensure_columns(DOMCY_DB)
    urls = get_urls_to_check(DOMCY_DB, only_unchecked)
    log.info("Dom.cy: %d URLs to check", len(urls))

    expired_count = 0
    checked = 0

    with ThreadPoolExecutor(max_workers=5) as pool:
        for i in range(0, len(urls), batch_size):
            batch = urls[i : i + batch_size]
            futures = {pool.submit(check_domcy_url, url): (lid, url) for lid, url in batch}
            for future in as_completed(futures):
                lid, url = futures[future]
                try:
                    is_expired = future.result()
                except Exception:
                    is_expired = False
                mark_expired(DOMCY_DB, lid, is_expired)
                checked += 1
                if is_expired:
                    expired_count += 1
                    log.info("EXPIRED: %s", url)
                if checked % 50 == 0:
                    log.info("Dom.cy progress: %d/%d checked, %d expired", checked, len(urls), expired_count)

    log.info("Dom.cy done: %d/%d expired", expired_count, len(urls))


# ── Sotheby's: HTTP check (same as dom.cy, no anti-bot) ──────────────────────

def check_sothebys_url(url: str) -> bool:
    """Return True if expired (404 or error)."""
    try:
        req = Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with urlopen(req, timeout=15, context=ctx) as resp:
            return resp.status == 404
    except Exception as e:
        if "404" in str(e):
            return True
        log.debug("Error checking %s: %s", url, e)
        return False


def check_sothebys(only_unchecked: bool, batch_size: int):
    """Check all Sotheby's listings for expiry using HTTP HEAD."""
    if not SOTHEBYS_DB.exists():
        log.warning("Sotheby's DB not found")
        return

    ensure_columns(SOTHEBYS_DB)
    urls = get_urls_to_check(SOTHEBYS_DB, only_unchecked)
    log.info("Sotheby's: %d URLs to check", len(urls))

    expired_count = 0
    checked = 0

    with ThreadPoolExecutor(max_workers=5) as pool:
        for i in range(0, len(urls), batch_size):
            batch = urls[i : i + batch_size]
            futures = {pool.submit(check_sothebys_url, url): (lid, url) for lid, url in batch}
            for future in as_completed(futures):
                lid, url = futures[future]
                try:
                    is_expired = future.result()
                except Exception:
                    is_expired = False
                mark_expired(SOTHEBYS_DB, lid, is_expired)
                checked += 1
                if is_expired:
                    expired_count += 1
                    log.info("EXPIRED: %s", url)
                if checked % 50 == 0:
                    log.info("Sotheby's progress: %d/%d checked, %d expired", checked, len(urls), expired_count)

    log.info("Sotheby's done: %d/%d expired", expired_count, len(urls))


# ── Bazaraki: Playwright check ────────────────────────────────────────────────

BAZARAKI_EXPIRED_SIGNALS = [
    "это объявление было деактивировано",
    "this ad has been deactivated",
    "объявление не найдено",
    "ad not found",
    "page not found",
    "404",
    "has been removed",
    "больше не доступно",
    "no longer available",
]


def check_bazaraki(only_unchecked: bool, batch_size: int):
    """Check bazaraki listings using Playwright (Cloudflare-protected)."""
    if not BAZARAKI_DB.exists():
        log.warning("Bazaraki DB not found")
        return

    ensure_columns(BAZARAKI_DB)
    urls = get_urls_to_check(BAZARAKI_DB, only_unchecked)
    log.info("Bazaraki: %d URLs to check", len(urls))

    if not urls:
        return

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.error("Playwright not installed. Run: pip install playwright && playwright install chromium")
        return

    expired_count = 0
    checked = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=[
                "--disable-blink-features=AutomationControlled",
                f"--user-agent={USER_AGENT}",
            ],
        )
        context = browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )
        page = context.new_page()
        page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        """)

        for lid, url in urls:
            try:
                resp = page.goto(url, wait_until="domcontentloaded", timeout=30000)

                # Wait for Cloudflare
                start = time.time()
                while time.time() - start < 30:
                    title = page.title().lower()
                    if "just a moment" not in title and "cloudflare" not in title:
                        break
                    time.sleep(1)

                page.wait_for_timeout(1500)

                # Check if expired
                is_expired = False

                # Check HTTP status
                if resp and resp.status in (404, 410):
                    is_expired = True
                else:
                    # Check page content for expiry signals
                    body_text = page.inner_text("body").lower()
                    for signal in BAZARAKI_EXPIRED_SIGNALS:
                        if signal in body_text:
                            is_expired = True
                            break

                    # Check if redirected to homepage or search
                    current_url = page.url.lower()
                    if "/adv/" not in current_url and url.lower().startswith("https://www.bazaraki.com/adv/"):
                        is_expired = True

                mark_expired(BAZARAKI_DB, lid, is_expired)
                checked += 1
                if is_expired:
                    expired_count += 1
                    log.info("EXPIRED: %s", url)
                if checked % 20 == 0:
                    log.info("Bazaraki progress: %d/%d checked, %d expired", checked, len(urls), expired_count)

                # Rate limiting
                time.sleep(1 + (0.5 if checked % 10 == 0 else 0))

            except Exception as e:
                log.warning("Error checking %s: %s", url, e)
                mark_expired(BAZARAKI_DB, lid, False)
                checked += 1

        browser.close()

    log.info("Bazaraki done: %d/%d expired", expired_count, len(urls))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Check listing expiry")
    parser.add_argument("--source", choices=["bazaraki", "dom_cy", "sothebys", "all"], default="all")
    parser.add_argument("--batch", type=int, default=50, help="Batch size")
    parser.add_argument("--only-unchecked", action="store_true",
                        help="Only check listings not yet checked")
    args = parser.parse_args()

    if args.source in ("dom_cy", "all"):
        check_domcy(args.only_unchecked, args.batch)
    if args.source in ("bazaraki", "all"):
        check_bazaraki(args.only_unchecked, args.batch)
    if args.source in ("sothebys", "all"):
        check_sothebys(args.only_unchecked, args.batch)


if __name__ == "__main__":
    main()
