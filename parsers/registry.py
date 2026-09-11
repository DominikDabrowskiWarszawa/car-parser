"""
Rejestr dostępnych parserów.

Żeby dodać obsługę nowej domeny:
1. stwórz parsers/<nazwa>.py z klasą dziedziczącą po SiteParser
   (patrz parsers/otomoto.py lub parsers/findcar.py jako wzór),
2. zaimportuj ją tutaj i dopisz instancję do listy PARSERS.

main.py (i cała reszta pipeline'u) nie wymaga żadnych zmian.
"""

from __future__ import annotations

from urllib.parse import urlparse

from .base import SiteParser
from .findcar import FindCarParser
from .otomoto import OtomotoParser

PARSERS: list[SiteParser] = [
    OtomotoParser(),
    FindCarParser(),
]


class NoParserFoundError(Exception):
    """Brak zarejestrowanego parsera dla danej domeny."""


def get_parser_for_url(url: str) -> SiteParser:
    netloc = urlparse(url).netloc
    for parser in PARSERS:
        if parser.matches(netloc):
            return parser
    raise NoParserFoundError(
        f"Brak parsera dla domeny '{netloc}' (url: {url}). "
        f"Dodaj nowy parser w parsers/ i zarejestruj go w parsers/registry.py."
    )
