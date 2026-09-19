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
import time
from pathlib import Path

from parsers.registry import NoParserFoundError, get_parser_for_url
from utils.http import fetch_html
from utils.notify import notify_price_change
from utils.text import pct_change

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


def process_url(url: str, state: dict, max_pages: int = 50, page_delay: float = 1.0) -> int:
    """
    Przetwarza jeden URL WRAZ Z PAGINACJĄ: jeśli to strona wyszukiwania
    z wieloma stronami wyników, idzie za linkiem "Następna" (patrz
    SiteParser.get_next_page_url) aż do ostatniej strony albo do
    `max_pages` (zabezpieczenie przed nieskończoną pętlą).

    `page_delay` - pauza (w sekundach) między kolejnymi stronami tego
    samego wyszukiwania, żeby nie zasypywać serwisu setkami requestów
    pod rząd (np. wyszukiwania na kilka marek naraz mogą mieć >200 stron).

    Zwraca łączną liczbę ofert dodanych/zaktualizowanych ze WSZYSTKICH stron.
    """
    try:
        parser = get_parser_for_url(url)
    except NoParserFoundError as exc:
        logger.error(str(exc))
        return 0

    total_added = 0
    current_url = url
    visited: set[str] = set()
    declared_total: int | None = None

    for page_num in range(1, max_pages + 1):
        if current_url in visited:
            logger.warning("Wykryto pętlę w paginacji dla %s - przerywam.", url)
            break
        visited.add(current_url)

        if page_num > 1 and page_delay > 0:
            time.sleep(page_delay)

        try:
            html = fetch_html(current_url)
        except Exception as exc:  # noqa: BLE001
            logger.error("Nie udało się pobrać %s: %s", current_url, exc)
            break

        try:
            offers = parser.parse(html, current_url)
        except Exception as exc:  # noqa: BLE001
            logger.error("Błąd parsowania %s: %s", current_url, exc)
            break

        if not offers:
            if page_num == 1:
                logger.warning(
                    "Parser %s nie znalazł żadnych ofert dla %s - "
                    "prawdopodobnie strona zmieniła strukturę albo wymaga renderowania JS "
                    "(patrz utils/http.fetch_with_playwright).",
                    type(parser).__name__,
                    current_url,
                )
            break  # pusta strona = koniec wyników

        if page_num == 1:
            declared_total = parser.extract_total_count(html)
            if declared_total is not None:
                logger.info("%s: strona deklaruje %d wyników łącznie.", url, declared_total)

        for offer in offers:
            state[offer.url] = offer.to_dict()
        total_added += len(offers)
        logger.info("%s (str. %d): znaleziono %d ofert(y)", url, page_num, len(offers))

        if not parser.is_listing_url(current_url):
            break  # pojedyncza oferta - nie ma paginacji

        next_url = parser.get_next_page_url(html, current_url)
        if not next_url:
            break  # brak linku "Następna" - to była ostatnia strona
        if page_num == max_pages:
            logger.warning(
                "%s: osiągnięto limit %d stron (--max-pages) - mogą zostać jeszcze wyniki.",
                url,
                max_pages,
            )
        current_url = next_url

    if declared_total is not None and total_added < declared_total:
        logger.warning(
            "%s: zebrano %d ofert, a strona deklarowała %d - część mogła zostać pominięta "
            "(sprawdź czy nie trafiono na --max-pages albo błąd pobierania w trakcie).",
            url,
            total_added,
            declared_total,
        )

    return total_added


