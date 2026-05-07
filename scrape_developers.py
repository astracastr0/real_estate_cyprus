#!/usr/bin/env python3
"""Scrape developers and their projects from dom.com.cy for Larnaca.

Crawls the developer listing pages, extracts project names and listing IDs,
then visits each listing to get full details. Stores developer/project info
in user_data.db and listings in dom_cy.db.

Usage:
    python3 scrape_developers.py [--delay 2.0] [--fast]

Requires Chrome running with CDP on port 9222 (same as dom_cy scraper).
"""

import os
import re
import sys
import time
import random
import signal
import logging
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, Page

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger(__name__)

BASE = Path(__file__).parent
USER_DATA_DB = BASE / "user_data.db"
DOM_CY_DB = BASE / "dom_cy" / "dom_cy.db"

# Import dom_cy DB helpers
sys.path.insert(0, str(BASE / "dom_cy"))
from db import init_db as init_domcy_db, upsert_listing, listing_exists
from config import BASE_URL

CDP_PORT = 9222
CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
CHROME_PROFILE = "/tmp/dom_cy_chrome_profile"
CLOUDFLARE_TIMEOUT = 30

DEVELOPERS_PAGE = "https://dom.com.cy/en/live/developers/zastroyshchiki-larnaki/"
CITY = "Larnaca"


# ── Chrome management ─────────────────────────────────────────────────────────

def _kill_chrome():
    try:
        result = subprocess.run(["lsof", "-ti", f":{CDP_PORT}"], capture_output=True, text=True)
        if result.stdout.strip():
            for pid in result.stdout.strip().split("\n"):
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except (ProcessLookupError, ValueError):
                    pass
            time.sleep(2)
    except Exception:
        pass


