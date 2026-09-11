#!/usr/bin/env python3
"""
FindCar.pl watcher
-------------------
Sprawdza podaną listę wyników wyszukiwania na findcar.pl, wykrywa:
  - nowe oferty (pojawiły się od ostatniego sprawdzenia),
  - zmiany ceny w ofertach już znanych,
  - oferty, które zniknęły z listy (np. sprzedane),
i wysyła powiadomienie push przez ntfy.sh.

Stan (lista znanych ofert + cen) jest trzymany w pliku JSON (state.json),
żeby przy kolejnym uruchomieniu wiedzieć, co się zmieniło.

Wymagane biblioteki:
    pip install requests beautifulsoup4 --break-system-packages

Konfiguracja przez zmienne środowiskowe (patrz README.md):
    FINDCAR_URL   - pełny URL wyników wyszukiwania (z filtrami)
    NTFY_TOPIC    - nazwa "tematu" w ntfy.sh, na który mają iść powiadomienia
    NTFY_SERVER   - (opcjonalnie) własny serwer ntfy, domyślnie https://ntfy.sh
    STATE_FILE    - (opcjonalnie) ścieżka do pliku ze stanem, domyślnie state.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, asdict
from html import unescape
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

# --------------------------------------------------------------------------
# Konfiguracja
# --------------------------------------------------------------------------

FINDCAR_URL = os.environ.get(
    "FINDCAR_URL",
    "https://findcar.pl/znajdz-samochod?makes=lexus&models=es",
)
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
STATE_FILE = os.environ.get("STATE_FILE", "state.json")

HEADERS = {
    # Udajemy zwykłą przeglądarkę - część serwerów blokuje domyślny User-Agent requests.
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8",
}

MAX_PAGES = 250          # zabezpieczenie przed nieskończoną pętlą (np. 1750 ofert / ~15 na stronę ≈ 117 stron)
MAX_ANCESTOR_LEVELS = 6  # ile poziomów w górę drzewa DOM sprawdzić szukając "karty" oferty
MAX_CARD_TEXT_LEN = 1500  # zabezpieczenie, żeby nie złapać całej strony jako "karty"

PRICE_RE = re.compile(r"([\d][\d\s\u00A0]{2,12})\s*z[łl]\s*brutto", re.IGNORECASE)


@dataclass
class Offer:
    url: str
    title: str
    price: int
    image_url: Optional[str] = None


def normalize_price(raw: str) -> int:
    """'259 999' / '259\u00a0999' -> 259999"""
    digits = re.sub(r"[^\d]", "", raw)
    return int(digits)


def fetch(url: str) -> str:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.text


def find_card_node(anchor):
    """
    Idzie w górę drzewa DOM od linku do oferty, szukając najmniejszego
    kontenera ("karty"), którego tekst zawiera cenę w formacie "... zł brutto".
    Dzięki temu nie musimy znać dokładnych nazw klas CSS strony.
    Zwraca sam węzeł BeautifulSoup (żeby móc z niego wyciągnąć też zdjęcie),
    nie tylko tekst.
    """
    node = anchor
    for _ in range(MAX_ANCESTOR_LEVELS):
        if node is None:
            break
        text = node.get_text(separator=" ", strip=True)
        if PRICE_RE.search(text) and len(text) <= MAX_CARD_TEXT_LEN:
            return node
        node = node.parent
    return None


def find_card_image(card_node, base_url: str) -> Optional[str]:
    """
    Szuka zdjęcia samochodu w obrębie karty oferty. Strona serwuje zdjęcia
    przez proxy pod /thumb?src=..., więc najpierw szukamy takiego obrazka
    (żeby nie złapać przez pomyłkę ikonki paliwa/loga), a dopiero w razie
    braku bierzemy pierwszy obrazek z karty jako fallback.
    """
    img = card_node.find("img", src=re.compile(r"/thumb\?"))
    if img is None:
        img = card_node.find("img")
    if img is None:
        return None

    src = img.get("src") or img.get("data-src")
    if not src:
        return None

    return urljoin(base_url, src)


def parse_offers(html: str, base_url: str) -> dict:
    """Parsuje stronę wyników i zwraca dict {offer_url: Offer}."""
    soup = BeautifulSoup(html, "html.parser")
    offers = {}

    anchors = soup.find_all("a", href=re.compile(r"^/oferty-dealerow/"))
    for anchor in anchors:
        offer_url = urljoin(base_url, anchor["href"])
        if offer_url in offers:
            continue  # ta sama oferta może mieć kilka linków (np. zdjęcie + tytuł)

        card_node = find_card_node(anchor)
        if card_node is None:
            continue  # nie udało się znaleźć ceny w pobliżu - pomijamy, żeby nie zgłaszać fałszywek

        card_text = card_node.get_text(separator=" ", strip=True)
        price_match = PRICE_RE.search(card_text)
        price = normalize_price(price_match.group(1))

        image_url = find_card_image(card_node, base_url)

        title = anchor.get_text(strip=True)
        if not title:
            slug = anchor["href"].rstrip("/").split("/")[-1]
            title = slug.replace("-", " ")

        offers[offer_url] = Offer(url=offer_url, title=title, price=price, image_url=image_url)

    return offers


def extract_total_count(html: str) -> Optional[int]:
    """
    Wyciąga deklarowaną przez stronę liczbę wszystkich ofert dla danego
    zapytania (np. "Znaleziono 1750 aut" / "Filtruj 1750 aut"). Używane
    jako dodatkowa kontrola, żeby wiedzieć, kiedy przestać pobierać strony.
    Zwraca None, jeśli nie udało się tego znaleźć (skrypt i tak działa dalej,
    tylko bez tej dodatkowej kontroli).
    """
    unescaped_html = unescape(html)
    match = re.search(r"(?:Znaleziono|Filtruj)\s+([\d\s\u00A0]+)\s*aut", unescaped_html)
    if not match:
        return None
    return normalize_price(match.group(1))


def build_page_url(start_url: str, page: int) -> str:
    """
    Buduje URL dla danej strony wyników na podstawie ORYGINALNEGO URL-a
    podanego w konfiguracji (FINDCAR_URL) - a nie na podstawie linków
    znalezionych w HTML-u strony.

    To jest kluczowe: findcar.pl przy renderowaniu linków paginacji zmienia
    kolejność parametrów i sposób kodowania niektórych znaków (np. przecinek
    w "sort=price,desc" bywa zakodowany jako "%2C", a parametry są
    poukładane w innej kolejności niż w URL-u, który wpisujesz). Próba
    dopasowania takiego linku 1:1 do naszego oryginalnego query stringa
    (jak robił poprzedni, usunięty already find_next_page_url) zawodzi przy
    bardziej złożonych filtrach - stąd sami budujemy URL kolejnej strony,
    zamiast szukać go w HTML-u.
    """
    parsed = urlparse(start_url)
    base_path = re.sub(r"/znajdz-samochod(?:/\d+)?$", "/znajdz-samochod", parsed.path)
    new_path = base_path if page <= 1 else f"{base_path}/{page}"
    return urlunparse(parsed._replace(path=new_path))


def fetch_all_offers(start_url: str) -> dict:
    """
    Przechodzi po wszystkich stronach wyników i zwraca połączony dict ofert.

    Strony pobieramy po kolei (2, 3, 4, ...), budując URL samodzielnie
    (patrz build_page_url). Zatrzymujemy się, gdy:
      - strona nie zwróci żadnych ofert (typowy sygnał "koniec wyników"),
      - zebrana liczba ofert osiągnie deklarowaną przez stronę wartość
        ("Znaleziono X aut"), jeśli udało się ją odczytać,
      - albo osiągniemy MAX_PAGES (zabezpieczenie przed nieskończoną pętlą).
    """
    all_offers = {}
    total_expected = None

    for page in range(1, MAX_PAGES + 1):
        url = build_page_url(start_url, page)
        html = fetch(url)

        if page == 1:
            total_expected = extract_total_count(html)
            if total_expected is not None:
                print(f"Strona deklaruje {total_expected} ofert łącznie.")

        page_offers = parse_offers(html, url)
        if not page_offers:
            break  # koniec wyników - ta strona jest już pusta

        all_offers.update(page_offers)

        if total_expected is not None and len(all_offers) >= total_expected:
            break  # zebraliśmy już tyle ofert, ile strona deklarowała

        time.sleep(1)  # nie bombardujemy serwera

    return all_offers


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return {k: Offer(**v) for k, v in raw.items()}
    except (json.JSONDecodeError, TypeError, KeyError):
        return {}


def save_state(path: str, offers: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({k: asdict(v) for k, v in offers.items()}, f, ensure_ascii=False, indent=2)


def send_push(
    title: str,
    message: str,
    url_to_open: Optional[str] = None,
    image_url: Optional[str] = None,
) -> None:
    if not NTFY_TOPIC:
        print("[UWAGA] Brak NTFY_TOPIC - pomijam wysyłkę push, tylko wypisuję na ekran.")
        print(f"{title}: {message}")
        return

    push_url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    headers = {
        "Title": title.encode("utf-8"),
        "Priority": "default",
        "Tags": "car",
    }
    if url_to_open:
        headers["Click"] = url_to_open
    if image_url:
        # ntfy pokaże to zdjęcie jako miniaturę w powiadomieniu.
        headers["Attach"] = image_url

    try:
        resp = requests.post(
            push_url,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        print(f"[OK] Wysłano push: {title}")
    except requests.RequestException as e:
        print(f"[BŁĄD] Nie udało się wysłać push: {e}", file=sys.stderr)


def format_price(price: int) -> str:
    return f"{price:,}".replace(",", " ") + " zł"


def main() -> int:
    print(f"Sprawdzam: {FINDCAR_URL}")
    try:
        current_offers = fetch_all_offers(FINDCAR_URL)
    except requests.RequestException as e:
        print(f"[BŁĄD] Nie udało się pobrać strony: {e}", file=sys.stderr)
        return 1

    if not current_offers:
        print("[UWAGA] Nie znaleziono żadnych ofert - sprawdź, czy strona nie zmieniła struktury.")
        return 1

    print(f"Znaleziono {len(current_offers)} ofert na stronie.")

    previous_offers = load_state(STATE_FILE)

    if not previous_offers:
        # Pierwsze uruchomienie - tylko zapisujemy stan bazowy, bez powiadomień
        # o "nowych" ofertach (bo w rzeczywistości widzimy je po prostu pierwszy raz).
        print("Pierwsze uruchomienie - zapisuję stan bazowy bez wysyłania powiadomień.")
        save_state(STATE_FILE, current_offers)
        return 0

    new_offers = [o for url, o in current_offers.items() if url not in previous_offers]
    price_changes = []
    for url, offer in current_offers.items():
        old = previous_offers.get(url)
        if old and old.price != offer.price:
            price_changes.append((old, offer))

    removed_offers = [o for url, o in previous_offers.items() if url not in current_offers]

    for offer in new_offers:
        send_push(
            title="Nowa oferta",
            message=f"{offer.title}\n{format_price(offer.price)}\n{offer.url}",
            url_to_open=offer.url,
            image_url=offer.image_url,
        )

    for old, new in price_changes:
        arrow = "\u2193" if new.price < old.price else "\u2191"
        send_push(
            title=f"Zmiana ceny {arrow}",
            message=(
                f"{new.title}\n"
                f"{format_price(old.price)} -> {format_price(new.price)}\n"
                f"{new.url}"
            ),
            url_to_open=new.url,
            image_url=new.image_url,
        )

    for offer in removed_offers:
        send_push(
            title="Oferta zniknęła",
            message=f"{offer.title}\n{format_price(offer.price)}\n{offer.url}",
            url_to_open=offer.url,
            image_url=offer.image_url,
        )

    if not new_offers and not price_changes and not removed_offers:
        print("Brak zmian.")
    else:
        print(
            f"Zmiany: {len(new_offers)} nowych, "
            f"{len(price_changes)} zmian ceny, "
            f"{len(removed_offers)} zniknęło."
        )

    save_state(STATE_FILE, current_offers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
