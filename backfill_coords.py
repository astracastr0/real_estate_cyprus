#!/usr/bin/env python3
"""Backfill latitude/longitude for existing listings that don't have coordinates.

Usage:
    python3 backfill_coords.py --source sothebys [--delay 1.5]
    python3 backfill_coords.py --source bazaraki [--delay 2.0]
    python3 backfill_coords.py --source sothebys --district Larnaca --max-price 500000
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
SOTHEBYS_DB = BASE / "sothebys" / "sothebys.db"

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)


def get_listings_without_coords(db_path: Path, district: str | None = None,
                                max_price: int | None = None,
                                bedrooms: int | None = None,
                                exclude_conditions: list[str] | None = None,
                                exclude_areas: list[str] | None = None) -> list[tuple[str, str]]:
    """Return (id, url) for non-expired listings missing coordinates."""
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}

    # Ensure columns exist
    if "latitude" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN latitude REAL")
        conn.commit()
    if "longitude" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN longitude REAL")
        conn.commit()

    conditions = [
        "(latitude IS NULL)",
        "url IS NOT NULL",
    ]
    params = []
    if "is_expired" in cols:
        conditions.append("(is_expired IS NULL OR is_expired = 0)")
    if district:
        conditions.append("district = ?")
        params.append(district)
    if max_price is not None:
        conditions.append("price_eur <= ?")
        params.append(max_price)
    if bedrooms is not None:
        conditions.append("bedrooms = ?")
        params.append(bedrooms)
    if exclude_conditions:
        placeholders = ",".join("?" * len(exclude_conditions))
        conditions.append(f"(condition IS NULL OR LOWER(condition) NOT IN ({placeholders}))")
        params.extend(c.lower() for c in exclude_conditions)
    if exclude_areas:
        placeholders = ",".join("?" * len(exclude_areas))
        conditions.append(f"(area IS NULL OR LOWER(area) NOT IN ({placeholders}))")
        params.extend(a.lower() for a in exclude_areas)
    sql = "SELECT id, url FROM listings WHERE " + " AND ".join(conditions)
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return rows


def update_coords(db_path: Path, listing_id: str, lat: float, lng: float):
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE listings SET latitude = ?, longitude = ? WHERE id = ?", (lat, lng, listing_id))
    conn.commit()
    conn.close()


# -- Sotheby's: plain HTTP -------------------------------------------------------

def fetch_html(url: str) -> str:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urlopen(req, timeout=30, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


def backfill_sothebys(delay: float, district=None, max_price=None, bedrooms=None,
                      exclude_conditions=None, exclude_areas=None):
    if not SOTHEBYS_DB.exists():
        log.warning("Sotheby's DB not found")
        return
    listings = get_listings_without_coords(SOTHEBYS_DB, district, max_price, bedrooms,
                                           exclude_conditions, exclude_areas)
    log.info("Sotheby's: %d listings to backfill coordinates", len(listings))
    updated = 0
    for i, (lid, url) in enumerate(listings):
        try:
            html = fetch_html(url)
            coord_match = re.search(r'LatLng\(([\d.]+),\s*([\d.]+)\)', html)
            if coord_match:
                lat = float(coord_match.group(1))
                lng = float(coord_match.group(2))
                update_coords(SOTHEBYS_DB, lid, lat, lng)
                updated += 1
                log.info("  [%d/%d] %s -> %.6f, %.6f", i + 1, len(listings), lid, lat, lng)
            else:
                log.info("  [%d/%d] %s -> no coordinates found", i + 1, len(listings), lid)
        except Exception as e:
            log.warning("  [%d/%d] %s -> error: %s", i + 1, len(listings), lid, e)
        time.sleep(random.uniform(delay * 0.5, delay * 1.5))
    log.info("Sotheby's done: %d/%d updated", updated, len(listings))


# -- Bazaraki: Playwright --------------------------------------------------------

def backfill_bazaraki(delay: float, district=None, max_price=None, bedrooms=None,
                      exclude_conditions=None, exclude_areas=None):
    if not BAZARAKI_DB.exists():
        log.warning("Bazaraki DB not found")
        return
    listings = get_listings_without_coords(BAZARAKI_DB, district, max_price, bedrooms,
                                           exclude_conditions, exclude_areas)
    log.info("Bazaraki: %d listings to backfill coordinates", len(listings))
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

                coords = page.evaluate(r"""() => {
                    const el = document.querySelector('[data-lat]');
                    if (el) return {lat: el.dataset.lat, lng: el.dataset.lng};
                    const txt = document.body.innerHTML;
                    const latM = txt.match(/lat[=:]["']?([\d.]+)/);
                    const lngM = txt.match(/lng[=:]["']?([\d.]+)/);
                    if (latM && lngM) return {lat: latM[1], lng: lngM[1]};
                    return null;
                }""")
                if coords:
                    lat = float(coords["lat"])
                    lng = float(coords["lng"])
                    update_coords(BAZARAKI_DB, lid, lat, lng)
                    updated += 1
                    log.info("  [%d/%d] %s -> %.6f, %.6f", i + 1, len(listings), lid, lat, lng)
                else:
                    log.info("  [%d/%d] %s -> no coordinates found", i + 1, len(listings), lid)
            except Exception as e:
                log.warning("  [%d/%d] %s -> error: %s", i + 1, len(listings), lid, e)

            time.sleep(random.uniform(delay * 0.8, delay * 1.5))

        browser.close()

    log.info("Bazaraki done: %d/%d updated", updated, len(listings))


# -- Main -----------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Backfill coordinates for existing listings")
    parser.add_argument("--source", required=True, choices=["bazaraki", "sothebys", "all"])
    parser.add_argument("--delay", type=float, default=1.5, help="Avg delay between requests (seconds)")
    parser.add_argument("--district", type=str, default=None, help="Filter by district (e.g. Larnaca)")
    parser.add_argument("--max-price", type=int, default=None, help="Filter by max price")
    parser.add_argument("--bedrooms", type=int, default=None, help="Filter by bedroom count")
    parser.add_argument("--exclude-conditions", type=str, default=None,
                        help="Comma-separated conditions to exclude (e.g. resale)")
    parser.add_argument("--exclude-areas", type=str, default=None,
                        help="Comma-separated areas to exclude (e.g. Pyla,Mazotos)")
    args = parser.parse_args()

    exclude_cond = [c.strip() for c in args.exclude_conditions.split(",") if c.strip()] if args.exclude_conditions else None
    exclude_ar = [a.strip() for a in args.exclude_areas.split(",") if a.strip()] if args.exclude_areas else None

    kw = dict(district=args.district, max_price=args.max_price, bedrooms=args.bedrooms,
              exclude_conditions=exclude_cond, exclude_areas=exclude_ar)

    if args.source in ("sothebys", "all"):
        backfill_sothebys(args.delay, **kw)
    if args.source in ("bazaraki", "all"):
        backfill_bazaraki(args.delay, **kw)


if __name__ == "__main__":
    main()
