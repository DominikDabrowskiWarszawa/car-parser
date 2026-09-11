"""
Parser dla findcar.pl.

Strona ma zupełnie inny model danych niż otomoto:
- tytuł oferty to sklejone "MarkaModel" bez spacji (np. "LexusES 300h
  Prestige") - programowo bezużyteczne do wyciągnięcia marki/modelu,
  dlatego bierzemy je z parametrów zapytania (`makes=`, `models=`)
  strony wyszukiwania, a jako fallback ze slug-a w URL-u oferty
  (np. /oferty-dealerow/lexus-es-nowy-2026-...).
- linia ze skrzynią/mocą/rokiem też bywa sklejona (np. "Automat201 KM2026").

Zamiast zgadywać klasy CSS (te są renderowane przez frameworki typu
Next.js i mogą być hashowane / zmieniać się między buildami), parser
kotwiczy się o coś stabilnego: URL oferty zawsze pasuje do wzorca
`/oferty-dealerow/<slug>`. Dla każdego takiego linku szukamy najmniejszego
wspólnego kontenera (przodka), który obejmuje TYLKO tę jedną ofertę
(czyli: idziemy w górę drzewa DOM, dopóki rodzic nie zawiera więcej niż
jednego takiego linku) i z tego kontenera wyciągamy cenę / rok / itd.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from .base import Offer, SiteParser
from utils.images import extract_all_image_urls, extract_image_url
from utils.text import clean_text, format_model_name, parse_price, parse_year, slug_to_name

_OFFER_HREF_RE = re.compile(r"/oferty-dealerow/([a-z0-9-]+)")
_YEAR_AFTER_KM_RE = re.compile(r"KM\D{0,3}(\d{4})")
_PRICE_RE = re.compile(r"([\d][\d\s\u00A0]{3,})\s*zł")
_GALLERY_SELECTOR = '[class*="gallery"] img, [class*="Gallery"] img, [class*="carousel"] img'
# Prawdziwe zdjęcia aut na findcar są serwowane przez własny proxy pod tym
# wzorcem URL-a - ikony (typ paliwa, itp.) mają zupełnie inną ścieżkę i są
# plikami .svg, więc ten wzorzec pozwala je jednoznacznie odróżnić.
_PHOTO_SRC_RE = re.compile(r"/thumb\?src=", re.IGNORECASE)


class FindCarParser(SiteParser):
    domains = {"findcar.pl"}

    def is_listing_url(self, url: str) -> bool:
        return "/oferty-dealerow/" not in urlparse(url).path

    # ------------------------------------------------------------------ #
    # LISTING (np. /znajdz-samochod?makes=lexus&models=es)
    # ------------------------------------------------------------------ #
    def parse_listing(self, html: str, url: str) -> list[Offer]:
        soup = BeautifulSoup(html, "html.parser")
        brand_model = self._brand_model_from_query(url)

        offers: list[Offer] = []
        seen_hrefs: set[str] = set()
        idx = 0

        # Zdjęcia ofert (proxy /thumb?src=...) pojawiają się w tej samej
        # kolejności co karty ofert, ale nie zawsze mają wspólnego przodka
        # z linkiem tytułowym w drzewie DOM (layouty CSS grid bywają płaskie).
        # Dlatego parujemy je po POZYCJI, zamiast szukać "kontenera" oferty -
        # to odporniejsze niż branie pierwszego <img> z kontenera (który
        # potrafi trafić na ikonę typu paliwa zamiast prawdziwego zdjęcia).
        photo_imgs = soup.find_all("img", src=_PHOTO_SRC_RE)

        for anchor in soup.find_all("a", href=_OFFER_HREF_RE):
            href = anchor["href"]
            if href in seen_hrefs:
                continue
            seen_hrefs.add(href)

            offer_url = href if href.startswith("http") else f"https://findcar.pl{href}"
            title = clean_text(anchor.get_text())

            container = self._single_offer_container(anchor)
            container_text = container.get_text(" ", strip=True)

            price = self._extract_price(container_text)
            year = self._extract_year(container_text)

            if idx < len(photo_imgs):
                image = extract_image_url(photo_imgs[idx], url)
            else:
                # fallback: szukaj w kontenerze, ale pomiń oczywiste ikony (.svg)
                image = self._pick_non_icon_image(container, url)

            bm = dict(brand_model)
            if not bm.get("brand") or not bm.get("model"):
                bm = self._brand_model_from_slug(href) or bm

            offers.append(
                Offer(
                    url=f"{url}#offer-{idx}",
                    title=title,
                    price=price,
                    year=year,
                    brand=bm.get("brand"),
                    model=bm.get("model"),
                    image=image,
                    extra={"source_offer_url": offer_url},
                )
            )
            idx += 1

        return offers

    # ------------------------------------------------------------------ #
    # POJEDYNCZA OFERTA (np. /oferty-dealerow/lexus-es-nowy-2026-...)
    # ------------------------------------------------------------------ #
    def parse_single(self, html: str, url: str) -> Offer:
        soup = BeautifulSoup(html, "html.parser")

        title_el = soup.select_one("h1")
        title = clean_text(title_el.get_text()) if title_el else None

        body_text = soup.get_text(" ", strip=True)
        price = self._extract_price(body_text)
        year = self._extract_year(body_text) or parse_year(url)

        bm = self._brand_model_from_slug(url) or {}

        # Najpierw próbujemy tego samego, rozpoznawalnego wzorca proxy co w listingu.
        photo_imgs = soup.find_all("img", src=_PHOTO_SRC_RE)
        images = extract_all_image_urls(photo_imgs, url)

        if not images:
            gallery_imgs = soup.select(_GALLERY_SELECTOR)
            images = extract_all_image_urls(
                [img for img in gallery_imgs if not (img.get("src") or "").lower().endswith(".svg")],
                url,
            )

        if not images:
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
            **bm,
        )

    # ------------------------------------------------------------------ #
    @staticmethod
    def _single_offer_container(anchor):
        """Idzie w górę drzewa DOM aż trafi na węzeł obejmujący >1 ofertę."""
        node = anchor
        for _ in range(8):
            parent = node.parent
            if parent is None:
                break
            offer_links = parent.find_all("a", href=_OFFER_HREF_RE)
            if len(offer_links) > 1:
                return node
            node = parent
        return node

    @staticmethod
    def _pick_non_icon_image(container, base_url: str) -> str | None:
        """Fallback: pierwszy <img> w kontenerze, który nie wygląda na ikonę (.svg)."""
        for img in container.find_all("img"):
            src = (img.get("src") or img.get("data-src") or "").lower()
            if src.endswith(".svg"):
                continue
            image = extract_image_url(img, base_url)
            if image:
                return image
        return None

    @staticmethod
    def _extract_price(text: str) -> int | None:
        match = _PRICE_RE.search(text)
        return parse_price(match.group(0)) if match else None

    @staticmethod
    def _extract_year(text: str) -> int | None:
        match = _YEAR_AFTER_KM_RE.search(text)
        if match:
            return int(match.group(1))
        return parse_year(text)

    @staticmethod
    def _brand_model_from_query(url: str) -> dict:
        """Strona wyszukiwania trzyma markę/model w query stringu: ?makes=lexus&models=es."""
        qs = parse_qs(urlparse(url).query)
        brand_slug = (qs.get("makes") or [None])[0]
        model_slug = (qs.get("models") or [None])[0]
        return {
            "brand": slug_to_name(brand_slug) if brand_slug else None,
            "model": format_model_name(model_slug) if model_slug else None,
        }

    @staticmethod
    def _brand_model_from_slug(href: str) -> dict | None:
        """
        Fallback: parsuje slug oferty, np.
        'lexus-es-nowy-2026-hybryda-czarny-...' -> brand='Lexus', model='ES'.
        Zakładamy, że pierwsze dwa segmenty to marka i model - to działa dla
        większości marek jednoczłonowych, ale np. dla 'mercedes-benz-c-klasa-...'
        może dać błędny wynik. Traktować jako fallback, nie główne źródło.
        """
        match = _OFFER_HREF_RE.search(href)
        if not match:
            return None
        segments = match.group(1).split("-")
        if len(segments) < 2:
            return None
        return {"brand": slug_to_name(segments[0]), "model": format_model_name(segments[1])}
