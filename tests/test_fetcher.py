from types import SimpleNamespace

import requests

from scraper import fetcher


def _response(status_code=200, html="", url=None):
    response = SimpleNamespace(status_code=status_code, text=html, encoding=None)
    if url is not None:
        response.url = url
    return response


def _large_html(marker):
    return f"<html><body>{marker}{'x' * fetcher.MIN_PAGE_SIZE}</body></html>"


def test_content_validation_recognises_expected_page_structure():
    assert fetcher._is_valid_content('<div id="ida123"></div>', "listings") is True
    assert fetcher._is_valid_content('<div class="SearchInfoLine">0 обяви</div>', "listings") is True
    assert fetcher._is_valid_content('<div id="other"></div>', "listings") is False
    assert fetcher._is_valid_content('<div class="adPrice"></div>', "detail") is True
    assert fetcher._is_valid_content('<div class="price"></div>', "detail") is False
    assert fetcher._is_valid_content("anything", None) is True
    assert fetcher._is_valid_content("anything", "unknown") is True


def test_successful_fetch_returns_html_and_applies_expected_settings(monkeypatch):
    html = _large_html('<div id="ida123"></div>')
    response = _response(html=html)
    calls = []
    sleeps = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return response

    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    monkeypatch.setattr(fetcher.time, "sleep", sleeps.append)

    result = fetcher.fetch_page("https://example.test/listings", page_type="listings")

    assert result == html
    assert calls == [("https://example.test/listings", fetcher.HEADERS, 15)]
    assert response.encoding == "windows-1251"
    assert sleeps == [fetcher.REQUEST_DELAY]


def test_403_and_404_stop_without_retrying(monkeypatch):
    calls = []

    def fake_get(url, headers, timeout):
        calls.append(url)
        return _response(status_code=403 if "forbidden" in url else 404)

    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)

    assert fetcher.fetch_page("https://example.test/forbidden") is None
    assert fetcher.fetch_page("https://example.test/missing") is None
    assert calls == ["https://example.test/forbidden", "https://example.test/missing"]


def test_server_error_is_retried_and_can_recover(monkeypatch):
    valid_html = _large_html('<div class="adPrice"></div>')
    responses = iter([_response(status_code=503), _response(html=valid_html)])
    sleeps = []

    monkeypatch.setattr(fetcher, "MAX_RETRIES", 2)
    monkeypatch.setattr(fetcher.requests, "get", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(fetcher.time, "sleep", sleeps.append)

    assert fetcher.fetch_page("https://example.test/detail", page_type="detail") == valid_html
    assert sleeps == [fetcher.RETRY_DELAY, fetcher.REQUEST_DELAY]


def test_small_page_is_retried_until_attempts_are_exhausted(monkeypatch):
    calls = []
    sleeps = []

    def fake_get(*_args, **_kwargs):
        calls.append(True)
        return _response(html="too small")

    monkeypatch.setattr(fetcher, "MAX_RETRIES", 2)
    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    monkeypatch.setattr(fetcher.time, "sleep", sleeps.append)

    assert fetcher.fetch_page("https://example.test/small") is None
    assert len(calls) == 2
    assert sleeps == [fetcher.RETRY_DELAY, fetcher.RETRY_DELAY]


def test_soft_block_after_partial_page_stops_immediately(monkeypatch):
    invalid_html = _large_html("<div>no listings here</div>")
    calls = []
    sleeps = []

    def fake_get(*_args, **_kwargs):
        calls.append(True)
        return _response(html=invalid_html)

    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    monkeypatch.setattr(fetcher.time, "sleep", sleeps.append)

    result = fetcher.fetch_page(
        "https://example.test/listings",
        page_type="listings",
        last_page_was_partial=True,
    )

    assert result is None
    assert len(calls) == 1
    assert sleeps == []


def test_removed_detail_redirect_stops_without_soft_block_retry(monkeypatch):
    html = _large_html('<div class="SearchInfoLine">listings</div>')
    response = _response(
        html=html,
        url="https://www.imot.bg/obiavi/prodazhbi/ednostaen",
    )
    calls = []
    sleeps = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return response

    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    monkeypatch.setattr(fetcher.time, "sleep", sleeps.append)

    result = fetcher.fetch_page(
        "https://www.imot.bg/obiava-removed-listing",
        page_type="detail",
    )

    assert result is None
    assert len(calls) == 1
    assert sleeps == []


def test_listings_404_is_treated_as_empty_page(monkeypatch):
    calls = []
    sleeps = []

    def fake_get(url, headers, timeout):
        calls.append((url, headers, timeout))
        return _response(status_code=404)

    monkeypatch.setattr(fetcher.requests, "get", fake_get)
    monkeypatch.setattr(fetcher.time, "sleep", sleeps.append)

    result = fetcher.fetch_page(
        "https://www.imot.bg/obiavi/naemi/grad-burgas/partsel",
        page_type="listings",
    )

    assert result == ""
    assert len(calls) == 1
    assert sleeps == []


def test_detail_404_remains_a_failed_refresh(monkeypatch):
    monkeypatch.setattr(
        fetcher.requests,
        "get",
        lambda url, headers, timeout: _response(status_code=404),
    )

    result = fetcher.fetch_page(
        "https://www.imot.bg/obiava-missing-listing",
        page_type="detail",
    )

    assert result is None


def test_timeout_is_retried_then_returns_none(monkeypatch):
    calls = []
    sleeps = []

    def raise_timeout(*_args, **_kwargs):
        calls.append(True)
        raise requests.exceptions.Timeout

    monkeypatch.setattr(fetcher, "MAX_RETRIES", 2)
    monkeypatch.setattr(fetcher.requests, "get", raise_timeout)
    monkeypatch.setattr(fetcher.time, "sleep", sleeps.append)

    assert fetcher.fetch_page("https://example.test/timeout") is None
    assert len(calls) == 2
    assert sleeps == [fetcher.RETRY_DELAY, fetcher.RETRY_DELAY]


def test_reusable_connection_uses_session_without_reapplying_headers(monkeypatch):
    html = _large_html('<div id="ida123"></div>')
    calls = []

    class FakeSession:
        def get(self, url, timeout):
            calls.append((url, timeout))
            return _response(html=html)

    monkeypatch.setattr(fetcher, "_get_session", lambda: FakeSession())
    monkeypatch.setattr(fetcher.requests, "get", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("requests.get should not be used")
    ))
    monkeypatch.setattr(fetcher.time, "sleep", lambda _seconds: None)

    assert fetcher.fetch_page(
        "https://example.test/listings",
        page_type="listings",
        reuse_connection=True,
    ) == html
    assert calls == [("https://example.test/listings", 15)]
