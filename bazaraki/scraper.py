"""Bazaraki real-estate scraper using Playwright."""

import re
import sys
import time
import random
import logging
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright, Page, Browser

from config import (
    BASE_URL, QUERIES, MAX_PAGES_PER_QUERY,
    DELAY_BETWEEN_DETAILS, DELAY_BETWEEN_PAGES, CLOUDFLARE_TIMEOUT,
    USER_AGENT,
    SEL_LISTING_CARD, SEL_CARD_LINK, SEL_CARD_PRICE, SEL_CARD_TITLE,
    SEL_CARD_LOCATION, SEL_CARD_FEATURE,
    SEL_DETAIL_TITLE, SEL_DETAIL_PRICE, SEL_DETAIL_PRICE_PER_SQM,
    SEL_DETAIL_LOCATION, SEL_DETAIL_CHARS_ROW, SEL_DETAIL_CHAR_KEY,
    SEL_DETAIL_CHAR_VALUE, SEL_DETAIL_DATE, SEL_DETAIL_AD_ID,
)
from db import init_db, listing_exists, upsert_listing, count_listings
from notify import notify_deals

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# --- Parsers ---

def parse_price(text: str) -> int | None:
    """Convert '€470.000' or '€1.200.000' to integer cents-free euros."""
    if not text:
        return None
    # Remove currency symbol, spaces, and non-breaking spaces
    cleaned = re.sub(r"[€\s\u00a0]", "", text.strip())

    # Handle price ranges: "300.000350.000" or "300000350000"
    # Split on €, or detect two dot-separated groups stuck together
    # First, try splitting original text by € to get multiple prices
    prices = re.findall(r"€?\s*([\d.,]+)", text)
    if len(prices) > 1:
        # Multiple prices found (price range) — take the first one
        cleaned = prices[0]

    # Handle European format: dots as thousands separators
    # e.g. "470.000" -> "470000", "1.200.000" -> "1200000"
    if "," in cleaned and "." in cleaned:
        # Format like "1.200.000,00" — dots are thousands, comma is decimal
        cleaned = cleaned.replace(".", "").replace(",", ".")
    elif cleaned.count(".") > 1:
        # Multiple dots = thousands separators: "1.200.000"
        cleaned = cleaned.replace(".", "")
    elif "." in cleaned:
        parts = cleaned.split(".")
        if len(parts[1]) == 3:
            # Single dot with 3 digits after = thousands separator: "470.000"
            cleaned = cleaned.replace(".", "")
        # else: decimal point, keep as is
    elif "," in cleaned:
        cleaned = cleaned.replace(",", ".")

    try:
        val = int(float(cleaned))
        # Sanity check: if value seems unreasonable (>1 billion), likely concatenated
        if val > 100_000_000:
            # Try splitting the digit string in half
            s = str(val)
            mid = len(s) // 2
            try:
                return int(s[:mid])
            except ValueError:
                pass
        return val
    except (ValueError, TypeError):
        return None


def parse_sqm(text: str) -> float | None:
    """Extract number from '210 m²' or '210m²'."""
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


def parse_price_per_sqm(text: str) -> float | None:
    """Extract number from '€2.238/m²'."""
    if not text:
        return None
    match = re.search(r"€?\s*([\d.,]+)\s*/\s*m", text)
    if match:
        num_str = match.group(1).replace(".", "").replace(",", ".")
        try:
            return float(num_str)
        except ValueError:
            return None
    return None


def parse_bedrooms_from_title(title: str) -> int | None:
    """Extract bedroom count from title like '2-bedroom apartment for sale'."""
    if not title:
        return None
    match = re.search(r"(\d+)-bedroom", title, re.IGNORECASE)
    if match:
        return int(match.group(1))
    if re.search(r"studio", title, re.IGNORECASE):
        return 0
    return None


