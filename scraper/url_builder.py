# url_builder.py

# Module for constructing listing URLs for imot.bg.


from config import BASE_URL


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