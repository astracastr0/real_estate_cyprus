#!/usr/bin/env python3
"""Backfill image_url for existing listings that don't have one.

Usage:
    python3 backfill_images.py --source dom_cy [--batch 50] [--delay 1.5]
    python3 backfill_images.py --source sothebys
    python3 backfill_images.py --source bazaraki
"""

import argparse
import logging
import re
import sqlite3
import ssl
import time
import random
from pathlib import Path
from urllib.request import urlopen, Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
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


def get_listings_without_image(db_path: Path, district: str | None = None,
                               max_price: int | None = None,
                               bedrooms: int | None = None) -> list[tuple[str, str]]:
    """Return (id, url) for non-expired listings missing image_url."""
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
    conditions = [
        "(image_url IS NULL OR image_url = '')",
        "url IS NOT NULL",
    ]
    params = []
    if "is_expired" in cols:
        conditions.append("(is_expired IS NULL OR is_expired = 0)")
    if district:
        conditions.append("district = ?")
        params.append(district)
    if max_price is not None:
        conditions.append("price_eur < ?")
        params.append(max_price)
    if bedrooms is not None:
        conditions.append("bedrooms = ?")
        params.append(bedrooms)
    sql = "SELECT id, url FROM listings WHERE " + " AND ".join(conditions)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def update_image_url(db_path: Path, listing_id: str, image_url: str):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE listings SET image_url = ? WHERE id = ?", (image_url, listing_id))
    conn.commit()
    conn.close()


# ── Sotheby's: plain HTTP ────────────────────────────────────────────────────

