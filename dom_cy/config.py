"""Configuration constants for dom.com.cy real-estate scraper."""

BASE_URL = "https://dom.com.cy"

# City mappings: display name -> URL slug
CITIES = {
    "Limassol": "city-limassol",
    "Larnaca": "city-larnaca",
    "Paphos": "city-paphos",
}

# Property type mappings: display name -> URL slug
PROPERTY_TYPES = {
    "apartment": "type-apartment",
    "house": "type-house",
}

# Build all search query URLs
QUERIES = []
for city_name, city_slug in CITIES.items():
    for prop_type, type_slug in PROPERTY_TYPES.items():
        QUERIES.append({
            "district": city_name,
            "property_type": prop_type,
            "path": f"/en/catalog/sale/{city_slug}/{type_slug}/",
        })

# --- CSS Selectors ---

# Search page: cards are <a> elements linking to /en/catalog/sale/{id}/
# The site wraps each listing in a link; we match all detail-page links
SEL_LISTING_LINK = 'a[href*="/en/catalog/sale/"]'

# Detail page — characteristics are <strong>Label:</strong> Value pairs
# We extract the JS object `arCatalogElementResult` instead (more reliable)

# --- Scraping limits ---
MAX_PAGES_PER_QUERY = 10       # ~15-20 listings per page
DELAY_BETWEEN_DETAILS = (2, 3)  # seconds (min, max)
DELAY_BETWEEN_PAGES = (3, 5)    # seconds (min, max)
CLOUDFLARE_TIMEOUT = 30         # seconds to wait for challenge

# --- Browser settings ---
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

# Database
DB_PATH = "dom_cy.db"
