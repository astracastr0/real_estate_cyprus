"""One-off export: Larnaca 2-bed & 3-bed listings ≤€320k to Telegram."""

import os
import sys
import json
import time
import sqlite3
import logging
from pathlib import Path
from urllib.request import urlopen, Request


def _load_env(path: Path):
    """Minimal .env loader (key=value lines)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env(Path(__file__).parent / "bazaraki" / ".env")

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
MAX_TG_LENGTH = 4000

BASE_DIR = Path(__file__).parent
BAZARAKI_DB = BASE_DIR / "bazaraki" / "bazaraki.db"
DOM_CY_DB = BASE_DIR / "dom_cy" / "dom_cy.db"
SOTHEBYS_DB = BASE_DIR / "sothebys" / "sothebys.db"
USER_DATA_DB = BASE_DIR / "user_data.db"

MAX_PRICE = 320_000
BEDROOMS = (2, 3)
DISTRICT = "Larnaca"

EXCLUDED_AREAS = {
    a.lower() for a in [
        "Pyla", "Mazotos", "Alaminos", "Anglisides", "Kalo Chorio",
        "Kiti", "Oroklini", "Ormideia", "Alethriko", "Agios Theodoros",
        "Anafotia", "Tersefanou", "Klavdia",
        "Avdellero", "Kalavasos", "Psevdas",
    ]
}

EXCLUDED_CONDITIONS = {"resale"}


# ── Telegram ──────────────────────────────────────────────────────────

def _send_one_telegram(text: str) -> bool:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    for attempt in range(4):
        try:
            req = Request(url, data=payload, method="POST",
                          headers={"Content-Type": "application/json"})
            with urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return True
                log.warning("Telegram API returned %d", resp.status)
        except Exception as e:
            if "429" in str(e) and attempt < 3:
                wait = (attempt + 1) * 5
                log.info("Rate limited, waiting %ds...", wait)
                time.sleep(wait)
                continue
            log.warning("Failed to send Telegram message: %s", e)
            return False
    return False


def send_telegram(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not set")
        return False
    if len(text) <= MAX_TG_LENGTH:
        return _send_one_telegram(text)
    lines = text.split("\n")
    chunks, current = [], ""
    for line in lines:
        if current and len(current) + len(line) + 1 > MAX_TG_LENGTH:
            chunks.append(current)
            current = line
        else:
            current = current + "\n" + line if current else line
    if current:
        chunks.append(current)
    ok = True
    for i, chunk in enumerate(chunks):
        if not _send_one_telegram(chunk):
            ok = False
        if i < len(chunks) - 1:
            time.sleep(0.5)
    return ok


# ── Threshold computation ─────────────────────────────────────────────

def compute_p25(conn: sqlite3.Connection) -> dict:
    """p25 €/m² per (district, bedrooms, condition) — bazaraki style."""
    rows = conn.execute("""
        SELECT district, bedrooms, condition, price_per_sqm
        FROM listings
        WHERE price_per_sqm IS NOT NULL AND price_per_sqm > 100
          AND area_sqm > 20 AND area_sqm < 500
          AND bedrooms IS NOT NULL AND condition IS NOT NULL
          AND price_eur > 10000
        ORDER BY district, bedrooms, condition, price_per_sqm
    """).fetchall()
    segments: dict[tuple, list[float]] = {}
    for dist, beds, cond, ppsqm in rows:
        segments.setdefault((dist, beds, cond), []).append(ppsqm)
    p25 = {}
    for key, vals in segments.items():
        n = len(vals)
        if n < 4:
            p25[key] = vals[0]
        else:
            p25[key] = vals[max(0, int(n * 0.25) - 1)]
    return p25


def compute_median(conn: sqlite3.Connection) -> dict:
    """Median €/m² per (district, bedrooms, condition) — dom_cy style."""
    rows = conn.execute("""
        SELECT district, bedrooms, condition, price_per_sqm
        FROM listings
        WHERE price_per_sqm IS NOT NULL AND price_per_sqm > 100
          AND area_sqm > 20 AND area_sqm < 500
          AND bedrooms IS NOT NULL AND condition IS NOT NULL
          AND price_eur > 10000
        ORDER BY district, bedrooms, condition, price_per_sqm
    """).fetchall()
    segments: dict[tuple, list[float]] = {}
    for dist, beds, cond, ppsqm in rows:
        segments.setdefault((dist, beds, cond), []).append(ppsqm)
    medians = {}
    for key, vals in segments.items():
        n = len(vals)
        if n < 2:
            medians[key] = vals[0]
        elif n % 2 == 1:
            medians[key] = vals[n // 2]
        else:
            medians[key] = (vals[n // 2 - 1] + vals[n // 2]) / 2
    return medians


# ── Query listings ────────────────────────────────────────────────────

def _area_excluded(area: str | None) -> bool:
    if not area:
        return False
    return area.strip().lower() in EXCLUDED_AREAS


def query_listings(db_path: Path) -> list[dict]:
    """Fetch Larnaca 2/3-bed listings ≤ MAX_PRICE, excluding certain areas."""
    conn = sqlite3.connect(db_path)
    beds_placeholders = ",".join("?" * len(BEDROOMS))
    rows = conn.execute(f"""
        SELECT id, url, title, price_eur, area_sqm, price_per_sqm,
               bedrooms, district, area, condition, property_type, has_pool
        FROM listings
        WHERE district = ?
          AND bedrooms IN ({beds_placeholders})
          AND price_eur <= ?
          AND price_per_sqm IS NOT NULL AND price_per_sqm > 100
          AND area_sqm > 20 AND area_sqm < 500
          AND condition IS NOT NULL
          AND price_eur > 10000
        ORDER BY price_per_sqm
    """, (DISTRICT, *BEDROOMS, MAX_PRICE)).fetchall()
    conn.close()

    cols = ["id", "url", "title", "price_eur", "area_sqm", "price_per_sqm",
            "bedrooms", "district", "area", "condition", "property_type", "has_pool"]
    listings = []
    for row in rows:
        d = dict(zip(cols, row))
        if _area_excluded(d["area"]):
            continue
        if d["condition"] and d["condition"].strip().lower() in EXCLUDED_CONDITIONS:
            continue
        d["has_pool"] = bool(d["has_pool"])
        listings.append(d)
    return listings


# ── Format message ────────────────────────────────────────────────────

def format_message(source: str, beds: int, listings: list[dict],
                   thresholds: dict, threshold_label: str) -> str:
    """Build Telegram HTML for one source+bedroom group."""
    header = (f"🏠 <b>{source} — Larnaca {beds}-bed — "
              f"{len(listings)} listings (≤€{MAX_PRICE // 1000}k)</b>\n")
    lines = [header]

    # Group by area, sort by price_per_sqm within each area
    by_area: dict[str, list[dict]] = {}
    for d in listings:
        area = d["area"] or "?"
        by_area.setdefault(area, []).append(d)

    for area in sorted(by_area):
        area_listings = sorted(by_area[area], key=lambda x: x["price_per_sqm"])
        lines.append(f"\n<b>{area}</b>")
        for d in area_listings:
            key = (d["district"], d["bedrooms"], d["condition"])
            thr = thresholds.get(key)
            thr_str = f" ({threshold_label}: €{thr:,.0f})" if thr else ""
            pool = "🏊" if d["has_pool"] else "—"
            ptype = "🏢" if d["property_type"] == "apartment" else "🏡"
            lines.append(
                f"  {ptype} <a href=\"{d['url']}\"><b>€{d['price_eur']:,}</b></a> | "
                f"{d['area_sqm']:.0f}m² | "
                f"€{d['price_per_sqm']:,.0f}/m²{thr_str} | "
                f"{d['condition']} | {pool}"
            )

    return "\n".join(lines)


# ── Favorites ─────────────────────────────────────────────────────────

def get_favorite_ids() -> set[tuple[str, str]]:
    """Return set of (listing_id, source) from user_data.db."""
    if not USER_DATA_DB.exists():
        return set()
    conn = sqlite3.connect(USER_DATA_DB)
    rows = conn.execute("SELECT listing_id, source FROM favorites").fetchall()
    conn.close()
    return {(r[0], r[1]) for r in rows}


def get_comments() -> dict[str, str]:
    """Return {listing_id|source: comment} map."""
    if not USER_DATA_DB.exists():
        return {}
    conn = sqlite3.connect(USER_DATA_DB)
    rows = conn.execute("SELECT listing_id, source, comment FROM comments").fetchall()
    conn.close()
    return {f"{r[0]}|{r[1]}": r[2] for r in rows}


def query_favorites() -> list[dict]:
    """Fetch all favorite listings across all DBs."""
    fav_ids = get_favorite_ids()
    if not fav_ids:
        return []

    comments = get_comments()

    source_map = {
        "Bazaraki": BAZARAKI_DB,
        "Dom.cy": DOM_CY_DB,
        "Sothebys": SOTHEBYS_DB,
    }
    cols = ["id", "url", "title", "price_eur", "area_sqm", "price_per_sqm",
            "bedrooms", "district", "area", "condition", "property_type", "has_pool"]

    results = []
    for source, db_path in source_map.items():
        if not db_path.exists():
            continue
        ids_for_source = [lid for lid, src in fav_ids if src == source]
        if not ids_for_source:
            continue
        conn = sqlite3.connect(db_path)
        placeholders = ",".join("?" * len(ids_for_source))
        rows = conn.execute(f"""
            SELECT id, url, title, price_eur, area_sqm, price_per_sqm,
                   bedrooms, district, area, condition, property_type, has_pool
            FROM listings WHERE id IN ({placeholders})
        """, ids_for_source).fetchall()
        conn.close()
        for row in rows:
            d = dict(zip(cols, row))
            d["source"] = source
            d["has_pool"] = bool(d["has_pool"])
            d["comment"] = comments.get(f"{d['id']}|{source}", "")
            results.append(d)
    return results


def format_favorites_message(listings: list[dict]) -> str:
    """Build Telegram HTML for favorites, grouped by district."""
    header = f"⭐ <b>Favorites — {len(listings)} listings</b>\n"
    lines = [header]

    by_district: dict[str, dict[str, list[dict]]] = {}
    for d in listings:
        district = d.get("district") or "Unknown"
        area = d.get("area") or "Other"
        by_district.setdefault(district, {}).setdefault(area, []).append(d)

    for district in sorted(by_district):
        lines.append(f"\n📍 <b>{district}</b>")
        for area in sorted(by_district[district]):
            area_listings = sorted(by_district[district][area], key=lambda x: x.get("price_per_sqm") or 0)
            lines.append(f"\n  <b>{area}</b>")
            for d in area_listings:
                pool = "🏊" if d["has_pool"] else ""
                ptype = "🏢" if d.get("property_type") == "apartment" else "🏡"
                price = f"€{d['price_eur']:,}" if d.get("price_eur") else "?"
                sqm = f"{d['area_sqm']:.0f}m²" if d.get("area_sqm") else "?"
                ppsqm = f"€{d['price_per_sqm']:,.0f}/m²" if d.get("price_per_sqm") else ""
                beds = f"{d['bedrooms']}bed" if d.get("bedrooms") else ""
                lines.append(
                    f"    {ptype} <a href=\"{d['url']}\"><b>{price}</b></a> | "
                    f"{sqm} | {ppsqm} | {beds} | {area} {pool}"
                )
                if d.get("comment"):
                    lines.append(f"       ↳ <i>💬 {d['comment']}</i>")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────

def main():
    favorites_only = "--favorites-only" in sys.argv

    if favorites_only:
        listings = query_favorites()
        print(f"Favorites: {len(listings)} listings")
        if not listings:
            print("No favorites found.")
            return
        msg = format_favorites_message(listings)
        ok = send_telegram(msg)
        print(f"  Message: {'sent' if ok else 'FAILED'}")
        return

    # Bazaraki
    if not BAZARAKI_DB.exists():
        log.error("Bazaraki DB not found: %s", BAZARAKI_DB)
        sys.exit(1)
    if not DOM_CY_DB.exists():
        log.error("Dom.cy DB not found: %s", DOM_CY_DB)
        sys.exit(1)

    baz_conn = sqlite3.connect(BAZARAKI_DB)
    baz_p25 = compute_p25(baz_conn)
    baz_conn.close()

    dom_conn = sqlite3.connect(DOM_CY_DB)
    dom_med = compute_median(dom_conn)
    dom_conn.close()

    baz_listings = query_listings(BAZARAKI_DB)
    dom_listings = query_listings(DOM_CY_DB)

    print(f"Bazaraki: {len(baz_listings)} listings | Dom.cy: {len(dom_listings)} listings")

    messages = []
    for beds in BEDROOMS:
        bl = [d for d in baz_listings if d["bedrooms"] == beds]
        if bl:
            messages.append(format_message("Bazaraki", beds, bl, baz_p25, "p25"))
            print(f"  Bazaraki {beds}-bed: {len(bl)}")

    for beds in BEDROOMS:
        dl = [d for d in dom_listings if d["bedrooms"] == beds]
        if dl:
            messages.append(format_message("Dom.cy", beds, dl, dom_med, "med"))
            print(f"  Dom.cy {beds}-bed: {len(dl)}")

    if not messages:
        print("No listings matched the criteria.")
        return

    for i, msg in enumerate(messages):
        ok = send_telegram(msg)
        status = "sent" if ok else "FAILED"
        print(f"  Message {i+1}/{len(messages)}: {status}")
        if i < len(messages) - 1:
            time.sleep(3)


if __name__ == "__main__":
    main()