def fetch_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urlopen(req, timeout=30, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_image_sothebys(html: str) -> str | None:
    m = re.search(r'<img[^>]+src="([^"]+/data/content/images/[^"]+)"', html)
    if m:
        return m.group(1)
    m = re.search(r'<img[^>]+src="(https?://[^"]+\.(?:jpg|jpeg|png|webp))"', html, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def backfill_sothebys(delay: float, district=None, max_price=None, bedrooms=None):
    if not SOTHEBYS_DB.exists():
        log.warning("Sotheby's DB not found")
        return
    listings = get_listings_without_image(SOTHEBYS_DB, district, max_price, bedrooms)
    log.info("Sotheby's: %d listings to backfill", len(listings))
    updated = 0
    for i, (lid, url) in enumerate(listings):
        try:
            html = fetch_html(url)
            img = extract_image_sothebys(html)
            if img:
                update_image_url(SOTHEBYS_DB, lid, img)
                updated += 1
                log.info("  [%d/%d] %s -> %s", i + 1, len(listings), lid, img[:80])
            else:
                log.info("  [%d/%d] %s -> no image found", i + 1, len(listings), lid)
        except Exception as e:
            log.warning("  [%d/%d] %s -> error: %s", i + 1, len(listings), lid, e)
        time.sleep(random.uniform(delay * 0.5, delay * 1.5))
    log.info("Sotheby's done: %d/%d updated", updated, len(listings))


# ── Dom.cy: Playwright via CDP ───────────────────────────────────────────────

def backfill_domcy(delay: float, district=None, max_price=None, bedrooms=None):
    if not DOMCY_DB.exists():
        log.warning("Dom.cy DB not found")
        return
    listings = get_listings_without_image(DOMCY_DB, district, max_price, bedrooms)
    log.info("Dom.cy: %d listings to backfill", len(listings))
    if not listings:
        return

    import subprocess, socket
    from playwright.sync_api import sync_playwright

    CDP_PORT = 9222
    CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    CHROME_PROFILE = "/tmp/dom_cy_chrome_profile"

    # Ensure Chrome is running
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    if sock.connect_ex(("127.0.0.1", CDP_PORT)) != 0:
        log.info("Launching Chrome with CDP on port %d...", CDP_PORT)
        subprocess.Popen(
            [CHROME_PATH, f"--remote-debugging-port={CDP_PORT}",
             f"--user-data-dir={CHROME_PROFILE}",
             "--no-first-run", "--no-default-browser-check", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        time.sleep(3)
    sock.close()

    updated = 0
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}")
        ctx = browser.contexts[0]
        page = ctx.new_page()

        for i, (lid, url) in enumerate(listings):
            try:
                page.goto(url, wait_until="domcontentloaded")
                # Wait for content
                start = time.time()
                while time.time() - start < 15:
                    if len(page.inner_text("body")) > 500:
                        break
                    time.sleep(1)
                page.wait_for_timeout(1000)

                img_el = (
                    page.query_selector("img[src*='/upload/']")
                    or page.query_selector(".detail-gallery img")
                    or page.query_selector(".catalog-element img")
                    or page.query_selector("img[src*='.jpg']")
                    or page.query_selector("img[src*='.jpeg']")
                    or page.query_selector("img[src*='.webp']")
                )
                if img_el:
                    img_src = img_el.get_attribute("src") or img_el.get_attribute("data-src")
                    if img_src and not img_src.endswith(".svg"):
                        if not img_src.startswith("http"):
                            img_src = "https://dom.com.cy" + img_src
                        update_image_url(DOMCY_DB, lid, img_src)
                        updated += 1
                        log.info("  [%d/%d] %s -> %s", i + 1, len(listings), lid, img_src[:80])
                    else:
                        log.info("  [%d/%d] %s -> no image found", i + 1, len(listings), lid)
                else:
                    log.info("  [%d/%d] %s -> no image found", i + 1, len(listings), lid)
            except Exception as e:
                log.warning("  [%d/%d] %s -> error: %s", i + 1, len(listings), lid, e)

            time.sleep(random.uniform(delay * 0.5, delay * 1.5))

        page.close()
        browser.close()

    log.info("Dom.cy done: %d/%d updated", updated, len(listings))


# ── Bazaraki: Playwright headed ──────────────────────────────────────────────

def backfill_bazaraki(delay: float, district=None, max_price=None, bedrooms=None):
    if not BAZARAKI_DB.exists():
        log.warning("Bazaraki DB not found")
        return
    listings = get_listings_without_image(BAZARAKI_DB, district, max_price, bedrooms)
    log.info("Bazaraki: %d listings to backfill", len(listings))
    if not listings:
        return

    from playwright.sync_api import sync_playwright

    updated = 0
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=False,
            args=["--disable-blink-features=AutomationControlled", f"--user-agent={USER_AGENT}"],
        )
        context = browser.new_context(user_agent=USER_AGENT, viewport={"width": 1920, "height": 1080}, locale="en-US")
        page = context.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")

        for i, (lid, url) in enumerate(listings):
            try:
                page.goto(url, wait_until="domcontentloaded")
                # Wait for Cloudflare
                start = time.time()
                while time.time() - start < 30:
                    title = page.title().lower()
                    if "just a moment" not in title and "cloudflare" not in title:
                        break
                    time.sleep(1)
                page.wait_for_timeout(1500)

                img_el = (
                    page.query_selector("img.announcement__images-item")
                    or page.query_selector("img[src*='cdn1.bazaraki.com']")
                    or page.query_selector("img[src*='cdn.bazaraki.com']")
                )
                if img_el:
                    img_src = img_el.get_attribute("src") or img_el.get_attribute("data-src")
                    if img_src:
                        update_image_url(BAZARAKI_DB, lid, img_src)
                        updated += 1
                        log.info("  [%d/%d] %s -> %s", i + 1, len(listings), lid, img_src[:80])
                    else:
                        log.info("  [%d/%d] %s -> no image found", i + 1, len(listings), lid)
                else:
                    log.info("  [%d/%d] %s -> no image found", i + 1, len(listings), lid)
            except Exception as e:
                log.warning("  [%d/%d] %s -> error: %s", i + 1, len(listings), lid, e)

            time.sleep(random.uniform(delay * 0.8, delay * 1.5))

        browser.close()

    log.info("Bazaraki done: %d/%d updated", updated, len(listings))


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Backfill image_url for existing listings")
    parser.add_argument("--source", required=True, choices=["bazaraki", "dom_cy", "sothebys", "all"])
    parser.add_argument("--delay", type=float, default=1.5, help="Avg delay between requests (seconds)")
    parser.add_argument("--district", type=str, default=None, help="Filter by district (e.g. Larnaca)")
    parser.add_argument("--max-price", type=int, default=None, help="Filter by max price")
    parser.add_argument("--bedrooms", type=int, default=None, help="Filter by bedroom count")
    args = parser.parse_args()

    kw = dict(district=args.district, max_price=args.max_price, bedrooms=args.bedrooms)

    if args.source in ("sothebys", "all"):
        backfill_sothebys(args.delay, **kw)
    if args.source in ("dom_cy", "all"):
        backfill_domcy(args.delay, **kw)
    if args.source in ("bazaraki", "all"):
        backfill_bazaraki(args.delay, **kw)


if __name__ == "__main__":
    main()
