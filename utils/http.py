"""
Pobieranie stron HTML.

Domyślnie korzysta z `requests`. Część serwisów (np. SPA renderowane
w całości po stronie klienta) może wymagać renderowania JS - do tego
służy `fetch_with_playwright`, który jest opcjonalny (wymaga
`pip install playwright` + `playwright install chromium`).

W praktyce zarówno otomoto.pl, jak i findcar.pl serwują pełny,
wyrenderowany po stronie serwera HTML (SSR), więc zwykłe `requests`
powinno wystarczyć. Jeśli parser nic nie znajduje, a w przeglądarce
oferty są widoczne - to pierwszy trop, żeby przejść na Playwright.
"""

from __future__ import annotations

import time
import logging

import requests

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def fetch_html(url: str, *, timeout: int = 20, retries: int = 3, backoff: float = 1.5) -> str:
    """Pobiera HTML danego URL-a z prostym mechanizmem retry."""
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, headers=DEFAULT_HEADERS, timeout=timeout)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:  # noqa: PERF203
            last_exc = exc
            logger.warning("Próba %s/%s nieudana dla %s: %s", attempt, retries, url, exc)
            if attempt < retries:
                time.sleep(backoff * attempt)
    assert last_exc is not None
    raise last_exc


def fetch_with_playwright(url: str, *, wait_selector: str | None = None, timeout: int = 30000) -> str:
    """
    Fallback dla stron wymagających renderowania JS.

    Wymaga: pip install playwright && playwright install chromium
    Odkomentuj / użyj, jeśli `fetch_html` zwraca puste/niepełne dane.
    """
    from playwright.sync_api import sync_playwright  # import lokalny - zależność opcjonalna

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=DEFAULT_HEADERS["User-Agent"])
        page.goto(url, timeout=timeout, wait_until="networkidle")
        if wait_selector:
            page.wait_for_selector(wait_selector, timeout=timeout)
        html = page.content()
        browser.close()
        return html
