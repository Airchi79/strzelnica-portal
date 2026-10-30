# Strzelnica Sątok — etap 4: płatności

**Wersja:** 1.0, 2026-10-31 · **Status:** zbudowany (Tpay — kod gotowy, wymaga danych dostępowych do sandboxa)

## 1. Zasada: saldo strzelca

Każda wpłata trafia na **saldo** strzelca, a opłaty za rezerwacje są pobierane z salda automatycznie
(od najstarszej należności, w całości — bez częściowych potrąceń). Model zaakceptowany przez właściciela
(„jak w braterstwo.eu”).

| Zdarzenie | Skutek |
|---|---|
| Rezerwacja, saldo wystarcza | opłata pobrana od razu, rezerwacja „Opłacona” |
| Rezerwacja, saldo nie wystarcza | „Do zapłaty”; pobranie przy najbliższej wpłacie |
| Anulowanie bezpłatne (strzelec ≥ 48 h, zarząd zawsze) | zwrot pobranej kwoty na saldo |
| Anulowanie po terminie | opłata zostaje pobrana / należna |
| Zmiana liczby osób przez zarząd | nadpłata wraca na saldo, niedopłata staje się należnością |
| Rozliczenie wg książki pobytu (etap 3) | dopłata pobierana z salda; rezerwacje już opłacone — tylko powiadomienie zarządu |
| Zwolnienie z opłaty | zarząd może zwolnić z pozostałej kwoty i cofnąć zwolnienie |

Saldo może być ujemne wyłącznie wskutek korekty zarządu lub zwrotu płatności do wpłacającego (chargeback Tpay).
Każda operacja jest zapisana w księdze sald, której wpisów nie można zmienić ani usunąć (trigger w bazie);
korekta to nowa operacja z powodem.

## 2. Kanały wpłat

1. **Przelew tradycyjny + import wyciągu CSV.** Tytuł: numer rezerwacji `SATOK-R-RRRR-NNNN` albo osobisty kod
   strzelca `SATOK-K-NNNNNL` (litera kontrolna chroni przed literówką). Import rozpoznaje nagłówki typowych
   eksportów (PKO/mBank i podobne), kodowanie UTF-8/Windows-1250, pomija obciążenia i wiersze już wczytane.
   Wpłaty bez kodu trafiają na listę „Nieprzypisane” — zarząd przypisuje je do strzelca lub pomija (np. darowizna).
2. **Gotówka / BLIK na telefon** — zarząd rejestruje w panelu („Przyjmij wpłatę”).
3. **Tpay (Open API)** — doładowanie salda lub zapłata za rezerwację (kwota trafia na saldo i opłaca należność).
   Powiadomienie weryfikowane podpisem JWS (certyfikat Tpay sprawdzany względem CA Tpay) **i** sumą md5 z kodem
   bezpieczeństwa; obsługa idempotentna; niezgodna kwota → do decyzji zarządu; chargeback → obciążenie salda.

## 3. Konfiguracja Tpay

W pliku `.env` (nigdy w repozytorium):

```
TPAY_CLIENT_ID=...        # Panel Tpay → Integracja → API (Open API)
TPAY_CLIENT_SECRET=...
TPAY_SECURITY_CODE=...    # Panel Tpay → Powiadomienia → Bezpieczeństwo (kod zabezpieczający)
TPAY_SANDBOX=true         # false dopiero na produkcji
```

Adres powiadomień przekazywany automatycznie: `{SITE_URL}/platnosci/tpay/powiadomienie/` — musi być dostępny
z internetu po HTTPS (port 443), bez przekierowań. Bez kompletu danych przyciski płatności online są ukryte.

## 4. Poza zakresem

- Karnety: na razie realizuje je przedpłata na saldo. Odrębne karnety (np. 10 wejść) — do decyzji.
- Wypłata salda na rachunek strzelca — do decyzji (obecnie wyłącznie korekta zarządu).
