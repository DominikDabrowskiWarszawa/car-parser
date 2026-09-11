"""
Wspólny kontrakt dla wszystkich parserów "per domena".

Dodanie obsługi nowej strony = dodanie nowego pliku w parsers/,
zaimplementowanie SiteParser i zarejestrowanie go w registry.py.
Reszta pipeline'u (main.py) nic o konkretnych domenach nie wie.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Offer:
    """Pojedyncza oferta - dokładnie taki kształt, jaki ma trafić do state.json."""

    url: str
    title: Optional[str] = None
    price: Optional[int] = None
    year: Optional[int] = None
    brand: Optional[str] = None
    model: Optional[str] = None
    image: Optional[str] = None
    # miejsce na dodatkowe, opcjonalne pola per-domena (np. przebieg, paliwo, pełna galeria)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        base = {
            "url": self.url,
            "title": self.title,
            "price": self.price,
            "year": self.year,
            "brand": self.brand,
            "model": self.model,
            "image": self.image,
        }
        base.update(self.extra)
        return base


class SiteParser(ABC):
    """Interfejs, jaki musi zaimplementować parser dla danej domeny."""

    #: domeny obsługiwane przez ten parser, np. {"otomoto.pl", "www.otomoto.pl"}
    domains: set[str] = set()

    def matches(self, netloc: str) -> bool:
        netloc = netloc.lower()
        return netloc in self.domains or any(netloc.endswith("." + d) for d in self.domains)

    @abstractmethod
    def is_listing_url(self, url: str) -> bool:
        """
        Czy dany URL to strona z listą ofert (wyszukiwarka / kategoria),
        czy strona pojedynczej oferty.

        Ma to znaczenie dla main.py: dla listingu generujemy wiele wpisów
        (url#offer-0, url#offer-1, ...), dla pojedynczej oferty - jeden wpis
        pod samym URL-em.
        """

    @abstractmethod
    def parse_listing(self, html: str, url: str) -> list[Offer]:
        """Parsuje stronę z wieloma ofertami i zwraca ich listę."""

    @abstractmethod
    def parse_single(self, html: str, url: str) -> Offer:
        """Parsuje stronę pojedynczej oferty."""

    def parse(self, html: str, url: str) -> list[Offer]:
        """Punkt wejścia używany przez main.py - sam decyduje którą metodę odpalić."""
        if self.is_listing_url(url):
            return self.parse_listing(html, url)
        return [self.parse_single(html, url)]
