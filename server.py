#!/usr/bin/env python3
"""Real Estate CY — Local HTML Dashboard with Script Runner.

Zero-dependency server (stdlib only + Chart.js CDN).
Usage: python3 server.py [--port 8080]
"""

import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).parent
BAZARAKI_DB = BASE / "bazaraki" / "bazaraki.db"
DOMCY_DB = BASE / "dom_cy" / "dom_cy.db"
SOTHEBYS_DB = BASE / "sothebys" / "sothebys.db"
USER_DATA_DB = BASE / "user_data.db"
VENV_PYTHON = BASE / "bazaraki" / ".venv" / "bin" / "python3"


def _init_user_data_db():
    """Create user_data.db with favorites and comments tables."""
    conn = sqlite3.connect(USER_DATA_DB)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS favorites (
            listing_id TEXT,
            source TEXT,
            saved_at TEXT,
            PRIMARY KEY (listing_id, source)
        );
        CREATE TABLE IF NOT EXISTS comments (
            listing_id TEXT,
            source TEXT,
            comment TEXT,
            updated_at TEXT,
            PRIMARY KEY (listing_id, source)
        );
        CREATE TABLE IF NOT EXISTS excluded (
            listing_id TEXT,
            source TEXT,
            excluded_at TEXT,
            PRIMARY KEY (listing_id, source)
        );
        CREATE TABLE IF NOT EXISTS ai_reviews (
            listing_id TEXT,
            source TEXT,
            review TEXT,
            updated_at TEXT,
            PRIMARY KEY (listing_id, source)
        );
        CREATE TABLE IF NOT EXISTS linked_listings (
            group_id INTEGER NOT NULL,
            listing_id TEXT NOT NULL,
            source TEXT NOT NULL,
            linked_at TEXT,
            PRIMARY KEY (listing_id, source)
        );
    """)
    conn.close()


def _get_excluded() -> set[tuple[str, str]]:
    """Return set of (listing_id, source) pairs that are excluded."""
    conn = sqlite3.connect(USER_DATA_DB)
    rows = conn.execute("SELECT listing_id, source FROM excluded").fetchall()
    conn.close()
    return {(r[0], r[1]) for r in rows}


def _ensure_image_url_column():
    """Add image_url column to existing scraper DBs if missing (idempotent)."""
    for db_path in [BAZARAKI_DB, DOMCY_DB, SOTHEBYS_DB]:
        if not db_path.exists():
            continue
        conn = sqlite3.connect(db_path)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()]
        if "image_url" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN image_url TEXT")
            conn.commit()
        conn.close()


def _ensure_coord_columns():
    """Add latitude/longitude columns to existing scraper DBs if missing (idempotent)."""
    for db_path in [BAZARAKI_DB, DOMCY_DB, SOTHEBYS_DB]:
        if not db_path.exists():
            continue
        conn = sqlite3.connect(db_path)
        cols = [row[1] for row in conn.execute("PRAGMA table_info(listings)").fetchall()]
        if "latitude" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN latitude REAL")
            conn.commit()
        if "longitude" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN longitude REAL")
            conn.commit()
        if "developer_name" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN developer_name TEXT")
            conn.commit()
        if "project_name" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN project_name TEXT")
            conn.commit()
        if "indoor_area_sqm" not in cols:
            conn.execute("ALTER TABLE listings ADD COLUMN indoor_area_sqm REAL")
            conn.commit()
        conn.close()


def _get_favorites() -> set[tuple[str, str]]:
    """Return set of (listing_id, source) pairs."""
    conn = sqlite3.connect(USER_DATA_DB)
    rows = conn.execute("SELECT listing_id, source FROM favorites").fetchall()
    conn.close()
    return {(r[0], r[1]) for r in rows}


def _get_comments() -> dict[str, str]:
    """Return {listing_id|source: comment} map."""
    conn = sqlite3.connect(USER_DATA_DB)
    rows = conn.execute("SELECT listing_id, source, comment FROM comments").fetchall()
    conn.close()
    return {f"{r[0]}|{r[1]}": r[2] for r in rows}


def _get_ai_reviews() -> dict[str, str]:
    """Return {listing_id|source: review} map."""
    conn = sqlite3.connect(USER_DATA_DB)
    try:
        rows = conn.execute("SELECT listing_id, source, review FROM ai_reviews").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return {f"{r[0]}|{r[1]}": r[2] for r in rows}

# ── Linked listings helpers ───────────────────────────────────────────────────

def _get_linked_groups() -> dict[str, int]:
    """Return {listing_id|source: group_id} map."""
    conn = sqlite3.connect(USER_DATA_DB)
    try:
        rows = conn.execute("SELECT listing_id, source, group_id FROM linked_listings").fetchall()
    except sqlite3.OperationalError:
        rows = []
    conn.close()
    return {f"{r[0]}|{r[1]}": r[2] for r in rows}


def _get_group_listings(group_id: int) -> list[dict]:
    """Return all listings in a group with their full data from source DBs."""
    conn = sqlite3.connect(USER_DATA_DB)
    rows = conn.execute(
        "SELECT listing_id, source FROM linked_listings WHERE group_id=?", (group_id,)
    ).fetchall()
    conn.close()

    result = []
    for lid, src in rows:
        db_map = {"Bazaraki": BAZARAKI_DB, "Dom.cy": DOMCY_DB, "Sothebys": SOTHEBYS_DB}
        db_path = db_map.get(src)
        if not db_path or not db_path.exists():
            result.append({"id": lid, "source": src})
            continue
        sconn = sqlite3.connect(db_path)
        sconn.row_factory = sqlite3.Row
        row = sconn.execute("SELECT * FROM listings WHERE id=?", (lid,)).fetchone()
        sconn.close()
        if row:
            d = dict(row)
            d["source"] = src
            d["id"] = lid
            result.append(d)
        else:
            result.append({"id": lid, "source": src})
    return result


def _update_listing_in_source_db(lid: str, source: str, updates: dict):
    """Update specific fields of a listing in its source DB."""
    db_map = {"Bazaraki": BAZARAKI_DB, "Dom.cy": DOMCY_DB, "Sothebys": SOTHEBYS_DB}
    db_path = db_map.get(source)
    if not db_path or not db_path.exists() or not updates:
        return
    set_clause = ", ".join(f"{k}=?" for k in updates)
    values = list(updates.values()) + [lid]
    conn = sqlite3.connect(db_path)
    conn.execute(f"UPDATE listings SET {set_clause} WHERE id=?", values)
    conn.commit()
    conn.close()


def _enrich_linked_group(group_id: int):
    """Auto-enrich all listings in a group with best available data."""
    listings = _get_group_listings(group_id)
    if len(listings) < 2:
        return

    # Collect best data across all listings
    best_lat = next((l.get("latitude") for l in listings if l.get("latitude")), None)
    best_lng = next((l.get("longitude") for l in listings if l.get("longitude")), None)
    best_dev = next((l.get("developer_name") for l in listings if l.get("developer_name")), None)
    best_proj = next((l.get("project_name") for l in listings if l.get("project_name")), None)
    best_img = next((l.get("image_url") for l in listings if l.get("image_url")), None)

    # Update each listing with missing fields
    for l in listings:
        updates = {}
        if not l.get("latitude") and best_lat:
            updates["latitude"] = best_lat
        if not l.get("longitude") and best_lng:
            updates["longitude"] = best_lng
        if not l.get("developer_name") and best_dev:
            updates["developer_name"] = best_dev
        if not l.get("project_name") and best_proj:
            updates["project_name"] = best_proj
        if not l.get("image_url") and best_img:
            updates["image_url"] = best_img
        if updates:
            _update_listing_in_source_db(str(l["id"]), l["source"], updates)


def _get_match_candidates(listing_id: str, source: str) -> list[dict]:
    """Find candidate matches from other sources for a given listing."""
    # Get the reference listing
    db_map = {"Bazaraki": BAZARAKI_DB, "Dom.cy": DOMCY_DB, "Sothebys": SOTHEBYS_DB}
    db_path = db_map.get(source)
    if not db_path or not db_path.exists():
        return []

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    ref = conn.execute("SELECT * FROM listings WHERE id=?", (listing_id,)).fetchone()
    conn.close()
    if not ref:
        return []
    ref = dict(ref)

    # Query all OTHER sources
    candidates = []
    for src_name, src_db in db_map.items():
        if src_name == source or not src_db.exists():
            continue
        sconn = sqlite3.connect(src_db)
        sconn.row_factory = sqlite3.Row
        rows = sconn.execute("SELECT * FROM listings").fetchall()
        sconn.close()
        for row in rows:
            d = dict(row)
            d["source"] = src_name
            score = _match_score(ref, d)
            if score >= 40:
                d["match_score"] = score
                candidates.append(d)

    # Sort by score descending, return top 10
    candidates.sort(key=lambda c: c["match_score"], reverse=True)
    return candidates[:10]


def _match_score(ref: dict, candidate: dict) -> int:
    """Score a candidate match (0-100)."""
    score = 0

    # Same district: +30
    if ref.get("district") and candidate.get("district") and ref["district"] == candidate["district"]:
        score += 30

    # Same bedrooms: +25
    if ref.get("bedrooms") is not None and candidate.get("bedrooms") is not None:
        if int(ref["bedrooms"]) == int(candidate["bedrooms"]):
            score += 25

    # Price within ±10%: +20 (scaled)
    rp = ref.get("price_eur")
    cp = candidate.get("price_eur")
    if rp and cp and rp > 0:
        diff = abs(rp - cp) / rp
        if diff <= 0.10:
            score += int(20 * (1 - diff / 0.10))

    # Area within ±10%: +15 (scaled)
    ra = ref.get("area_sqm")
    ca = candidate.get("area_sqm")
    if ra and ca and ra > 0:
        diff = abs(ra - ca) / ra
        if diff <= 0.10:
            score += int(15 * (1 - diff / 0.10))

    # Same area (sub-district): +10
    if ref.get("area") and candidate.get("area") and ref["area"] == candidate["area"]:
        score += 10

    return score


# ── Job tracking ─────────────────────────────────────────────────────────────
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()


def _run_job(job_id: str, cmd: list[str], cwd: str | None = None):
    """Run a subprocess, capturing output to a temp file."""
    with jobs_lock:
        job = jobs[job_id]
    log_path = job["log_file"]
    try:
        with open(log_path, "w") as log_f:
            proc = subprocess.Popen(
                cmd, stdout=log_f, stderr=subprocess.STDOUT,
                cwd=cwd or str(BASE), text=True,
            )
            with jobs_lock:
                jobs[job_id]["process"] = proc
            proc.wait()
            with jobs_lock:
                jobs[job_id]["status"] = "completed" if proc.returncode == 0 else "failed"
                jobs[job_id]["return_code"] = proc.returncode
    except Exception as e:
        with open(log_path, "a") as log_f:
            log_f.write(f"\n[ERROR] {e}\n")
        with jobs_lock:
            jobs[job_id]["status"] = "failed"


def start_job(name: str, cmd: list[str], cwd: str | None = None) -> str:
    job_id = str(uuid.uuid4())[:8]
    log_file = tempfile.NamedTemporaryFile(
        prefix=f"job_{job_id}_", suffix=".log", delete=False, mode="w"
    )
    log_file.close()
    with jobs_lock:
        jobs[job_id] = {
            "id": job_id,
            "name": name,
            "cmd": cmd,
            "status": "running",
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "log_file": log_file.name,
            "process": None,
            "return_code": None,
        }
    t = threading.Thread(target=_run_job, args=(job_id, cmd, cwd), daemon=True)
    t.start()
    return job_id


# ── Database helpers ─────────────────────────────────────────────────────────

def _connect(db_path: Path) -> sqlite3.Connection | None:
    if not db_path.exists():
        return None
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _query_all(db_path: Path, source: str) -> list[dict]:
    conn = _connect(db_path)
    if not conn:
        return []
    try:
        rows = conn.execute("SELECT * FROM listings").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["source"] = source
            result.append(d)
        return result
    finally:
        conn.close()


def _apply_filters(all_listings: list[dict], params: dict) -> list[dict]:
    """Apply common filters to a listing set. params uses parse_qs format (lists)."""
    district = params.get("district", [None])[0]
    beds = params.get("beds", [None])[0]
    price_max = params.get("price_max", [None])[0]
    price_min = params.get("price_min", [None])[0]
    area_min = params.get("area_min", [None])[0]
    area_max = params.get("area_max", [None])[0]
    prop_type = params.get("type", [None])[0]
    pool = params.get("pool", [None])[0]
    source = params.get("source", [None])[0]
    area_raw = params.get("area", [None])[0]
    areas_set = {a.strip() for a in area_raw.split(",") if a.strip()} if area_raw else set()
    date = params.get("date", [None])[0]
    condition_raw = params.get("condition", [None])[0]
    conditions_set = {c.strip().lower() for c in condition_raw.split(",") if c.strip()} if condition_raw else set()
    exclude_areas_raw = params.get("exclude_areas", [None])[0]
    exclude_areas = set()
    if exclude_areas_raw:
        exclude_areas = {a.strip().lower() for a in exclude_areas_raw.split(",") if a.strip()}
    exclude_cond_raw = params.get("exclude_conditions", [None])[0]
    exclude_conditions = set()
    if exclude_cond_raw:
        exclude_conditions = {c.strip().lower() for c in exclude_cond_raw.split(",") if c.strip()}
    hide_expired = params.get("hide_expired", ["0"])[0] == "1"
    favorites_only = params.get("favorites", ["0"])[0] == "1"
    favorites_set = _get_favorites() if favorites_only else set()
    hide_excluded = params.get("hide_excluded", ["0"])[0] == "1"
    excluded_set = _get_excluded() if hide_excluded else set()

    filtered = []
    for r in all_listings:
        if hide_expired and r.get("is_expired"):
            continue
        if favorites_only and (str(r.get("id", "")), r.get("source", "")) not in favorites_set:
            continue
        if hide_excluded and (str(r.get("id", "")), r.get("source", "")) in excluded_set:
            continue
        if district and r.get("district") != district:
            continue
        if beds and r.get("bedrooms") != int(beds):
            continue
        if price_max and (r.get("price_eur") or 0) > int(price_max):
            continue
        if price_min and (r.get("price_eur") or 0) < int(price_min):
            continue
        if area_min and (r.get("area_sqm") or 0) < int(area_min):
            continue
        if area_max and (r.get("area_sqm") or 0) > int(area_max):
            continue
        if prop_type and r.get("property_type") != prop_type:
            continue
        if pool == "1" and not r.get("has_pool"):
            continue
        if source and r.get("source") != source:
            continue
        if areas_set and r.get("area") not in areas_set:
            continue
        if date and (r.get("scraped_at") or "")[:10] != date:
            continue
        if conditions_set and (r.get("condition") or "").lower() not in conditions_set:
            continue
        if exclude_conditions and (r.get("condition") or "").strip().lower() in exclude_conditions:
            continue
        if exclude_areas and (r.get("area") or "").strip().lower() in exclude_areas:
            continue
        filtered.append(r)
    return filtered


def get_stats(params: dict | None = None) -> dict:
    """KPI stats + chart data from both DBs, optionally filtered."""
    all_listings = _query_all(BAZARAKI_DB, "Bazaraki") + _query_all(DOMCY_DB, "Dom.cy") + _query_all(SOTHEBYS_DB, "Sothebys")

    # Save unfiltered copy for cascading filter options
    all_listings_raw = all_listings

    # Apply filters if provided
    if params:
        all_listings = _apply_filters(all_listings, params)

    # Compute filter options: for each dimension, apply all filters EXCEPT that
    # dimension so the dropdown shows what's available given other selections.
    def _opts(exclude_key):
        if not params:
            return all_raw
        p2 = {k: v for k, v in params.items() if k != exclude_key}
        return _apply_filters(all_raw, p2) if p2 else all_raw

    all_raw = all_listings_raw  # unfiltered copy saved above
    all_districts = sorted(set(r.get("district") for r in _opts("district") if r.get("district")))
    all_types = sorted(set(r.get("property_type") for r in _opts("type") if r.get("property_type")))
    all_beds = sorted(set(int(r.get("bedrooms")) for r in _opts("beds") if r.get("bedrooms") is not None))
    all_areas = sorted(set(r.get("area") for r in _opts("area") if r.get("area")))
    all_dates = sorted(set((r.get("scraped_at") or "")[:10] for r in _opts("date") if r.get("scraped_at")), reverse=True)
    all_conditions = sorted(set(r.get("condition") for r in _opts("condition") if r.get("condition")))

    filter_options = {"districts": all_districts, "types": all_types,
                      "bedrooms": all_beds, "areas": all_areas, "dates": all_dates,
                      "conditions": all_conditions}

    if not all_listings:
        return {"total": 0, "avg_price": 0, "med_price": 0, "avg_sqm": 0,
                "with_pool": 0, "new_count": 0, "last_date": None,
                "charts": {}, "filter_options": filter_options}

    prices = [r["price_eur"] for r in all_listings if r.get("price_eur")]
    sqm_prices = [r["price_per_sqm"] for r in all_listings if r.get("price_per_sqm")]
    prices.sort()
    total = len(all_listings)
    avg_price = int(sum(prices) / len(prices)) if prices else 0
    med_price = int(prices[len(prices) // 2]) if prices else 0
    avg_sqm = int(sum(sqm_prices) / len(sqm_prices)) if sqm_prices else 0
    with_pool = sum(1 for r in all_listings if r.get("has_pool"))

    # Last session
    dates = [r.get("scraped_at", "")[:10] for r in all_listings if r.get("scraped_at")]
    last_date = max(dates) if dates else None
    new_count = sum(1 for r in all_listings if r.get("scraped_at", "")[:10] == last_date) if last_date else 0

    # Chart: avg price by district + type
    dist_type: dict[str, dict[str, list]] = {}
    for r in all_listings:
        d = r.get("district") or "Unknown"
        t = r.get("property_type") or "unknown"
        dist_type.setdefault(d, {}).setdefault(t, []).append(r.get("price_eur", 0))
    chart_avg_by_district = []
    for dist, types in sorted(dist_type.items()):
        for ptype, plist in sorted(types.items()):
            chart_avg_by_district.append({
                "district": dist, "type": ptype,
                "avg_price": int(sum(plist) / len(plist)) if plist else 0
            })

    # Chart: price histogram (buckets)
    bucket_size = 50000
    max_hist_price = 3000000
    hist_prices = [p for p in prices if p <= max_hist_price]
    buckets: dict[int, int] = {}
    for p in hist_prices:
        b = (p // bucket_size) * bucket_size
        buckets[b] = buckets.get(b, 0) + 1
    chart_histogram = [{"bucket": b, "count": c} for b, c in sorted(buckets.items())]

    # Chart: listings by district (pie)
    dist_counts: dict[str, int] = {}
    for r in all_listings:
        d = r.get("district") or "Unknown"
        dist_counts[d] = dist_counts.get(d, 0) + 1
    chart_by_district = [{"district": d, "count": c} for d, c in sorted(dist_counts.items())]

    # Chart: bedrooms distribution
    bed_counts: dict[int, int] = {}
    for r in all_listings:
        b = r.get("bedrooms")
        if b is not None:
            bed_counts[int(b)] = bed_counts.get(int(b), 0) + 1
    chart_bedrooms = [{"bedrooms": b, "count": c} for b, c in sorted(bed_counts.items())]

    # Chart: condition distribution (count + avg price)
    cond_data: dict[str, list] = {}
    for r in all_listings:
        c = r.get("condition") or "Unknown"
        p = r.get("price_eur")
        if p:
            cond_data.setdefault(c, []).append(p)
    chart_by_condition = []
    for c, plist in sorted(cond_data.items()):
        chart_by_condition.append({
            "condition": c, "count": len(plist),
            "avg_price": int(sum(plist) / len(plist)) if plist else 0
        })
    chart_by_condition.sort(key=lambda x: x["count"], reverse=True)

    # Chart: avg price by area (top 25 by listing count)
    area_prices: dict[str, list] = {}
    for r in all_listings:
        a = r.get("area")
        p = r.get("price_eur")
        if a and p:
            area_prices.setdefault(a, []).append(p)
    area_stats = []
    for a, plist in area_prices.items():
        area_stats.append({"area": a, "avg_price": int(sum(plist) / len(plist)), "count": len(plist)})
    area_stats.sort(key=lambda x: x["count"], reverse=True)
    chart_avg_by_area = area_stats[:25]
    chart_avg_by_area.sort(key=lambda x: x["avg_price"], reverse=True)

    # History: listings by date + source
    date_source: dict[str, dict[str, int]] = {}
    for r in all_listings:
        d = (r.get("scraped_at") or "")[:10]
        if not d:
            continue
        s = r.get("source", "?")
        date_source.setdefault(d, {}).setdefault(s, 0)
        date_source[d][s] += 1
    chart_history = []
    for d in sorted(date_source):
        for s, c in date_source[d].items():
            chart_history.append({"date": d, "source": s, "count": c})

    # History: median price by date + source
    date_source_prices: dict[str, dict[str, list]] = {}
    for r in all_listings:
        d = (r.get("scraped_at") or "")[:10]
        if not d:
            continue
        s = r.get("source", "?")
        p = r.get("price_eur")
        if p:
            date_source_prices.setdefault(d, {}).setdefault(s, []).append(p)
    chart_price_trend = []
    for d in sorted(date_source_prices):
        for s, plist in date_source_prices[d].items():
            plist.sort()
            med = plist[len(plist) // 2] if plist else 0
            chart_price_trend.append({"date": d, "source": s, "median_price": med})

    # Sessions table
    sessions_data: dict[str, dict] = {}
    for r in all_listings:
        d = (r.get("scraped_at") or "")[:10]
        s = r.get("source", "?")
        if not d:
            continue
        key = f"{d}|{s}"
        if key not in sessions_data:
            sessions_data[key] = {"date": d, "source": s, "count": 0,
                                  "prices": [], "ids": set()}
        sessions_data[key]["count"] += 1
        sessions_data[key]["ids"].add(r.get("id"))
        p = r.get("price_eur")
        if p:
            sessions_data[key]["prices"].append(p)
    sessions = []
    for v in sorted(sessions_data.values(), key=lambda x: x["date"], reverse=True):
        ps = v["prices"]
        sessions.append({
            "date": v["date"], "source": v["source"],
            "count": v["count"], "unique": len(v["ids"]),
            "min_price": min(ps) if ps else 0,
            "max_price": max(ps) if ps else 0,
            "avg_price": int(sum(ps) / len(ps)) if ps else 0,
        })

    return {
        "total": total, "avg_price": avg_price, "med_price": med_price,
        "avg_sqm": avg_sqm, "with_pool": with_pool,
        "expired_count": sum(1 for r in all_listings if r.get("is_expired")),
        "new_count": new_count, "last_date": last_date,
        "filter_options": filter_options,
        "charts": {
            "avg_by_district": chart_avg_by_district,
            "avg_by_area": chart_avg_by_area,
            "histogram": chart_histogram,
            "by_district": chart_by_district,
            "bedrooms": chart_bedrooms,
            "by_condition": chart_by_condition,
            "history": chart_history,
            "price_trend": chart_price_trend,
            "sessions": sessions,
        },
    }


def get_listings(params: dict) -> list[dict]:
    """Filtered listings for the table tab."""
    all_listings = _query_all(BAZARAKI_DB, "Bazaraki") + _query_all(DOMCY_DB, "Dom.cy") + _query_all(SOTHEBYS_DB, "Sothebys")

    filtered = _apply_filters(all_listings, params)
    sort = params.get("sort", ["price_desc"])[0]
    limit = int(params.get("limit", [500])[0])

    sort_map = {
        "price_asc": ("price_eur", False),
        "price_desc": ("price_eur", True),
        "sqm_asc": ("price_per_sqm", False),
        "sqm_desc": ("price_per_sqm", True),
        "area_desc": ("area_sqm", True),
        "date_desc": ("scraped_at", True),
    }
    sort_key, reverse = sort_map.get(sort, ("price_eur", True))
    filtered.sort(key=lambda r: r.get(sort_key) or 0, reverse=reverse)

    favorites = _get_favorites()
    comments = _get_comments()
    excluded = _get_excluded()
    ai_reviews = _get_ai_reviews()
    linked_groups = _get_linked_groups()

    # Build group size counts
    group_counts: dict[int, int] = {}
    for gid in linked_groups.values():
        group_counts[gid] = group_counts.get(gid, 0) + 1

    result = []
    for r in filtered[:limit]:
        lid = str(r.get("id", ""))
        src = r.get("source", "")
        result.append({
            "id": lid,
            "source": src,
            "title": r.get("title", ""),
            "district": r.get("district", ""),
            "area": r.get("area", ""),
            "property_type": r.get("property_type", ""),
            "price_eur": r.get("price_eur"),
            "price_per_sqm": r.get("price_per_sqm"),
            "area_sqm": r.get("area_sqm"),
            "indoor_area_sqm": r.get("indoor_area_sqm"),
            "bedrooms": r.get("bedrooms"),
            "bathrooms": r.get("bathrooms"),
            "has_pool": bool(r.get("has_pool")),
            "furnishing": r.get("furnishing", ""),
            "condition": r.get("condition", ""),
            "is_expired": bool(r.get("is_expired")),
            "posted_date": r.get("posted_date", ""),
            "url": r.get("url", ""),
            "scraped_at": r.get("scraped_at", ""),
            "image_url": r.get("image_url", ""),
            "latitude": r.get("latitude"),
            "longitude": r.get("longitude"),
            "is_favorite": (lid, src) in favorites,
            "is_excluded": (lid, src) in excluded,
            "comment": comments.get(f"{lid}|{src}", ""),
            "ai_review": ai_reviews.get(f"{lid}|{src}", ""),
            "developer_name": r.get("developer_name", ""),
            "project_name": r.get("project_name", ""),
            "group_id": linked_groups.get(f"{lid}|{src}"),
            "linked_count": group_counts.get(linked_groups.get(f"{lid}|{src}"), 0) - 1 if linked_groups.get(f"{lid}|{src}") else 0,
        })
    return result


# ── HTML Dashboard ───────────────────────────────────────────────────────────

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Real Estate CY</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.css">
<link rel="stylesheet" href="https://unpkg.com/leaflet.markercluster@1.5.3/dist/MarkerCluster.Default.css">
<script src="https://unpkg.com/leaflet.markercluster@1.5.3/dist/leaflet.markercluster.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0f1117;color:#e2e8f0;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;font-size:14px}
a{color:#60a5fa;text-decoration:none}a:hover{text-decoration:underline}

/* Header */
.header{background:#161b27;padding:12px 24px;display:flex;align-items:center;gap:16px;border-bottom:1px solid #1e2636}
.header h1{font-size:1.2rem;font-weight:600}

/* Tabs */
.tabs{display:flex;gap:0;background:#161b27;border-bottom:1px solid #2a3550;padding:0 24px}
.tab{padding:10px 20px;cursor:pointer;color:#94a3b8;font-size:.85rem;font-weight:500;border-bottom:2px solid transparent;transition:all .2s}
.tab:hover{color:#e2e8f0}.tab.active{color:#60a5fa;border-bottom-color:#60a5fa}
.tab-content{display:none;padding:24px}.tab-content.active{display:block}

/* KPI cards */
.kpi-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}
.kpi{background:linear-gradient(135deg,#1a2235,#1e2840);border:1px solid #2a3550;border-left:3px solid #3b82f6;border-radius:10px;padding:14px 16px}
.kpi-val{font-size:1.3rem;font-weight:700;color:#e2e8f0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi-lbl{font-size:.68rem;color:#64748b;text-transform:uppercase;letter-spacing:.07em;margin-top:2px}
.kpi-delta{font-size:.72rem;color:#34d399;margin-top:1px}
.kpi-green{border-left-color:#10b981!important}.kpi-purple{border-left-color:#8b5cf6!important}
.kpi-amber{border-left-color:#f59e0b!important}.kpi-rose{border-left-color:#f43f5e!important}
.kpi-cyan{border-left-color:#06b6d4!important}

/* Charts grid */
.charts-grid{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px}
.chart-card{background:#1e2636;border:1px solid #2a3550;border-radius:10px;padding:16px}
.chart-card h3{font-size:.85rem;color:#94a3b8;margin-bottom:12px;font-weight:500}
@media(max-width:900px){.charts-grid{grid-template-columns:1fr}}

/* Table */
.table-wrap{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.82rem}
th{background:#1e2636;color:#94a3b8;padding:8px 10px;text-align:left;border-bottom:1px solid #2d3748;font-weight:600;font-size:.72rem;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap;position:sticky;top:0;cursor:pointer}
th:hover{color:#e2e8f0}
td{padding:7px 10px;border-bottom:1px solid #1a2030;color:#e2e8f0;vertical-align:middle;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
tr:hover td{background:#1c2538}

/* Filters bar */
.filters{display:flex;flex-wrap:wrap;gap:10px;margin-bottom:16px;align-items:flex-end}
.filter-group{display:flex;flex-direction:column;gap:3px}
.filter-group label{font-size:.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.05em}
.filter-group select,.filter-group input{background:#1a2235;border:1px solid #2a3550;color:#e2e8f0;padding:6px 10px;border-radius:6px;font-size:.82rem}
.filter-group select:focus,.filter-group input:focus{outline:none;border-color:#3b82f6}

/* Buttons */
.btn{padding:8px 18px;border:none;border-radius:6px;font-size:.85rem;font-weight:500;cursor:pointer;transition:all .2s}
.btn-primary{background:#3b82f6;color:#fff}.btn-primary:hover{background:#2563eb}
.btn-danger{background:#ef4444;color:#fff}.btn-danger:hover{background:#dc2626}
.btn-success{background:#10b981;color:#fff}.btn-success:hover{background:#059669}
.btn:disabled{opacity:.5;cursor:not-allowed}

/* Runner */
.runner-grid{display:grid;grid-template-columns:1fr 1fr;gap:20px}
@media(max-width:900px){.runner-grid{grid-template-columns:1fr}}
.runner-panel{background:#1e2636;border:1px solid #2a3550;border-radius:10px;padding:20px}
.runner-panel h3{font-size:.95rem;margin-bottom:16px;color:#e2e8f0}
.form-row{margin-bottom:12px}
.form-row label{display:block;font-size:.75rem;color:#94a3b8;margin-bottom:4px;text-transform:uppercase;letter-spacing:.05em}
.checkbox-group{display:flex;flex-wrap:wrap;gap:8px}
.checkbox-group label{display:flex;align-items:center;gap:4px;font-size:.82rem;color:#e2e8f0;text-transform:none;letter-spacing:0;cursor:pointer}
.checkbox-group input[type="checkbox"]{accent-color:#3b82f6}
textarea{background:#1a2235;border:1px solid #2a3550;color:#e2e8f0;padding:8px;border-radius:6px;font-size:.82rem;width:100%;resize:vertical;font-family:inherit}
textarea:focus{outline:none;border-color:#3b82f6}

/* Log viewer */
.log-viewer{background:#0d1117;border:1px solid #1e2636;border-radius:8px;padding:12px;font-family:'SF Mono',Monaco,Consolas,monospace;font-size:.75rem;color:#8b949e;max-height:400px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;margin-top:16px}

/* Jobs list */
.job-item{background:#1a2235;border:1px solid #2a3550;border-radius:8px;padding:12px 16px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center}
.job-info{display:flex;flex-direction:column;gap:2px}
.job-name{font-weight:600;font-size:.85rem}.job-meta{font-size:.72rem;color:#64748b}
.badge{display:inline-block;padding:2px 8px;border-radius:4px;font-size:.7rem;font-weight:600;text-transform:uppercase}
.badge-running{background:#1e3a5f;color:#60a5fa}.badge-completed{background:#064e3b;color:#34d399}
.badge-failed{background:#4c1d1d;color:#f87171}.badge-stopped{background:#3b3020;color:#fbbf24}
.count-badge{font-size:.7rem;color:#64748b;margin-left:4px}

/* Sessions table */
.sessions-table{margin-top:20px}

/* Custom multiselect dropdown */
.multi-dropdown{position:relative;display:inline-block;min-width:120px}
.multi-dropdown .multi-btn{background:#1e2636;color:#e2e8f0;border:1px solid #2a3550;border-radius:6px;padding:6px 28px 6px 10px;
  font-size:.85rem;cursor:pointer;width:100%;text-align:left;appearance:none;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.multi-dropdown .multi-btn::after{content:'▾';position:absolute;right:8px;top:50%;transform:translateY(-50%);color:#64748b;pointer-events:none}
.multi-dropdown .multi-panel{display:none;position:absolute;top:100%;left:0;min-width:100%;background:#1e2636;border:1px solid #2a3550;
  border-radius:6px;margin-top:2px;z-index:100;padding:4px 0;max-height:200px;overflow-y:auto;box-shadow:0 4px 12px rgba(0,0,0,.4)}
.multi-dropdown.open .multi-panel{display:block}
.multi-dropdown .multi-panel label{display:flex;align-items:center;gap:6px;padding:4px 10px;cursor:pointer;font-size:.82rem;color:#e2e8f0;white-space:nowrap}
.multi-dropdown .multi-panel label:hover{background:#2a3550}
.multi-dropdown .multi-panel input[type=checkbox]{accent-color:#60a5fa}

/* View toggle */
.view-toggle{display:flex;gap:4px;margin-bottom:12px}
.vt-btn{background:#1e2636;border:1px solid #2a3550;color:#94a3b8;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:1rem;line-height:1}
.vt-btn:hover{color:#e2e8f0;border-color:#3b82f6}.vt-btn.active{color:#60a5fa;border-color:#60a5fa;background:#1a2a4a}

/* Star / favorite button */
.star-btn{background:none;border:none;cursor:pointer;font-size:1.1rem;padding:0 2px;color:#475569;transition:color .15s}
.star-btn:hover{color:#fbbf24}.star-btn.fav{color:#fbbf24}
tr.fav-row>td:first-child{border-left:3px solid #fbbf24}

/* Comment */
.comment-btn{background:none;border:none;cursor:pointer;font-size:.9rem;padding:0 2px;color:#475569;transition:color .15s}
.comment-btn:hover{color:#60a5fa}.comment-btn.has-comment{color:#60a5fa}
.comment-row td{background:#141c2b!important;padding:4px 10px!important;border-bottom:1px solid #1e2636!important}
.comment-input{background:#1a2235;border:1px solid #2a3550;color:#e2e8f0;padding:4px 8px;border-radius:4px;font-size:.8rem;width:100%;max-width:600px}
.comment-input:focus{outline:none;border-color:#3b82f6}

/* Grid / card view */
.listings-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.listing-card{background:#1e2636;border:1px solid #2a3550;border-radius:10px;overflow:hidden;transition:border-color .2s;position:relative}
.listing-card:hover{border-color:#3b82f6}
.listing-card.fav-card{border-left:3px solid #fbbf24}
.card-img{width:100%;height:180px;object-fit:cover;background:#161b27;display:block}
.card-img-placeholder{width:100%;height:180px;background:#161b27;display:flex;align-items:center;justify-content:center;color:#475569;font-size:2rem}
.card-body{padding:12px}
.card-title{font-size:.85rem;font-weight:600;color:#e2e8f0;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:6px;line-height:1.3}
.card-price{font-size:1.1rem;font-weight:700;color:#60a5fa;margin-bottom:4px}
.card-specs{font-size:.75rem;color:#94a3b8;display:flex;gap:8px;flex-wrap:wrap;margin-bottom:4px}
.card-specs span{background:#1a2235;padding:2px 6px;border-radius:4px}
.card-location{font-size:.72rem;color:#64748b;margin-bottom:6px}
.card-footer{display:flex;justify-content:space-between;align-items:center}
.card-star{position:absolute;top:8px;right:8px;background:rgba(0,0,0,.6);border:none;cursor:pointer;font-size:1.2rem;color:#475569;padding:4px 6px;border-radius:6px;transition:color .15s}
.card-star:hover{color:#fbbf24}.card-star.fav{color:#fbbf24}
.card-comment-indicator{font-size:.75rem;color:#60a5fa;cursor:pointer}

/* Exclude button */
.exclude-btn{background:none;border:none;cursor:pointer;font-size:.85rem;padding:0 2px;color:#475569;transition:color .15s}
.exclude-btn:hover{color:#ef4444}.exclude-btn.excluded{color:#ef4444}
tr.excluded-row>td{opacity:.45}
.listing-card.excluded-card{opacity:.45}
.card-exclude{position:absolute;top:8px;left:8px;background:rgba(0,0,0,.6);border:none;cursor:pointer;font-size:.9rem;color:#475569;padding:4px 6px;border-radius:6px;transition:color .15s}
.card-exclude:hover{color:#ef4444}.card-exclude.excluded{color:#ef4444}

/* Link / match */
.link-btn{background:none;border:none;cursor:pointer;font-size:.85rem;padding:0 2px;color:#475569;transition:color .15s}
.link-btn:hover{color:#8b5cf6}.link-btn.linked{color:#8b5cf6}
.group-badge{display:inline-block;padding:1px 5px;border-radius:4px;font-size:.65rem;font-weight:600;background:#2d1b69;color:#a78bfa;margin-left:4px;vertical-align:middle}
.card-link{position:absolute;top:8px;right:40px;background:rgba(0,0,0,.6);border:none;cursor:pointer;font-size:.9rem;color:#475569;padding:4px 6px;border-radius:6px;transition:color .15s}
.card-link:hover{color:#8b5cf6}.card-link.linked{color:#8b5cf6}

/* Match modal */
.match-modal-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.6);z-index:1000;display:flex;align-items:center;justify-content:center}
.match-modal{background:#1e2636;border:1px solid #2a3550;border-radius:12px;width:90%;max-width:700px;max-height:80vh;overflow-y:auto;padding:20px}
.match-modal h3{font-size:1rem;margin-bottom:4px;color:#e2e8f0}
.match-modal .match-ref{font-size:.8rem;color:#94a3b8;margin-bottom:16px}
.match-modal .match-loading{text-align:center;color:#64748b;padding:30px}
.match-candidate{display:flex;gap:12px;align-items:center;padding:10px;border:1px solid #2a3550;border-radius:8px;margin-bottom:8px;transition:border-color .2s}
.match-candidate:hover{border-color:#3b82f6}
.match-candidate img{width:80px;height:60px;object-fit:cover;border-radius:6px;flex-shrink:0}
.match-candidate .mc-placeholder{width:80px;height:60px;background:#161b27;border-radius:6px;display:flex;align-items:center;justify-content:center;color:#475569;flex-shrink:0}
.match-candidate .mc-info{flex:1;min-width:0}
.match-candidate .mc-title{font-size:.82rem;font-weight:600;color:#e2e8f0;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.match-candidate .mc-details{font-size:.75rem;color:#94a3b8;margin-top:2px}
.match-candidate .mc-source{display:inline-block;padding:1px 6px;border-radius:4px;font-size:.65rem;font-weight:600;background:#1a2a4a;color:#60a5fa}
.match-candidate .mc-score{font-size:.75rem;font-weight:700;color:#34d399;white-space:nowrap}
.match-candidate .mc-link-btn{background:#3b82f6;color:#fff;border:none;padding:5px 12px;border-radius:6px;font-size:.78rem;cursor:pointer;white-space:nowrap}
.match-candidate .mc-link-btn:hover{background:#2563eb}
.match-linked{border-color:#8b5cf6;background:#1a1530}
.match-linked .mc-unlink-btn{background:#ef4444;color:#fff;border:none;padding:5px 10px;border-radius:6px;font-size:.78rem;cursor:pointer;white-space:nowrap}
.match-linked .mc-unlink-btn:hover{background:#dc2626}
.match-modal .match-close{float:right;background:none;border:none;color:#94a3b8;font-size:1.3rem;cursor:pointer}
.match-modal .match-close:hover{color:#e2e8f0}
.match-empty{text-align:center;color:#64748b;padding:20px;font-size:.85rem}
</style>
</head>
<body>
<div class="header">
  <h1>&#127968; Real Estate Dashboard — Cyprus</h1>
</div>

<!-- ═══════════ GLOBAL FILTERS ═══════════ -->
<div style="background:#161b27;padding:10px 24px 10px;border-bottom:1px solid #2a3550">
  <div class="filters" style="margin-bottom:0">
    <div class="filter-group">
      <label>District</label><select id="f-district"><option value="">All</option></select>
    </div>
    <div class="filter-group">
      <label>Area</label>
      <div class="multi-dropdown" id="f-area-wrap">
        <button type="button" class="multi-btn" id="f-area-btn">All</button>
        <div class="multi-panel" id="f-area-panel"></div>
      </div>
    </div>
    <div class="filter-group">
      <label style="display:flex;align-items:center;gap:4px;cursor:pointer;text-transform:none;letter-spacing:0;font-size:.82rem;color:#e2e8f0;margin-top:18px">
        <input type="checkbox" id="f-exclude-suburban" style="accent-color:#f59e0b"> Exclude suburban
      </label>
    </div>
    <div class="filter-group">
      <label>Bedrooms</label><select id="f-beds"><option value="">All</option></select>
    </div>
    <div class="filter-group">
      <label>Type</label><select id="f-type"><option value="">All</option></select>
    </div>
    <div class="filter-group">
      <label>Source</label>
      <select id="f-source"><option value="">All</option><option>Bazaraki</option><option>Dom.cy</option><option>Sothebys</option></select>
    </div>
    <div class="filter-group">
      <label>Condition</label>
      <div class="multi-dropdown" id="f-condition-wrap">
        <button type="button" class="multi-btn" id="f-condition-btn">All</button>
        <div class="multi-panel" id="f-condition-panel"></div>
      </div>
    </div>
    <div class="filter-group">
      <label>Pool</label>
      <select id="f-pool"><option value="">Any</option><option value="1">Yes</option></select>
    </div>
    <div class="filter-group">
      <label>Max Price</label><input id="f-price-max" type="number" placeholder="e.g. 500000" value="400000" style="width:110px">
    </div>
    <div class="filter-group">
      <label>Min Area m&sup2;</label><input id="f-area-min" type="number" placeholder="e.g. 80" style="width:90px">
    </div>
    <div class="filter-group">
      <label>Max Area m&sup2;</label><input id="f-area-max" type="number" placeholder="e.g. 200" style="width:90px">
    </div>
    <div class="filter-group">
      <label>Date</label><select id="f-date"><option value="">All</option></select>
    </div>
    <div class="filter-group">
      <label>Sort <span style="font-size:.6rem;color:#475569">(listings)</span></label>
      <select id="f-sort">
        <option value="price_desc">Price &#8595;</option><option value="price_asc">Price &#8593;</option>
        <option value="sqm_desc">&#8364;/m&#178; &#8595;</option><option value="sqm_asc">&#8364;/m&#178; &#8593;</option>
        <option value="area_desc">Area &#8595;</option><option value="date_desc">Date &#8595;</option>
      </select>
    </div>
    <div class="filter-group">
      <label style="display:flex;align-items:center;gap:4px;cursor:pointer;text-transform:none;letter-spacing:0;font-size:.82rem;color:#e2e8f0;margin-top:18px">
        <input type="checkbox" id="f-hide-expired" checked style="accent-color:#ef4444"> Hide expired
      </label>
    </div>
    <div class="filter-group">
      <label style="display:flex;align-items:center;gap:4px;cursor:pointer;text-transform:none;letter-spacing:0;font-size:.82rem;color:#fbbf24;margin-top:18px">
        <input type="checkbox" id="f-favorites" style="accent-color:#fbbf24"> Favorites only
      </label>
    </div>
    <div class="filter-group">
      <label style="display:flex;align-items:center;gap:4px;cursor:pointer;text-transform:none;letter-spacing:0;font-size:.82rem;color:#e2e8f0;margin-top:18px">
        <input type="checkbox" id="f-hide-excluded" checked style="accent-color:#94a3b8"> Hide excluded
      </label>
    </div>
  </div>
</div>

<div class="tabs">
  <div class="tab active" data-tab="dashboard">Dashboard</div>
  <div class="tab" data-tab="listings">Listings <span id="listings-count" class="count-badge"></span></div>
  <div class="tab" data-tab="history">History</div>
  <div class="tab" data-tab="runner">Runner</div>
</div>

<!-- ═══════════ DASHBOARD TAB ═══════════ -->
<div id="tab-dashboard" class="tab-content active">
  <div class="kpi-grid" id="kpi-grid"></div>
  <div class="charts-grid">
    <div class="chart-card"><h3>Average Price by District & Type</h3><canvas id="chart-avg"></canvas></div>
    <div class="chart-card"><h3>Price Distribution</h3><canvas id="chart-hist"></canvas></div>
    <div class="chart-card"><h3>Listings by District</h3><canvas id="chart-pie"></canvas></div>
    <div class="chart-card"><h3>Bedrooms Distribution</h3><canvas id="chart-beds"></canvas></div>
    <div class="chart-card"><h3>By Condition (count)</h3><canvas id="chart-cond-count"></canvas></div>
    <div class="chart-card"><h3>Avg Price by Condition</h3><canvas id="chart-cond-price"></canvas></div>
  </div>
  <div class="charts-grid">
    <div class="chart-card" style="grid-column:1/-1"><h3>Average Price by Area (top 25)</h3><canvas id="chart-area-avg"></canvas></div>
  </div>
</div>

<!-- ═══════════ HISTORY TAB ═══════════ -->
<div id="tab-history" class="tab-content">
  <div class="charts-grid">
    <div class="chart-card"><h3>Listings Collected Over Time</h3><canvas id="chart-history"></canvas></div>
    <div class="chart-card"><h3>Median Price Trend</h3><canvas id="chart-trend"></canvas></div>
  </div>
  <div class="sessions-table">
    <h3 style="font-size:.95rem;margin-bottom:12px;color:#94a3b8">Scraping Sessions</h3>
    <div class="table-wrap"><table id="sessions-table"><thead><tr>
      <th>Date</th><th>Source</th><th>Count</th><th>Unique</th><th>Min Price</th><th>Max Price</th><th>Avg Price</th>
    </tr></thead><tbody></tbody></table></div>
  </div>
</div>

<!-- ═══════════ LISTINGS TAB ═══════════ -->
<div id="tab-listings" class="tab-content">
  <div class="view-toggle">
    <button class="vt-btn active" id="vt-table" onclick="setView('table')" title="Table view">&#9776;</button>
    <button class="vt-btn" id="vt-grid" onclick="setView('grid')" title="Grid view">&#9638;</button>
    <button class="vt-btn" id="vt-map" onclick="setView('map')" title="Map view">&#127758;</button>
  </div>
  <div id="listings-table-wrap" class="table-wrap"><table id="listings-table"><thead><tr>
    <th>&#9733;</th><th>&#10005;</th><th>&#128172;</th><th>&#128279;</th><th>Link</th><th>Title</th><th>District</th><th>Area</th><th>Type</th>
    <th>Price &#8364;</th><th>&#8364;/m&#178;</th><th>Area m&#178;</th><th>Beds</th><th>Baths</th>
    <th>Pool</th><th>Condition</th><th>Posted</th><th>Scraped</th><th>Source</th>
  </tr></thead><tbody></tbody></table></div>
  <div id="listings-grid" class="listings-grid" style="display:none"></div>
  <div id="listings-map" style="display:none;height:600px;border-radius:10px;border:1px solid #2a3550"></div>
</div>

<!-- ═══════════ RUNNER TAB ═══════════ -->
<div id="tab-runner" class="tab-content">
  <div class="runner-grid">
    <div class="runner-panel">
      <h3>&#128269; Scraper</h3>
      <div class="form-row">
        <label>Source</label>
        <div class="checkbox-group">
          <label><input type="checkbox" id="src-bazaraki" checked> Bazaraki</label>
          <label><input type="checkbox" id="src-domcy" checked> Dom.cy</label>
          <label><input type="checkbox" id="src-sothebys"> Sotheby's</label>
        </div>
      </div>
      <div class="form-row">
        <label>Districts</label>
        <div class="checkbox-group">
          <label><input type="checkbox" class="dist-cb" value="Larnaca" checked> Larnaca</label>
          <label><input type="checkbox" class="dist-cb" value="Limassol" checked> Limassol</label>
          <label><input type="checkbox" class="dist-cb" value="Paphos" checked> Paphos</label>
        </div>
      </div>
      <div class="form-row">
        <label>Bedrooms</label>
        <div class="checkbox-group">
          <label><input type="checkbox" class="beds-cb" value="1"> 1</label>
          <label><input type="checkbox" class="beds-cb" value="2" checked> 2</label>
          <label><input type="checkbox" class="beds-cb" value="3" checked> 3</label>
          <label><input type="checkbox" class="beds-cb" value="4"> 4</label>
          <label><input type="checkbox" class="beds-cb" value="5"> 5+</label>
        </div>
      </div>
      <div class="form-row">
        <label>Max Pages</label>
        <input type="number" id="max-pages" value="5" min="1" max="50" style="width:80px;background:#1a2235;border:1px solid #2a3550;color:#e2e8f0;padding:6px 10px;border-radius:6px">
      </div>
      <button class="btn btn-primary" onclick="startScrape()">Start Scrape</button>
    </div>

    <div class="runner-panel">
      <h3>&#128228; Export to Telegram</h3>
      <div class="form-row">
        <label>District</label>
        <input type="text" id="exp-district" value="Larnaca" style="width:100%;background:#1a2235;border:1px solid #2a3550;color:#e2e8f0;padding:6px 10px;border-radius:6px">
      </div>
      <div class="form-row">
        <label>Bedrooms (comma-separated)</label>
        <input type="text" id="exp-beds" value="2,3" style="width:100%;background:#1a2235;border:1px solid #2a3550;color:#e2e8f0;padding:6px 10px;border-radius:6px">
      </div>
      <div class="form-row">
        <label>Max Price €</label>
        <input type="number" id="exp-price" value="320000" style="width:100%;background:#1a2235;border:1px solid #2a3550;color:#e2e8f0;padding:6px 10px;border-radius:6px">
      </div>
      <div class="form-row">
        <label>Excluded Areas (one per line)</label>
        <textarea id="exp-excluded" rows="4">Pyla
Mazotos
Alaminos
Anglisides
Kalo Chorio
Kiti
Oroklini
Ormideia
Alethriko
Agios Theodoros
Anafotia
Tersefanou
Klavdia
Avdellero
Kalavasos
Psevdas</textarea>
      </div>
      <div class="form-row">
        <label>Excluded Conditions (one per line)</label>
        <textarea id="exp-conditions" rows="2">resale</textarea>
      </div>
      <div class="form-row">
        <label><input type="checkbox" id="exp-favorites-only"> Favorites only</label>
      </div>
      <button class="btn btn-success" onclick="startExport()">Send to Telegram</button>
    </div>

    <div class="runner-panel">
      <h3>&#128270; Check Expired</h3>
      <div class="form-row">
        <label>Source</label>
        <div class="checkbox-group">
          <label><input type="checkbox" id="exp-chk-bazaraki" checked> Bazaraki</label>
          <label><input type="checkbox" id="exp-chk-domcy" checked> Dom.cy</label>
          <label><input type="checkbox" id="exp-chk-sothebys"> Sotheby's</label>
        </div>
      </div>
      <div class="form-row">
        <label><input type="checkbox" id="exp-chk-unchecked" checked> Only unchecked listings</label>
      </div>
      <button class="btn btn-primary" onclick="startCheckExpired()" style="background:#7c3aed">Check Expired</button>
    </div>

    <div class="runner-panel">
      <h3>&#128205; Backfill Coordinates</h3>
      <div class="form-row">
        <label>Source</label>
        <div class="checkbox-group">
          <label><input type="checkbox" id="coords-sothebys" checked> Sotheby's</label>
          <label><input type="checkbox" id="coords-bazaraki"> Bazaraki</label>
        </div>
      </div>
      <button class="btn btn-primary" onclick="startBackfillCoords()" style="background:#059669">Backfill Coords</button>
    </div>
  </div>

  <h3 style="margin-top:24px;font-size:.95rem;color:#94a3b8">Jobs</h3>
  <div id="jobs-list" style="margin-top:12px"></div>
  <div class="log-viewer" id="log-viewer" style="display:none"></div>
</div>

<script>
// ── Tab switching ────────────────────────────────────────────────────────────
document.querySelectorAll('.tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    document.getElementById('tab-' + tab.dataset.tab).classList.add('active');
  });
});

// ── Utility ──────────────────────────────────────────────────────────────────
const fmt = n => n == null ? '—' : '€' + Number(n).toLocaleString('en');
const fmtK = n => {
  if (n == null) return '—';
  if (n >= 1000000) return '€' + (n/1000000).toFixed(2).replace(/\.?0+$/, '') + 'M';
  if (n >= 1000) return '€' + Math.round(n/1000) + 'K';
  return '€' + n;
};
const COLORS = {
  blue: '#60a5fa', green: '#34d399', purple: '#a78bfa', amber: '#fbbf24',
  rose: '#fb7185', cyan: '#22d3ee', orange: '#fb923c', lime: '#a3e635',
};
const PALETTE = Object.values(COLORS);
let chartInstances = {};
function destroyChart(id) { if (chartInstances[id]) { chartInstances[id].destroy(); delete chartInstances[id]; } }
Chart.defaults.color = '#94a3b8';
Chart.defaults.borderColor = '#1a2030';
Chart.defaults.font.family = '-apple-system, BlinkMacSystemFont, sans-serif';

function esc(s) { if (!s) return ''; const d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function developerSearchUrl(r) {
  if (r.image_url) return 'https://lens.google.com/uploadbyurl?url=' + encodeURIComponent(r.image_url);
  const area = r.area || r.district || '';
  const q = `cyprus developer ${area} new building`;
  return 'https://www.google.com/search?tbm=isch&q=' + encodeURIComponent(q);
}
function kpiCard(icon, value, label, delta, cls) {
  return `<div class="kpi ${cls}"><div style="font-size:1rem">${icon}</div><div class="kpi-val">${value}</div><div class="kpi-lbl">${label}</div>${delta ? '<div class="kpi-delta">'+delta+'</div>' : ''}</div>`;
}

// ── Unified filters (single f-* set) ────────────────────────────────────────
let _cachedStats = null;

function _fillSelect(id, values) {
  const sel = document.getElementById(id);
  if (!sel) return;
  const cur = sel.value;
  while (sel.options.length > 1) sel.remove(1);
  values.forEach(v => { const o = document.createElement('option'); o.value = v; o.textContent = v; sel.appendChild(o); });
  if (cur && values.map(String).includes(String(cur))) sel.value = cur;
  else if (cur) sel.value = '';
}

// Generic custom multiselect dropdown
const _multiState = {};
function _initMulti(name, btnId, panelId, wrapId) {
  _multiState[name] = new Set();
  document.getElementById(btnId).addEventListener('click', e => {
    e.stopPropagation();
    // Close other dropdowns first
    document.querySelectorAll('.multi-dropdown.open').forEach(el => {
      if (el.id !== wrapId) el.classList.remove('open');
    });
    document.getElementById(wrapId).classList.toggle('open');
  });
}
function _fillMulti(name, panelId, btnId, values) {
  const panel = document.getElementById(panelId);
  if (!panel) return;
  const sel = _multiState[name];
  _multiState[name] = new Set([...sel].filter(v => values.includes(v)));
  panel.innerHTML = '';
  values.forEach(v => {
    const lbl = document.createElement('label');
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.value = v;
    cb.checked = _multiState[name].has(v);
    cb.addEventListener('change', () => {
      if (cb.checked) _multiState[name].add(v); else _multiState[name].delete(v);
      _updateMultiBtn(name, btnId);
      applyFilters();
    });
    lbl.appendChild(cb);
    lbl.appendChild(document.createTextNode(v));
    panel.appendChild(lbl);
  });
  _updateMultiBtn(name, btnId);
}
function _updateMultiBtn(name, btnId) {
  const btn = document.getElementById(btnId);
  const sel = _multiState[name];
  if (!btn) return;
  if (sel.size === 0) btn.textContent = 'All';
  else if (sel.size <= 2) btn.textContent = [...sel].join(', ');
  else btn.textContent = sel.size + ' selected';
}
_initMulti('condition', 'f-condition-btn', 'f-condition-panel', 'f-condition-wrap');
_initMulti('area', 'f-area-btn', 'f-area-panel', 'f-area-wrap');
// Close all dropdowns on outside click
document.addEventListener('click', e => {
  document.querySelectorAll('.multi-dropdown').forEach(wrap => {
    if (!wrap.contains(e.target)) wrap.classList.remove('open');
  });
});

const SUBURBAN_AREAS = [
  'Pyla','Mazotos','Alaminos','Anglisides','Kalo Chorio','Kiti','Oroklini','Ormideia',
  'Alethriko','Agios Theodoros','Anafotia','Tersefanou','Klavdia','Avdellero','Kalavasos',
  'Psevdas','Meneou','Pervolia','Dromolaxia','Xylofagou','Xylotymvou','Mosfiloti',
  'Athienou','Aradippou','Livadia','Kalo Chorio Lemesou','Choirokoitia',
  'Psematismenos','Tochni','Maroni','Zygi','Agios Theodoros Larnaca',
  'Agios Theodoros Limassol','Kornos','Pyrga Larnakas','Alaminos',
  'Asgata','Kellia','Softades','Troulloi','Voroklini'
];

function getFilterParams() {
  const params = new URLSearchParams();
  const map = {district:'f-district', beds:'f-beds', type:'f-type', source:'f-source',
               pool:'f-pool', price_max:'f-price-max', area_min:'f-area-min', area_max:'f-area-max', date:'f-date'};
  for (const [k, id] of Object.entries(map)) {
    const el = document.getElementById(id);
    if (el && el.value) params.set(k, el.value);
  }
  // Multiselect filters
  if (_multiState.condition.size) params.set('condition', [..._multiState.condition].join(','));
  if (_multiState.area.size) params.set('area', [..._multiState.area].join(','));
  // Exclude suburban areas
  if (document.getElementById('f-exclude-suburban').checked) {
    params.set('exclude_areas', SUBURBAN_AREAS.join(','));
  }
  // Hide expired
  if (document.getElementById('f-hide-expired').checked) {
    params.set('hide_expired', '1');
  }
  // Favorites only
  if (document.getElementById('f-favorites').checked) {
    params.set('favorites', '1');
  }
  // Hide excluded
  if (document.getElementById('f-hide-excluded').checked) {
    params.set('hide_excluded', '1');
  }
  return params;
}

function updateDropdowns(fo) {
  _fillSelect('f-district', fo.districts || []);
  _fillSelect('f-beds', (fo.bedrooms || []).map(String));
  _fillSelect('f-type', fo.types || []);
  _fillMulti('area', 'f-area-panel', 'f-area-btn', fo.areas || []);
  _fillSelect('f-date', fo.dates || []);
  _fillMulti('condition', 'f-condition-panel', 'f-condition-btn', fo.conditions || []);
}

// ── Central apply: updates dashboard + listings + history ───────────────────
let _applyTimer = null;
function applyFilters() {
  clearTimeout(_applyTimer);
  _applyTimer = setTimeout(_doApply, 80);
}
async function _doApply() {
  const params = getFilterParams();
  const qs = params.toString();

  // 1. Fetch filtered stats
  const statsRes = await fetch('/api/stats' + (qs ? '?' + qs : ''));
  const data = await statsRes.json();

  // Cache stats for history (always update — history uses its own source filter)
  _cachedStats = data;

  // Update cascading dropdowns
  updateDropdowns(data.filter_options || {});

  // 2. Render dashboard
  renderDashboard(data);

  // 3. Fetch + render listings
  params.set('sort', document.getElementById('f-sort').value);
  params.set('limit', '500');
  const listRes = await fetch('/api/listings?' + params.toString());
  const listings = await listRes.json();
  renderListings(listings);

  // 4. Render history (from cached unfiltered, filtered client-side by source)
  renderHistory();
}

// Attach onchange to all filter elements
['f-district','f-beds','f-type','f-source','f-pool','f-price-max','f-area-min','f-area-max','f-date','f-sort','f-exclude-suburban','f-hide-expired','f-favorites','f-hide-excluded'].forEach(id => {
  const el = document.getElementById(id);
  if (el) el.addEventListener('change', () => { _mapUserInteracted = false; applyFilters(); });
});

// ── Dashboard rendering ─────────────────────────────────────────────────────
function renderDashboard(data) {
  const c = data.charts || {};

  document.getElementById('kpi-grid').innerHTML = [
    kpiCard('&#127960;', data.total?.toLocaleString() || '0', 'Listings', '', ''),
    kpiCard('&#128176;', fmtK(data.avg_price), 'Avg Price', '', 'kpi-green'),
    kpiCard('&#128202;', fmtK(data.med_price), 'Median', '', 'kpi-cyan'),
    kpiCard('&#128208;', data.avg_sqm ? '€'+data.avg_sqm.toLocaleString()+'/m²' : '—', 'Price/m²', '', 'kpi-purple'),
    kpiCard('&#127946;', String(data.with_pool || 0), 'With Pool', '', 'kpi-amber'),
    kpiCard('&#127381;', String(data.new_count || 0), 'New (last)', data.last_date ? 'from ' + data.last_date : '', 'kpi-rose'),
  ].join('');

  // Avg price by district
  destroyChart('chart-avg');
  const avgData = c.avg_by_district || [];
  const districts = [...new Set(avgData.map(d => d.district))];
  const types = [...new Set(avgData.map(d => d.type))];
  chartInstances['chart-avg'] = new Chart(document.getElementById('chart-avg'), {
    type: 'bar',
    data: { labels: districts, datasets: types.map((t, i) => ({
      label: t,
      data: districts.map(d => { const m = avgData.find(x => x.district === d && x.type === t); return m ? m.avg_price : 0; }),
      backgroundColor: PALETTE[i % PALETTE.length] + 'cc',
    })) },
    options: { responsive:true, plugins:{legend:{position:'top'}}, scales:{y:{ticks:{callback:v=>fmtK(v)}}} }
  });

  // Histogram
  destroyChart('chart-hist');
  const hist = c.histogram || [];
  chartInstances['chart-hist'] = new Chart(document.getElementById('chart-hist'), {
    type: 'bar',
    data: { labels: hist.map(h => fmtK(h.bucket)), datasets: [{label:'Listings', data:hist.map(h=>h.count), backgroundColor:COLORS.blue+'cc'}] },
    options: { responsive:true, plugins:{legend:{display:false}} }
  });

  // Pie by district
  destroyChart('chart-pie');
  const pie = c.by_district || [];
  chartInstances['chart-pie'] = new Chart(document.getElementById('chart-pie'), {
    type: 'doughnut',
    data: { labels: pie.map(p=>p.district), datasets: [{data:pie.map(p=>p.count), backgroundColor:PALETTE.slice(0,pie.length)}] },
    options: { responsive:true, plugins:{legend:{position:'right'}} }
  });

  // Bedrooms
  destroyChart('chart-beds');
  const beds = c.bedrooms || [];
  chartInstances['chart-beds'] = new Chart(document.getElementById('chart-beds'), {
    type: 'bar',
    data: { labels: beds.map(b=>b.bedrooms+' bed'), datasets: [{label:'Listings', data:beds.map(b=>b.count), backgroundColor:beds.map((_,i)=>PALETTE[i%PALETTE.length]+'cc')}] },
    options: { responsive:true, plugins:{legend:{display:false}} }
  });

  // Avg price by area (horizontal bar, top 25)
  destroyChart('chart-area-avg');
  const areaData = c.avg_by_area || [];
  chartInstances['chart-area-avg'] = new Chart(document.getElementById('chart-area-avg'), {
    type: 'bar',
    data: {
      labels: areaData.map(a => a.area + ' (' + a.count + ')'),
      datasets: [{
        label: 'Avg Price',
        data: areaData.map(a => a.avg_price),
        backgroundColor: areaData.map((_,i) => PALETTE[i % PALETTE.length] + 'cc'),
      }]
    },
    options: {
      indexAxis: 'y', responsive: true,
      plugins: { legend: { display: false },
        tooltip: { callbacks: { label: ctx => fmtK(ctx.raw) } } },
      scales: { x: { ticks: { callback: v => fmtK(v) } } }
    }
  });

  // By condition (count)
  destroyChart('chart-cond-count');
  const condData = c.by_condition || [];
  chartInstances['chart-cond-count'] = new Chart(document.getElementById('chart-cond-count'), {
    type: 'bar',
    data: { labels: condData.map(d=>d.condition), datasets: [{label:'Listings', data:condData.map(d=>d.count), backgroundColor:condData.map((_,i)=>PALETTE[i%PALETTE.length]+'cc')}] },
    options: { responsive:true, plugins:{legend:{display:false}} }
  });

  // Avg price by condition
  destroyChart('chart-cond-price');
  chartInstances['chart-cond-price'] = new Chart(document.getElementById('chart-cond-price'), {
    type: 'bar',
    data: { labels: condData.map(d=>d.condition), datasets: [{label:'Avg Price', data:condData.map(d=>d.avg_price), backgroundColor:condData.map((_,i)=>PALETTE[i%PALETTE.length]+'cc')}] },
    options: { responsive:true, plugins:{legend:{display:false}}, scales:{y:{ticks:{callback:v=>fmtK(v)}}} }
  });
}

// ── History (client-side filter from cached unfiltered data) ─────────────────
function renderHistory() {
  const src = _cachedStats;
  if (!src) return;
  const c = src.charts || {};
  // Use global source filter for history too
  const sourceFilter = document.getElementById('f-source').value;

  let historyData = c.history || [];
  if (sourceFilter) historyData = historyData.filter(h => h.source === sourceFilter);
  destroyChart('chart-history');
  const hDates = [...new Set(historyData.map(h => h.date))].sort();
  const hSources = [...new Set(historyData.map(h => h.source))];
  chartInstances['chart-history'] = new Chart(document.getElementById('chart-history'), {
    type: 'bar',
    data: { labels: hDates, datasets: hSources.map((s, i) => ({
      label: s,
      data: hDates.map(d => { const m = historyData.find(x => x.date===d && x.source===s); return m ? m.count : 0; }),
      backgroundColor: PALETTE[i % PALETTE.length] + 'cc',
    })) },
    options: { responsive:true, plugins:{legend:{position:'top'}} }
  });

  let trendData = c.price_trend || [];
  if (sourceFilter) trendData = trendData.filter(t => t.source === sourceFilter);
  destroyChart('chart-trend');
  const tDates = [...new Set(trendData.map(t => t.date))].sort();
  const tSources = [...new Set(trendData.map(t => t.source))];
  chartInstances['chart-trend'] = new Chart(document.getElementById('chart-trend'), {
    type: 'line',
    data: { labels: tDates, datasets: tSources.map((s, i) => ({
      label: s,
      data: tDates.map(d => { const m = trendData.find(x => x.date===d && x.source===s); return m ? m.median_price : null; }),
      borderColor: PALETTE[i%PALETTE.length], backgroundColor: PALETTE[i%PALETTE.length]+'33',
      tension: 0.3, fill: false, pointRadius: 4,
    })) },
    options: { responsive:true, plugins:{legend:{position:'top'}}, scales:{y:{ticks:{callback:v=>fmtK(v)}}} }
  });

  let sessions = c.sessions || [];
  if (sourceFilter) sessions = sessions.filter(s => s.source === sourceFilter);
  document.querySelector('#sessions-table tbody').innerHTML = sessions.map(s => `<tr>
    <td>${s.date}</td><td>${s.source}</td><td>${s.count}</td><td>${s.unique}</td>
    <td>${fmt(s.min_price)}</td><td>${fmt(s.max_price)}</td><td>${fmt(s.avg_price)}</td>
  </tr>`).join('');
}

// ── View toggle ──────────────────────────────────────────────────────────────
let currentView = 'table';
let _currentListings = [];
let _leafletMap = null;
let _markerCluster = null;
let _mapUserInteracted = false;

function setView(view) {
  currentView = view;
  document.getElementById('vt-table').classList.toggle('active', view === 'table');
  document.getElementById('vt-grid').classList.toggle('active', view === 'grid');
  document.getElementById('vt-map').classList.toggle('active', view === 'map');
  document.getElementById('listings-table-wrap').style.display = view === 'table' ? '' : 'none';
  document.getElementById('listings-grid').style.display = view === 'grid' ? '' : 'none';
  document.getElementById('listings-map').style.display = view === 'map' ? '' : 'none';
  renderListings(_currentListings);
  if (view === 'map') {
    setTimeout(() => {
      if (!_leafletMap) {
        _leafletMap = L.map('listings-map').setView([34.7, 33.2], 9);
        L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
          attribution: '&copy; OpenStreetMap contributors',
          maxZoom: 18,
        }).addTo(_leafletMap);
        _markerCluster = L.markerClusterGroup();
        _leafletMap.addLayer(_markerCluster);
      }
      _leafletMap.invalidateSize();
      renderMapMarkers(_currentListings);
    }, 100);
  }
}

function renderMapMarkers(listings) {
  if (!_markerCluster) return;
  _markerCluster.clearLayers();
  const markers = [];
  const bounds = [];
  for (const r of listings) {
    if (!r.latitude || !r.longitude) continue;
    bounds.push([r.latitude, r.longitude]);
    const marker = L.marker([r.latitude, r.longitude]);
    const gmapsUrl = 'https://www.google.com/maps?q=' + r.latitude + ',' + r.longitude;
    const imgHtml = r.image_url ? '<img src="' + esc(r.image_url) + '" style="width:100%;max-width:230px;height:120px;object-fit:cover;border-radius:4px;margin-bottom:6px" onerror="this.style.display=\'none\'">' : '';
    const safeComment = (r.comment||'').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
    const popupHtml = `<div style="font-size:12px;max-width:250px">
      ${imgHtml}
      <b>${esc(r.title).substring(0,60)}</b><br>
      <span style="color:#2563eb;font-weight:700">${r.price_eur ? fmt(r.price_eur) : '—'}</span>
      ${r.price_per_sqm ? ' <span style="color:#666;font-size:11px">&euro;' + Math.round(r.price_per_sqm) + '/m&sup2;</span>' : ''}<br>
      ${r.bedrooms != null ? r.bedrooms + ' bed ' : ''}${r.bathrooms != null ? r.bathrooms + ' bath ' : ''}${r.area_sqm ? Math.round(r.area_sqm) + ' m&sup2;' : ''}${r.indoor_area_sqm ? ' ('+Math.round(r.indoor_area_sqm)+' int.)' : ''}<br>
      <span style="color:#666">${esc(r.district)}${r.area ? ' / ' + esc(r.area) : ''}</span><br>
      ${r.url ? '<a href="' + esc(r.url) + '" target="_blank">Listing &nearr;</a> &middot; ' : ''}
      <a href="${gmapsUrl}" target="_blank">Maps &nearr;</a> &middot;
      <a href="${developerSearchUrl(r)}" target="_blank">Find by image &nearr;</a>
      ${r.developer_name && r.project_name ? ' &middot; <a href="https://www.google.com/search?q=' + encodeURIComponent(r.developer_name + ' ' + r.project_name + ' in Cyprus') + '" target="_blank">Google project &nearr;</a>' : ''}
      <button onclick="toggleFavorite('${esc(r.id)}','${esc(r.source)}')" style="border:none;background:none;cursor:pointer;font-size:1.1rem">${r.is_favorite ? '&#9733;' : '&#9734;'}</button>
      <button onclick="toggleExclude('${esc(r.id)}','${esc(r.source)}')" style="border:none;background:none;cursor:pointer;font-size:.9rem;color:${r.is_excluded ? '#ef4444' : '#666'}" title="Exclude">&#10005;</button>
      <button onclick="openMatchModal('${esc(r.id)}','${esc(r.source)}')" style="border:none;background:none;cursor:pointer;font-size:.9rem;color:${r.group_id ? '#8b5cf6' : '#666'}" title="${r.group_id ? 'Group #'+r.group_id : 'Find matches'}">&#128279;</button>${r.linked_count > 0 ? '<span style="font-size:.65rem;color:#a78bfa;font-weight:700">'+r.linked_count+'</span>' : ''}
      ${r.developer_name ? '<div style="margin-top:4px;font-size:10px;color:#059669"><b>&#127959;</b> ' + esc(r.developer_name) + (r.project_name ? ' &middot; ' + esc(r.project_name) : '') + '</div>' : ''}
      ${r.ai_review ? '<div style="margin-top:4px;padding:4px 6px;background:#eff6ff;border-left:3px solid #3b82f6;border-radius:3px;font-size:10px;color:#1e40af"><b>AI:</b> ' + esc(r.ai_review) + '</div>' : ''}
      <div style="margin-top:6px"><input class="comment-input" placeholder="Add a note..." value="${safeComment}" oninput="saveComment('${esc(r.id)}','${esc(r.source)}',this.value)" style="width:100%;background:#f5f5f5;border:1px solid #ddd;color:#333;padding:4px 6px;border-radius:4px;font-size:11px"></div>
    </div>`;
    marker.bindPopup(popupHtml, {maxWidth: 260});
    markers.push(marker);
  }
  _markerCluster.addLayers(markers);
  if (bounds.length > 0 && !_mapUserInteracted) {
    _leafletMap.fitBounds(bounds, {padding: [30, 30], maxZoom: 14});
  }
  _mapUserInteracted = true;
}

// ── Favorite toggle ──────────────────────────────────────────────────────────
async function toggleFavorite(id, source) {
  try {
    const res = await fetch('/api/favorite', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({listing_id: id, source})
    });
    const data = await res.json();
    console.log('toggleFavorite', id, source, data);
    if (!res.ok) alert('Failed to toggle favorite: ' + res.status);
  } catch(e) {
    console.error('toggleFavorite error', e);
    alert('Failed to toggle favorite: ' + e.message);
  }
  applyFilters();
}

// ── Exclude toggle ───────────────────────────────────────────────────────────
async function toggleExclude(id, source) {
  await fetch('/api/exclude', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({listing_id: id, source})
  });
  applyFilters();
}

// ── Comment save ─────────────────────────────────────────────────────────────
let _commentTimeout = null;
function saveComment(id, source, value) {
  clearTimeout(_commentTimeout);
  _commentTimeout = setTimeout(async () => {
    await fetch('/api/comment', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({listing_id: id, source, comment: value})
    });
    // Update local data so icons reflect new state without full reload
    const item = _currentListings.find(l => l.id === id && l.source === source);
    if (item) item.comment = value;
    // Update the comment button icon in table view
    const idx = _currentListings.indexOf(item);
    if (idx >= 0) {
      const btn = document.querySelector(`#comment-row-${idx}`)?.previousElementSibling?.querySelector('.comment-btn');
      if (btn) {
        btn.className = 'comment-btn ' + (value ? 'has-comment' : '');
        btn.innerHTML = value ? '&#128172;' : '&#128173;';
        btn.title = value;
      }
    }
  }, 500);
}
function toggleCommentRow(rowId) {
  const row = document.getElementById(rowId);
  if (row) row.style.display = row.style.display === 'none' ? '' : 'none';
}
function toggleCardComment(cardId) {
  const el = document.getElementById(cardId);
  if (el) {
    el.style.display = el.style.display === 'none' ? '' : 'none';
    if (el.style.display !== 'none') el.querySelector('input')?.focus();
  }
}

// ── Listings rendering ──────────────────────────────────────────────────────
function renderListings(listings) {
  _currentListings = listings;
  document.getElementById('listings-count').textContent = '(' + listings.length + ')';

  if (currentView === 'table') {
    document.querySelector('#listings-table tbody').innerHTML = listings.map((r, i) => {
      const cid = 'comment-row-' + i;
      const safeComment = (r.comment||'').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
      const rowCls = [r.is_favorite ? 'fav-row' : '', r.is_excluded ? 'excluded-row' : ''].filter(Boolean).join(' ');
      return `<tr class="${rowCls}"${r.is_expired ? ' style="opacity:0.45"' : ''}>
    <td><button class="star-btn ${r.is_favorite ? 'fav' : ''}" onclick="toggleFavorite('${esc(r.id)}','${esc(r.source)}')">${r.is_favorite ? '&#9733;' : '&#9734;'}</button></td>
    <td><button class="exclude-btn ${r.is_excluded ? 'excluded' : ''}" onclick="toggleExclude('${esc(r.id)}','${esc(r.source)}')" title="${r.is_excluded ? 'Unexclude' : 'Exclude'}">${r.is_excluded ? '&#10005;' : '&#10005;'}</button></td>
    <td><button class="comment-btn ${r.comment || r.ai_review ? 'has-comment' : ''}" onclick="toggleCommentRow('${cid}')" title="${safeComment}">${r.comment || r.ai_review ? '&#128172;' : '&#128173;'}</button></td>
    <td><button class="link-btn ${r.group_id ? 'linked' : ''}" onclick="openMatchModal('${esc(r.id)}','${esc(r.source)}')" title="${r.group_id ? 'Group #'+r.group_id+' (linked to '+r.linked_count+')' : 'Find matches'}">&#128279;</button>${r.linked_count > 0 ? '<span class="group-badge">'+r.linked_count+'</span>' : ''}</td>
    <td>${r.url ? '<a href="'+esc(r.url)+'" target="_blank">open &#8599;</a>' : ''}</td>
    <td title="${esc(r.title)}">${esc((r.title||'').substring(0,60))}</td>
    <td>${esc(r.district)}</td>
    <td>${esc(r.area)}</td>
    <td>${esc(r.property_type)}</td>
    <td>${r.price_eur ? fmt(r.price_eur) : '—'}</td>
    <td>${r.price_per_sqm ? '€'+Math.round(r.price_per_sqm).toLocaleString() : '—'}</td>
    <td>${r.area_sqm ? Math.round(r.area_sqm) : '—'}</td>
    <td>${r.bedrooms ?? '—'}</td>
    <td>${r.bathrooms ?? '—'}</td>
    <td>${r.has_pool ? '&#9989;' : ''}</td>
    <td>${esc(r.condition)}</td>
    <td>${(r.posted_date||'').substring(0,10)}</td>
    <td>${(r.scraped_at||'').substring(0,10)}</td>
    <td>${esc(r.source)}</td>
  </tr><tr class="comment-row" id="${cid}" style="display:${r.comment || r.ai_review ? '' : 'none'}"><td colspan="19">${r.ai_review ? '<div style="margin-bottom:4px;padding:3px 6px;background:#eff6ff;border-left:3px solid #3b82f6;border-radius:3px;font-size:11px;color:#1e40af"><b>AI:</b> '+esc(r.ai_review)+'</div>' : ''}<input class="comment-input" placeholder="Add a note..." value="${safeComment}" oninput="saveComment('${esc(r.id)}','${esc(r.source)}',this.value)"></td></tr>`;
    }).join('');
  } else {
    document.getElementById('listings-grid').innerHTML = listings.map((r, i) => {
      const ccid = 'card-comment-' + i;
      const safeComment = (r.comment||'').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
      return `<div class="listing-card ${r.is_favorite ? 'fav-card' : ''} ${r.is_excluded ? 'excluded-card' : ''}">
      <button class="card-exclude ${r.is_excluded ? 'excluded' : ''}" onclick="toggleExclude('${esc(r.id)}','${esc(r.source)}')" title="${r.is_excluded ? 'Unexclude' : 'Exclude'}">&#10005;</button>
      <button class="card-link ${r.group_id ? 'linked' : ''}" onclick="openMatchModal('${esc(r.id)}','${esc(r.source)}')" title="${r.group_id ? 'Group #'+r.group_id : 'Find matches'}">&#128279;${r.linked_count > 0 ? '<span class="group-badge">'+r.linked_count+'</span>' : ''}</button>
      <button class="card-star ${r.is_favorite ? 'fav' : ''}" onclick="toggleFavorite('${esc(r.id)}','${esc(r.source)}')">${r.is_favorite ? '&#9733;' : '&#9734;'}</button>
      ${r.image_url ? '<img class="card-img" src="'+esc(r.image_url)+'" alt="" loading="lazy" onerror="this.outerHTML=\'<div class=card-img-placeholder>&#127968;</div>\'">' : '<div class="card-img-placeholder">&#127968;</div>'}
      <div class="card-body">
        <div class="card-title" title="${esc(r.title)}">${esc(r.title)}</div>
        <div class="card-price">${r.price_eur ? fmt(r.price_eur) : '—'}</div>
        <div class="card-specs">
          ${r.bedrooms != null ? '<span>'+r.bedrooms+' bed</span>' : ''}
          ${r.bathrooms != null ? '<span>'+r.bathrooms+' bath</span>' : ''}
          ${r.area_sqm ? '<span>'+Math.round(r.area_sqm)+' m&sup2;</span>' : ''}${r.indoor_area_sqm ? '<span>'+Math.round(r.indoor_area_sqm)+' int.</span>' : ''}
          ${r.price_per_sqm ? '<span>&euro;'+Math.round(r.price_per_sqm)+'/m&sup2;</span>' : ''}
        </div>
        <div class="card-location">${esc(r.district)}${r.area ? ' / '+esc(r.area) : ''} &middot; ${esc(r.source)}</div>
        ${r.developer_name ? '<div style="font-size:.7rem;color:#059669;margin-top:2px">&#127959; '+esc(r.developer_name)+(r.project_name ? ' &middot; '+esc(r.project_name) : '')+'</div>' : ''}
        <div class="card-footer">
          ${r.url ? '<a href="'+esc(r.url)+'" target="_blank" style="font-size:.78rem">Open &#8599;</a>' : '<span></span>'}
          <a href="${developerSearchUrl(r)}" target="_blank" style="font-size:.72rem;color:#10b981">Find by image &#8599;</a>
          ${r.developer_name && r.project_name ? '<a href="https://www.google.com/search?q='+encodeURIComponent(r.developer_name+' '+r.project_name+' in Cyprus')+'" target="_blank" style="font-size:.72rem;color:#2563eb">Google project &#8599;</a>' : ''}
          <button class="comment-btn ${r.comment ? 'has-comment' : ''}" onclick="toggleCardComment('${ccid}')" title="${safeComment}">${r.comment ? '&#128172;' : '&#128173;'}</button>
        </div>
        ${r.ai_review ? '<div style="margin-top:5px;padding:4px 6px;background:#eff6ff;border-left:3px solid #3b82f6;border-radius:3px;font-size:10px;color:#1e40af"><b>AI:</b> '+esc(r.ai_review)+'</div>' : ''}
        <div id="${ccid}" class="card-comment-editor" style="display:${r.comment ? '' : 'none'};margin-top:6px">
          <input class="comment-input" placeholder="Add a note..." value="${safeComment}" oninput="saveComment('${esc(r.id)}','${esc(r.source)}',this.value)">
        </div>
      </div>
    </div>`;
    }).join('');
  }

  // Update map markers if map is visible
  if (currentView === 'map' && _leafletMap) {
    renderMapMarkers(listings);
  }
}

// ── Match modal ──────────────────────────────────────────────────────────
async function openMatchModal(listingId, source) {
  // Build modal
  const overlay = document.createElement('div');
  overlay.className = 'match-modal-overlay';
  overlay.onclick = e => { if (e.target === overlay) overlay.remove(); };

  const listing = _currentListings.find(l => l.id === listingId && l.source === source);
  const refTitle = listing ? (listing.title || '').substring(0, 60) : listingId;
  const refDetails = listing ? `${listing.district || ''} · ${listing.bedrooms ?? '?'} bed · ${listing.area_sqm ? Math.round(listing.area_sqm) + ' m²' : '?'} · ${listing.price_eur ? fmt(listing.price_eur) : '?'}` : '';

  overlay.innerHTML = `<div class="match-modal">
    <button class="match-close" onclick="this.closest('.match-modal-overlay').remove()">&times;</button>
    <h3>Match: ${esc(refTitle)}</h3>
    <div class="match-ref">${esc(source)} #${esc(listingId)} &middot; ${esc(refDetails)}</div>
    <div id="match-linked-section"></div>
    <hr style="border-color:#2a3550;margin:12px 0">
    <h4 style="font-size:.85rem;color:#94a3b8;margin-bottom:8px">Candidate matches from other sources</h4>
    <div id="match-candidates"><div class="match-loading">Finding similar listings...</div></div>
  </div>`;
  document.body.appendChild(overlay);

  // Show existing linked listings if any
  if (listing && listing.group_id) {
    try {
      const linkedRes = await fetch('/api/listings?' + new URLSearchParams({limit: '50'}));
      const allListings = await linkedRes.json();
      const grouped = allListings.filter(l => l.group_id === listing.group_id && !(l.id === listingId && l.source === source));
      if (grouped.length > 0) {
        document.getElementById('match-linked-section').innerHTML = '<h4 style="font-size:.85rem;color:#a78bfa;margin-bottom:8px">Currently linked</h4>' +
          grouped.map(c => `<div class="match-candidate match-linked">
            ${c.image_url ? '<img src="'+esc(c.image_url)+'" onerror="this.outerHTML=\'<div class=mc-placeholder>&#127968;</div>\'">' : '<div class="mc-placeholder">&#127968;</div>'}
            <div class="mc-info">
              <div class="mc-title">${esc(c.title)}</div>
              <div class="mc-details">${c.price_eur ? fmt(c.price_eur) : '—'} · ${c.bedrooms ?? '?'} bed · ${c.area_sqm ? Math.round(c.area_sqm)+' m²' : '?'} · ${esc(c.district)}${c.area ? ' / '+esc(c.area) : ''}</div>
              <span class="mc-source">${esc(c.source)}</span>
              ${c.developer_name ? ' <span style="font-size:.7rem;color:#059669">&#127959; '+esc(c.developer_name)+'</span>' : ''}
              ${c.latitude ? ' <span style="font-size:.7rem;color:#22d3ee">&#128205; GPS</span>' : ''}
            </div>
            <button class="mc-unlink-btn" onclick="unlinkListing('${esc(c.id)}','${esc(c.source)}',this)">Unlink</button>
          </div>`).join('');
      }
    } catch(e) { console.error('Error loading linked listings', e); }
  }

  // Fetch candidates
  try {
    const res = await fetch(`/api/match-candidates?listing_id=${encodeURIComponent(listingId)}&source=${encodeURIComponent(source)}`);
    const candidates = await res.json();
    const container = document.getElementById('match-candidates');
    if (!container) return;
    if (!candidates.length) {
      container.innerHTML = '<div class="match-empty">No matching listings found in other sources</div>';
      return;
    }
    container.innerHTML = candidates.map(c => `<div class="match-candidate">
      ${c.image_url ? '<img src="'+esc(c.image_url)+'" onerror="this.outerHTML=\'<div class=mc-placeholder>&#127968;</div>\'">' : '<div class="mc-placeholder">&#127968;</div>'}
      <div class="mc-info">
        <div class="mc-title">${esc(c.title)}</div>
        <div class="mc-details">${c.price_eur ? fmt(c.price_eur) : '—'} · ${c.bedrooms ?? '?'} bed · ${c.area_sqm ? Math.round(c.area_sqm)+' m²' : '?'} · ${esc(c.district)}${c.area ? ' / '+esc(c.area) : ''}</div>
        <span class="mc-source">${esc(c.source)}</span>
        ${c.developer_name ? ' <span style="font-size:.7rem;color:#059669">&#127959; '+esc(c.developer_name)+'</span>' : ''}
        ${c.latitude ? ' <span style="font-size:.7rem;color:#22d3ee">&#128205; GPS</span>' : ''}
        ${c.url ? ' <a href="'+esc(c.url)+'" target="_blank" style="font-size:.7rem">open &#8599;</a>' : ''}
      </div>
      <div style="text-align:right">
        <div class="mc-score">${c.match_score}%</div>
        <button class="mc-link-btn" onclick="linkListings('${esc(listingId)}','${esc(source)}','${esc(c.id)}','${esc(c.source)}',this)">Link</button>
      </div>
    </div>`).join('');
  } catch(e) {
    const container = document.getElementById('match-candidates');
    if (container) container.innerHTML = '<div class="match-empty">Error loading candidates</div>';
  }
}

async function linkListings(id1, src1, id2, src2, btn) {
  btn.disabled = true;
  btn.textContent = 'Linking...';
  try {
    const res = await fetch('/api/link', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({listings: [{id: id1, source: src1}, {id: id2, source: src2}]})
    });
    const data = await res.json();
    if (data.ok) {
      btn.textContent = 'Linked ✓';
      btn.style.background = '#059669';
      // Refresh listings
      applyFilters();
    } else {
      btn.textContent = 'Error';
      btn.disabled = false;
    }
  } catch(e) {
    btn.textContent = 'Error';
    btn.disabled = false;
  }
}

async function unlinkListing(id, source, btn) {
  btn.disabled = true;
  btn.textContent = 'Unlinking...';
  try {
    await fetch('/api/unlink', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({listing_id: id, source})
    });
    btn.closest('.match-candidate').remove();
    applyFilters();
  } catch(e) {
    btn.textContent = 'Error';
    btn.disabled = false;
  }
}

// ── Runner ───────────────────────────────────────────────────────────────────
async function startScrape() {
  const sources = [];
  if (document.getElementById('src-bazaraki').checked) sources.push('bazaraki');
  if (document.getElementById('src-domcy').checked) sources.push('dom_cy');
  if (document.getElementById('src-sothebys').checked) sources.push('sothebys');
  const districts = [...document.querySelectorAll('.dist-cb:checked')].map(c => c.value);
  const bedrooms = [...document.querySelectorAll('.beds-cb:checked')].map(c => c.value);
  const maxPages = document.getElementById('max-pages').value || '5';

  if (!sources.length) { alert('Select at least one source'); return; }
  if (!districts.length) { alert('Select at least one district'); return; }

  for (const src of sources) {
    const res = await fetch('/api/scrape', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ source: src, districts, bedrooms, max_pages: maxPages })
    });
    const data = await res.json();
    if (data.job_id) selectJob(data.job_id);
  }
  loadJobs();
}

async function startExport() {
  const res = await fetch('/api/export', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      district: document.getElementById('exp-district').value,
      bedrooms: document.getElementById('exp-beds').value,
      max_price: document.getElementById('exp-price').value,
      excluded_areas: document.getElementById('exp-excluded').value,
      excluded_conditions: document.getElementById('exp-conditions').value,
      favorites_only: document.getElementById('exp-favorites-only').checked,
    })
  });
  const data = await res.json();
  if (data.job_id) selectJob(data.job_id);
  loadJobs();
}

async function startCheckExpired() {
  const sources = [];
  if (document.getElementById('exp-chk-bazaraki').checked) sources.push('bazaraki');
  if (document.getElementById('exp-chk-domcy').checked) sources.push('dom_cy');
  if (document.getElementById('exp-chk-sothebys').checked) sources.push('sothebys');
  if (!sources.length) { alert('Select at least one source'); return; }
  const res = await fetch('/api/check-expired', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      sources,
      only_unchecked: document.getElementById('exp-chk-unchecked').checked,
    })
  });
  const data = await res.json();
  if (data.job_id) selectJob(data.job_id);
  loadJobs();
}

async function startBackfillCoords() {
  const sources = [];
  if (document.getElementById('coords-sothebys').checked) sources.push('sothebys');
  if (document.getElementById('coords-bazaraki').checked) sources.push('bazaraki');
  if (!sources.length) { alert('Select at least one source'); return; }
  for (const src of sources) {
    const res = await fetch('/api/backfill-coords', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ source: src })
    });
    const data = await res.json();
    if (data.job_id) selectJob(data.job_id);
  }
  loadJobs();
}

let selectedJobId = null;
let logOffset = 0;
let logPollTimer = null;

function selectJob(jobId) {
  selectedJobId = jobId;
  logOffset = 0;
  document.getElementById('log-viewer').style.display = 'block';
  document.getElementById('log-viewer').textContent = '';
  pollLogs();
}

async function pollLogs() {
  if (!selectedJobId) return;
  try {
    const res = await fetch(`/api/logs/${selectedJobId}?offset=${logOffset}`);
    const data = await res.json();
    if (data.lines && data.lines.length > 0) {
      const viewer = document.getElementById('log-viewer');
      viewer.textContent += data.lines;
      logOffset = data.new_offset;
      viewer.scrollTop = viewer.scrollHeight;
    }
    if (data.status === 'running') {
      logPollTimer = setTimeout(pollLogs, 1500);
    } else {
      loadJobs();
    }
  } catch(e) { logPollTimer = setTimeout(pollLogs, 3000); }
}

async function loadJobs() {
  try {
    const res = await fetch('/api/jobs');
    const data = await res.json();
    const container = document.getElementById('jobs-list');
    if (!data.length) { container.innerHTML = '<div style="color:#64748b;font-size:.82rem">No jobs yet</div>'; return; }
    container.innerHTML = data.map(j => `<div class="job-item">
      <div class="job-info">
        <div class="job-name">${esc(j.name)}</div>
        <div class="job-meta">${j.started_at} &middot; ID: ${j.id}</div>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <span class="badge badge-${j.status}">${j.status}</span>
        <button class="btn btn-primary" style="padding:4px 10px;font-size:.75rem" onclick="selectJob('${j.id}')">Logs</button>
        ${j.status === 'running' ? '<button class="btn btn-danger" style="padding:4px 10px;font-size:.75rem" onclick="stopJob(\''+j.id+'\')">Stop</button>' : ''}
      </div>
    </div>`).join('');
  } catch(e) {}
}

async function stopJob(jobId) {
  await fetch(`/api/jobs/${jobId}/stop`, { method: 'POST' });
  loadJobs();
}

// ── Init ─────────────────────────────────────────────────────────────────────
(async () => {
  await _doApply();
  // After first load, set defaults (selects are now populated)
  const distEl = document.getElementById('f-district');
  if (distEl) { distEl.value = 'Larnaca'; }
  const bedsEl = document.getElementById('f-beds');
  if (bedsEl) { bedsEl.value = '3'; }
  // Re-apply with defaults active
  await _doApply();
  loadJobs();
  setInterval(loadJobs, 10000);
})();
</script>
</body>
</html>"""


