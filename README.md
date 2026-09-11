# car_parser

Uniwersalny parser ofert samochodowych → `state.json`, z parserem dobieranym
per domena (otomoto.pl, findcar.pl), obsługą paginacji, zdjęć, deduplikacji
i powiadomień push o zmianie ceny.

## Architektura

```
main.py                        – CLI: pobiera URL-e (z paginacją), woła parser, zapisuje state.json
parsers/
  base.py                      – kontrakt SiteParser + Offer (dataclass)
  otomoto.py                   – parser dla otomoto.pl
  findcar.py                   – parser dla findcar.pl
  registry.py                  – mapowanie domena → parser
utils/
  http.py                      – pobieranie HTML (requests + retry, opcjonalny Playwright)
  images.py                    – ekstrakcja URL-i zdjęć z <img> (lazy-loading)
  text.py                      – normalizacja ceny/roku/nazw marki-modelu
  notify.py                    – push (ntfy / Pushover) o zmianie ceny
.github/workflows/scrape.yml   – GitHub Actions: odpala main.py co 15 minut
urls.txt                       – lista URL-i do przetworzenia
```

Kluczowa decyzja projektowa: **jeden wspólny interfejs (`SiteParser`), zero
wspólnej logiki parsowania HTML** — otomoto i findcar różnią się strukturalnie
na tyle, że "uniwersalny" parser CSS skończyłby się stertą `if domain == ...`.
Dodanie nowej domeny = nowy plik w `parsers/` + jeden wpis w `registry.py`,
zero zmian w `main.py`.

Każdy parser sam decyduje, czy dany URL to:
- **listing / wyszukiwarka** → zwraca listę ofert, każda pod kluczem
  `f"{url}#offer-{i}"`,
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

# dla szerokich wyszukiwań (wiele stron wyników):
python main.py --urls-file urls.txt --max-pages 250 --page-delay 1.5
```

Domyślnie skrypt **dopisuje/aktualizuje** istniejący `state.json` (klucz =
URL), więc możesz go odpalać przyrostowo. Argumenty:

| Flaga | Domyślnie | Opis |
|---|---|---|
| `--output` | `state.json` | ścieżka do pliku wynikowego |
| `--fresh` | wyłączone | nadpisz state.json od zera |
| `--max-pages` | 50 | limit stron paginacji na jeden URL (szerokie zapytania mogą mieć 100+ stron - patrz log `strona deklaruje X wyników łącznie`) |
| `--page-delay` | 1.0 | pauza (sekundy) między stronami tego samego wyszukiwania |

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
    "image": "https://ireland.apollo.olxcdn.com/v1/files/.../image;s=320x240",
    "source_offer_url": "https://www.otomoto.pl/osobowe/oferta/toyota-rav4-ID6I8Kuo.html"
  }
}
```

`image` to URL głównego zdjęcia; jeśli na stronie pojedynczej oferty znaleziono
więcej niż jedno zdjęcie, dodatkowo pojawia się pole `images` z pełną listą.

## Paginacja

`main.py` samo podąża za kolejnymi stronami wyników:
- **otomoto** – szuka wyrenderowanego linku "Następna" / `rel="next"` w HTML-u
  (dopasowanie tolerancyjne na ikony i normalizację Unicode).
- **findcar** – buduje URL kolejnej strony samodzielnie z numeru strony w
  ścieżce (`/znajdz-samochod/N?...`), niezależnie od linków w HTML-u.

Pętla zatrzymuje się, gdy strona nie zwraca już żadnych ofert, albo po
osiągnięciu `--max-pages` (log ostrzega, jeśli limit został osiągnięty).
Jeśli parser zna deklarowaną przez stronę łączną liczbę wyników (np.
"Znaleziono 1750 aut" na findcar), log na końcu porówna ją z faktycznie
zebraną liczbą i ostrzeże o rozbieżności.

## Deduplikacja

Przy szerokich wyszukiwaniach z wieloma ofertami o IDENTYCZNEJ cenie (częste
przy nowych autach w cenach katalogowych) sortowanie po stronie źródłowej
bywa niestabilne między requestami o kolejne strony - ta sama oferta może
"przeciekać" na dwie sąsiednie strony. `main.py` po zebraniu wszystkich
ofert usuwa takie duplikaty (po `source_offer_url`), zachowując pierwsze
wystąpienie - log informuje, ile wpisów zostało usuniętych.

