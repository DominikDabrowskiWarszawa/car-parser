#!/usr/bin/env python3
"""
Uniwersalny parser ofert samochodowych -> state.json

Użycie:
    python main.py https://www.otomoto.pl/osobowe/nowe/toyota/rav4 \
                    "https://findcar.pl/znajdz-samochod?makes=lexus&models=es"

    # albo z pliku (jeden URL na linię):
    python main.py --urls-file urls.txt

    # nadpisać istniejący state.json od zera (domyślnie: dopisujemy/aktualizujemy):
    python main.py --urls-file urls.txt --fresh

Wynik: state.json w formacie
{
  "<url>": {"url": ..., "title": ..., "price": ..., "year": ..., "brand": ..., "model": ...},
  ...
}
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from parsers.registry import NoParserFoundError, get_parser_for_url
from utils.http import fetch_html

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def load_state(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            logger.warning("Nie udało się odczytać %s - zaczynam od pustego stanu.", path)
    return {}


def save_state(path: Path, state: dict) -> None:
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def process_url(url: str, state: dict) -> int:
    """Zwraca liczbę ofert dodanych/zaktualizowanych dla danego URL-a."""
    try:
        parser = get_parser_for_url(url)
    except NoParserFoundError as exc:
        logger.error(str(exc))
        return 0

    try:
        html = fetch_html(url)
    except Exception as exc:  # noqa: BLE001
        logger.error("Nie udało się pobrać %s: %s", url, exc)
        return 0

    try:
        offers = parser.parse(html, url)
    except Exception as exc:  # noqa: BLE001
        logger.error("Błąd parsowania %s: %s", url, exc)
        return 0

    if not offers:
        logger.warning(
            "Parser %s nie znalazł żadnych ofert dla %s - "
            "prawdopodobnie strona zmieniła strukturę albo wymaga renderowania JS "
            "(patrz utils/http.fetch_with_playwright).",
            type(parser).__name__,
            url,
        )
        return 0

    for offer in offers:
        state[offer.url] = offer.to_dict()

    logger.info("%s: znaleziono %d ofert(y)", url, len(offers))
    return len(offers)


def read_urls_from_file(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Parser ofert samochodowych -> state.json")
    parser.add_argument("urls", nargs="*", help="Adresy URL do przetworzenia")
    parser.add_argument("--urls-file", type=Path, help="Plik z listą URL-i (jeden na linię)")
    parser.add_argument(
        "--output", type=Path, default=Path("state.json"), help="Ścieżka do pliku wynikowego"
    )
    parser.add_argument(
        "--fresh",
        action="store_true",
        help="Nadpisz state.json od zera, zamiast dopisywać/aktualizować istniejące wpisy",
    )
    args = parser.parse_args(argv)

    urls = list(args.urls)
    if args.urls_file:
        urls.extend(read_urls_from_file(args.urls_file))

    if not urls:
        parser.error("Podaj co najmniej jeden URL (jako argument albo przez --urls-file).")

    state = {} if args.fresh else load_state(args.output)

    total = 0
    for url in urls:
        total += process_url(url, state)

    save_state(args.output, state)
    logger.info("Zapisano %s (łącznie %d wpisów, %d w tym przebiegu).", args.output, len(state), total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
