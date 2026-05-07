"""Telegram notification for below-median deals after scraping."""

import os
import json
import sqlite3
import logging
from urllib.request import urlopen, Request
from dotenv import load_dotenv

load_dotenv()
from db import get_connection

log = logging.getLogger(__name__)

# Set these via environment variables or .env file
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

MAX_TG_LENGTH = 4000  # Telegram limit is 4096, leave margin


def _send_one_telegram(text: str) -> bool:
    """Send a single message via Telegram Bot API (POST)."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = json.dumps({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode("utf-8")
    try:
        req = Request(url, data=payload, method="POST",
                      headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True
            log.warning("Telegram API returned %d", resp.status)
            return False
    except Exception as e:
        log.warning("Failed to send Telegram message: %s", e)
        return False


def send_telegram(text: str) -> bool:
    """Send a message via Telegram Bot API, splitting into chunks if needed."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("Telegram credentials not set (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)")
        return False

    if len(text) <= MAX_TG_LENGTH:
        return _send_one_telegram(text)

    # Split by lines, grouping into chunks under the limit
    lines = text.split("\n")
    chunks = []
    current = ""
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
            import time
            time.sleep(0.5)
    return ok


def compute_median_by_segment(conn: sqlite3.Connection) -> dict:
    """Compute median EUR/m2 for each (district, bedrooms, condition) segment.

    Returns dict like {('Limassol', 2, 'Resale'): 3200.0, ...}
    """
    rows = conn.execute("""
        SELECT district, bedrooms, condition, price_per_sqm
        FROM listings
        WHERE price_per_sqm IS NOT NULL
          AND price_per_sqm > 100
          AND area_sqm > 20
          AND area_sqm < 500
          AND bedrooms IS NOT NULL
          AND condition IS NOT NULL
          AND price_eur > 10000
        ORDER BY district, bedrooms, condition, price_per_sqm
    """).fetchall()

    # Group by segment
    segments: dict[tuple, list[float]] = {}
    for district, beds, cond, ppsqm in rows:
        key = (district, beds, cond)
        segments.setdefault(key, []).append(ppsqm)

    # Compute median for each segment
    medians = {}
    for key, values in segments.items():
        n = len(values)
        if n < 2:
            medians[key] = values[0]
        elif n % 2 == 1:
            medians[key] = values[n // 2]
        else:
            medians[key] = (values[n // 2 - 1] + values[n // 2]) / 2

    return medians


def find_deals(since_timestamp: str) -> list[dict]:
    """Find new listings (scraped after since_timestamp) that are below median EUR/m2
    for their (district, bedrooms, condition) segment.

    Returns list of deal dicts sorted by discount % (best first).
    """
    conn = get_connection()

    # Compute median thresholds from entire dataset
    medians = compute_median_by_segment(conn)

    # Get new listings (filter out anomalous area values)
    new_rows = conn.execute("""
        SELECT id, title, price_eur, area_sqm, price_per_sqm,
               bedrooms, district, area, condition, property_type, url, has_pool
        FROM listings
        WHERE scraped_at > ?
          AND price_per_sqm IS NOT NULL
          AND price_per_sqm > 100
          AND area_sqm > 20
          AND area_sqm < 500
          AND bedrooms IS NOT NULL
          AND condition IS NOT NULL
          AND price_eur > 10000
    """, (since_timestamp,)).fetchall()

    conn.close()

    deals = []
    for row in new_rows:
        id_, title, price, sqm, ppsqm, beds, district, area, cond, ptype, url, pool = row
        key = (district, beds, cond)
        threshold = medians.get(key)
        if threshold is None:
            continue
        if ppsqm <= threshold:
            discount = (1 - ppsqm / threshold) * 100 if threshold > 0 else 0
            deals.append({
                "id": id_,
                "title": title,
                "price_eur": price,
                "area_sqm": sqm,
                "price_per_sqm": ppsqm,
                "bedrooms": beds,
                "district": district,
                "area": area,
                "condition": cond,
                "property_type": ptype,
                "url": url,
                "has_pool": bool(pool),
                "median_threshold": threshold,
                "discount_pct": discount,
            })

    deals.sort(key=lambda d: -d["discount_pct"])
    return deals


def _group_deals(deals: list[dict]) -> dict:
    """Group deals by bedrooms -> district -> area."""
    grouped = {}
    for d in deals:
        beds = d["bedrooms"]
        dist = d["district"]
        area = d["area"] or "?"
        grouped.setdefault(beds, {}).setdefault(dist, {}).setdefault(area, []).append(d)
    return grouped


def _format_bed_group(beds: int, bed_deals: list[dict], source_label: str, threshold_key: str, threshold_label: str) -> str:
    """Format deals for a single bedroom count into a Telegram HTML message."""
    lines = [f"\U0001f3e0 <b>{source_label} \u2014 {beds}-bed \u2014 {len(bed_deals)} deals below {threshold_label}</b>\n"]
    grouped = _group_deals(bed_deals)

    for dist in sorted(grouped.get(beds, {})):
        lines.append(f"\n<b>{dist}</b>")
        for area in sorted(grouped[beds][dist]):
            lines.append(f"  <i>{area}</i>")
            for d in grouped[beds][dist][area]:
                pool = "\U0001f3ca" if d.get("has_pool") else "\u2014"
                ptype = "\U0001f3e2" if d["property_type"] == "apartment" else "\U0001f3e1"
                lines.append(
                    f"    {ptype} <a href=\"{d['url']}\"><b>\u20ac{d['price_eur']:,}</b></a> | "
                    f"{d['area_sqm']:.0f}m\u00b2 | "
                    f"\u20ac{d['price_per_sqm']:,.0f}/m\u00b2 "
                    f"({threshold_label}: \u20ac{d[threshold_key]:,.0f}) | "
                    f"{d['condition']} | {pool}"
                )

    return "\n".join(lines)


def notify_deals(since_timestamp: str):
    """Main entry: find deals and send to Telegram, one message per bedroom count."""
    deals = find_deals(since_timestamp)
    log.info("Found %d below-median deals since %s", len(deals), since_timestamp)

    if not deals:
        return

    # Group by bedrooms and send separate messages
    by_beds: dict[int, list[dict]] = {}
    for d in deals:
        by_beds.setdefault(d["bedrooms"], []).append(d)

    sent_any = False
    for beds in sorted(by_beds):
        msg = _format_bed_group(beds, by_beds[beds], "Dom.cy", "median_threshold", "med")
        if send_telegram(msg):
            sent_any = True
            import time
            time.sleep(0.5)

    if sent_any:
        log.info("Telegram notifications sent (%d deals, %d bedroom groups)", len(deals), len(by_beds))
    else:
        # Print to console as fallback
        print("\n" + "=" * 70)
        print(f"DOM.CY DEALS BELOW MEDIAN \u2014 {len(deals)} listings")
        print("=" * 70)
        grouped = _group_deals(deals)
        for beds in sorted(grouped):
            print(f"\n{'\u2500' * 30} {beds}-bed {'\u2500' * 30}")
            for dist in sorted(grouped[beds]):
                print(f"\n  \U0001f4cd {dist}")
                for area in sorted(grouped[beds][dist]):
                    print(f"    \u250c {area}")
                    for d in grouped[beds][dist][area]:
                        ptype = "apt" if d["property_type"] == "apartment" else "house"
                        pool = "\U0001f3ca" if d.get("has_pool") else "  "
                        print(
                            f"    \u2502 {pool} \u20ac{d['price_eur']:>7,} | "
                            f"{d['area_sqm']:>5.0f}m\u00b2 | "
                            f"\u20ac{d['price_per_sqm']:>5,.0f}/m\u00b2 "
                            f"(med: \u20ac{d['median_threshold']:>5,.0f}) | "
                            f"{ptype} | {d['condition']}"
                        )
                        print(f"    \u2502    {d['url']}")
                    print(f"    \u2514")
