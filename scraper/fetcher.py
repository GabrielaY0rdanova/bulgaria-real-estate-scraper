# fetcher.py

import requests
import time
import logging

from bs4 import BeautifulSoup

from config import HEADERS, REQUEST_DELAY, RETRY_DELAY, MAX_RETRIES, SOFT_BLOCK_DELAY

logger = logging.getLogger(__name__)

MIN_PAGE_SIZE = 5000    # bytes — anything smaller is definitely not a real page


def _is_valid_content(html: str, page_type: str | None) -> bool:
    """
    Check whether the fetched HTML looks like the expected page type.

    Structural signals used:
      "listings" — expects at least one <div id="ida..."> (a listing card)
      "detail"   — expects <div class="adPrice"> (the price/date block)
      None       — no content check; return True immediately

    A captcha or bot-detection page will be missing these elements even
    if it returns HTTP 200 and is large enough to pass the MIN_PAGE_SIZE check.

    Args:
        html:      decoded HTML string
        page_type: "listings", "detail", or None

    Returns:
        True if the page looks valid, False if it looks like a block page
    """
    if page_type is None:
        return True

    soup = BeautifulSoup(html, "html.parser")

    if page_type == "listings":
        # At least one listing card must be present
        found = soup.find("div", id=lambda x: x and x.startswith("ida"))
        return found is not None

    if page_type == "detail":
        # Price/date block must be present
        found = soup.select_one("div.adPrice")
        return found is not None

    return True


def fetch_page(url: str, page_type: str | None = None, last_page_was_partial: bool = False) -> str | None:
    """
    Fetch a single page and return its HTML content.
    Returns HTML string if successful, None if all retries failed.

    Soft-block detection is content-based: after a 200 OK the HTML is
    inspected for the structural elements expected for the given page type.
    If those elements are missing the page is treated as a soft-block and
    retried after SOFT_BLOCK_DELAY seconds.

    Args:
        url:                  URL to fetch
        page_type:            "listings" — check for listing card divs (id starts with "ida")
                              "detail"   — check for div.adPrice
                              None       — skip content check (e.g. unknown page type)
        last_page_was_partial: if True and a soft-block fires, return None immediately
                              instead of retrying. Used when the previous page had fewer
                              than 40 listings — soft-block is almost certainly end-of-results,
                              not a real block. Saves 3 × 90s of unnecessary waiting.
    """
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"Fetching: {url} (attempt {attempt}/{MAX_RETRIES})")

            response = requests.get(url, headers=HEADERS, timeout=15)

            # Hard block
            if response.status_code == 403:
                logger.warning(f"403 Forbidden — possibly blocked. URL: {url}")
                return None

            # Page not found
            if response.status_code == 404:
                logger.warning(f"404 Not Found — skipping. URL: {url}")
                return None

            # Server error — worth retrying
            if response.status_code == 503:
                logger.warning(f"503 Server unavailable (attempt {attempt}/{MAX_RETRIES}). Waiting {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
                continue

            # Unexpected status code
            if response.status_code != 200:
                logger.warning(f"Unexpected status {response.status_code} (attempt {attempt}/{MAX_RETRIES}). Waiting {RETRY_DELAY}s...")
                time.sleep(RETRY_DELAY)
                continue

            # Force Windows-1251 encoding before any .text access
            response.encoding = "windows-1251"

            # Sanity floor — anything this small is definitely not a real page
            if len(response.text) < MIN_PAGE_SIZE:
                logger.warning(
                    f"Page too small ({len(response.text)} bytes) — "
                    f"possible captcha or empty page (attempt {attempt}/{MAX_RETRIES})."
                )
                time.sleep(RETRY_DELAY)
                continue

            # Content-based soft-block detection
            if not _is_valid_content(response.text, page_type):
                if last_page_was_partial:
                    logger.info(
                        f"Soft-block on page after partial page ({len(response.text)} bytes) — "
                        f"treating as end of results, not retrying."
                    )
                    return None
                logger.warning(
                    f"Soft-block suspected: expected {page_type!r} content not found "
                    f"({len(response.text)} bytes). "
                    f"Waiting {SOFT_BLOCK_DELAY}s before retry (attempt {attempt}/{MAX_RETRIES})."
                )
                time.sleep(SOFT_BLOCK_DELAY)
                continue

            # All good
            logger.info(f"Successfully fetched {url} ({len(response.text)} bytes)")
            time.sleep(REQUEST_DELAY)
            return response.text

        except requests.exceptions.ConnectionError:
            logger.error(f"Connection error (attempt {attempt}/{MAX_RETRIES}). Waiting {RETRY_DELAY}s...")
            time.sleep(RETRY_DELAY)

        except requests.exceptions.Timeout:
            logger.error(f"Request timed out (attempt {attempt}/{MAX_RETRIES}). Waiting {RETRY_DELAY}s...")
            time.sleep(RETRY_DELAY)

        except requests.exceptions.RequestException as e:
            logger.error(f"Unexpected request error: {e}")
            return None

    logger.error(f"All {MAX_RETRIES} attempts failed for URL: {url}")
    return None
