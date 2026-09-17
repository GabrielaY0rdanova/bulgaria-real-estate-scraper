# url_builder.py

# Module for constructing listing URLs for imot.bg.


from config import BASE_URL, TRANSACTION_TYPES


def build_listings_url(
    transaction_type: str,
    slug: str,
    page: int | None = None,
    property_type: str | None = None,
    price_min: int | None = None,
    price_max: int | None = None,
) -> str:
    """
    Build the full URL for a real estate listings page.

    Supports three levels of the cascade strategy:
      Level 1 — region only:          /prodazhbi/grad-sofiya
      Level 2 — region + prop type:   /prodazhbi/grad-sofiya/dvustaen
      Level 3 — region + prop type
               + price range:         /prodazhbi/grad-sofiya/dvustaen?price_min=0&price_max=100000

    Args:
        transaction_type: 'prodazhbi' or 'naemi'
        slug:             region slug, e.g. 'oblast-blagoevgrad'
        page:             page number (1-based); page 1 has no /p-N suffix
        property_type:    property type slug, e.g. 'dvustaen' (optional)
        price_min:        minimum price in EUR (optional, requires price_max)
        price_max:        maximum price in EUR (optional, requires price_min)

    Returns:
        Full URL string
    """
    if transaction_type not in TRANSACTION_TYPES:
        raise ValueError(f"Unsupported transaction type: {transaction_type}")
    if not isinstance(slug, str) or not slug.strip():
        raise ValueError("slug must be a non-empty string")
    if page is not None and page < 1:
        raise ValueError("page must be 1 or greater")
    if (price_min is None) != (price_max is None):
        raise ValueError("price_min and price_max must be provided together")
    if price_min is not None and (price_min < 0 or price_max < price_min):
        raise ValueError("price range must be non-negative and ordered")

    # Build path: base / transaction_type / slug [/ property_type] [/ p-N]
    parts = [BASE_URL.rstrip("/"), transaction_type, slug]

    if property_type:
        parts.append(property_type)

    url = "/".join(parts)

    if page is not None and page > 1:
        url += f"/p-{page}"

    # Append price range as query params if both bounds are provided
    if price_min is not None and price_max is not None:
        url += f"?price_min={price_min}&price_max={price_max}"

    return url
