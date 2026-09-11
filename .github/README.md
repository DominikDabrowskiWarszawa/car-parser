# car_parser

Uniwersalny parser ofert samochodowych → `state.json`, z parserem dobieranym
per domena.

## Architektura

```
main.py                  – CLI: pobiera URL-e, woła odpowiedni parser, zapisuje state.json
parsers/
  base.py                – kontrakt SiteParser (wspólny dla wszystkich domen) + Offer (dataclass)
  otomoto.py              – parser dla otomoto.pl
  findcar.py             – parser dla findcar.pl
  registry.py            – mapowanie domena → parser
utils/
  http.py                – pobieranie HTML (requests + retry, opcjonalny fallback na Playwright)
  text.py                – normalizacja ceny/roku/nazw marki-modelu
```

Kluczowa decyzja projektowa: **jeden wspólny interfejs (`SiteParser`), zero
wspólnej logiki parsowania HTML** — bo otomoto i findcar różnią się
strukturalnie tak bardzo, że próba "uniwersalnego" parsera CSS skończyłaby
się stertą `if domain == ...` w środku jednej funkcji. Zamiast tego:

- `main.py` w ogóle nie wie, jak wygląda otomoto czy findcar,
- dodanie nowej domeny = nowy plik w `parsers/` + jeden wpis w `registry.py`.

Każdy parser sam decyduje, czy dany URL to:
- **listing / wyszukiwarka** → zwraca listę ofert, każda zapisywana pod
  kluczem `f"{url}#offer-{i}"`,
- **pojedyncza oferta** → jeden wpis pod samym URL-em.

## Instalacja

```bash
pip install -r requirements.txt
```

## Użycie

```bash
python main.py \
  "https://www.otomoto.pl/osobowe/nowe/toyota/rav4" \
  "https://findcar.pl/znajdz-samochod?makes=lexus&models=es"

# albo z pliku:
python main.py --urls-file urls.txt

# nadpisać state.json od zera zamiast dopisywać/aktualizować:
python main.py --urls-file urls.txt --fresh
```

Domyślnie skrypt **dopisuje/aktualizuje** istniejący `state.json` (klucz =
URL), więc możesz go odpalać przyrostowo dla różnych zestawów adresów.

## Jak dodać kolejną domenę

1. Utwórz `parsers/nowa_domena.py`:

```python
from .base import Offer, SiteParser

class NowaDomenaParser(SiteParser):
    domains = {"nowadomena.pl"}

    def is_listing_url(self, url: str) -> bool:
        ...  # True dla strony z wieloma ofertami

    def parse_listing(self, html: str, url: str) -> list[Offer]:
        ...

    def parse_single(self, html: str, url: str) -> Offer:
        ...
```

2. Zarejestruj w `parsers/registry.py`:

```python
from .nowa_domena import NowaDomenaParser
PARSERS = [OtomotoParser(), FindCarParser(), NowaDomenaParser()]
```

Reszta pipeline'u nie wymaga zmian.

## Ograniczenia / rzeczy do zweryfikowania na żywej stronie

To NIE jest gotowy, w 100% odporny na zmiany scraper — to szkielet + działająca
logika oparta o strukturę stron, którą sprawdziłem, ale bez podglądu
surowego HTML-a (devtools) w tym środowisku:

- **Selektory CSS w `otomoto.py`** (`_CARD_SELECTOR`, `_TITLE_SELECTOR`,
  `_PRICE_SELECTOR`) są oparte o typowe dla serwisów grupy OLX atrybuty
  `data-testid`, ale otomoto regularnie redesignuje frontend. Warto
  otworzyć stronę w przeglądarce, sprawdzić realne atrybuty (Ctrl+Shift+I
  → Elements) i podmienić stałe na górze pliku. Parser najpierw i tak
  próbuje JSON-LD (`<script type="application/ld+json">`), który jest
  stabilniejszy niż CSS — jeśli otomoto go udostępnia, selektory CSS w ogóle
  nie są używane.
- **`findcar.py`** kotwiczy się o wzorzec URL-a oferty (`/oferty-dealerow/...`)
  zamiast o klasy CSS, więc powinien być odporniejszy na zmiany layoutu —
  ale ekstrakcja ceny/roku z tekstu kontenera to parsowanie regexem sklejonego
  tekstu (`"Automat201 KM2026"`), co jest z natury kruche, jeśli findcar
  zmieni kolejność elementów.
- **Rozpoznawanie marki/modelu** jest heurystyczne (patrz
  `utils/text.format_model_name`) — dobrze radzi sobie z modelami typu
  `X3`, `RAV4`, `ES`, gorzej z wieloczłonowymi nazwami modeli. Dla
  precyzyjnych wymagań biznesowych warto dodać słownik wyjątków per marka.
- **JS-rendering**: obie strony w testach zwracały pełny HTML po stronie
  serwera (SSR), więc `requests` powinno wystarczyć. Gdyby jednak w
  praktyce `fetch_html()` zwracało puste karty (bo np. treść ładuje się
  dynamicznie po stronie klienta), użyj `utils.http.fetch_with_playwright`
  jako drop-in replacement w `main.py`.
- Skrypt nie obsługuje paginacji (`findcar.pl/znajdz-samochod/2?...`) —
  jeśli potrzebujesz wszystkich wyników, a nie tylko pierwszej strony,
  trzeba dodać pętlę po stronach (numer strony widać w URL-u).

## Format wyjściowy

```json
{
  "https://www.otomoto.pl/osobowe/nowe/toyota/rav4#offer-0": {
    "url": "https://www.otomoto.pl/osobowe/nowe/toyota/rav4#offer-0",
    "title": "Toyota RAV4 2.5 Hybrid Dynamic Force Comfort 4x2 e-CVT",
    "price": 181900,
    "year": 2026,
    "brand": "Toyota",
    "model": "RAV4",
    "source_offer_url": "https://www.otomoto.pl/osobowe/oferta/toyota-rav4-ID6I8Kuo.html"
  }
}
```
