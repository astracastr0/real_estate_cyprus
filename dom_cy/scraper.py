"""dom.com.cy real-estate scraper using Playwright.

Requires a running Chrome instance with remote debugging:
  /Applications/Google Chrome.app/Contents/MacOS/Google Chrome \
    --remote-debugging-port=9222 \
    --user-data-dir=/tmp/dom_cy_chrome_profile \
    --no-first-run --no-default-browser-check about:blank

This is needed because dom.com.cy uses SafeLine WAF which detects
Playwright's bundled Chromium. Connecting to real Chrome via CDP bypasses this.
"""

import os
import re
import sys
import time
import random
import logging
import subprocess
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, Page, Browser

from config import (
    BASE_URL, QUERIES, MAX_PAGES_PER_QUERY,
    DELAY_BETWEEN_DETAILS, DELAY_BETWEEN_PAGES, CLOUDFLARE_TIMEOUT,
)
from db import init_db, listing_exists, upsert_listing, count_listings
from notify import notify_deals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

CDP_PORT = 9222
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE = "/tmp/dom_cy_chrome_profile"


# --- Parsers ---

def parse_price(text: str) -> int | None:
    """Convert '2 000 000 EUR' or '350,000' to integer euros."""
    if not text:
        return None
    # Remove currency, spaces, non-breaking spaces, +VAT suffix
    cleaned = re.sub(r"[€\s\u00a0]", "", text.strip())
    cleaned = re.sub(r"\+?VAT.*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"EUR.*", "", cleaned, flags=re.IGNORECASE).strip()

    # Handle European format: dots as thousands separators
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(".") > 1:
        cleaned = cleaned.replace(".", "")
    elif "." in cleaned:
        parts = cleaned.split(".")
        if len(parts[1]) == 3:
            cleaned = cleaned.replace(".", "")
    elif "," in cleaned:
        parts = cleaned.split(",")
        if len(parts[1]) == 3:
            cleaned = cleaned.replace(",", "")
        else:
            cleaned = cleaned.replace(",", ".")

    try:
        val = int(float(cleaned))
        if val > 100_000_000:
            s = str(val)
            mid = len(s) // 2
            try:
                return int(s[:mid])
            except ValueError:
                pass
        return val
    except (ValueError, TypeError):
        return None


def parse_price_per_sqm(text: str) -> float | None:
    """Extract number from '12 658 EUR/m2'."""
    if not text:
        return None
    # Remove spaces within number: "12 658" -> "12658"
    cleaned = re.sub(r"(\d)\s+(\d)", r"\1\2", text)
    match = re.search(r"([\d.,]+)\s*(?:EUR|€)\s*/\s*m", cleaned, re.IGNORECASE)
    if match:
        num_str = match.group(1).replace(",", ".")
        try:
            return float(num_str)
        except ValueError:
            return None
    return None


def parse_sqm(text: str) -> float | None:
    """Extract number from '158 m2' or '210 m²'."""
    if not text:
        return None
    match = re.search(r"([\d.,]+)\s*m", text)
    if match:
        num_str = match.group(1).replace(",", ".")
        try:
            return float(num_str)
        except ValueError:
            return None
    return None


def parse_bedrooms_from_title(title: str) -> int | None:
    """Extract bedroom count from title like '3-bedroom apartment in Limassol'."""
    if not title:
        return None
    match = re.search(r"(\d+)-bedroom", title, re.IGNORECASE)
    if match:
        return int(match.group(1))
    if re.search(r"studio", title, re.IGNORECASE):
        return 0
    return None


def extract_id_from_url(url: str) -> str | None:
    """Extract listing ID from URL like '/en/catalog/sale/210124/'."""
    match = re.search(r"/catalog/sale/(\d+)/?", url)
    if match:
        return match.group(1)
    return None


# --- Browser helpers ---

def _kill_chrome():
    """Kill any existing Chrome debug instances."""
    import signal as sig
    try:
        result = subprocess.run(
            ["lsof", "-ti", f":{CDP_PORT}"], capture_output=True, text=True
        )
        if result.stdout.strip():
            for pid in result.stdout.strip().split("\n"):
                try:
                    os.kill(int(pid), sig.SIGTERM)
                except (ProcessLookupError, ValueError):
                    pass
            time.sleep(2)
    except Exception:
        pass


def _launch_chrome():
    """Launch Chrome with CDP."""
    log.info("Launching Chrome with remote debugging on port %d...", CDP_PORT)
    subprocess.Popen(
        [CHROME_PATH,
         f"--remote-debugging-port={CDP_PORT}",
         f"--user-data-dir={CHROME_PROFILE}",
         "--no-first-run", "--no-default-browser-check",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(3)


def ensure_chrome_running():
    """Launch Chrome with CDP if not already running."""
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(("127.0.0.1", CDP_PORT))
    sock.close()
    if result == 0:
        log.info("Chrome already running on port %d", CDP_PORT)
        return

    _launch_chrome()


def connect_browser(playwright):
    """Connect to Chrome via CDP. Restart Chrome if connection fails."""
    try:
        return playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{CDP_PORT}", timeout=30000
        )
    except Exception as e:
        log.warning("CDP connect failed: %s. Restarting Chrome...", e)
        _kill_chrome()
        _launch_chrome()
        return playwright.chromium.connect_over_cdp(
            f"http://127.0.0.1:{CDP_PORT}", timeout=30000
        )


def wait_for_content(page: Page, timeout: int = CLOUDFLARE_TIMEOUT):
    """Wait for SafeLine WAF challenge to pass and content to load."""
    start = time.time()
    while time.time() - start < timeout:
        body_len = len(page.inner_text("body"))
        if body_len > 500:
            return True
        time.sleep(1)
    log.warning("Content did not load after %ds (WAF challenge?)", timeout)
    return False


# --- Scraping functions ---

def scrape_search_page(page: Page, url: str, district: str, property_type: str) -> list[dict]:
    """Parse all listing cards on one search/catalog page.

    dom.com.cy uses .catalog-item containers with:
    - .number: "№ 210124"
    - .name a: "Apartment in Limassol / Limassol Marina"
    - .feature-item spans: "Total area:" "158 m2", "Bedrooms:" "3"
    - .price: "2 000 000 EUR"
    - .price-dop: "12 658 EUR/m2"
    """
    page.goto(url, wait_until="domcontentloaded")
    wait_for_content(page)
    page.wait_for_timeout(2000)

    items = page.query_selector_all(".catalog-item")
    log.info("Found %d catalog-item cards on %s", len(items), url)

    results = []
    for item in items:
        try:
            # Extract listing ID from .number text or link href
            number_el = item.query_selector(".number")
            listing_id = None
            if number_el:
                id_match = re.search(r"(\d+)", number_el.inner_text())
                if id_match:
                    listing_id = id_match.group(1)

            # Extract URL from first detail link
            link_el = item.query_selector('a[href*="/catalog/sale/"]')
            href = link_el.get_attribute("href") if link_el else None
            if href and not listing_id:
                listing_id = extract_id_from_url(href)
            if not listing_id:
                continue

            listing_url = BASE_URL + href if href and not href.startswith("http") else (href or "")

            # Title from .name
            name_el = item.query_selector(".name")
            title = name_el.inner_text().strip() if name_el else ""

            # Price from .price
            price_el = item.query_selector(".price")
            price_text = price_el.inner_text().strip() if price_el else ""
            price_eur = parse_price(price_text)

            # Price per sqm from .price-dop
            ppsqm_el = item.query_selector(".price-dop")
            price_per_sqm = parse_price_per_sqm(ppsqm_el.inner_text().strip()) if ppsqm_el else None

            # Features: Total area, Bedrooms from .feature-item spans
            bedrooms = None
            area_sqm = None
            feature_items = item.query_selector_all(".feature-item")
            for fi in feature_items:
                fi_text = fi.inner_text().strip()
                bed_match = re.search(r"Bedrooms?:?\s*(\d+)", fi_text, re.IGNORECASE)
                if bed_match:
                    bedrooms = int(bed_match.group(1))
                area_match = re.search(r"Total\s+area:?\s*([\d.,]+)\s*m", fi_text, re.IGNORECASE)
                if area_match:
                    try:
                        area_sqm = float(area_match.group(1).replace(",", "."))
                    except ValueError:
                        pass

            if not bedrooms:
                bedrooms = parse_bedrooms_from_title(title)

            # Extract area (sub-district) from title: "Apartment in Limassol / Limassol Marina"
            area = None
            if "/" in title:
                area = title.split("/")[-1].strip()

            # Derive condition from title prefix "New ..."
            condition = "New" if title.lower().startswith("new ") else "Resale"

            # Extract first image URL
            image_url = None
            img_el = item.query_selector("img")
            if img_el:
                image_url = img_el.get_attribute("src") or img_el.get_attribute("data-src")

            results.append({
                "id": listing_id,
                "url": listing_url,
                "title": title,
                "price_eur": price_eur,
                "price_original": price_text,
                "price_per_sqm": price_per_sqm,
                "property_type": property_type,
                "district": district,
                "area": area,
                "bedrooms": bedrooms,
                "area_sqm": area_sqm,
                "condition": condition,
                "source": "dom_cy",
                "image_url": image_url,
            })
        except Exception as e:
            log.warning("Error parsing card: %s", e)
            continue

    return results


def scrape_detail_page(page: Page, url: str, base_data: dict) -> dict:
    """Visit individual listing and extract full characteristics.

    dom.com.cy embeds `arCatalogElementResult` JS object with structured data.
    Fields are objects with CODE/NAME/ID keys (e.g. UF_BEDROOMS.CODE = "3").
    """
    data = dict(base_data)

    page.goto(url, wait_until="domcontentloaded")
    wait_for_content(page)
    page.wait_for_timeout(1000)

    # Extract the Bitrix JS object (most reliable data source)
    js_data = None
    try:
        js_data = page.evaluate("""
            () => {
                if (typeof arCatalogElementResult === 'undefined' || !arCatalogElementResult.row)
                    return null;
                const r = arCatalogElementResult.row;
                // Helper to extract CODE or NAME from object fields
                const val = (f) => {
                    if (!f) return null;
                    if (typeof f === 'object' && f.CODE !== undefined) return f.CODE;
                    if (typeof f === 'object' && f.NAME !== undefined) return f.NAME;
                    return f;
                };
                const name = (f) => {
                    if (!f) return null;
                    if (typeof f === 'object' && f.NAME !== undefined) return f.NAME;
                    return String(f);
                };
                // Extract availability names
                let avail_names = [];
                if (r.UF_AVAILABILITY && typeof r.UF_AVAILABILITY === 'object') {
                    for (const k of Object.keys(r.UF_AVAILABILITY)) {
                        const item = r.UF_AVAILABILITY[k];
                        if (item && item.NAME) avail_names.push(item.NAME);
                    }
                }
                return {
                    price: r.UF_PRICE,
                    total_area: r.UF_TOTAL_AREA,
                    indoor_area: r.UF_INDOOR_AREA,
                    land_area: r.UF_LAND_AREA,
                    bedrooms: val(r.UF_BEDROOMS),
                    bathrooms: val(r.UF_BATHROOMS),
                    city: name(r.UF_CITY),
                    district: name(r.UF_DISTRICT),
                    sea_distance: r.UF_SEA_DISTANCE,
                    condition: name(r.UF_STATUS),
                    year: r.UF_COMPLETE_YEAR,
                    energy: name(r.UF_ENERGY_CLASS),
                    floor: r.UF_FLOOR,
                    storeys: r.UF_STOREYS,
                    vat: r.UF_VAT,
                    availability: avail_names,
                    new_building: r.UF_NEW_BUILDING,
                    price_m: r.PRICE_M,
                    id: r.ID,
                };
            }
        """)
    except Exception as e:
        log.debug("Could not extract JS data: %s", e)

    if js_data:
        if js_data.get("price"):
            try:
                data["price_eur"] = int(float(str(js_data["price"])))
                data["price_original"] = f"{data['price_eur']} EUR"
            except (ValueError, TypeError):
                pass

        if js_data.get("total_area"):
            try:
                area_val = float(str(js_data["total_area"]))
                if area_val > 0:
                    data["area_sqm"] = area_val
            except (ValueError, TypeError):
                pass

        if js_data.get("land_area"):
            try:
                land = float(str(js_data["land_area"]))
                if land > 0:
                    data["plot_sqm"] = land
            except (ValueError, TypeError):
                pass

        if js_data.get("bedrooms"):
            try:
                data["bedrooms"] = int(str(js_data["bedrooms"]))
            except (ValueError, TypeError):
                pass

        if js_data.get("bathrooms"):
            try:
                data["bathrooms"] = int(str(js_data["bathrooms"]))
            except (ValueError, TypeError):
                pass

        if js_data.get("district"):
            data["area"] = str(js_data["district"])
        if js_data.get("city"):
            city = str(js_data["city"])
            dist = str(js_data.get("district") or "")
            data["location_full"] = f"{city}, {dist}" if dist else city

        # Derive condition from new_building flag or title
        # UF_STATUS is CRM status ("At work"), not property condition
        if js_data.get("new_building") and str(js_data["new_building"]) == "1":
            data["condition"] = "New"
        elif data.get("title") and data["title"].lower().startswith("new "):
            data["condition"] = "New"
        else:
            data["condition"] = "Resale"

        if js_data.get("year"):
            year_match = re.search(r"(\d{4})", str(js_data["year"]))
            if year_match:
                data["construction_year"] = int(year_match.group(1))

        if js_data.get("energy"):
            data["energy_efficiency"] = str(js_data["energy"])

        # Availability (Pool, Parking, Furniture, etc.)
        avail = js_data.get("availability") or []
        if avail:
            avail_str = ", ".join(avail)
            data["included"] = avail_str
            data["has_pool"] = any("pool" in a.lower() for a in avail)
            data["parking"] = "Yes" if any("parking" in a.lower() for a in avail) else None
            data["furnishing"] = "Yes" if any("furniture" in a.lower() for a in avail) else None

        # price_per_sqm from JS or computed
        if js_data.get("price_m"):
            try:
                data["price_per_sqm"] = float(str(js_data["price_m"]))
            except (ValueError, TypeError):
                pass

    # Title from <h1>
    h1 = page.query_selector("h1")
    if h1:
        data["title"] = h1.inner_text().strip()

    # Compute price_per_sqm if not set
    if not data.get("price_per_sqm") and data.get("price_eur") and data.get("area_sqm") and data["area_sqm"] > 0:
        data["price_per_sqm"] = round(data["price_eur"] / data["area_sqm"], 2)

    # If JS extraction failed, parse HTML characteristics
    if not js_data:
        _parse_html_characteristics(page, data)

    # Extract first image from detail page
    img_el = page.query_selector("img[src*='/upload/']") or page.query_selector(".detail-gallery img") or page.query_selector("img")
    if img_el:
        img_src = img_el.get_attribute("src") or img_el.get_attribute("data-src")
        if img_src and not img_src.endswith(".svg"):
            data["image_url"] = img_src if img_src.startswith("http") else BASE_URL + img_src

    # Ensure condition is always set (needed for p25 segmentation)
    if not data.get("condition") or data["condition"] == "At work":
        if data.get("title") and data["title"].lower().startswith("new "):
            data["condition"] = "New"
        else:
            data["condition"] = "Resale"

    data["scraped_at"] = datetime.now(timezone.utc).isoformat()
    data["source"] = "dom_cy"

    return data


def _parse_html_characteristics(page: Page, data: dict):
    """Fallback: parse characteristics from HTML key-value text pairs."""
    body_text = page.inner_text("body")
    lines = [l.strip() for l in body_text.split("\n") if l.strip()]

    chars = {}
    for line in lines:
        kv_match = re.match(r"^(.+?):\s*(.+)$", line)
        if kv_match:
            key = kv_match.group(1).strip().lower()
            val = kv_match.group(2).strip()
            chars[key] = val

    if "bedrooms" in chars and not data.get("bedrooms"):
        try:
            data["bedrooms"] = int(chars["bedrooms"])
        except ValueError:
            pass
    if "bathrooms" in chars and not data.get("bathrooms"):
        try:
            data["bathrooms"] = int(chars["bathrooms"])
        except ValueError:
            pass
    if "total area" in chars and not data.get("area_sqm"):
        data["area_sqm"] = parse_sqm(chars["total area"])
    if ("plot area" in chars or "land area" in chars) and not data.get("plot_sqm"):
        val = chars.get("plot area") or chars.get("land area", "")
        data["plot_sqm"] = parse_sqm(val)
    if "district" in chars and not data.get("area"):
        data["area"] = chars["district"]
    if "city" in chars:
        city = chars["city"]
        dist = chars.get("district", "")
        data["location_full"] = f"{city}, {dist}" if dist else city

    if not data.get("price_per_sqm") and data.get("price_eur") and data.get("area_sqm") and data["area_sqm"] > 0:
        data["price_per_sqm"] = round(data["price_eur"] / data["area_sqm"], 2)


def run(max_pages: int = MAX_PAGES_PER_QUERY, fast: bool = False,
        bedrooms_filter: set[int] | None = None,
        districts_filter: set[str] | None = None):
    """Main scraper loop. If fast=True, skip detail pages (cards only).
    If bedrooms_filter is set, only process listings with matching bedroom count.
    If districts_filter is set, only scrape matching districts."""
    init_db()
    run_started = datetime.now(timezone.utc).isoformat()

    queries = QUERIES
    if districts_filter:
        queries = [q for q in QUERIES if q["district"] in districts_filter]

    ensure_chrome_running()

    with sync_playwright() as pw:
        browser = connect_browser(pw)
        ctx = browser.contexts[0]
        page = ctx.new_page()

        total_new = 0
        total_skipped = 0

        for query in queries:
            district = query["district"]
            prop_type = query["property_type"]
            base_path = query["path"]

            log.info("=== Scraping %s in %s ===", prop_type, district)

            for page_num in range(1, max_pages + 1):
                url = BASE_URL + base_path
                if page_num > 1:
                    url += f"?page=page-{page_num}"

                log.info("Search page %d: %s", page_num, url)

                try:
                    cards = scrape_search_page(page, url, district, prop_type)
                except Exception as e:
                    log.error("Error on search page %s: %s", url, e)
                    break

                if not cards:
                    log.info("No cards found, stopping pagination for this query.")
                    break

                for card_data in cards:
                    listing_id = card_data["id"]

                    if listing_exists(listing_id):
                        total_skipped += 1
                        continue

                    # Filter by bedrooms if specified
                    if bedrooms_filter and card_data.get("bedrooms") not in bedrooms_filter:
                        continue

                    if fast:
                        card_data["scraped_at"] = datetime.now(timezone.utc).isoformat()
                        upsert_listing(card_data)
                        total_new += 1
                        log.info(
                            "  [%d new] %s — €%s — %s",
                            total_new,
                            card_data.get("title", "")[:50],
                            card_data.get("price_eur", "?"),
                            card_data.get("district", ""),
                        )
                    else:
                        detail_url = card_data.get("url")
                        if detail_url:
                            try:
                                full_data = scrape_detail_page(page, detail_url, card_data)
                                upsert_listing(full_data)
                                total_new += 1
                                log.info(
                                    "  [%d new] %s — €%s — %s",
                                    total_new,
                                    full_data.get("title", "")[:50],
                                    full_data.get("price_eur", "?"),
                                    full_data.get("district", ""),
                                )
                            except Exception as e:
                                log.warning("  Error on detail page %s: %s", detail_url, e)
                        else:
                            card_data["scraped_at"] = datetime.now(timezone.utc).isoformat()
                            upsert_listing(card_data)
                            total_new += 1

                    # Polite delay between detail pages
                    time.sleep(random.uniform(*DELAY_BETWEEN_DETAILS))

                # Polite delay between search pages
                time.sleep(random.uniform(*DELAY_BETWEEN_PAGES))

        page.close()
        browser.close()

    total = count_listings()
    log.info("=== Done. %d new listings, %d skipped, %d total in DB ===",
             total_new, total_skipped, total)

    if total_new > 0:
        notify_deals(run_started)


if __name__ == "__main__":
    # --notify: send deals from the last N hours without scraping
    if "--notify" in sys.argv:
        from datetime import timedelta
        hours = 24
        idx = sys.argv.index("--notify")
        if idx + 1 < len(sys.argv):
            try:
                hours = int(sys.argv[idx + 1])
            except ValueError:
                pass
        since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        log.info("Sending notification for deals from the last %d hours", hours)
        init_db()
        notify_deals(since)
        sys.exit(0)

    pages = MAX_PAGES_PER_QUERY
    fast_mode = "--fast" in sys.argv
    bed_filter = None
    dist_filter = None

    args = sys.argv[1:]
    i = 0
    positional = []
    while i < len(args):
        if args[i] == "--fast":
            i += 1
        elif args[i] == "--bedrooms" and i + 1 < len(args):
            bed_filter = {int(x) for x in args[i + 1].split(",")}
            i += 2
        elif args[i] == "--districts" and i + 1 < len(args):
            dist_filter = {x.strip() for x in args[i + 1].split(",")}
            i += 2
        else:
            positional.append(args[i])
            i += 1

    if positional:
        try:
            pages = int(positional[0])
        except ValueError:
            print(f"Usage: python {sys.argv[0]} [max_pages] [--fast] [--bedrooms 2,3] [--districts Larnaca,Limassol] [--notify [hours]]")
            sys.exit(1)

    if bed_filter:
        log.info("Filtering for bedrooms: %s", bed_filter)
    if dist_filter:
        log.info("Filtering for districts: %s", dist_filter)
    run(max_pages=pages, fast=fast_mode, bedrooms_filter=bed_filter, districts_filter=dist_filter)
