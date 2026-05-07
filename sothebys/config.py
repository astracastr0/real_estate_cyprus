"""Configuration constants for Cyprus Sotheby's Realty scraper."""

BASE_URL = "https://www.cyprus-sothebysrealty.com"

# City mappings: display name -> (URL slug, search_city param)
CITIES = {
    "Larnaca": ("larnaca", "Larnaca"),
    "Limassol": ("limassol", "Limassol"),
    "Paphos": ("pafos", "Pafos"),
}

# Property type mappings: internal key -> search_type param
PROPERTY_TYPES = {
    "apartment": "Apartment",
    "house": "House",
}

# Build all search queries (cartesian product of cities x types)
QUERIES = []
for city_name, (city_slug, search_city) in CITIES.items():
    for prop_key, search_type in PROPERTY_TYPES.items():
        QUERIES.append({
            "district": city_name,
            "property_type": prop_key,
            "city_slug": city_slug,
            "search_city": search_city,
            "search_type": search_type,
        })

# --- Scraping limits ---
MAX_PAGES_PER_QUERY = 10
DELAY_BETWEEN_PAGES = (1, 2)
DELAY_BETWEEN_DETAILS = (1, 2)

# --- HTTP settings ---
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Database
DB_PATH = "sothebys.db"