def _launch_chrome():
    log.info("Launching Chrome on port %d...", CDP_PORT)
    subprocess.Popen(
        [CHROME_PATH, f"--remote-debugging-port={CDP_PORT}",
         f"--user-data-dir={CHROME_PROFILE}",
         "--no-first-run", "--no-default-browser-check", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(3)


def ensure_chrome():
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(("127.0.0.1", CDP_PORT))
    sock.close()
    if result == 0:
        log.info("Chrome already running on port %d", CDP_PORT)
        return
    _launch_chrome()


def connect_browser(pw):
    try:
        return pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}", timeout=30000)
    except Exception as e:
        log.warning("CDP connect failed: %s. Restarting Chrome...", e)
        _kill_chrome()
        _launch_chrome()
        return pw.chromium.connect_over_cdp(f"http://127.0.0.1:{CDP_PORT}", timeout=30000)


def wait_for_content(page: Page, timeout: int = CLOUDFLARE_TIMEOUT):
    start = time.time()
    while time.time() - start < timeout:
        body_len = len(page.inner_text("body"))
        if body_len > 500:
            return True
        time.sleep(1)
    log.warning("Content did not load after %ds", timeout)
    return False


# ── DB setup ──────────────────────────────────────────────────────────────────

def init_user_data_tables():
    conn = sqlite3.connect(USER_DATA_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS developers (
            name TEXT NOT NULL,
            city TEXT NOT NULL,
            url TEXT,
            description TEXT,
            contact TEXT,
            updated_at TEXT,
            PRIMARY KEY (name, city)
        );
        CREATE TABLE IF NOT EXISTS developer_projects (
            developer_name TEXT NOT NULL,
            project_name TEXT NOT NULL,
            area TEXT,
            city TEXT,
            sample_beds INTEGER,
            sample_price_eur INTEGER,
            sample_area_sqm REAL,
            updated_at TEXT,
            PRIMARY KEY (developer_name, project_name)
        );
    """)
    conn.close()


def _ensure_developer_columns():
    """Add developer_name and project_name columns to dom_cy listings if missing."""
    conn = sqlite3.connect(DOM_CY_DB)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()}
    if "developer_name" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN developer_name TEXT")
        conn.commit()
    if "project_name" not in cols:
        conn.execute("ALTER TABLE listings ADD COLUMN project_name TEXT")
        conn.commit()
    conn.close()


# ── Scraping functions ────────────────────────────────────────────────────────

def scrape_developers_list(page: Page) -> list[dict]:
    """Get all developer pages from the Larnaca developers listing (all pages)."""
    developers = []
    MAX_DEV_PAGES = 10  # dom.cy has ~7 pages of Larnaca developers

    for page_num in range(1, MAX_DEV_PAGES + 1):
        url = DEVELOPERS_PAGE if page_num == 1 else f"{DEVELOPERS_PAGE}?PAGEN_1={page_num}"
        log.info("Developer list page %d: %s", page_num, url)
        page.goto(url, wait_until="domcontentloaded")
        wait_for_content(page)
        page.wait_for_timeout(2000)

        # Extract developer links from the page
        links = page.query_selector_all('a[href*="/live/developers/zastroyshchiki-larnaki/"]')
        found_new = False
        for link in links:
            href = link.get_attribute("href") or ""
            # Only get links to specific developer pages (must have slug after base path)
            slug_match = re.search(r'/zastroyshchiki-larnaki/([^/?]+)', href)
            if slug_match and slug_match.group(1):
                # Extract name from <img alt="..."> first, then from slug
                name = ""
                img_el = link.query_selector("img")
                if img_el:
                    name = (img_el.get_attribute("alt") or "").strip()
                # Filter out city names that might be in alt
                city_names = {"larnaca", "limassol", "paphos", "nicosia", "famagusta", "cyprus"}
                if name.lower() in city_names:
                    name = ""
                if not name:
                    # Derive from URL slug as most reliable fallback
                    slug = slug_match.group(1)
                    name = slug.replace("-", " ").replace("amp", "&").title()
                    # Clean up common patterns
                    name = name.replace(" Ltd", " Ltd").replace(" And ", " & ")
                full_url = href if href.startswith("http") else f"https://dom.com.cy{href}"
                if not any(d["url"] == full_url for d in developers):
                    developers.append({"name": name, "url": full_url})
                    found_new = True

        if not found_new:
            log.info("No new developers on page %d, stopping.", page_num)
            break
        time.sleep(random.uniform(1, 2))

    log.info("Found %d developers", len(developers))
    return developers


def _scrape_developer_single_page(page: Page, url: str) -> dict:
    """Extract data from one page of a developer's profile."""
    page.goto(url, wait_until="domcontentloaded")
    wait_for_content(page)
    page.wait_for_timeout(2000)

    body_text = page.inner_text("body")

    # Extract description
    description = ""
    desc_el = page.query_selector(".developer-description, .text-content, .about-text, .description")
    if desc_el:
        description = desc_el.inner_text().strip()[:500]
    if not description:
        m = re.search(r'"([^"]{20,300})"', body_text)
        if m:
            description = m.group(1)

    # Extract contact
    contact = ""
    phone_m = re.search(r'(\+357[\s\d]{8,15})', body_text)
    if phone_m:
        contact = phone_m.group(1).strip()

    # Extract projects (complex names) — links use /en/live/complexes/complex-*/
    # Scroll down to load lazy content
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)

    projects = []
    complex_els = page.query_selector_all('a[href*="/live/complexes/"]')
    log.debug("  Found %d complex links on page", len(complex_els))
    for el in complex_els:
        href = el.get_attribute("href") or ""
        # Get project name from <h3> inside the link, or from <img alt>, or from slug
        pname = ""
        h3_el = el.query_selector("h3")
        if h3_el:
            pname = h3_el.inner_text().strip()
        if not pname:
            img_el = el.query_selector("img")
            if img_el:
                pname = (img_el.get_attribute("alt") or "").strip()
        if not pname:
            slug_m = re.search(r'/complexes/[^/]+/([^/?]+)', href)
            if slug_m:
                pname = slug_m.group(1).replace("-", " ").title()
        # Extract area from text (e.g. "Area: <strong>Livadia</strong>")
        area = None
        area_el = el.query_selector("strong")
        if area_el:
            area_text = area_el.inner_text().strip()
            # Skip city names, keep area names
            if area_text.lower() not in ("larnaca", "limassol", "paphos", "nicosia", "famagusta"):
                area = area_text
        if pname and len(pname) < 100:
            if not any(p["name"] == pname for p in projects):
                projects.append({"name": pname, "area": area})

    # Fallback: extract via JS if selector found nothing
    if not projects:
        try:
            js_projects = page.evaluate("""() => {
                const results = [];
                const links = document.querySelectorAll('a[href*="/complexes/"]');
                for (const a of links) {
                    const href = a.getAttribute('href') || '';
                    const h3 = a.querySelector('h3');
                    const img = a.querySelector('img');
                    let name = h3 ? h3.textContent.trim() : (img ? img.alt.trim() : '');
                    if (!name) {
                        const m = href.match(/complexes\\/[^/]+\\/([^/?]+)/);
                        if (m) name = m[1].replace(/-/g, ' ').replace(/_/g, ' ');
                    }
                    if (name && name.length < 100) results.push({name, href});
                }
                return results;
            }""")
            seen = set()
            for p in js_projects:
                if p["name"] not in seen:
                    projects.append({"name": p["name"], "area": None})
                    seen.add(p["name"])
            if js_projects:
                log.info("    JS fallback found %d projects", len(projects))
        except Exception as e:
            log.debug("    JS project extraction failed: %s", e)

    # Extract listing IDs with bedroom count from card text
    listing_ids = []
    listing_beds = {}  # lid -> bedrooms count
    try:
        cards = page.evaluate("""() => {
            const results = [];
            const links = document.querySelectorAll('a[href*="/en/catalog/sale/"]');
            for (const a of links) {
                const idM = a.href.match(/\\/catalog\\/sale\\/(\\d+)/);
                if (!idM) continue;
                const text = a.textContent || '';
                const bedM = text.match(/(\\d+)\\s*bed/i) || text.match(/Bedrooms?:?\\s*(\\d+)/i);
                results.push({id: idM[1], beds: bedM ? parseInt(bedM[1]) : null});
            }
            return results;
        }""")
        for c in cards:
            lid = c["id"]
            if lid not in listing_ids:
                listing_ids.append(lid)
                if c["beds"] is not None:
                    listing_beds[lid] = c["beds"]
    except Exception:
        # Fallback to simple extraction
        listing_links = page.query_selector_all('a[href*="/en/catalog/sale/"]')
        for link in listing_links:
            href = link.get_attribute("href") or ""
            id_match = re.search(r"/catalog/sale/(\d+)/?", href)
            if id_match:
                lid = id_match.group(1)
                if lid not in listing_ids:
                    listing_ids.append(lid)

    # Map listings to projects — try to find listing IDs inside project links
    # or use page JS to get complex→listing mapping
    listing_project_map = {}
    # Try extracting from complexes sections on the page
    try:
        mapping = page.evaluate("""() => {
            const result = {};
            // Look for sections that group listings under a project heading
            const allLinks = document.querySelectorAll('a[href*="/en/catalog/sale/"]');
            for (const link of allLinks) {
                const idMatch = link.href.match(/\\/catalog\\/sale\\/(\\d+)/);
                if (!idMatch) continue;
                // Walk up DOM to find nearest heading or complex name
                let el = link.parentElement;
                for (let i = 0; i < 10 && el; i++) {
                    const h = el.querySelector('h2, h3, h4');
                    if (h && h.textContent.trim().length > 2 && h.textContent.trim().length < 80) {
                        result[idMatch[1]] = h.textContent.trim();
                        break;
                    }
                    el = el.parentElement;
                }
            }
            return result;
        }""")
        if mapping:
            listing_project_map = mapping
    except Exception:
        pass

    # Check if there's a next page
    has_next = False
    nav_links = page.query_selector_all('a[href*="PAGEN_1="]')
    current_page_num = 1
    page_match = re.search(r'PAGEN_1=(\d+)', url)
    if page_match:
        current_page_num = int(page_match.group(1))
    for nav in nav_links:
        nav_href = nav.get_attribute("href") or ""
        nav_match = re.search(r'PAGEN_1=(\d+)', nav_href)
        if nav_match and int(nav_match.group(1)) > current_page_num:
            has_next = True
            break

    return {
        "description": description,
        "contact": contact,
        "projects": projects,
        "listing_ids": listing_ids,
        "listing_beds": listing_beds,
        "listing_project_map": listing_project_map,
        "has_next": has_next,
    }


def scrape_developer_page(page: Page, dev: dict, delay: float, fast: bool) -> dict:
    """Visit a developer page (all pages), extract info, projects, and listing IDs."""
    base_url = dev["url"]
    log.info("  Scraping developer: %s", dev["name"])

    all_listing_ids = []
    all_listing_beds = {}
    all_listing_project_map = {}
    all_projects = []
    description = ""
    contact = ""
    page_num = 1

    while True:
        url = base_url if page_num == 1 else f"{base_url}?PAGEN_1={page_num}"
        result = _scrape_developer_single_page(page, url)

        if page_num == 1:
            description = result["description"]
            contact = result["contact"]

        # Merge projects
        for p in result["projects"]:
            if not any(ep["name"] == p["name"] for ep in all_projects):
                all_projects.append(p)

        # Merge listing IDs and beds (preserve order)
        for lid in result["listing_ids"]:
            if lid not in all_listing_ids:
                all_listing_ids.append(lid)
        all_listing_beds.update(result.get("listing_beds", {}))

        # Merge project map
        all_listing_project_map.update(result["listing_project_map"])

        if not result["has_next"] or not result["listing_ids"]:
            break

        page_num += 1
        time.sleep(random.uniform(delay * 0.5, delay))

    if page_num > 1:
        log.info("    Scraped %d pages for %s", page_num, dev["name"])

    return {
        "name": dev["name"],
        "url": base_url,
        "description": description,
        "contact": contact,
        "projects": all_projects,
        "listing_ids": all_listing_ids,
        "listing_beds": all_listing_beds,
        "listing_project_map": all_listing_project_map,
    }


def scrape_listing_detail(page: Page, listing_id: str, developer_name: str,
                          project_name: str | None, delay: float) -> dict | None:
    """Scrape a single listing detail page from dom.cy."""
    url = f"{BASE_URL}/en/catalog/sale/{listing_id}/"
    page.goto(url, wait_until="domcontentloaded")
    wait_for_content(page)
    page.wait_for_timeout(1000)

    # Use the same JS extraction as dom_cy scraper
    js_data = None
    try:
        js_data = page.evaluate("""
            () => {
                if (typeof arCatalogElementResult === 'undefined' || !arCatalogElementResult.row)
                    return null;
                const r = arCatalogElementResult.row;
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
        log.debug("Could not extract JS data for %s: %s", listing_id, e)

    if not js_data:
        log.info("    %s: no JS data (might be expired or blocked)", listing_id)
        return None

    data = {
        "id": listing_id,
        "url": url,
        "source": "dom_cy",
        "developer_name": developer_name,
        "project_name": project_name,
    }

    # Parse price
    if js_data.get("price"):
        try:
            data["price_eur"] = int(float(str(js_data["price"])))
            data["price_original"] = f"{data['price_eur']} EUR"
        except (ValueError, TypeError):
            pass

    # Parse area
    if js_data.get("total_area"):
        try:
            area_val = float(str(js_data["total_area"]))
            if area_val > 0:
                data["area_sqm"] = area_val
        except (ValueError, TypeError):
            pass

    if js_data.get("indoor_area"):
        try:
            indoor = float(str(js_data["indoor_area"]))
            if indoor > 0:
                data["indoor_area_sqm"] = indoor
        except (ValueError, TypeError):
            pass

    if js_data.get("land_area"):
        try:
            land = float(str(js_data["land_area"]))
            if land > 0:
                data["plot_sqm"] = land
        except (ValueError, TypeError):
            pass

    # Parse bedrooms/bathrooms
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

    # Location
    if js_data.get("city"):
        city = str(js_data["city"])
        data["district"] = city if city in ("Larnaca", "Limassol", "Paphos", "Nicosia") else "Larnaca"
    else:
        data["district"] = "Larnaca"

    if js_data.get("district"):
        data["area"] = str(js_data["district"])
        data["location_full"] = f"{data['district']}, {data['area']}"

    # Condition
    if js_data.get("new_building") and str(js_data["new_building"]) == "1":
        data["condition"] = "New"
    else:
        data["condition"] = "Resale"

    # Year
    if js_data.get("year"):
        year_match = re.search(r"(\d{4})", str(js_data["year"]))
        if year_match:
            data["construction_year"] = int(year_match.group(1))

    # Energy
    if js_data.get("energy"):
        data["energy_efficiency"] = str(js_data["energy"])

    # Availability (Pool, Parking, etc.)
    avail = js_data.get("availability") or []
    if avail:
        data["included"] = ", ".join(avail)
        data["has_pool"] = any("pool" in a.lower() for a in avail)
        data["parking"] = "Yes" if any("parking" in a.lower() for a in avail) else None
        data["furnishing"] = "Yes" if any("furniture" in a.lower() for a in avail) else None

    # Price per sqm — prefer indoor area over total area
    price_area = data.get("indoor_area_sqm") or data.get("area_sqm")
    if data.get("price_eur") and price_area and price_area > 0:
        data["price_per_sqm"] = round(data["price_eur"] / price_area, 2)
    elif js_data.get("price_m"):
        try:
            data["price_per_sqm"] = float(str(js_data["price_m"]))
        except (ValueError, TypeError):
            pass

    # Title from <h1>
    h1 = page.query_selector("h1")
    if h1:
        data["title"] = h1.inner_text().strip()
    else:
        beds = data.get("bedrooms", "?")
        data["title"] = f"New {beds}-bedroom in {data.get('area', 'Larnaca')}"

    # Property type from title
    title_lower = (data.get("title") or "").lower()
    if "penthouse" in title_lower:
        data["property_type"] = "apartment"
    elif "villa" in title_lower or "house" in title_lower:
        data["property_type"] = "house"
    else:
        data["property_type"] = "apartment"

    # Image
    img_el = (page.query_selector("img[src*='/upload/']")
              or page.query_selector(".detail-gallery img")
              or page.query_selector("img"))
    if img_el:
        img_src = img_el.get_attribute("src") or img_el.get_attribute("data-src")
        if img_src and not img_src.endswith(".svg"):
            data["image_url"] = img_src if img_src.startswith("http") else BASE_URL + img_src

    # Extract coordinates from Google Maps iframe/link
    try:
        coords = page.evaluate("""() => {
            // Check iframe src
            const iframe = document.querySelector('iframe[src*="google.com/maps"]');
            if (iframe) {
                const src = iframe.src || iframe.getAttribute('src') || '';
                // Try &q=lat,lng or !2d...!3d... or @lat,lng
                let m = src.match(/[?&]q=([-\\d.]+),([-\\d.]+)/);
                if (m) return {lat: parseFloat(m[1]), lng: parseFloat(m[2])};
                m = src.match(/!3d([-\\d.]+)!.*!2d([-\\d.]+)/);
                if (m) return {lat: parseFloat(m[1]), lng: parseFloat(m[2])};
                m = src.match(/!2d([-\\d.]+)!3d([-\\d.]+)/);
                if (m) return {lat: parseFloat(m[2]), lng: parseFloat(m[1])};
                m = src.match(/@([-\\d.]+),([-\\d.]+)/);
                if (m) return {lat: parseFloat(m[1]), lng: parseFloat(m[2])};
            }
            // Check any link to google maps
            const links = document.querySelectorAll('a[href*="google.com/maps"], a[href*="maps.google"]');
            for (const a of links) {
                const href = a.href || '';
                let m = href.match(/[?&]q=([-\\d.]+),([-\\d.]+)/);
                if (m) return {lat: parseFloat(m[1]), lng: parseFloat(m[2])};
                m = href.match(/@([-\\d.]+),([-\\d.]+)/);
                if (m) return {lat: parseFloat(m[1]), lng: parseFloat(m[2])};
            }
            // Check page HTML for coordinate patterns
            const html = document.body.innerHTML;
            const latM = html.match(/["']lat["']\\s*:\\s*(3[34]\\.\\d{3,})/);
            const lngM = html.match(/["']lng["']\\s*:\\s*(3[23]\\.\\d{3,})/);
            if (latM && lngM) return {lat: parseFloat(latM[1]), lng: parseFloat(lngM[1])};
            return null;
        }""")
        if coords and 34.0 < coords["lat"] < 36.0 and 32.0 < coords["lng"] < 35.0:
            data["latitude"] = coords["lat"]
            data["longitude"] = coords["lng"]
    except Exception:
        pass

    data["scraped_at"] = datetime.now(timezone.utc).isoformat()

    return data


# ── Backfill coordinates ──────────────────────────────────────────────────────

def _extract_coords(page: Page) -> tuple[float, float] | None:
    """Extract lat/lng from the current page via Google Maps iframe/link."""
    try:
        coords = page.evaluate("""() => {
            const iframe = document.querySelector('iframe[src*="google.com/maps"]');
            if (iframe) {
                const src = iframe.src || iframe.getAttribute('src') || '';
                let m = src.match(/[?&]q=([-\\d.]+),([-\\d.]+)/);
                if (m) return {lat: parseFloat(m[1]), lng: parseFloat(m[2])};
                m = src.match(/!3d([-\\d.]+)!.*!2d([-\\d.]+)/);
                if (m) return {lat: parseFloat(m[1]), lng: parseFloat(m[2])};
                m = src.match(/!2d([-\\d.]+)!3d([-\\d.]+)/);
                if (m) return {lat: parseFloat(m[2]), lng: parseFloat(m[1])};
                m = src.match(/@([-\\d.]+),([-\\d.]+)/);
                if (m) return {lat: parseFloat(m[1]), lng: parseFloat(m[2])};
            }
            const links = document.querySelectorAll('a[href*="google.com/maps"], a[href*="maps.google"]');
            for (const a of links) {
                const href = a.href || '';
                let m = href.match(/[?&]q=([-\\d.]+),([-\\d.]+)/);
                if (m) return {lat: parseFloat(m[1]), lng: parseFloat(m[2])};
                m = href.match(/@([-\\d.]+),([-\\d.]+)/);
                if (m) return {lat: parseFloat(m[1]), lng: parseFloat(m[2])};
            }
            const html = document.body.innerHTML;
            const latM = html.match(/["']lat["']\\s*:\\s*(3[34]\\.\\d{3,})/);
            const lngM = html.match(/["']lng["']\\s*:\\s*(3[23]\\.\\d{3,})/);
            if (latM && lngM) return {lat: parseFloat(latM[1]), lng: parseFloat(lngM[1])};
            return null;
        }""")
        if coords and 34.0 < coords["lat"] < 36.0 and 32.0 < coords["lng"] < 35.0:
            return (coords["lat"], coords["lng"])
    except Exception:
        pass
    return None


def _backfill_coords(delay: float):
    """Visit existing dom_cy listings and extract coordinates."""
    conn = sqlite3.connect(DOM_CY_DB)
    rows = conn.execute(
        "SELECT id FROM listings WHERE latitude IS NULL AND (is_expired IS NULL OR is_expired = 0)"
    ).fetchall()
    conn.close()
    listing_ids = [r[0] for r in rows]
    log.info("Backfill coords: %d listings without coordinates", len(listing_ids))
    if not listing_ids:
        return

    ensure_chrome()
    updated = 0
    with sync_playwright() as pw:
        browser = connect_browser(pw)
        ctx = browser.contexts[0]
        page = ctx.new_page()

        for i, lid in enumerate(listing_ids):
            url = f"{BASE_URL}/en/catalog/sale/{lid}/"
            try:
                page.goto(url, wait_until="domcontentloaded")
                wait_for_content(page)
                page.wait_for_timeout(1500)

                coords = _extract_coords(page)
                if coords:
                    conn = sqlite3.connect(DOM_CY_DB)
                    conn.execute(
                        "UPDATE listings SET latitude=?, longitude=? WHERE id=?",
                        (coords[0], coords[1], lid)
                    )
                    conn.commit()
                    conn.close()
                    updated += 1
                    log.info("  [%d/%d] %s → %.6f, %.6f", i+1, len(listing_ids), lid, coords[0], coords[1])
                else:
                    log.debug("  [%d/%d] %s → no coords", i+1, len(listing_ids), lid)
            except Exception as e:
                log.warning("  [%d/%d] %s error: %s", i+1, len(listing_ids), lid, e)
                if "closed" in str(e).lower():
                    try: page.close()
                    except Exception: pass
                    try: browser.close()
                    except Exception: pass
                    _kill_chrome()
                    _launch_chrome()
                    browser = connect_browser(pw)
                    ctx = browser.contexts[0]
                    page = ctx.new_page()

            time.sleep(random.uniform(delay * 0.5, delay))

        page.close()
        browser.close()

    log.info("=== Backfill done. Updated %d/%d listings with coordinates ===", updated, len(listing_ids))


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Scrape dom.cy developers for Larnaca")
    parser.add_argument("--delay", type=float, default=2.0, help="Avg delay between requests")
    parser.add_argument("--fast", action="store_true", help="Skip listing detail pages")
    parser.add_argument("--skip-developers", action="store_true", help="Reuse saved developers from DB, only scrape listings")
    parser.add_argument("--backfill-coords", action="store_true", help="Backfill coordinates for existing dom_cy listings")
    parser.add_argument("--bedrooms", type=int, default=0, help="Filter listings by bedroom count (0=all)")
    parser.add_argument("--max-listings", type=int, default=0, help="Max listings to scrape per developer (0=all)")
    args = parser.parse_args()

    init_user_data_tables()
    init_domcy_db()
    _ensure_developer_columns()

    if args.backfill_coords:
        _backfill_coords(args.delay)
        return

    ensure_chrome()

    with sync_playwright() as pw:
        browser = connect_browser(pw)
        ctx = browser.contexts[0]
        page = ctx.new_page()

        total_listings_scraped = 0
        total_projects = 0
        conn_user = sqlite3.connect(USER_DATA_DB)

        if args.skip_developers:
            # Load developers from DB, go straight to scraping their listing pages
            rows = conn_user.execute(
                "SELECT name, url FROM developers WHERE city = ?", (CITY,)
            ).fetchall()
            developers_info = []
            for row in rows:
                name, url = row
                developers_info.append({
                    "name": name, "url": url,
                    "projects": [], "listing_ids": [], "listing_beds": {},
                    "listing_project_map": {},
                })
            log.info("Loaded %d developers from DB (skip-developers mode)", len(developers_info))

            # For each developer, visit their page to get listing IDs only
            for dev_info in developers_info:
                if not dev_info["url"]:
                    continue
                log.info("  Getting listings for: %s", dev_info["name"])
                try:
                    scraped = scrape_developer_page(page, dev_info, args.delay, False)
                    dev_info["listing_ids"] = scraped["listing_ids"]
                    dev_info["listing_beds"] = scraped.get("listing_beds", {})
                    dev_info["listing_project_map"] = scraped["listing_project_map"]
                    log.info("    %d listings found", len(dev_info["listing_ids"]))
                except Exception as e:
                    log.warning("  Error getting listings for %s: %s", dev_info["name"], e)
                    if "closed" in str(e).lower():
                        try: page.close()
                        except Exception: pass
                        try: browser.close()
                        except Exception: pass
                        _kill_chrome()
                        _launch_chrome()
                        browser = connect_browser(pw)
                        ctx = browser.contexts[0]
                        page = ctx.new_page()
                    time.sleep(args.delay)
        else:
            # Step 1: Get all developers from website
            developers = scrape_developers_list(page)
            time.sleep(random.uniform(1, 2))

            developers_info = []
            for dev_idx, dev in enumerate(developers):
                # Step 2: Scrape each developer page
                try:
                    dev_info = scrape_developer_page(page, dev, args.delay, args.fast)
                except Exception as e:
                    log.warning("  Error scraping developer %s: %s", dev["name"], e)
                    if "closed" in str(e).lower():
                        try: page.close()
                        except Exception: pass
                        try: browser.close()
                        except Exception: pass
                        _kill_chrome()
                        _launch_chrome()
                        browser = connect_browser(pw)
                        ctx = browser.contexts[0]
                        page = ctx.new_page()
                        log.info("  Reconnected browser after crash")
                    time.sleep(args.delay)
                    continue

                developers_info.append(dev_info)

                # Save developer to DB
                conn_user.execute(
                    "INSERT OR REPLACE INTO developers (name, city, url, description, contact, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (dev_info["name"], CITY, dev_info["url"], dev_info.get("description", ""),
                     dev_info.get("contact", ""), datetime.now(timezone.utc).isoformat())
                )
                conn_user.commit()

                # Save projects
                for proj in dev_info["projects"]:
                    conn_user.execute(
                        "INSERT OR REPLACE INTO developer_projects "
                        "(developer_name, project_name, area, city, updated_at) VALUES (?, ?, ?, ?, ?)",
                        (dev_info["name"], proj["name"], proj.get("area"), CITY,
                         datetime.now(timezone.utc).isoformat())
                    )
                    total_projects += 1
                conn_user.commit()

                log.info("  Developer: %s | %d projects | %d listings found",
                         dev_info["name"], len(dev_info["projects"]), len(dev_info["listing_ids"]))

                if args.fast:
                    time.sleep(random.uniform(args.delay * 0.5, args.delay))

        # Step 3: Scrape listing details for all developers
        if not args.fast:
            for dev_info in developers_info:
                listings_to_scrape = dev_info["listing_ids"]
                if not listings_to_scrape:
                    continue

                # Pre-filter by bedrooms from card info
                if args.bedrooms > 0:
                    beds_map = dev_info.get("listing_beds", {})
                    filtered = [lid for lid in listings_to_scrape
                                if beds_map.get(lid) == args.bedrooms or lid not in beds_map]
                    skipped = len(listings_to_scrape) - len(filtered)
                    if skipped:
                        log.info("  %s: filtered %d→%d listings (%d-bed)",
                                 dev_info["name"], len(listings_to_scrape), len(filtered), args.bedrooms)
                    listings_to_scrape = filtered
                if args.max_listings > 0:
                    listings_to_scrape = listings_to_scrape[:args.max_listings]
                if not listings_to_scrape:
                    continue

                log.info("  Scraping %d listings for %s", len(listings_to_scrape), dev_info["name"])

                for lid in listings_to_scrape:
                    if listing_exists(lid):
                        # Update developer info for existing listing
                        proj_name = dev_info["listing_project_map"].get(lid)
                        conn_dom = sqlite3.connect(DOM_CY_DB)
                        conn_dom.execute(
                            "UPDATE listings SET developer_name=?, project_name=? WHERE id=?",
                            (dev_info["name"], proj_name, lid)
                        )
                        conn_dom.commit()
                        conn_dom.close()
                        continue

                    # Scrape new listing
                    try:
                        proj_name = dev_info["listing_project_map"].get(lid)
                        listing_data = scrape_listing_detail(
                            page, lid, dev_info["name"], proj_name, args.delay
                        )
                        if listing_data:
                            # Post-filter by bedrooms if card didn't have bed info
                            if args.bedrooms > 0 and listing_data.get("bedrooms") and listing_data["bedrooms"] != args.bedrooms:
                                log.debug("    Skip %s: %d-bed (want %d)", lid, listing_data["bedrooms"], args.bedrooms)
                                continue
                            upsert_listing(listing_data)
                            conn_dom = sqlite3.connect(DOM_CY_DB)
                            conn_dom.execute(
                                "UPDATE listings SET developer_name=?, project_name=? WHERE id=?",
                                (dev_info["name"], proj_name, lid)
                            )
                            conn_dom.commit()
                            conn_dom.close()
                            total_listings_scraped += 1
                            log.info("    [%d] %s — €%s — %s",
                                     total_listings_scraped,
                                     listing_data.get("title", "")[:50],
                                     listing_data.get("price_eur", "?"),
                                     listing_data.get("area", ""))
                    except Exception as e:
                        log.warning("    Error on listing %s: %s", lid, e)
                        if "closed" in str(e).lower():
                            try: page.close()
                            except Exception: pass
                            try: browser.close()
                            except Exception: pass
                            _kill_chrome()
                            _launch_chrome()
                            browser = connect_browser(pw)
                            ctx = browser.contexts[0]
                            page = ctx.new_page()
                            log.info("    Reconnected browser after crash")

                    time.sleep(random.uniform(args.delay * 0.8, args.delay * 1.5))

                time.sleep(random.uniform(args.delay, args.delay * 2))

        conn_user.close()
        page.close()
        browser.close()

    log.info("=== Done. %d developers, %d projects, %d new listings scraped ===",
             len(developers_info), total_projects, total_listings_scraped)


if __name__ == "__main__":
    main()
