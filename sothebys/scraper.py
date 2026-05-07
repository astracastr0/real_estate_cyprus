"""Cyprus Sotheby's International Realty scraper.

No Playwright needed — plain HTTP requests (no anti-bot protection).
Uses stdlib urllib + html.parser / regex for extraction.

Usage:
    python3 scraper.py [max_pages] [--fast] [--bedrooms 2,3] [--districts Larnaca,Limassol]
"""

import re
import ssl
import sys
import time
import random
import logging
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.request import urlopen, Request

from config import (
    BASE_URL, QUERIES, MAX_PAGES_PER_QUERY,
    DELAY_BETWEEN_DETAILS, DELAY_BETWEEN_PAGES, USER_AGENT,
)
from db import init_db, listing_exists, upsert_listing, count_listings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def fetch_html(url: str) -> str:
    """Fetch a URL and return the HTML body as string."""
    req = Request(url, headers={"User-Agent": USER_AGENT})
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urlopen(req, timeout=30, context=ctx) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── Parsers ──────────────────────────────────────────────────────────────────

def parse_price(text: str) -> int | None:
    """Convert 'EUR 220,000' or '€212.000+VAT' or '€1.100.000' to integer euros."""
    if not text:
        return None
    cleaned = re.sub(r"[€\s\u00a0]", "", text.strip())
    cleaned = re.sub(r"\+?VAT.*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"EUR\s*", "", cleaned, flags=re.IGNORECASE).strip()

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
        return int(float(cleaned))
    except (ValueError, TypeError):
        return None


def parse_sqm(text: str) -> float | None:
    """Extract number from '95.00 m2' or '210 m²'."""
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


def extract_id_from_url(url: str) -> str | None:
    """Extract property ID from URL like '/property/Title-Here-f37001'."""
    match = re.search(r"-([a-zA-Z]?\d+)(?:\?|#|$)", url)
    if match:
        return match.group(1)
    return None


def parse_bedrooms_from_title(title: str) -> int | None:
    """Extract bedroom count from '3 Bedroom Apartment in Larnaca'."""
    if not title:
        return None
    match = re.search(r"(\d+)\s*(?:-\s*)?Bedroom", title, re.IGNORECASE)
    if match:
        return int(match.group(1))
    if re.search(r"studio", title, re.IGNORECASE):
        return 0
    return None


# ── Search page parsing ──────────────────────────────────────────────────────

class SearchPageParser(HTMLParser):
    """Extract listing cards from search results HTML.

    Each card is an <a href="/property/..."> containing:
    - <h5>Title</h5>
    - <div>Price: €NNN</div> or text with Price:
    - <ul><li>95.00 m2</li><li>3 Bedrooms</li><li>2 Bathrooms</li></ul>
    - <div>Cyprus, Area, City</div>
    """

    def __init__(self):
        super().__init__()
        self.cards: list[dict] = []
        self._in_card = False
        self._card_href = None
        self._in_h5 = False
        self._in_li = False
        self._current_text = ""
        self._card_data: dict = {}
        self._depth = 0

    def handle_starttag(self, tag, attrs):
        attrs_dict = dict(attrs)

        if tag == "a":
            href = attrs_dict.get("href", "")
            if "/property/" in href:
                self._in_card = True
                self._card_href = href
                self._card_data = {"href": href}
                self._depth = 1
            elif self._in_card:
                self._depth += 1

        elif self._in_card:
            if tag == "img" and "image_url" not in self._card_data:
                src = attrs_dict.get("src", "")
                if src and not src.endswith(".svg"):
                    self._card_data["image_url"] = src
            if tag == "h5":
                self._in_h5 = True
                self._current_text = ""
            elif tag == "li":
                self._in_li = True
                self._current_text = ""

    def handle_endtag(self, tag):
        if not self._in_card:
            return

        if tag == "a":
            self._depth -= 1
            if self._depth <= 0:
                if self._card_data.get("href"):
                    self.cards.append(self._card_data)
                self._in_card = False
                self._card_href = None
                self._card_data = {}
        elif tag == "h5" and self._in_h5:
            self._in_h5 = False
            self._card_data["title"] = self._current_text.strip()
        elif tag == "li" and self._in_li:
            self._in_li = False
            text = self._current_text.strip()
            if "m2" in text.lower() or "m²" in text.lower():
                self._card_data.setdefault("area_text", text)
            elif "bedroom" in text.lower():
                self._card_data.setdefault("bedrooms_text", text)
            elif "bathroom" in text.lower():
                self._card_data.setdefault("bathrooms_text", text)

    def handle_data(self, data):
        if not self._in_card:
            return

        if self._in_h5 or self._in_li:
            self._current_text += data

        text = data.strip()
        if not text:
            return

        if "price:" in text.lower() or "€" in text:
            price_match = re.search(r"€[\d.,]+", text)
            if price_match:
                self._card_data.setdefault("price_text", price_match.group())

        if text.startswith("Cyprus,"):
            self._card_data.setdefault("location_text", text)


def parse_search_page(html: str, district: str, property_type: str) -> list[dict]:
    """Parse search results HTML and return list of card dicts."""
    parser = SearchPageParser()
    parser.feed(html)

    # Also extract prices from raw HTML with regex (more reliable)
    # Pattern: Price: <h5>€NNN</h5> or Price: €NNN
    price_blocks = re.findall(
        r'<a\s+href="(/property/[^"]+)"[^>]*>.*?</a>',
        html, re.DOTALL
    )

    # Build a map of href -> price from raw HTML
    href_prices = {}
    for block in price_blocks:
        href = block
        # Find price in the surrounding context
        idx = html.find(f'href="{href}"')
        if idx >= 0:
            # Get the card HTML (next ~2000 chars)
            card_html = html[idx:idx + 2000]
            end_a = card_html.find("</a>")
            if end_a > 0:
                card_html = card_html[:end_a]
            price_m = re.search(r"€([\d.,]+)", card_html)
            if price_m:
                href_prices[href] = "€" + price_m.group(1)

    results = []
    for card in parser.cards:
        href = card.get("href", "")
        listing_id = extract_id_from_url(href)
        if not listing_id:
            continue

        listing_url = BASE_URL + href if not href.startswith("http") else href
        title = card.get("title", "")

        # Price
        price_text = card.get("price_text") or href_prices.get(href, "")
        price_eur = parse_price(price_text)

        # Bedrooms
        bedrooms = None
        bed_text = card.get("bedrooms_text", "")
        bed_match = re.search(r"(\d+)", bed_text)
        if bed_match:
            bedrooms = int(bed_match.group(1))
        if bedrooms is None:
            bedrooms = parse_bedrooms_from_title(title)

        # Bathrooms
        bathrooms = None
        bath_text = card.get("bathrooms_text", "")
        bath_match = re.search(r"(\d+)", bath_text)
        if bath_match:
            bathrooms = int(bath_match.group(1))

        # Area
        area_sqm = parse_sqm(card.get("area_text", ""))

        # Location / sub-area
        area = None
        loc = card.get("location_text", "")
        if loc:
            parts = [p.strip() for p in loc.split(",")]
            # "Cyprus, Kiti, Larnaca" -> area = "Kiti"
            if len(parts) >= 3:
                area = parts[1]
            elif len(parts) == 2:
                area = parts[1]

        results.append({
            "id": listing_id,
            "url": listing_url,
            "title": title,
            "price_eur": price_eur,
            "price_original": price_text,
            "property_type": property_type,
            "district": district,
            "area": area,
            "location_full": loc.replace("Cyprus, ", "") if loc else None,
            "bedrooms": bedrooms,
            "bathrooms": bathrooms,
            "area_sqm": area_sqm,
            "image_url": card.get("image_url"),
        })

    return results


def get_max_page(html: str) -> int:
    """Extract the maximum page number from pagination links."""
    page_nums = re.findall(r'[?&]page=(\d+)', html)
    if page_nums:
        return max(int(p) for p in page_nums)
    return 1


# ── Detail page parsing ──────────────────────────────────────────────────────

def parse_detail_page(html: str, base_data: dict) -> dict:
    """Parse a detail page HTML and enhance base_data with extracted fields.

    Detail pages use a table with <td class="pt-name">Key</td><td>Value</td> rows.
    """
    data = dict(base_data)

    # Extract title from <h1>
    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.DOTALL)
    if h1_match:
        title = re.sub(r"<[^>]+>", "", h1_match.group(1)).strip()
        if title:
            data["title"] = title

    # Extract key-value pairs from <td class="pt-name">Key</td><td>Value</td>
    pairs = {}
    for m in re.finditer(
        r'<td\s+class="pt-name">(.*?)</td>\s*<td[^>]*>(.*?)</td>',
        html, re.DOTALL,
    ):
        key = re.sub(r"<[^>]+>", "", m.group(1)).strip().lower()
        val = re.sub(r"<[^>]+>", "", m.group(2)).strip()
        if key and val:
            pairs[key] = val

    # Price
    price_text = pairs.get("price", "")
    if price_text:
        price = parse_price(price_text)
        if price:
            data["price_eur"] = price
            data["price_original"] = price_text

    # Bedrooms
    if "bedrooms" in pairs:
        try:
            data["bedrooms"] = int(pairs["bedrooms"])
        except ValueError:
            pass

    # Bathrooms
    if "bathrooms" in pairs:
        try:
            data["bathrooms"] = int(pairs["bathrooms"])
        except ValueError:
            pass

    # Area - prefer "internal area", then "covered area", then "total area"
    for area_key in ("internal area", "covered area", "total area"):
        if area_key in pairs:
            sqm = parse_sqm(pairs[area_key])
            if sqm and sqm > 0:
                data["area_sqm"] = sqm
                break

    # Plot area
    for plot_key in ("plot area", "land area"):
        if plot_key in pairs:
            plot = parse_sqm(pairs[plot_key])
            if plot and plot > 0:
                data["plot_sqm"] = plot
                break

    # Property type
    if "property type" in pairs:
        data["property_type"] = pairs["property type"].lower()

    # Status / condition
    if "status" in pairs:
        data["condition"] = pairs["status"]

    # Address / location
    if "address" in pairs:
        addr = pairs["address"]
        data["location_full"] = addr.replace("Cyprus, ", "")
        parts = [p.strip() for p in addr.split(",")]
        if len(parts) >= 3:
            data["area"] = parts[1]  # e.g. "Kiti" from "Cyprus, Kiti, Larnaca"

    # Features / amenities from page text
    body_text = re.sub(r"<[^>]+>", " ", html).lower()
    data["has_pool"] = bool(re.search(r"\bpool\b", body_text))
    if re.search(r"\bparking\b", body_text):
        data["parking"] = "Yes"
    if re.search(r"\bfurnish", body_text):
        data["furnishing"] = "Yes"

    # Collect amenities
    amenities = []
    for keyword in ("pool", "parking", "air conditioning", "central heating",
                     "solar panels", "storage", "garden", "sea view"):
        if keyword in body_text:
            amenities.append(keyword.title())
    if amenities:
        data["included"] = ", ".join(amenities)

    # Extract first image from detail page
    img_match = re.search(r'<img[^>]+src="([^"]+/data/content/images/[^"]+)"', html)
    if img_match:
        data["image_url"] = img_match.group(1)
    elif not data.get("image_url"):
        # Fallback: any non-SVG image
        img_fb = re.search(r'<img[^>]+src="(https?://[^"]+\.(?:jpg|jpeg|png|webp))"', html, re.IGNORECASE)
        if img_fb:
            data["image_url"] = img_fb.group(1)

    # Extract coordinates from LatLng() in page source
    coord_match = re.search(r'LatLng\(([\d.]+),\s*([\d.]+)\)', html)
    if coord_match:
        data["latitude"] = float(coord_match.group(1))
        data["longitude"] = float(coord_match.group(2))

    # Compute price_per_sqm
    if data.get("price_eur") and data.get("area_sqm") and data["area_sqm"] > 0:
        data["price_per_sqm"] = round(data["price_eur"] / data["area_sqm"], 2)

    data["scraped_at"] = datetime.now(timezone.utc).isoformat()

    return data


