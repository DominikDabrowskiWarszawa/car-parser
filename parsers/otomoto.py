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
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

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
# Segmenty ścieżki URL, które NIE są nazwą modelu, mimo że siedzą na tej
# samej pozycji co model w typowym /osobowe/<marka>/<model> - najczęściej
# to filtr statusu (nowe/uzywane) albo filtr rocznika (od-2023, do-2023),
# używany przy szerszych wyszukiwaniach typu "wszystkie Lexusy od 2023".
_NON_MODEL_SEGMENT_RE = re.compile(r"^(nowe|uzywane|od-\d{4}|do-\d{4})$")
# ---------------------------------------------------------------------------


def _guess_model_from_title(title: str | None, brand: str | None) -> str | None:
    """
    Fallback, gdy w URL-u wyszukiwania nie ma konkretnego modelu (np.
    "/osobowe/lexus/od-2023" przeszukuje WSZYSTKIE modele Lexusa naraz).
    Zgaduje model z tytułu konkretnej oferty, np. "Lexus NX 350h Prestige
    AWD" -> "NX", pomijając na początku tyle słów, ile ma sama nazwa marki
    (żeby poprawnie obsłużyć marki dwuczłonowe jak "Land Rover").

    Modele wieloczłonowe (np. "Seria 1", "Klasa C") są też obsłużone: jeśli
    drugie słowo po marce jest krótkie (<=2 znaki - liczba albo pojedyncza
    litera), doklejamy je do modelu.

    To uproszczona heurystyka - dla nietypowych tytułów może się mylić, ale
    jest lepsza niż zostawienie modelu pustym.
    """
    if not title:
        return None
    tokens = title.split()
    if not tokens:
        return None

    if brand:
        brand_tokens = brand.split()
        n = len(brand_tokens)
        if len(tokens) > n and all(
            tokens[i].lower() == brand_tokens[i].lower() for i in range(n)
        ):
            remaining = tokens[n:]
        else:
            remaining = tokens[1:] if len(tokens) > 1 else []
    else:
        remaining = tokens[1:] if len(tokens) > 1 else []

    if not remaining:
        return None

    model_tokens = [remaining[0]]
    if len(remaining) > 1 and len(remaining[1]) <= 2 and remaining[1].isalnum():
        model_tokens.append(remaining[1])

    return " ".join(model_tokens)


class OtomotoParser(SiteParser):
    domains = {"otomoto.pl"}

    def is_listing_url(self, url: str) -> bool:
        path = urlparse(url).path.rstrip("/")
        # pojedyncza oferta zawsze ma w ścieżce "/oferta/" i kończy się na -ID....html
        return "/oferta/" not in path

    # ------------------------------------------------------------------ #
    # LISTING (np. /osobowe/nowe/toyota/rav4, /osobowe/bmw/x3/od-2024)
    # ------------------------------------------------------------------ #
    def parse_listing(self, html: str, url: str) -> list[Offer]:
        soup = BeautifulSoup(html, "html.parser")

        json_ld_offers = self._offers_from_json_ld(soup, url)
        if json_ld_offers:
            return json_ld_offers

        return self._offers_from_cards(soup, url)

    def _offers_from_json_ld(self, soup: BeautifulSoup, url: str) -> list[Offer]:
        offers: list[Offer] = []
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

                bm = self._brand_model_from_url(url)
                if not bm.get("model"):
                    bm["model"] = _guess_model_from_title(clean_text(name), bm.get("brand"))

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

            brand_model = self._brand_model_from_url(url)
            if not brand_model.get("model"):
                brand_model["model"] = _guess_model_from_title(title, brand_model.get("brand"))

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

        return Offer(
            url=url,
            title=title,
            price=price,
            year=year,
            image=images[0] if images else None,
            extra={"images": images} if len(images) > 1 else {},
            **self._brand_model_from_url_with_title_fallback(url, title),
        )

    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    def _brand_model_from_url_with_title_fallback(self, url: str, title: str | None) -> dict:
        bm = self._brand_model_from_url(url)
        if not bm.get("model"):
            bm["model"] = _guess_model_from_title(title, bm.get("brand"))
        return bm

    @staticmethod
    def _brand_model_from_url(url: str) -> dict:
        """
        otomoto trzyma markę/model w samej ścieżce URL, np.:
          /osobowe/nowe/toyota/rav4
          /osobowe/bmw/x3/od-2024
        Format: /osobowe/[nowe|uzywane/]<marka>/<model>[/...]

        UWAGA: przy szerszych wyszukiwaniach (np. /osobowe/lexus/od-2023 -
        wszystkie modele Lexusa nowsze niż 2023) w ścieżce w ogóle nie ma
        segmentu modelu - jest tylko filtr roku (`od-2023` / `do-2023`).
        Odróżniamy to od nazwy modelu wzorcem `_NON_MODEL_SEGMENT_RE` poniżej,
        żeby nie wpisać np. "OD 2023" jako model.
        """
        path_parts = [p for p in urlparse(url).path.split("/") if p]
        try:
            start = path_parts.index("osobowe") + 1
        except ValueError:
            return {"brand": None, "model": None}

        remaining = [p for p in path_parts[start:] if not _NON_MODEL_SEGMENT_RE.match(p)]

        brand = slug_to_name(remaining[0]) if len(remaining) > 0 else None
        model = format_model_name(remaining[1]) if len(remaining) > 1 else None
        return {"brand": brand, "model": model}
