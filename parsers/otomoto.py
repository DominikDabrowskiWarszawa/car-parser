"""
Parser dla otomoto.pl.

Strategia (od najbardziej do najmniej niezawodnej):
1. JSON-LD (<script type="application/ld+json">) - jeśli otomoto go
   udostępnia dla danej podstrony, to najbardziej stabilne źródło
   (nie zależy od nazw klas CSS, które zmieniają się przy redesignach).
2. Selektory CSS oparte o atrybuty `data-testid` - otomoto (grupa OLX)
   konsekwentnie je stosuje w warstwie frontendu, są dużo stabilniejsze
   niż klasy CSS (te bywają hashowane / generowane).
3. Fallback: URL oferty + tekst nagłówka.

WAŻNE: dokładne selektory (`_CARD_SELECTOR` itd.) trzeba zweryfikować
na żywym HTML-u (devtools -> Elements) i w razie potrzeby skorygować -
strona mogła się zmienić od czasu napisania tego kodu. Kod jest napisany
tak, żeby literały selektorów były w jednym miejscu i łatwo je było
podmienić.

MARKA / MODEL - skąd się biorą:
Tytuły ogłoszeń na otomoto to DOWOLNY tekst wpisany przez sprzedającego
(np. "AMG GT R 585 KM | Vossen | Ceramika" albo "1.5 T-GDI Super Hybrid
Prestige" - bez marki na początku!), więc zgadywanie marki/modelu z
tytułu jest z natury zawodne. Dużo bardziej wiarygodny jest slug w URL-u
KONKRETNEJ oferty (np. ".../oferta/toyota-rav4-ID6I8Kuo.html"), bo ten
jest generowany programowo przez samo otomoto z jego wewnętrznej
taksonomii marka/model - niezależnie od tego, co sprzedający wpisał w
tytule. Dlatego markę/model wyciągamy WYŁĄCZNIE z tego sluga (patrz
_brand_model_from_offer_slug), zarówno dla wyszukiwań jednomarkowych, jak
i wielomarkowych (np. /osobowe/audi--bmw--lexus--mercedes-benz/od-2023 -
marki rozdzielone podwójnym myślnikiem).
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from .base import Offer, SiteParser
from utils.images import extract_all_image_urls, extract_image_url
from utils.text import clean_text, format_model_name, parse_price, parse_year, slug_to_name

# --- selektory do weryfikacji / dostrojenia -------------------------------
_CARD_SELECTOR = '[data-testid="listing-ad"], article'
_TITLE_SELECTOR = 'h2 a, [data-testid="ad-title"] a, h1'
_PRICE_SELECTOR = '[data-testid="ad-price"], h3, [class*="price"]'
_PARAMS_SELECTOR = '[data-parameter], dl, [data-testid="parameters-container"]'
_CARD_IMAGE_SELECTOR = "img"
_GALLERY_SELECTOR = (
    '[data-testid="gallery"] img, [data-testid="photo-gallery"] img, '
    '.gallery img, [class*="gallery"] img'
)
# ---------------------------------------------------------------------------

# Segmenty ścieżki URL wyszukiwania, które NIE są marką/modelem, mimo że
# siedzą na tej samej pozycji co one w typowym /osobowe/<marka>/<model> -
# to filtr statusu (nowe/uzywane) albo filtr rocznika (od-2023, do-2023).
_NON_BRAND_SEGMENT_RE = re.compile(r"^(nowe|uzywane|od-\d{4}|do-\d{4})$")

# Slug KONKRETNEJ oferty, np. .../oferta/toyota-rav4-ID6I8Kuo.html
_OFFER_SLUG_RE = re.compile(r"/oferta/([a-z0-9-]+)-ID[0-9A-Za-z]+\.html", re.IGNORECASE)

# Marki dwuczłonowe (myślnik w slugu) - używane jako podpowiedź przy
# dopasowywaniu marki ze slug-a oferty, żeby np. "mercedes-benz-glc-..."
# nie rozjechało się na markę "Mercedes" + model "Benz". Lista niepełna -
# dopisuj kolejne w miarę napotykania błędnych przypadków.
_KNOWN_MULTI_WORD_BRAND_SLUGS = {
    "mercedes-benz", "land-rover", "alfa-romeo", "aston-martin", "rolls-royce",
}
_MAX_MODEL_SEGMENTS = 2

# "68 ogłoszeń" / "19 826 ogłoszeń" - deklarowana przez stronę łączna liczba
# wyników, używana WYŁĄCZNIE do diagnostyki/logowania (porównanie z
# faktycznie zebraną liczbą ofert), nie do sterowania pętlą paginacji.
_TOTAL_COUNT_RE = re.compile(r"([\d\s\u00A0]+)\s*ogłoszeń", re.IGNORECASE)


def _known_brand_slugs_from_search_url(url: str) -> set[str]:
    """
    Wyciąga zbiór slugów marek z URL-a WYSZUKIWANIA (nie pojedynczej oferty), np.:
      /osobowe/lexus/od-2023                              -> {'lexus'}
      /osobowe/audi--bmw--lexus--mercedes-benz/od-2023     -> {'audi','bmw','lexus','mercedes-benz'}

    Marki wielokrotne są rozdzielone PODWÓJNYM myślnikiem "--" (w
    odróżnieniu od pojedynczego myślnika używanego wewnątrz nazwy marki,
    np. "mercedes-benz"). Używane jako podpowiedź przy dopasowywaniu marki
    ze slug-a KONKRETNEJ oferty (patrz _brand_model_from_offer_slug).
    """
    path_parts = [p for p in urlparse(url).path.split("/") if p]
    try:
        start = path_parts.index("osobowe") + 1
    except ValueError:
        return set()

    for seg in path_parts[start:]:
        if _NON_BRAND_SEGMENT_RE.match(seg):
            continue
        if "--" in seg:
            return {s for s in seg.split("--") if s}
        return {seg}
    return set()


def _brand_model_from_offer_slug(offer_url: str | None, known_brand_slugs: set[str]) -> dict:
    """
    Wyciąga markę/model ze sluga KONKRETNEJ oferty, np.:
      .../oferta/toyota-rav4-ID6I8Kuo.html      -> brand='Toyota', model='RAV4'
      .../oferta/mercedes-benz-glc-ID6xYz.html  -> brand='Mercedes-Benz', model='GLC'

    Najpierw próbuje dopasować NAJDŁUŻSZY prefiks z `known_brand_slugs`
    (połączenie podpowiedzi z URL-a wyszukiwania i statycznej listy marek
    dwuczłonowych) - to poprawnie obsługuje marki dwuczłonowe. Bez
    dopasowania zakłada markę jednosegmentową (pierwszy segment sluga).
    """
    match = _OFFER_SLUG_RE.search(offer_url or "")
    if not match:
        return {"brand": None, "model": None}

    segments = match.group(1).split("-")
    if not segments:
        return {"brand": None, "model": None}

    all_known = known_brand_slugs | _KNOWN_MULTI_WORD_BRAND_SLUGS
    brand_slug = segments[0]
    brand_len = 1
    for candidate in sorted(all_known, key=len, reverse=True):
        candidate_segments = candidate.split("-")
        n = len(candidate_segments)
        if segments[:n] == candidate_segments:
            brand_slug = candidate
            brand_len = n
            break

    model_segments = segments[brand_len:brand_len + _MAX_MODEL_SEGMENTS]
    model = format_model_name("-".join(model_segments)) if model_segments else None
    return {"brand": slug_to_name(brand_slug), "model": model}


class OtomotoParser(SiteParser):
    domains = {"otomoto.pl"}

    def is_listing_url(self, url: str) -> bool:
        path = urlparse(url).path.rstrip("/")
        # pojedyncza oferta zawsze ma w ścieżce "/oferta/" i kończy się na -ID....html
        return "/oferta/" not in path

    def get_next_page_url(self, html: str, url: str) -> str | None:
        """
        Zamiast szukać linku "Następna" w HTML-u (patrz uzasadnienie w
        base.py - dopasowanie tekstu bywa kruche i nigdy nie zostało
        zweryfikowane na żywym markupie otomoto), BUDUJEMY URL kolejnej
        strony bezpośrednio: otomoto koduje numer strony jako parametr
        query `page` (potwierdzone np. na /twoje-obserwowane-wyszukiwania?page=2),
        więc wystarczy go odczytać i zinkrementować - reszta parametrów
        (marka, filtry) zostaje bez zmian.

        Nie sprawdzamy tu, czy "kolejna strona istnieje" - main.py i tak
        zatrzymuje pętlę, gdy strona nie zwraca już żadnych ofert.
        """
        parsed = urlparse(url)
        query_pairs = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k != "page"]

        current_page_values = [v for k, v in parse_qsl(parsed.query) if k == "page"]
        current_page = int(current_page_values[0]) if current_page_values else 1

        query_pairs.append(("page", str(current_page + 1)))
        new_query = urlencode(query_pairs)
        return urlunparse(parsed._replace(query=new_query))

    def extract_total_count(self, html: str) -> int | None:
        """Czyta deklarowaną przez stronę liczbę wyników ('X ogłoszeń') - do diagnostyki w logach."""
        match = _TOTAL_COUNT_RE.search(html)
        if not match:
            return None
        digits = re.sub(r"[^\d]", "", match.group(1))
        return int(digits) if digits else None

    # ------------------------------------------------------------------ #
    # LISTING (np. /osobowe/nowe/toyota/rav4, /osobowe/audi--bmw--lexus--mercedes-benz/od-2023)
    # ------------------------------------------------------------------ #
    def parse_listing(self, html: str, url: str) -> list[Offer]:
        soup = BeautifulSoup(html, "html.parser")

        json_ld_offers = self._offers_from_json_ld(soup, url)
        if json_ld_offers:
            return json_ld_offers

        return self._offers_from_cards(soup, url)

    def _offers_from_json_ld(self, soup: BeautifulSoup, url: str) -> list[Offer]:
        offers: list[Offer] = []
        known_brand_slugs = _known_brand_slugs_from_search_url(url)

        for script in soup.find_all("script", {"type": "application/ld+json"}):
            try:
                data = json.loads(script.string or "")
            except (json.JSONDecodeError, TypeError):
                continue
            items = data.get("itemListElement") if isinstance(data, dict) else None
            if not items:
                continue
            for idx, item in enumerate(items):
                product = item.get("item", item) if isinstance(item, dict) else None
                if not product:
                    continue
                offer_url = product.get("url") or url
                name = product.get("name")
                price = None
                offers_block = product.get("offers")
                if isinstance(offers_block, dict):
                    price = parse_price(str(offers_block.get("price", "")))

                image = product.get("image")
                if isinstance(image, list):
                    image = image[0] if image else None

                bm = _brand_model_from_offer_slug(offer_url, known_brand_slugs)

                offers.append(
                    Offer(
                        url=f"{url}#offer-{idx}",
                        title=clean_text(name),
                        price=price,
                        year=parse_year(name),
                        image=image,
                        **bm,
                    )
                )
        return offers

    def _offers_from_cards(self, soup: BeautifulSoup, url: str) -> list[Offer]:
        offers: list[Offer] = []
        cards = soup.select(_CARD_SELECTOR)
        known_brand_slugs = _known_brand_slugs_from_search_url(url)
        idx = 0
        for card in cards:
            title_el = card.select_one(_TITLE_SELECTOR)
            if not title_el or not title_el.get("href"):
                continue  # pomijamy elementy, które nie są realną kartą oferty

            title = clean_text(title_el.get_text())
            offer_url = title_el["href"]
            if offer_url.startswith("/"):
                offer_url = f"https://www.otomoto.pl{offer_url}"

            price_el = card.select_one(_PRICE_SELECTOR)
            price = parse_price(price_el.get_text()) if price_el else None

            year = self._extract_year_from_params(card)

            image_el = card.select_one(_CARD_IMAGE_SELECTOR)
            image = extract_image_url(image_el, url)

            brand_model = _brand_model_from_offer_slug(offer_url, known_brand_slugs)

            offers.append(
                Offer(
                    url=f"{url}#offer-{idx}",
                    title=title,
                    price=price,
                    year=year,
                    image=image,
                    **brand_model,
                    extra={"source_offer_url": offer_url},
                )
            )
            idx += 1
        return offers

    @staticmethod
    def _extract_year_from_params(card) -> int | None:
        """
        Na karcie oferty rok bywa osobnym polem (np. etykieta 'year' + wartość
        obok) albo trzeba go wyłuskać z opisu / podtytułu. Próbujemy obu.
        """
        text = card.get_text(" ", strip=True)
        # szukaj wzorca "year 2026" / "Rok produkcji: 2026"
        match = re.search(r"(?:year|rok(?: produkcji)?)\D{0,5}(\d{4})", text, re.IGNORECASE)
        if match:
            return parse_year(match.group(1))
        return parse_year(text)

    # ------------------------------------------------------------------ #
    # POJEDYNCZA OFERTA (np. .../osobowe/oferta/toyota-rav4-ID6I8Kuo.html)
    # ------------------------------------------------------------------ #
    def parse_single(self, html: str, url: str) -> Offer:
        soup = BeautifulSoup(html, "html.parser")

        title_el = soup.select_one('h1, [data-testid="ad-title"]')
        title = clean_text(title_el.get_text()) if title_el else None

        price_el = soup.select_one('[data-testid="ad-price-container"], [class*="price"] h3, h3')
        price = parse_price(price_el.get_text()) if price_el else None

        params_text = " ".join(
            clean_text(el.get_text()) or "" for el in soup.select(_PARAMS_SELECTOR)
        )
        year = parse_year(params_text) or parse_year(title)

        gallery_imgs = soup.select(_GALLERY_SELECTOR)
        images = extract_all_image_urls(gallery_imgs, url)
        if not images:
            # fallback: pierwszy sensowny <img> na stronie (np. og:image jako ostatnia deska ratunku)
            og_image = soup.select_one('meta[property="og:image"]')
            if og_image and og_image.get("content"):
                images = [og_image["content"]]

        # tu `url` JEST już adresem konkretnej oferty - nie ma potrzeby
        # podpowiedzi z URL-a wyszukiwania, tylko statyczna lista marek dwuczłonowych
        bm = _brand_model_from_offer_slug(url, set())

        return Offer(
            url=url,
            title=title,
            price=price,
            year=year,
            image=images[0] if images else None,
            extra={"images": images} if len(images) > 1 else {},
            **bm,
        )