**Uwaga architektoniczna**: klucze w `state.json` są POZYCYJNE
(`{search_url}#offer-N`). Przy niestabilnym sortowaniu ta sama realna oferta
może dostać inny klucz w różnych przebiegach - przy uruchamianiu bez
`--fresh` co 15 minut stare, "osierocone" klucze z poprzednich przebiegów
nie są automatycznie czyszczone. Dla bardzo szerokich/zmiennych zapytań
warto rozważyć `--fresh` przy każdym uruchomieniu (kosztem historii do
wykrywania zmian cen) - patrz historia rozmowy przy tym repo dla szerszego
omówienia.

## Powiadomienia push o zmianie ceny (ntfy / Pushover)

`main.py` po każdym przebiegu porównuje starą i nową cenę dla każdej oferty
(tylko oferty już wcześniej widziane - nowe oferty nie generują powiadomienia)
i wysyła push przez `utils/notify.py`, jeśli jest skonfigurowany jeden z:

- **ntfy** (darmowe) - zmienna środowiskowa `NTFY_TOPIC` (nazwa "tematu"
  subskrybowanego w apce ntfy na telefonie),
- **Pushover** (płatna apka, ~5 USD) - zmienne `PUSHOVER_APP_TOKEN` i
  `PUSHOVER_USER_KEY`.

Bez ustawienia żadnej z tych zmiennych funkcje po prostu nic nie robią -
skrypt działa normalnie lokalnie bez konfiguracji push.

## GitHub Actions (automatyczne odpalanie co 15 minut)

`.github/workflows/scrape.yml` odpala `main.py` co 15 minut i commituje
zmieniony `state.json` z powrotem do repo. Wymaga:

1. Sekretu `NTFY_TOPIC` w **Settings → Secrets and variables → Actions**
   (jeśli chcesz push na telefon).
2. `state.json` NIE może być w `.gitignore` (workflow go commituje).
3. Możesz odpalić ręcznie z zakładki **Actions** (dzięki `workflow_dispatch`),
   nie czekając na najbliższy `cron`.

Harmonogram `cron` w Actions nie jest dokładny do minuty - GitHub może go
opóźnić przy dużym obciążeniu serwerów. Dla bardzo szerokich zapytań
(100+ stron) rozważ rzadszy harmonogram niż co 15 minut, żeby kolejne
uruchomienie nie nakładało się na poprzednie.

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

    # opcjonalnie: przeciąż get_next_page_url() i extract_total_count(),
    # jeśli domyślna implementacja w base.py nie wystarcza
```

2. Zarejestruj w `parsers/registry.py`:

```python
from .nowa_domena import NowaDomenaParser
PARSERS = [OtomotoParser(), FindCarParser(), NowaDomenaParser()]
```

Reszta pipeline'u nie wymaga zmian.

## Znane ograniczenia (uczciwie, żeby nie zaskoczyły)

- **Selektory CSS w `otomoto.py`** (`_CARD_SELECTOR` itd.) są best-effort -
  otomoto regularnie redesignuje frontend. Parser najpierw i tak próbuje
  JSON-LD (stabilniejsze niż CSS), CSS to fallback.
- **Marka/model** - dla otomoto wyciągane ze sluga URL-a KONKRETNEJ oferty
  (nie z wolnego tekstu tytułu, bo ten bywa dowolny), dla findcar analogicznie
  ze sluga + parametrów `makes=`/`models=`. Marki dwuczłonowe (Mercedes-Benz,
  Land Rover...) obsługiwane przez listę wyjątków w `utils/text.py` i
  `_KNOWN_MULTI_WORD_BRAND_SLUGS` - dopisuj kolejne w miarę napotykania
  błędnych przypadków.
- **Zdjęcia** - findcar serwuje je przez własny proxy (`/thumb?src=...`),
  który bywa niedostępny spoza przeglądarki (403) - kod dekoduje i zwraca
  bezpośredni link do CDN-a dealera zamiast do proxy.
- **JS-rendering** - obie strony w testach zwracały pełny HTML po stronie
  serwera (SSR). Gdyby to się zmieniło, użyj `utils.http.fetch_with_playwright`
  jako zamiennika `fetch_html` w `main.py`.
- Klucze `state.json` są pozycyjne, nie stabilne per-oferta między przebiegami
  przy niestabilnym sortowaniu strony źródłowej - patrz sekcja "Deduplikacja"
  wyżej.
