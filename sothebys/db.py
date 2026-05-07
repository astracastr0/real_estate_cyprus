"""SQLite database helpers for Sotheby's listings."""

import sqlite3
from pathlib import Path

from config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id TEXT PRIMARY KEY,
    url TEXT,
    title TEXT,
    price_eur INTEGER,
    price_original TEXT,
    price_per_sqm REAL,
    property_type TEXT,
    district TEXT,
    area TEXT,
    location_full TEXT,
    bedrooms INTEGER,
    bathrooms INTEGER,
    area_sqm REAL,
    plot_sqm REAL,
    condition TEXT,
    construction_year INTEGER,
    energy_efficiency TEXT,
    parking TEXT,
    furnishing TEXT,
    has_pool BOOLEAN,
    included TEXT,
    posted_date TEXT,
    scraped_at TEXT,
    image_url TEXT,
    latitude REAL,
    longitude REAL,
    UNIQUE(id)
);
"""

COLUMNS = [
    "id", "url", "title", "price_eur", "price_original", "price_per_sqm",
    "property_type", "district", "area", "location_full",
    "bedrooms", "bathrooms", "area_sqm", "plot_sqm",
    "condition", "construction_year", "energy_efficiency", "parking",
    "furnishing", "has_pool", "included", "posted_date", "scraped_at",
    "image_url", "latitude", "longitude",
]


def get_db_path() -> Path:
    return Path(__file__).parent / DB_PATH


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(get_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_connection()
    conn.executescript(SCHEMA)
    conn.close()


def listing_exists(listing_id: str) -> bool:
    conn = get_connection()
    row = conn.execute("SELECT 1 FROM listings WHERE id = ?", (listing_id,)).fetchone()
    conn.close()
    return row is not None


def upsert_listing(data: dict):
    conn = get_connection()
    filtered = {k: data.get(k) for k in COLUMNS}
    placeholders = ", ".join(["?"] * len(COLUMNS))
    col_names = ", ".join(COLUMNS)
    updates = ", ".join(f"{c} = excluded.{c}" for c in COLUMNS if c != "id")

    conn.execute(
        f"INSERT INTO listings ({col_names}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        [filtered.get(c) for c in COLUMNS],
    )
    conn.commit()
    conn.close()


def count_listings() -> int:
    conn = get_connection()
    row = conn.execute("SELECT COUNT(*) FROM listings").fetchone()
    conn.close()
    return row[0]


def query_df(sql: str, params=()):
    """Execute SQL and return results as list of dicts."""
    conn = get_connection()
    cursor = conn.execute(sql, params)
    cols = [d[0] for d in cursor.description]
    rows = [dict(zip(cols, row)) for row in cursor.fetchall()]
    conn.close()
    return rows
