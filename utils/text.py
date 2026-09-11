"""Pomocnicze funkcje do normalizacji surowego tekstu wyciągniętego z HTML."""

from __future__ import annotations

import re
import unicodedata

_PRICE_RE = re.compile(r"[\d\s\u00A0.,]+")
_YEAR_RE = re.compile(r"\b(19[5-9]\d|20[0-4]\d)\b")


def clean_text(value: str | None) -> str | None:
    """Usuwa nadmiarowe białe znaki (w tym &nbsp;) i przycina tekst."""
    if value is None:
        return None
    value = unicodedata.normalize("NFKC", value)
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def parse_price(raw: str | None) -> int | None:
    """
    Zamienia string typu '352 100 zł', '352.100,00', '259 999 złbrutto'
    na liczbę całkowitą (grosze pomijamy - większość ofert i tak podaje
    ceny w pełnych złotych).
    """
    if not raw:
        return None
    match = _PRICE_RE.search(raw)
    if not match:
        return None
    digits = re.sub(r"[^\d]", "", match.group(0))
    if not digits:
        return None
    return int(digits)


def parse_year(raw: str | None) -> int | None:
    """Wyciąga 4-cyfrowy rok (1950-2049) z dowolnego stringa."""
    if not raw:
        return None
    match = _YEAR_RE.search(raw)
    return int(match.group(0)) if match else None


def slug_to_name(slug: str) -> str:
    """np. 'mercedes-benz' -> 'Mercedes-Benz' (best effort, do dopracowania per marka)."""
    parts = re.split(r"[-_]", slug)
    return "-".join(p.capitalize() for p in parts)


def format_model_name(slug: str) -> str:
    """
    Best-effort formatowanie nazwy modelu z URL-owego sluga.

    Nazewnictwo modeli samochodów jest bardzo niespójne (RAV4, X3, ES, ale
    Corolla, Passat, Octavia), więc stosujemy heurystykę:
    - krótkie (<=3 znaki) lub zawierające cyfrę fragmenty -> UPPERCASE
      (X3, RAV4, ES, GLA, C4)
    - dłuższe, czysto literowe fragmenty -> Capitalize
      (Corolla, Passat, Octavia)

    To NIE jest rozwiązanie w 100% poprawne dla każdej marki - dla
    krytycznych przypadków warto trzymać osobny słownik wyjątków
    (np. {"clA": "CLA", "id.4": "ID.4"}) i sprawdzać go przed heurystyką.
    """
    if not slug:
        return slug
    parts = re.split(r"[-\s]+", slug.replace("_", "-"))
    formatted = []
    for part in parts:
        if not part:
            continue
        if len(part) <= 3 or re.search(r"\d", part):
            formatted.append(part.upper())
        else:
            formatted.append(part.capitalize())
    return " ".join(formatted)