def parse_bedrooms(features: list[str]) -> int | None:
    """Extract bedroom count from feature strings like '3 bed.'."""
    for f in features:
        match = re.search(r"(\d+)\s*bed", f, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def parse_bathrooms(features: list[str]) -> int | None:
    """Extract bathroom count from feature strings."""
    for f in features:
        match = re.search(r"(\d+)\s*bath", f, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def parse_area_from_features(features: list[str]) -> float | None:
    """Extract area in m² from feature strings like '120 m²'."""
    for f in features:
        val = parse_sqm(f)
        if val:
            return val
    return None


def has_pool(included: str | None) -> bool:
    """Check if 'Included' field mentions a pool."""
    if not included:
        return False
    return "pool" in included.lower()


def extract_id_from_card(card) -> str | None:
    """Extract listing ID from card's data-id attribute or link URL."""
    data_id = card.get_attribute("data-id")
    if data_id:
        return data_id.strip()
    # Fallback: extract from URL
    link = card.query_selector("a[href*='/adv/']")
    if link:
        href = link.get_attribute("href") or ""
        match = re.search(r"/adv/(\d+)", href)
        if match:
            return match.group(1)
    return None


# --- Browser helpers ---

def create_browser(playwright) -> Browser:
    """Launch headed Chromium with stealth settings."""
    browser = playwright.chromium.launch(
        headless=False,
        args=[
            "--disable-blink-features=AutomationControlled",
            f"--user-agent={USER_AGENT}",
        ],
    )
    return browser


def create_page(browser: Browser) -> Page:
    """Create a new page with stealth overrides."""
    context = browser.new_context(
        user_agent=USER_AGENT,
        viewport={"width": 1920, "height": 1080},
        locale="en-US",
    )
    page = context.new_page()
    # Override webdriver property
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
    """)
    return page


def wait_cloudflare(page: Page):
    """Wait for Cloudflare challenge to pass."""
    start = time.time()
    while time.time() - start < CLOUDFLARE_TIMEOUT:
        title = page.title().lower()
        if "just a moment" not in title and "cloudflare" not in title:
            return
        time.sleep(1)
    log.warning("Cloudflare challenge may not have passed after %ds", CLOUDFLARE_TIMEOUT)


# --- Scraping functions ---

def scrape_search_page(page: Page, url: str, district: str, property_type: str) -> list[dict]:
    """Parse all listing cards on one search page."""
    page.goto(url, wait_until="domcontentloaded")
    wait_cloudflare(page)
    page.wait_for_timeout(2000)  # Let cards render

    cards = page.query_selector_all(SEL_LISTING_CARD)
    log.info("Found %d listing cards on %s", len(cards), url)

    results = []
    for card in cards:
        try:
            listing_id = extract_id_from_card(card)
            if not listing_id:
                continue

            # Extract link
            link_el = card.query_selector(SEL_CARD_LINK)
            href = link_el.get_attribute("href") if link_el else None
            listing_url = BASE_URL + href if href and not href.startswith("http") else href

            # Extract price
            price_el = card.query_selector(SEL_CARD_PRICE)
            price_text = price_el.inner_text().strip() if price_el else ""

            # Extract title
            title_el = card.query_selector(SEL_CARD_TITLE)
            title = title_el.inner_text().strip() if title_el else ""

            # Extract location
            loc_el = card.query_selector(SEL_CARD_LOCATION)
            location = loc_el.inner_text().strip() if loc_el else ""

            # Extract features
            feature_els = card.query_selector_all(SEL_CARD_FEATURE)
            features = [f.inner_text().strip() for f in feature_els]

            bedrooms = parse_bedrooms(features) or parse_bedrooms_from_title(title)

            # Extract first image URL
            image_url = None
            img_el = card.query_selector("img")
            if img_el:
                image_url = img_el.get_attribute("src") or img_el.get_attribute("data-src")

            results.append({
                "id": listing_id,
                "url": listing_url,
                "title": title,
                "price_eur": parse_price(price_text),
                "price_original": price_text,
                "property_type": property_type,
                "district": district,
                "location_full": location,
                "bedrooms": bedrooms,
                "bathrooms": parse_bathrooms(features),
                "area_sqm": parse_area_from_features(features),
                "image_url": image_url,
            })
        except Exception as e:
            log.warning("Error parsing card: %s", e)
            continue

    return results


def scrape_detail_page(page: Page, url: str, base_data: dict) -> dict:
    """Visit individual listing and extract full characteristics."""
    data = dict(base_data)

    page.goto(url, wait_until="domcontentloaded")
    wait_cloudflare(page)
    page.wait_for_timeout(1500)

    # Title
    title_el = page.query_selector(SEL_DETAIL_TITLE)
    if title_el:
        data["title"] = title_el.inner_text().strip()

    # Price
    price_el = page.query_selector(SEL_DETAIL_PRICE)
    if price_el:
        price_text = price_el.inner_text().strip()
        parsed = parse_price(price_text)
        if parsed:
            data["price_eur"] = parsed
            data["price_original"] = price_text

    # Price per m²
    ppsqm_el = page.query_selector(SEL_DETAIL_PRICE_PER_SQM)
    if ppsqm_el:
        data["price_per_sqm"] = parse_price_per_sqm(ppsqm_el.inner_text().strip())

    # Location
    loc_el = page.query_selector(SEL_DETAIL_LOCATION)
    if loc_el:
        loc_text = loc_el.inner_text().strip()
        data["location_full"] = loc_text
        # Extract sub-area (last part after comma)
        parts = [p.strip() for p in loc_text.split(",")]
        if len(parts) >= 2:
            data["area"] = parts[-1]

    # Ad ID from detail page
    id_el = page.query_selector(SEL_DETAIL_AD_ID)
    if id_el:
        detail_id = id_el.inner_text().strip()
        if detail_id:
            data["id"] = detail_id

    # Posted date
    date_el = page.query_selector(SEL_DETAIL_DATE)
    if date_el:
        data["posted_date"] = date_el.inner_text().strip()

    # Characteristics
    chars = {}
    char_rows = page.query_selector_all(SEL_DETAIL_CHARS_ROW)
    for row in char_rows:
        key_el = row.query_selector(SEL_DETAIL_CHAR_KEY)
        val_el = row.query_selector(SEL_DETAIL_CHAR_VALUE)
        if key_el and val_el:
            key = key_el.inner_text().strip().rstrip(":")
            val = val_el.inner_text().strip()
            chars[key.lower()] = val

    # Map characteristics to fields
    if "bedrooms" in chars:
        try:
            data["bedrooms"] = int(chars["bedrooms"])
        except ValueError:
            pass
    if "bathrooms" in chars:
        try:
            data["bathrooms"] = int(chars["bathrooms"])
        except ValueError:
            pass
    if "property area" in chars:
        data["area_sqm"] = parse_sqm(chars["property area"])
    if "plot area" in chars:
        data["plot_sqm"] = parse_sqm(chars["plot area"])
    if "condition" in chars:
        data["condition"] = chars["condition"]
    if "construction year" in chars or "year of construction" in chars:
        year_str = chars.get("construction year") or chars.get("year of construction", "")
        match = re.search(r"(\d{4})", year_str)
        if match:
            data["construction_year"] = int(match.group(1))
    if "energy efficiency" in chars:
        data["energy_efficiency"] = chars["energy efficiency"]
    if "parking" in chars:
        data["parking"] = chars["parking"]
    if "furnishing" in chars:
        data["furnishing"] = chars["furnishing"]
    if "included" in chars:
        data["included"] = chars["included"]
        data["has_pool"] = has_pool(chars["included"])

    # Extract first image from detail page
    img_el = page.query_selector("img.announcement__images-item") or page.query_selector("img[src*='cdn1.bazaraki.com']")
    if img_el:
        data["image_url"] = img_el.get_attribute("src") or img_el.get_attribute("data-src")

    # Extract coordinates from map element
    try:
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
            data["latitude"] = float(coords["lat"])
            data["longitude"] = float(coords["lng"])
    except Exception:
        pass

    data["scraped_at"] = datetime.now(timezone.utc).isoformat()

    return data


def check_has_results(page: Page) -> bool:
    """Check if the search page has any listing results."""
    cards = page.query_selector_all(SEL_LISTING_CARD)
    return len(cards) > 0


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

    with sync_playwright() as pw:
        browser = create_browser(pw)
        page = create_page(browser)

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
                    url += f"?page={page_num}"

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
                        # Card-only mode: save without visiting detail page
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
                        # Full mode: visit detail page
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

        browser.close()

    total = count_listings()
    log.info("=== Done. %d new listings, %d skipped, %d total in DB ===",
             total_new, total_skipped, total)

    # Notify about below-p25 deals from this run
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
