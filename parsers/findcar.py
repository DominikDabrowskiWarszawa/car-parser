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
from urllib.parse import parse_qs, unquote, urlparse

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
# Marki dwuczłonowe (myślnik w slugu) - używane jako fallback przy zgadywaniu
# marki/modelu ze slug-a oferty, gdy nie mamy podpowiedzi z parametru `makes=`
# (patrz _brand_model_from_slug). Lista niepełna - w razie potrzeby dopisz
# kolejne marki dwuczłonowe, które napotkasz w danych.
_KNOWN_MULTI_WORD_BRAND_SLUGS = {
    "mercedes-benz", "land-rover", "alfa-romeo", "aston-martin", "rolls-royce",
}
# Segmenty sluga, które sygnalizują KONIEC nazwy modelu (a nie są jej częścią) -
# status oferty, paliwo, napęd itp. Gdy trafimy na jeden z nich (albo na
# 4-cyfrowy rok), przestajemy zbierać kolejne segmenty jako model. Dzięki
# temu modele wieloczłonowe (np. "seria-1", "klasa-c") są łapane w całości,
# zamiast urywać się na pierwszym segmencie.
_MODEL_STOP_WORDS = {
    "nowy", "nowe", "uzywany", "uzywana", "uzywane",
    "benzyna", "diesel", "hybryda", "hybrydowy", "elektryczny",
    "lpg", "phev", "hev", "mhev",
}
_YEAR_SEGMENT_RE = re.compile(r"^\d{4}$")
_MAX_MODEL_SEGMENTS = 3


def _extract_model_segments(segments: list[str]) -> list[str]:
    """Zbiera kolejne segmenty jako nazwę modelu, dopóki nie trafi na stop-słowo/rok."""
    model_segments: list[str] = []
    for seg in segments:
        if seg in _MODEL_STOP_WORDS or _YEAR_SEGMENT_RE.match(seg):
            break
        model_segments.append(seg)
        if len(model_segments) >= _MAX_MODEL_SEGMENTS:
            break
    return model_segments


def _resolve_findcar_image(image_url: str | None) -> str | None:
    """
    findcar serwuje zdjęcia przez własny proxy resize'ujący (`/thumb?src=<encoded-url>`),
    który zwraca 403 Forbidden przy żądaniach spoza przeglądarki (najpewniej wymaga
    nagłówka Referer / ochrona przed hotlinkingiem). Realny, bezpośredni URL zdjęcia
    jest jednak zakodowany w parametrze `src` tego samego linku - dekodujemy go i
    zwracamy link bezpośrednio do CDN-a hostującego oryginalne zdjęcie, zamiast do
    proxy findcar.
    """
    if not image_url:
        return image_url
    parsed = urlparse(image_url)
    if "/thumb" not in parsed.path:
        return image_url
    qs = parse_qs(parsed.query)
    direct = (qs.get("src") or [None])[0]
    return unquote(direct) if direct else image_url


def _resolve_findcar_images(image_urls: list[str]) -> list[str]:
    resolved = []
    seen = set()
    for u in image_urls:
        direct = _resolve_findcar_image(u)
        if direct and direct not in seen:
            seen.add(direct)
            resolved.append(direct)
    return resolved


def _known_brand_slugs_from_query(url: str) -> set[str]:
    """Wyciąga zbiór slugów marek z parametru ?makes=... (może być ich kilka, po przecinku)."""
    qs = parse_qs(urlparse(url).query)
    makes_raw = (qs.get("makes") or [None])[0]
    if not makes_raw:
        return set()
    return {m.strip() for m in makes_raw.split(",") if m.strip()}


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
        known_brand_slugs = _known_brand_slugs_from_query(url) | _KNOWN_MULTI_WORD_BRAND_SLUGS

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
                image = _resolve_findcar_image(extract_image_url(photo_imgs[idx], url))
            else:
                # fallback: szukaj w kontenerze, ale pomiń oczywiste ikony (.svg)
                image = _resolve_findcar_image(self._pick_non_icon_image(container, url))

            bm = dict(brand_model)
            if not bm.get("brand") or not bm.get("model"):
                bm = self._brand_model_from_slug(href, known_brand_slugs) or bm

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

        bm = self._brand_model_from_slug(url, _KNOWN_MULTI_WORD_BRAND_SLUGS) or {}

        # Najpierw próbujemy tego samego, rozpoznawalnego wzorca proxy co w listingu.
        photo_imgs = soup.find_all("img", src=_PHOTO_SRC_RE)
        images = _resolve_findcar_images(extract_all_image_urls(photo_imgs, url))

        if not images:
            gallery_imgs = soup.select(_GALLERY_SELECTOR)
            images = _resolve_findcar_images(
                extract_all_image_urls(
                    [img for img in gallery_imgs if not (img.get("src") or "").lower().endswith(".svg")],
                    url,
                )
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
        """
        Strona wyszukiwania trzyma markę/model w query stringu: ?makes=lexus&models=es.

        UWAGA: przy szerszych wyszukiwaniach (?makes=lexus,audi,bmw,...) wartość
        zawiera wiele marek naraz, oddzielonych przecinkiem - w takim wypadku NIE
        da się przypisać jednej marki/modelu do wszystkich ofert, więc celowo
        zwracamy None, co wymusza użycie fallbacku _brand_model_from_slug()
        (per-oferta, na podstawie jej własnego URL-a).
        """
        qs = parse_qs(urlparse(url).query)
        brand_slug = (qs.get("makes") or [None])[0]
        model_slug = (qs.get("models") or [None])[0]

        if brand_slug and "," in brand_slug:
            brand_slug = None
        if model_slug and "," in model_slug:
            model_slug = None

        return {
            "brand": slug_to_name(brand_slug) if brand_slug else None,
            "model": format_model_name(model_slug) if model_slug else None,
        }

    @staticmethod
    def _brand_model_from_slug(href: str, known_brand_slugs: set[str] | None = None) -> dict | None:
        """
        Fallback: parsuje slug oferty, np.
        'bmw-seria-1-uzywany-2023-benzyna-...' -> brand='BMW', model='Seria 1'.

        1. Dopasowuje markę: jeśli podano `known_brand_slugs`, próbuje NAJDŁUŻSZEGO
           pasującego prefiksu (np. 'mercedes-benz' zamiast tylko 'mercedes');
           bez dopasowania zakłada markę jednosegmentową (pierwszy segment).
        2. Dla modelu zbiera KOLEJNE segmenty po marce, dopóki nie trafi na
           stop-słowo (status oferty, paliwo) albo 4-cyfrowy rok - dzięki temu
           modele wieloczłonowe (np. "Seria 1", "Klasa C") są łapane w całości,
           a nie urywane na pierwszym słowie.
        """
        match = _OFFER_HREF_RE.search(href)
        if not match:
            return None
        segments = match.group(1).split("-")
        if len(segments) < 2:
            return None

        brand_slug = segments[0]
        brand_len = 1
        if known_brand_slugs:
            for candidate in sorted(known_brand_slugs, key=len, reverse=True):
                candidate_segments = candidate.split("-")
                n = len(candidate_segments)
                if segments[:n] == candidate_segments:
                    brand_slug = candidate
                    brand_len = n
                    break

        model_segments = _extract_model_segments(segments[brand_len:])
        model = format_model_name("-".join(model_segments)) if model_segments else None
        return {"brand": slug_to_name(brand_slug), "model": model}
