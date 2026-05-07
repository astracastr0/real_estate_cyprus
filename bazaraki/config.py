"""Configuration constants for Bazaraki real-estate scraper."""

BASE_URL = "https://www.bazaraki.com"

# District mappings: display name -> URL slug
DISTRICTS = {
    "Limassol": "lemesos-district-limassol",
    "Larnaca": "larnaka-district-larnaca",
    "Paphos": "pafos-district-paphos",
}

# Property type mappings: display name -> URL slug
PROPERTY_TYPES = {
    "apartment": "apartments-flats",
    "house": "houses",
}

# Build all search query URLs
QUERIES = []
for district_name, district_slug in DISTRICTS.items():
    for prop_type, prop_slug in PROPERTY_TYPES.items():
        QUERIES.append({
            "district": district_name,
            "property_type": prop_type,
            "path": f"/real-estate-for-sale/{prop_slug}/{district_slug}/",
        })

# --- CSS Selectors ---

# Search page
SEL_LISTING_CARD = ".advert.js-item-listing"
SEL_CARD_LINK = 'a[href*="/adv/"]'
SEL_CARD_PRICE = ".advert__content-price"
SEL_CARD_TITLE = ".advert__content-title"
SEL_CARD_LOCATION = ".advert__content-place"
SEL_CARD_FEATURE = ".advert__content-feature"

# Detail page
SEL_DETAIL_TITLE = ".title-announcement"
SEL_DETAIL_PRICE = ".announcement-price__cost"
SEL_DETAIL_PRICE_PER_SQM = ".announcement-price__per-meter"
SEL_DETAIL_LOCATION = ".announcement__location"
SEL_DETAIL_CHARS_ROW = ".chars-column li"
SEL_DETAIL_CHAR_KEY = ".key-chars"
SEL_DETAIL_CHAR_VALUE = ".value-chars"
SEL_DETAIL_DATE = ".date-meta"
SEL_DETAIL_AD_ID = '.number-announcement span[itemprop="sku"]'

# --- Scraping limits ---
MAX_PAGES_PER_QUERY = 5        # ~300 listings per query at 60/page
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
DB_PATH = "bazaraki.db"
