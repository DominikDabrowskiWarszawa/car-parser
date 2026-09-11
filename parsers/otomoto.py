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
# ---------------------------------------------------------------------------


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

                offers.append(
                    Offer(
                        url=f"{url}#offer-{idx}",
                        title=clean_text(name),
                        price=price,
                        year=parse_year(name),
                        image=image,
                        **self._brand_model_from_url(url),
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
            **self._brand_model_from_url(url),
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _brand_model_from_url(url: str) -> dict:
        """
        otomoto trzyma markę/model w samej ścieżce URL, np.:
          /osobowe/nowe/toyota/rav4
          /osobowe/bmw/x3/od-2024
        Format: /osobowe/[nowe|uzywane/]<marka>/<model>[/...]
        """
        path_parts = [p for p in urlparse(url).path.split("/") if p]
        try:
            start = path_parts.index("osobowe") + 1
        except ValueError:
            return {"brand": None, "model": None}

        remaining = path_parts[start:]
        if remaining and remaining[0] in ("nowe", "uzywane"):
            remaining = remaining[1:]

        brand = slug_to_name(remaining[0]) if len(remaining) > 0 else None
        model = format_model_name(remaining[1]) if len(remaining) > 1 else None
        return {"brand": brand, "model": model}
