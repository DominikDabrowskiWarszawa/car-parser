"""
Ekstrakcja URL-i zdjęć z tagów <img>.

Wiele serwisów (w tym otomoto i findcar) stosuje lazy-loading obrazków:
prawdziwy URL zdjęcia nie zawsze siedzi w atrybucie `src` (tam bywa
placeholder / obrazek 1x1px), tylko w `data-src`, `data-lazy-src` itp.
Dlatego sprawdzamy kilka atrybutów po kolei, zanim uznamy, że zdjęcia
nie ma.
"""

from __future__ import annotations

from urllib.parse import urljoin

_IMG_ATTR_CANDIDATES = ("src", "data-src", "data-lazy-src", "data-original")


def extract_image_url(img_tag, base_url: str) -> str | None:
    """Zwraca absolutny URL zdjęcia z pojedynczego tagu <img>, albo None."""
    if img_tag is None:
        return None

    for attr in _IMG_ATTR_CANDIDATES:
        value = img_tag.get(attr)
        if value:
            return urljoin(base_url, value.strip())

    # ostatnia deska ratunku: pierwszy URL z srcset / data-srcset
    srcset = img_tag.get("srcset") or img_tag.get("data-srcset")
    if srcset:
        first_candidate = srcset.split(",")[0].strip().split(" ")[0]
        if first_candidate:
            return urljoin(base_url, first_candidate)

    return None


def extract_all_image_urls(img_tags, base_url: str) -> list[str]:
    """Zwraca listę unikalnych URL-i zdjęć (z zachowaniem kolejności) dla wielu tagów <img>."""
    urls: list[str] = []
    seen: set[str] = set()
    for tag in img_tags:
        url = extract_image_url(tag, base_url)
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls
