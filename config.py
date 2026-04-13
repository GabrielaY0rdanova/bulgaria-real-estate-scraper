# config.py

# --- Target ---
BASE_URL = "https://www.imot.bg/obiavi/"
TRANSACTION_TYPES = ["prodazhbi", "naemi"]

# --- Pagination ---
MAX_PAGES = 1000          # how many pages to scrape in one run
START_PAGE = 1

# --- Politeness ---
REQUEST_DELAY = 1       # seconds between requests
RETRY_DELAY = REQUEST_DELAY * 2   # seconds to wait before retrying a failed page
MAX_RETRIES = 2         # how many times to retry a failed page before skipping
SOFT_BLOCK_DELAY = 30   # seconds to wait when a soft-block is detected

# --- Headers ---
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.8",
}

# --- Output ---
OUTPUT_DIR = "data"
LOG_DIR = "logs"

# --- Cascade scraping ---
# If a slug (or slug+property_type) returns exactly this many listings,
# we assume the cap was hit and we need to split further.
CAP_THRESHOLD = 1000

# All property type slugs as they appear in imot.bg URLs.
# Used as the second dimension when a region hits the cap.
PROPERTY_TYPES = [
    "ednostaen",
    "dvustaen",
    "tristaen",
    "chetiristaen",
    "mnogostaen",
    "mezonet",
    "atelie-tavan",
    "ofis",
    "magazin",
    "zavedenie",
    "sklad",
    "hotel",
    "promishleno-pomeshtenie",
    "biznes-imot",
    "etazh-ot-kashta",
    "kashta",
    "vila",
    "partsel",
    "garazh-parkomyasto",
    "zemedelska-zemya",
]

# Price range for binary search fallback (EUR).
# Covers the full realistic range on imot.bg.

# Prodazhbi (sales) price range in EUR
PRODAZHBI_PRICE_MIN = 0
PRODAZHBI_PRICE_MAX = 10_000_000

# Naemi (rentals) price range in EUR
NAEMI_PRICE_MIN = 0
NAEMI_PRICE_MAX = 10_000