# ── HTTP Handler ─────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Quieter logging
        pass

    def _send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html, status=200):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        params = parse_qs(parsed.query)

        if path == "/":
            self._send_html(DASHBOARD_HTML)

        elif path == "/api/stats":
            self._send_json(get_stats(params if params else None))

        elif path == "/api/listings":
            self._send_json(get_listings(params))

        elif path == "/api/favorites":
            favorites = _get_favorites()
            self._send_json([{"listing_id": f[0], "source": f[1]} for f in favorites])

        elif path == "/api/comments":
            self._send_json(_get_comments())

        elif path == "/api/jobs":
            with jobs_lock:
                job_list = [{
                    "id": j["id"], "name": j["name"], "status": j["status"],
                    "started_at": j["started_at"], "return_code": j["return_code"],
                } for j in reversed(list(jobs.values()))]
            self._send_json(job_list)

        elif path == "/api/match-candidates":
            listing_id = params.get("listing_id", [None])[0]
            source = params.get("source", [None])[0]
            if not listing_id or not source:
                self._send_json({"error": "listing_id and source required"}, 400)
                return
            candidates = _get_match_candidates(listing_id, source)
            # Return compact candidate info
            result = []
            for c in candidates:
                result.append({
                    "id": str(c.get("id", "")),
                    "source": c.get("source", ""),
                    "title": c.get("title", ""),
                    "price_eur": c.get("price_eur"),
                    "bedrooms": c.get("bedrooms"),
                    "area_sqm": c.get("area_sqm"),
                    "district": c.get("district", ""),
                    "area": c.get("area", ""),
                    "image_url": c.get("image_url", ""),
                    "url": c.get("url", ""),
                    "match_score": c.get("match_score", 0),
                    "developer_name": c.get("developer_name", ""),
                    "project_name": c.get("project_name", ""),
                    "latitude": c.get("latitude"),
                    "longitude": c.get("longitude"),
                })
            self._send_json(result)

        elif path.startswith("/api/logs/"):
            job_id = path.split("/")[-1]
            offset = int(params.get("offset", [0])[0])
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                self._send_json({"error": "Job not found"}, 404)
                return
            try:
                with open(job["log_file"], "r") as f:
                    f.seek(offset)
                    lines = f.read()
                    new_offset = f.tell()
                self._send_json({
                    "lines": lines, "new_offset": new_offset,
                    "status": job["status"],
                })
            except FileNotFoundError:
                self._send_json({"lines": "", "new_offset": 0, "status": job["status"]})

        else:
            self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/favorite":
            body = self._read_body()
            lid = body.get("listing_id", "")
            src = body.get("source", "")
            conn = sqlite3.connect(USER_DATA_DB)
            exists = conn.execute(
                "SELECT 1 FROM favorites WHERE listing_id=? AND source=?", (lid, src)
            ).fetchone()
            if exists:
                conn.execute("DELETE FROM favorites WHERE listing_id=? AND source=?", (lid, src))
            else:
                conn.execute(
                    "INSERT INTO favorites (listing_id, source, saved_at) VALUES (?,?,?)",
                    (lid, src, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
                )
            conn.commit()
            conn.close()
            self._send_json({"ok": True, "is_favorite": not exists})

        elif path == "/api/exclude":
            body = self._read_body()
            lid = body.get("listing_id", "")
            src = body.get("source", "")
            conn = sqlite3.connect(USER_DATA_DB)
            exists = conn.execute(
                "SELECT 1 FROM excluded WHERE listing_id=? AND source=?", (lid, src)
            ).fetchone()
            if exists:
                conn.execute("DELETE FROM excluded WHERE listing_id=? AND source=?", (lid, src))
            else:
                conn.execute(
                    "INSERT INTO excluded (listing_id, source, excluded_at) VALUES (?,?,?)",
                    (lid, src, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
                )
            conn.commit()
            conn.close()
            self._send_json({"ok": True, "is_excluded": not exists})

        elif path == "/api/comment":
            body = self._read_body()
            lid = body.get("listing_id", "")
            src = body.get("source", "")
            comment = body.get("comment", "")
            conn = sqlite3.connect(USER_DATA_DB)
            if comment.strip():
                conn.execute(
                    "INSERT INTO comments (listing_id, source, comment, updated_at) VALUES (?,?,?,?) "
                    "ON CONFLICT(listing_id, source) DO UPDATE SET comment=excluded.comment, updated_at=excluded.updated_at",
                    (lid, src, comment, time.strftime("%Y-%m-%dT%H:%M:%SZ")),
                )
            else:
                conn.execute("DELETE FROM comments WHERE listing_id=? AND source=?", (lid, src))
            conn.commit()
            conn.close()
            self._send_json({"ok": True})

        elif path == "/api/link":
            body = self._read_body()
            items = body.get("listings", [])
            if len(items) < 2:
                self._send_json({"error": "Need at least 2 listings to link"}, 400)
                return

            conn = sqlite3.connect(USER_DATA_DB)
            # Check if any listing is already in a group
            existing_group = None
            for item in items:
                row = conn.execute(
                    "SELECT group_id FROM linked_listings WHERE listing_id=? AND source=?",
                    (str(item["id"]), item["source"])
                ).fetchone()
                if row:
                    existing_group = row[0]
                    break

            if existing_group is None:
                # Create new group: max+1
                max_row = conn.execute("SELECT MAX(group_id) FROM linked_listings").fetchone()
                existing_group = (max_row[0] or 0) + 1

            # Insert all listings into the group
            for item in items:
                conn.execute(
                    "INSERT OR REPLACE INTO linked_listings (group_id, listing_id, source, linked_at) VALUES (?,?,?,?)",
                    (existing_group, str(item["id"]), item["source"], time.strftime("%Y-%m-%dT%H:%M:%SZ"))
                )
            conn.commit()
            conn.close()

            # Auto-enrich
            _enrich_linked_group(existing_group)
            self._send_json({"ok": True, "group_id": existing_group})

        elif path == "/api/unlink":
            body = self._read_body()
            lid = str(body.get("listing_id", ""))
            src = body.get("source", "")
            conn = sqlite3.connect(USER_DATA_DB)
            row = conn.execute(
                "SELECT group_id FROM linked_listings WHERE listing_id=? AND source=?",
                (lid, src)
            ).fetchone()
            if not row:
                conn.close()
                self._send_json({"ok": True})
                return
            group_id = row[0]
            conn.execute(
                "DELETE FROM linked_listings WHERE listing_id=? AND source=?", (lid, src)
            )
            # If group has only 1 member left, delete it
            remaining = conn.execute(
                "SELECT COUNT(*) FROM linked_listings WHERE group_id=?", (group_id,)
            ).fetchone()[0]
            if remaining <= 1:
                conn.execute("DELETE FROM linked_listings WHERE group_id=?", (group_id,))
            conn.commit()
            conn.close()
            self._send_json({"ok": True})

        elif path == "/api/scrape":
            body = self._read_body()
            source = body.get("source", "bazaraki")
            districts = body.get("districts", ["Larnaca"])
            bedrooms = body.get("bedrooms", [])
            max_pages = body.get("max_pages", "5")

            python = str(VENV_PYTHON)
            if source == "dom_cy":
                script = str(BASE / "dom_cy" / "scraper.py")
            elif source == "sothebys":
                script = str(BASE / "sothebys" / "scraper.py")
            else:
                script = str(BASE / "bazaraki" / "scraper.py")

            cmd = [python, script, str(max_pages)]
            if districts:
                cmd += ["--districts", ",".join(districts)]
            if bedrooms:
                cmd += ["--bedrooms", ",".join(str(b) for b in bedrooms)]

            name = f"Scrape {source} [{','.join(districts)}]"
            job_id = start_job(name, cmd)
            self._send_json({"job_id": job_id, "name": name})

        elif path == "/api/export":
            body = self._read_body()
            python = str(VENV_PYTHON)
            script = str(BASE / "export_larnaca.py")
            cmd = [python, script]
            favorites_only = body.get("favorites_only", False)
            if favorites_only:
                cmd.append("--favorites-only")
                name = "Export favorites to Telegram"
            else:
                name = "Export to Telegram"
            job_id = start_job(name, cmd)
            self._send_json({"job_id": job_id, "name": name})

        elif path == "/api/check-expired":
            body = self._read_body()
            sources = body.get("sources", ["bazaraki", "dom_cy"])
            only_unchecked = body.get("only_unchecked", True)
            python = str(VENV_PYTHON)
            script = str(BASE / "check_expired.py")
            # Run per source so each is a separate job
            last_job_id = None
            for src in sources:
                cmd = [python, script, "--source", src]
                if only_unchecked:
                    cmd.append("--only-unchecked")
                name = f"Check expired ({src})"
                last_job_id = start_job(name, cmd)
            self._send_json({"job_id": last_job_id, "name": f"Check expired [{','.join(sources)}]"})

        elif path == "/api/backfill-coords":
            body = self._read_body()
            source = body.get("source", "sothebys")
            python = str(VENV_PYTHON)
            script = str(BASE / "backfill_coords.py")
            cmd = [python, script, "--source", source]
            name = f"Backfill coords ({source})"
            job_id = start_job(name, cmd)
            self._send_json({"job_id": job_id, "name": name})

        elif path.startswith("/api/jobs/") and path.endswith("/stop"):
            job_id = path.split("/")[-2]
            with jobs_lock:
                job = jobs.get(job_id)
            if not job:
                self._send_json({"error": "Job not found"}, 404)
                return
            proc = job.get("process")
            if proc and proc.poll() is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                except (ProcessLookupError, PermissionError):
                    try:
                        proc.terminate()
                    except Exception:
                        pass
                with jobs_lock:
                    jobs[job_id]["status"] = "stopped"
            self._send_json({"ok": True})

        else:
            self.send_error(404)


def main():
    port = 8080
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--port" and i < len(sys.argv) - 1:
            port = int(sys.argv[i + 1])

    _init_user_data_db()
    _ensure_image_url_column()
    _ensure_coord_columns()

    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"  Dashboard: http://localhost:{port}")
    print(f"  Press Ctrl+C to stop\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.server_close()


if __name__ == "__main__":
    main()
