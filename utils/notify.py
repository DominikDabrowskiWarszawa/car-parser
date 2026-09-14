"""
Wysyłka powiadomień push o zmianie ceny.

Obsługiwane kanały (oba opcjonalne, włączane przez zmienne środowiskowe -
w GitHub Actions ustawiane jako "secrets"):

- ntfy.sh   -> zmienna NTFY_TOPIC (nazwa "tematu", np. losowy ciąg znaków)
- Pushover  -> zmienne PUSHOVER_APP_TOKEN i PUSHOVER_USER_KEY

Jeśli żadna zmienna nie jest ustawiona, funkcje po prostu nic nie robią
(dzięki temu skrypt normalnie działa też lokalnie, bez konfiguracji push).
"""

from __future__ import annotations

import logging
import os

import requests

from utils.text import pct_change

logger = logging.getLogger(__name__)


def _fmt_price(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def send_ntfy(title: str, message: str, priority: str = "default") -> None:
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return
    try:
        requests.post(
            f"https://ntfy.sh/{topic}",
            data=message.encode("utf-8"),
            headers={
                "Title": title.encode("utf-8"),
                "Priority": priority,
                "Tags": "moneybag",
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("Nie udało się wysłać powiadomienia ntfy: %s", exc)


def send_pushover(title: str, message: str) -> None:
    token = os.environ.get("PUSHOVER_APP_TOKEN")
    user = os.environ.get("PUSHOVER_USER_KEY")
    if not token or not user:
        return
    try:
        requests.post(
            "https://api.pushover.net/1/messages.json",
            data={"token": token, "user": user, "title": title, "message": message},
            timeout=10,
        )
    except requests.RequestException as exc:
        logger.warning("Nie udało się wysłać powiadomienia Pushover: %s", exc)


def notify_price_change(old_offer: dict, new_offer: dict, min_change_pct: float = 0.0) -> None:
    """
    Wysyła powiadomienie o zmianie ceny, jeśli faktycznie się zmieniła.

    `min_change_pct` - minimalna procentowa zmiana ceny, żeby wysłać
    powiadomienie (np. 10.0 = tylko zmiany >= 10%). Domyślnie 0 - powiadamia
    o KAŻDEJ zmianie, jak dotychczas.
    """
    old_price = old_offer.get("price")
    new_price = new_offer.get("price")
    if old_price is None or new_price is None or old_price == new_price:
        return

    diff = new_price - old_price
    pct = abs(pct_change(old_price, new_price) or 0.0)

    if pct < min_change_pct:
        logger.info(
            "Pominięto powiadomienie (zmiana %.1f%% < progu %.1f%%): %s -> %s",
            pct, min_change_pct, old_price, new_price,
        )
        return

    direction = "Wzrost" if diff > 0 else "Spadek"
    arrow = "⬆️" if diff > 0 else "⬇️"

    car_name = new_offer.get("title") or " ".join(
        filter(None, [new_offer.get("brand"), new_offer.get("model")])
    ) or "Oferta"

    title = f"{arrow} {direction} ceny: {car_name}"
    message = (
        f"{_fmt_price(old_price)} PLN -> {_fmt_price(new_price)} PLN "
        f"({'+' if diff > 0 else ''}{_fmt_price(diff)} PLN, {pct:.1f}%)\n"
        f"{new_offer.get('source_offer_url') or new_offer.get('url')}"
    )

    logger.info("Zmiana ceny: %s", message.replace("\n", " | "))
    send_ntfy(title, message, priority="high" if diff < 0 else "default")
    send_pushover(title, message)