def read_urls_from_file(path: Path) -> list[str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def offer_identity(offer: dict) -> str | None:
    """
    Tożsamość oferty do porównań między przebiegami: source_offer_url (dla
    wpisów z listingu), a dla wpisów pojedynczej oferty (gdzie go nie ma) -
    sam url. Używana zarówno do wykrywania zmian ceny, jak i oznaczania
    nowych ofert - patrz detect_price_changes i mark_new_offers.
    """
    return offer.get("source_offer_url") or offer.get("url")


def detect_price_changes(old_state: dict, new_state: dict) -> list[tuple[dict, dict]]:
    """
    Zwraca pary (stara_oferta, nowa_oferta) dla OFERT (nie kluczy state.json!),
    gdzie cena się zmieniła.

    Porównanie odbywa się po TOŻSAMOŚCI oferty (source_offer_url - a dla
    wpisów pojedynczej oferty, gdzie go nie ma, po samym url), a NIE po
    pozycyjnym kluczu w state.json ("{search_url}#offer-N"). Klucz pozycyjny
    może się zmienić między przebiegami przy niestabilnym sortowaniu strony
    źródłowej (patrz deduplicate_by_offer_identity) - porównywanie po samym
    kluczu ryzykowałoby albo pominięcie realnej zmiany ceny (ta sama oferta
    pod innym kluczem), albo fałszywy alarm (dwie RÓŻNE oferty przypadkiem
    pod tym samym kluczem w dwóch różnych przebiegach).
    """
    old_by_identity = {offer_identity(o): o for o in old_state.values() if offer_identity(o)}

    changes = []
    seen: set[str] = set()
    for new_offer in new_state.values():
        ident = offer_identity(new_offer)
        if not ident or ident in seen:
            continue
        seen.add(ident)

        old_offer = old_by_identity.get(ident)
        if not old_offer:
            continue  # nowa oferta, nie "zmiana ceny" - pomijamy zgodnie z wymaganiem
        old_price = old_offer.get("price")
        new_price = new_offer.get("price")
        if old_price is not None and new_price is not None and old_price != new_price:
            changes.append((old_offer, new_offer))
    return changes


def mark_new_offers(previous_state: dict, state: dict) -> int:
    """
    Ustawia pole `is_new` na KAŻDEJ ofercie w `state` (mutacja w miejscu -
    dlatego zawsze nadpisuje, nigdy nie zostają nieaktualne wartości z
    poprzedniego przebiegu).

    Jeśli `previous_state` jest puste (pierwsze uruchomienie w ogóle, albo
    świeże po --fresh - brak jakiegokolwiek wcześniejszego punktu
    odniesienia), WSZYSTKIE oferty dostają `is_new = False`. To budowanie
    początkowego katalogu, nie "nowe ogłoszenia" w sensie użytkowym - nie
    ma z czym ich porównać, więc oznaczanie ich jako nowe byłoby mylące
    (wszystko wyglądałoby na "nowe" od razu przy pierwszym imporcie).

    Dopiero gdy istnieje realny poprzedni stan (drugie i kolejne
    uruchomienia), oferty których tożsamości (source_offer_url) w nim nie
    było, dostają `is_new = True`.

    Zwraca liczbę ofert oznaczonych jako nowe (0 przy pustym previous_state).
    """
    had_previous_baseline = bool(previous_state)
    previous_identities = (
        {offer_identity(o) for o in previous_state.values()} if had_previous_baseline else set()
    )

    new_count = 0
    for offer in state.values():
        is_new = had_previous_baseline and offer_identity(offer) not in previous_identities
        offer["is_new"] = is_new
        if is_new:
            new_count += 1
    return new_count


def update_price_history(
    previous_state: dict, state: dict, changes: list[tuple[dict, dict]], max_entries: int = 5
) -> None:
    """
    Utrzymuje w state.json pole `price_history` - listę do `max_entries`
    POPRZEDNICH cen tej samej oferty (najnowsza zmiana na początku listy),
    narastająco między przebiegami.

    Krok 1: dla KAŻDEJ oferty w bieżącym `state` przenosimy istniejącą
    historię ze starego stanu (dopasowanie po tożsamości - source_offer_url),
    żeby nie zgubić jej między przebiegami, w których cena akurat się nie
    zmieniła. Pole jest ustawiane zawsze (co najmniej pusta lista `[]`),
    nigdy nie brakuje go / nie jest `null` - łatwiej zdekodować jako zwykłą
    (nieopcjonalną) tablicę po stronie klienta.

    Krok 2: dla KAŻDEJ wykrytej w tym przebiegu zmiany ceny (te same pary,
    które dostaje detect_price_changes) dopisujemy STARĄ cenę na początek
    listy i obcinamy do `max_entries` (domyślnie 5) - najstarsze wypadają.
    """
    old_by_identity = {offer_identity(o): o for o in previous_state.values() if offer_identity(o)}

    for offer in state.values():
        ident = offer_identity(offer)
        old_offer = old_by_identity.get(ident) if ident else None
        old_history = old_offer.get("price_history") if old_offer else None
        offer["price_history"] = list(old_history) if old_history else []

    for old_offer, new_offer in changes:
        history = [old_offer.get("price")] + new_offer.get("price_history", [])
        new_offer["price_history"] = history[:max_entries]


def deduplicate_by_offer_identity(state: dict) -> tuple[dict, int]:
    """
    Usuwa zduplikowane wpisy reprezentujące TĘ SAMĄ realną ofertę pod różnymi
    (pozycyjnymi) kluczami - np. "#offer-14" na jednej stronie wyników i
    "#offer-0" na kolejnej. Zdarza się przy niestabilnym sortowaniu po cenie
    na stronie źródłowej: gdy wiele ofert ma IDENTYCZNĄ cenę (częste przy
    nowych autach w cenach katalogowych), kolejność między requestem o
    stronę N i N+1 nie jest gwarantowana, więc ta sama oferta może
    "przeciekać" na dwie kolejne strony w trakcie jednego przebiegu
    paginacji. To nie błąd w budowaniu URL-i stron - to niestabilność
    sortowania po stronie serwisu źródłowego.

    Identyfikuje ofertę po `source_offer_url` (wpisy z listingu) albo po
    samym `url` (wpisy pojedynczej oferty, gdzie url == kanoniczny adres
    oferty). Zachowuje PIERWSZE napotkane wystąpienie każdej oferty.
    """
    seen: set[str] = set()
    deduped: dict = {}
    removed = 0
    for key, offer in state.items():
        identity = offer.get("source_offer_url") or offer.get("url")
        if identity in seen:
            removed += 1
            continue
        seen.add(identity)
        deduped[key] = offer
    return deduped, removed


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
    parser.add_argument(
        "--max-pages",
        type=int,
        default=50,
        help="Maksymalna liczba stron paginacji na jeden URL wyszukiwania (domyślnie 50)",
    )
    parser.add_argument(
        "--page-delay",
        type=float,
        default=1.0,
        help="Pauza w sekundach między kolejnymi stronami paginacji (domyślnie 1.0)",
    )
    parser.add_argument(
        "--price-change-threshold",
        type=float,
        default=0.0,
        help="Minimalna procentowa zmiana ceny, żeby wysłać powiadomienie push "
             "(np. 10 = tylko zmiany >= 10%%). Domyślnie 0 - powiadamia o każdej zmianie.",
    )
    args = parser.parse_args(argv)

    urls = list(args.urls)
    if args.urls_file:
        urls.extend(read_urls_from_file(args.urls_file))

    if not urls:
        parser.error("Podaj co najmniej jeden URL (jako argument albo przez --urls-file).")

    state = {} if args.fresh else load_state(args.output)
    previous_state = dict(state)  # migawka sprzed przetwarzania - do wykrycia zmian cen

    total = 0
    for url in urls:
        total += process_url(url, state, max_pages=args.max_pages, page_delay=args.page_delay)

    state, removed_dupes = deduplicate_by_offer_identity(state)
    if removed_dupes:
        logger.info(
            "Usunięto %d zduplikowanych wpisów (ta sama oferta pod różnymi kluczami - "
            "prawdopodobnie niestabilne sortowanie po cenie przy remisach).",
            removed_dupes,
        )

    new_offers_count = mark_new_offers(previous_state, state)
    if not previous_state:
        logger.info(
            "Brak poprzedniego stanu (pierwsze uruchomienie albo --fresh) - "
            "budowanie początkowego katalogu, żadna oferta nie oznaczona jako is_new."
        )
    elif new_offers_count:
        logger.info("Oznaczono %d nowych ofert (is_new=true).", new_offers_count)

    changes = detect_price_changes(previous_state, state)
    update_price_history(previous_state, state, changes, max_entries=5)

    # Czyścimy adnotacje o zmianie ceny z POPRZEDNIEGO przebiegu (żeby nie
    # zostawały "wiszące" na ofertach, których cena akurat teraz się nie
    # zmieniła), a potem dopisujemy świeże - dla KAŻDEJ wykrytej zmiany,
    # niezależnie od tego, czy przekroczyła --price-change-threshold. Dzięki
    # temu widać w samym state.json, o ile realnie zmieniła się cena, nawet
    # jeśli akurat nie wygenerowało to powiadomienia push.
    for offer in state.values():
        offer.pop("previous_price", None)
        offer.pop("price_change_amount", None)
        offer.pop("price_change_pct", None)

    for old_offer, new_offer in changes:
        old_price = old_offer.get("price")
        new_price = new_offer.get("price")
        new_offer["previous_price"] = old_price
        new_offer["price_change_amount"] = new_price - old_price
        pct = pct_change(old_price, new_price)
        new_offer["price_change_pct"] = round(pct, 1) if pct is not None else None
        notify_price_change(old_offer, new_offer, min_change_pct=args.price_change_threshold)

    if changes:
        logger.info(
            "Wykryto %d zmian(y) ceny (próg powiadomień: %.1f%%).",
            len(changes), args.price_change_threshold,
        )

    save_state(args.output, state)
    logger.info("Zapisano %s (łącznie %d wpisów, %d w tym przebiegu).", args.output, len(state), total)
    return 0


if __name__ == "__main__":
    sys.exit(main())
