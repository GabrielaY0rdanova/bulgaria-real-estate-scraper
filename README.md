# Bulgaria Real Estate Scraper

## Project Badges

[![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white)](https://www.python.org/)
[![BeautifulSoup](https://img.shields.io/badge/BeautifulSoup-4-green?logo=python&logoColor=white)](https://www.crummy.com/software/BeautifulSoup/)
[![Kaggle](https://img.shields.io/badge/Kaggle-Dataset-orange?logo=kaggle&logoColor=white)](https://www.kaggle.com/datasets/gabrielagencheva/bulgaria-real-estate-listings)
[![License](https://img.shields.io/badge/License-MIT-lightgrey)](LICENSE.txt)


## Overview

A modular Python scraper for [imot.bg](https://www.imot.bg), Bulgaria's largest real estate portal.
Collects property listings across all 54 Bulgarian regions (27 administrative regions + 27 major cities) for both sales and rentals,
enriching each listing with detail-page data including construction type, year built, utilities, and features.
Outputs flat CSV files ready for downstream cleaning and analysis.

Part of a larger **Real Estate Data Platform**: [`real_estate_scraper`](https://github.com/GabrielaY0rdanova/bulgaria-real-estate-scraper) to [`real_estate_cleaning`](https://github.com/GabrielaY0rdanova/bulgaria-real-estate-cleaning) to [`real_estate_analysis`](https://github.com/GabrielaY0rdanova/bulgaria-real-estate-analysis) to `real_estate_visualization`


## Dataset

| File | Rows | Transaction type | Run |
|---|---|---|---|
| `prodazhbi_06_04_2026.csv` | 160,886 | Sales | Full scrape |
| `naemi_10_04_2026.csv` | 38,610 | Rentals | Full scrape |
| `prodazhbi_05_05_2026.csv` | 45,186 | Sales | Incremental update |
| `naemi_07_05_2026.csv` | 19,038 | Rentals | Incremental update |

These files document the first two published collection periods. Later raw runs
remain append-only in Kaggle, while the PostgreSQL history reached 351,188 unique
listings after the September 2026 catch-up and rental recovery updates.

The dataset is published on Kaggle: [Bulgaria Real Estate Listings](https://www.kaggle.com/datasets/gabrielagencheva/bulgaria-real-estate-listings)


## Project Structure

```
real_estate_scraper/
│
├── main.py                     # Full scraper: cascade logic, CSV output, resume
├── config.py                   # All scraping parameters, headers, price ranges
├── regions.py                  # 54 Bulgarian regions (slug + name)
├── requirements.txt
├── LICENSE.txt
├── README.md
├── .gitignore
│
├── data/                       # CSV output (gitignored)
├── logs/                       # Log files (gitignored)
│
├── monitoring/
│   ├── log_watcher.py          # Telegram notification bot (full scraper)
│   └── time_estimates.xlsx     # Per-region ETAs
│
├── tests/
│   ├── test_detail_parser.py
│   └── test_url_builder.py
│
├── scraper/
│   ├── fetcher.py              # HTTP requests, retry logic, soft-block detection
│   ├── parser.py               # Index page parsing
│   ├── detail_parser.py        # Detail page parsing
│   ├── url_builder.py          # URL construction for all cascade levels
│   ├── validator.py            # Field validation and sanity checks
│   └── progress.py             # Resume state (read/write progress.json)
│
└── light_scraper/
    ├── main.py                 # Incremental scraper: two-pass architecture
    ├── comparator.py           # Classifies listings: new / changed / unchanged / reappeared / missing
    ├── db.py                   # PostgreSQL queries for active/inactive listing lookups
    ├── progress.py             # Resume state for light scraper runs
    └── log_watcher.py          # Telegram notification bot (light scraper)
```


## Architecture

### Two-Layer Scraping

Each listing requires two HTTP requests: one to the index page and one to the detail page:

| Layer | Source | Fields collected |
|---|---|---|
| Layer 1 | Index page | `source_id`, `listing_tier`, `listing_url`, `property_type`, `locality`, `locality_type`, `area`, `price`, `area_m2`, `floor`, `has_photos`, `poster_type`, `agency_name`, `agency_phone` |
| Layer 2 | Detail page | `construction_type`, `construction_status`, `year_built`, `gas`, `tec`, `features`, `date_posted`, `date_modified` |

### Cascade Cap Handling

imot.bg caps search results at 1,000 listings per URL. The scraper handles this automatically with a three-level cascade:

```
Level 1: /prodazhbi/oblast-sofiya
           ↓ if capped (1000+ results)
Level 2: /prodazhbi/oblast-sofiya/dvustaen      (split by property type)
           ↓ if still capped
Level 3: /prodazhbi/oblast-sofiya/dvustaen?price_min=0&price_max=500000
           ↓ binary price split until each bucket is under cap
```

Before scraping any URL, a **pre-flight check** fetches page 1 and inspects the result count: determining which cascade level to start at and avoiding duplicate scraping entirely.

### Resume Support

After each page is saved, `progress.json` records the current position (transaction type, region, property type, price bucket, page number). On restart, the scraper picks up exactly where it left off: no data is re-scraped or lost.

### Two-Scraper Architecture

The platform uses two complementary scrapers:

| | Full Scraper (`main.py`) | Light Scraper (`light_scraper/main.py`) |
|---|---|---|
| **Purpose** | Initial full dataset collection | Incremental updates after the full scrape |
| **Runtime** | ~84 hours for the original collection | Depends on change volume. The verified rental-only recovery run completed in 4h 16m |
| **Approach** | Scrapes all regions, all pages, all listings | Two-pass: index-only comparison + rolling detail refresh |
| **DB required** | No: writes flat CSV only | Yes: compares against PostgreSQL to detect changes |
| **Output** | Raw CSV files | Run manifest, seen IDs, action files, and deduplicated rows for new, changed, reappeared, and refreshed listings |

**Light scraper: two-pass design:**

- **Pass 1**: scrapes index pages only (no detail fetches), compares each listing against the DB by `source_id` and price, and classifies it as new, changed, unchanged, reappeared, or missing. Only new, changed, and reappeared listings get a detail page fetch.
- **Pass 2**: creates and persists a fixed selection from the oldest 5% of eligible active listings. It excludes listings already handled in Pass 1 and re-fetches detail pages to detect changes that price alone cannot reveal.

Each light-scraper run has an immutable UTC run ID and writes its files to `data/runs/<run_id>/`. Page data reaches durable staging files before the resume checkpoint advances. If Pass 1 is incomplete, Pass 2 and missing-listing updates are blocked.


## Scraping Workflow

1. **Pre-flight check**: fetch page 1, detect whether the result count is capped
2. **Level 1 scrape**: if under cap, scrape the region slug directly
3. **Level 2 split**: if capped, pre-flight each property type; scrape those under cap normally
4. **Level 3 split**: if a property type is still capped, binary-search the price range until each bucket fits
5. **Detail enrichment**: for each listing, visit the detail URL and merge additional fields
6. **Validate & save**: validate required fields, append valid rows to CSV, save progress


## Data Schema

Each row represents one listing at the time of scraping.

| Field | Type | Notes |
|---|---|---|
| `region` | string | Bulgarian administrative region e.g. `"София"`, `"Варна"` (27 unique values) |
| `locality` | string | Settlement name e.g. `"Варна"`, `"Банско"` (2,948 unique values) |
| `locality_type` | string | `"град"`, `"село"`, or raw prefix like `"к.к."`: standardised downstream |
| `area` | string | Sub-area within a city e.g. `"Център"`, `"Малинова долина"`; `None` for oblast slugs |
| `property_type` | string | Raw site value e.g. `"3-СТАЕН"`, `"ПАРЦЕЛ"`, `"КЪЩА"` (46 unique values) |
| `bedrooms` | int | Always `None` at scrape time: extracted from `property_type` downstream |
| `poster_type` | string | `"агенция"` (94%) or `"собственик"` (6%) |
| `agency_name` | string | Agency name if poster is an agency (3,841 unique agencies) |
| `price` | string | Raw e.g. `"89 990 €"`; `None` if price on request |
| `area_m2` | float | Square metres |
| `floor` | string | e.g. `"2 от 5"` (current / total floors); `None` for houses, land, etc. |
| `construction_type` | string | e.g. `"Тухла"`, `"Панел"`, `"Гредоред"`, `"ЕПК"` |
| `construction_status` | string | e.g. `"Въведен в експлоатация"`, `"Ще бъде въведен в експлоатация"` |
| `year_built` | int | 4-digit year (range: 1920–2040) |
| `gas` / `tec` | string | `"ДА"` or `"НЕ"`: standardised downstream |
| `features` | string | Comma-separated list e.g. `"Асансьор, Обзаведен"` |
| `date_posted` | string | Raw string e.g. `"Публикувана в 15:55 на 6 април, 2026 год."`: parsed downstream |
| `date_modified` | string | Raw string e.g. `"Коригирана в 15:58 на 25 март, 2026 год."`: parsed downstream |
| `has_photos` | bool | Whether listing has photos |
| `agency_phone` | string | Populated for agencies only (GDPR: private individual phones never collected) |
| `listing_url` | string | Full URL to the listing (192,007 unique) |
| `source_id` | string | imot.bg's listing identifier, used as the durable update key |
| `listing_tier` | string | `"VIP"`, `"TOP"`, or `"BEST"`; `None` for standard listings |
| `transaction_type` | string | `"prodazhbi"` (sales) or `"naemi"` (rentals) |
| `scraped_at` | string | UTC ISO timestamp of when the listing was scraped |
| `status` | string | Always `"active"` at scrape time: updated in `real_estate_cleaning` |


## How to Run

### 1. Clone and set up environment

```bash
git clone https://github.com/GabrielaY0rdanova/bulgaria-real-estate-scraper.git
cd real_estate_scraper
conda create -n re_scraper python=3.11
conda activate re_scraper
pip install -r requirements.txt
```

### 2. Run the scraper

```bash
python main.py
```

Output CSVs are written to `data/`. Logs are written to `logs/`.

### 3. Pause and resume

Stop the scraper at any time (Ctrl+C or machine shutdown) and restart with the same command.
It detects `progress.json` automatically and continues from where it left off: no data is re-scraped or lost.

### 4. Monitoring (optional)

To receive Telegram notifications during a run, create a `.env` file in the project root:

```
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

Then start the watcher in a separate terminal:

```bash
python monitoring/log_watcher.py
```

The watcher sends real-time Telegram messages for each region start/finish, pre-flight results, per-type scraping progress, fetch failures, and a final run summary.


## Running the Light Scraper (Incremental Updates)

Run the light scraper after the cleaning pipeline has loaded data into PostgreSQL.

### 1. Set up database credentials

Add PostgreSQL connection details to your `.env` file:

```
PGHOST=your_host
PGPORT=5432
PGDATABASE=your_database
PGUSER=your_user
PGPASSWORD=your_password
```
### 2. Run the light scraper

```bash
python -m light_scraper.main
```

Run files are written to `data/runs/<run_id>/`. Logs are written to `logs/`.

To run a single transaction type for a targeted recovery, use:

```bash
python -m light_scraper.main --only naemi
```

The excluded transaction receives a valid empty no-op package, so the completed
run remains compatible with the cleaning pipeline. Use this mode only when the
database already contains the other transaction type in its desired state.

The September 2026 rental recovery run used this mode after an earlier incomplete
rental scan. It completed all 54 regions, saved 27,719 Pass 1 rows, refreshed 520
additional listings in Pass 2, recorded no fetch failures and produced a complete
manifest. The cleaning pipeline then applied 28,267 effective actions atomically.

The run is safe for downstream processing only when `manifest.json` has
`status: "complete"`. Missing actions also require
`allow_missing_updates: true` for the relevant transaction.

Each completed light-scraper run contains:

- `manifest.json`, with completion status and counters
- transaction-specific row files
- transaction-specific action files
- complete Pass 1 seen-ID files
- fixed Pass 2 selection files
- append-only staging files used for safe resume

### 3. Pause and resume

Stop at any time and restart with the same command. It detects `light_scraper_progress.json`, verifies that it belongs to the same run, restores durable seen IDs and staging rows, and continues from the last persisted checkpoint.

Do not delete `light_scraper_progress.json` or its matching run directory while a run is incomplete.

### 4. Monitoring (optional)

Start the light scraper watcher in a separate terminal:

```bash
python -m light_scraper.log_watcher
```

The watcher sends real-time Telegram messages for each region, Pass 1 classification results (new / changed / reappeared / unchanged), Pass 2 progress, fetch failures, and a final run summary.


## Configuration

All parameters live in `config.py`:

| Parameter | Default | Notes |
|---|---|---|
| `REQUEST_DELAY` | `1s` | Delay between requests |
| `RETRY_DELAY` | `2s` | Wait before retrying a failed request |
| `MAX_RETRIES` | `2` | Retry attempts before skipping |
| `SOFT_BLOCK_DELAY` | `30s` | Wait when a soft-block is detected |
| `MAX_PAGES` | `1000` | Max pages per URL combination |
| `PRODAZHBI_PRICE_MAX` | `10,000,000` | Upper price bound for sales (EUR) |
| `NAEMI_PRICE_MAX` | `10,000` | Upper price bound for rentals (EUR) |


## Notes

- **`date_modified` unreliable**: imot.bg updates this field on internal re-indexing events, not just user edits. Do not use as a change detection signal.
- **Duplicate sightings**: the full scraper can contain repeated sightings that cleaning deduplicates. The light scraper deduplicates its seen IDs, output rows and effective actions by `source_id` within each transaction run.
- **`area` field contains mixed sub-settlement types**: city-level slugs can produce values like `"м-ст Акчелар"`, `"в.з. Траката"`, `"к.к. Слънчев бряг"` alongside plain neighbourhood names like `"Център"`. Actual settlements misplaced here (`с.`/`гр.` prefix) are reclassified into `locality`/`locality_type` at scrape time. The prefix-based split into `area_name` + `area_type` is handled in `real_estate_cleaning`.
- **`last_page_was_partial` guard**: if imot.bg throttles mid-session and serves an incomplete page, a small number of listings may be missed. Acceptable tradeoff given the site's behaviour.
- **Full re-scrape vs incremental.** The initial full run took about 84 hours. The July light run took about 46 hours across interrupted sessions. A September catch-up run covered both transaction types, followed by a targeted rental recovery that took 4 hours and 16 minutes. Runtime depends mainly on how many new, changed and reappeared listings require detail-page enrichment.
- **Legal & ethics**: `robots.txt` Disallow is empty , Terms of Service contain no scraping prohibition , and a 1-second delay is applied between all requests. Agency phone numbers only: private individual phones are never collected (GDPR).


## Technologies

- **Python 3.11**
- **requests**: HTTP fetching with retry logic
- **BeautifulSoup4**: HTML parsing
- **csv**: incremental, crash-safe CSV output
- **logging**: structured file + console logging
- **python-dotenv**: Telegram credentials management
- **openpyxl**: per-region ETA estimates from Excel


## License

This project is licensed under the [MIT License](LICENSE.txt) and is available for educational and portfolio purposes.