# ── Main scraper loop ────────────────────────────────────────────────────────

def run(max_pages: int = MAX_PAGES_PER_QUERY, fast: bool = False,
        bedrooms_filter: set[int] | None = None,
        districts_filter: set[str] | None = None):
    """Main scraper loop."""
    init_db()
    run_started = datetime.now(timezone.utc).isoformat()

    queries = QUERIES
    if districts_filter:
        queries = [q for q in QUERIES if q["district"] in districts_filter]

    total_new = 0
    total_skipped = 0

    for query in queries:
        district = query["district"]
        prop_type = query["property_type"]
        city_slug = query["city_slug"]
        search_city = query["search_city"]
        search_type = query["search_type"]

        log.info("=== Scraping %s in %s ===", prop_type, district)

        for page_num in range(1, max_pages + 1):
            url = (
                f"{BASE_URL}/main/{city_slug}"
                f"?page={page_num}&buy=on"
                f"&search_city={search_city}"
                f"&search_type={search_type}"
            )
            if bedrooms_filter and len(bedrooms_filter) == 1:
                url += f"&search_bedrooms={list(bedrooms_filter)[0]}"

            log.info("Search page %d: %s", page_num, url)

            try:
                html = fetch_html(url)
            except Exception as e:
                log.error("Error fetching %s: %s", url, e)
                break

            cards = parse_search_page(html, district, prop_type)

            if not cards:
                log.info("No cards found, stopping pagination for this query.")
                break

            log.info("Found %d cards on page %d", len(cards), page_num)

            for card_data in cards:
                listing_id = card_data["id"]

                if listing_exists(listing_id):
                    total_skipped += 1
                    continue

                # Filter by bedrooms (client-side for multi-value filters)
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
                            detail_html = fetch_html(detail_url)
                            full_data = parse_detail_page(detail_html, card_data)
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

                time.sleep(random.uniform(*DELAY_BETWEEN_DETAILS))

            # Check if we've reached the last page
            actual_max = get_max_page(html)
            if page_num >= actual_max:
                log.info("Reached last page (%d)", actual_max)
                break

            time.sleep(random.uniform(*DELAY_BETWEEN_PAGES))

    total = count_listings()
    log.info("=== Done. %d new listings, %d skipped, %d total in DB ===",
             total_new, total_skipped, total)


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
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
            print(f"Usage: python {sys.argv[0]} [max_pages] [--fast] [--bedrooms 2,3] [--districts Larnaca,Limassol]")
            sys.exit(1)

    if bed_filter:
        log.info("Filtering for bedrooms: %s", bed_filter)
    if dist_filter:
        log.info("Filtering for districts: %s", dist_filter)
    run(max_pages=pages, fast=fast_mode, bedrooms_filter=bed_filter, districts_filter=dist_filter)
