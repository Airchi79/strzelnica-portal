# Strzelnica Sątok — etap 5: stowarzyszenie

**Wersja:** 1.0, 2026-10-31 · **Status:** zbudowany

Podstawa: Regulamin Stowarzyszenia „Strzelnica Sątok” (tekst z 20.04.2026) oraz decyzje właściciela z 31.10.2026.

## Decyzje
| Sprawa | Ustalenie |
|---|---|
| Składka | 200 zł rocznie, płatna do 31.01; przy przyjęciu w trakcie roku pełna kwota (termin: 14 dni od przyjęcia) |
| Wpisowe | 50 zł jednorazowo |
| Członkowie nadzwyczajni | zwolnieni ze składek (§ 31) |
| Cennik osi | równy dla członków i niezrzeszonych (§ 40 — zakaz preferencyjnego korzystania z majątku) |
| Numeracja uchwał | osobna dla Zarządu, WZC, członków nadzwyczajnych i zebrania założycielskiego: `Nr X/RRRR` |
| Kluby | kody dostępu; rezerwacja z kodem rozliczana wg umowy z klubem, poza płatnościami strzelca |
| Płatności | domyślnie bezpośrednio za każdą rezerwację/składkę; saldo (przedpłaty) — opcja do włączenia |
| Karnety, wypłata salda | odłożone |

## Zasady z Regulaminu odwzorowane w systemie
- Zarząd (Prezes, Wiceprezes): głosowanie online, zwykła większość przy głosach całego Zarządu, remis — decyduje Prezes (§ 25).
- WZC: głosy ważone (zwyczajny 1, nadzwyczajny 50), wymagana osobista obecność Prezesa (§ 19); pełnomocnictwa:
  pełnomocnik obecny osobiście, zwyczajny najwyżej za 2 osoby (§ 21).
- Członkowie nadzwyczajni: zmiana rodzaju członkostwa, quorum ≥ połowa uprawnionych (§ 31 pkt 4).
- Deklaracja elektroniczna; termin decyzji 2 miesiące (§ 31–32) — licznik i przypomnienie Zarządu.
- Zawieszenie / wykreślenie wyłącznie uchwałą Zarządu; termin odwołania 7 dni (§ 37–38).
- Skutki przyjętej uchwały wykonywane automatycznie (przyjęcie, odmowa, zawieszenie, przywrócenie, wykreślenie,
  zmiana rodzaju, stawki składek, umorzenie) i zapisywane w dzienniku zdarzeń.

## Dane z dokumentów
Pakiet importu (`dane.json`, regulamin, skany uchwał 1–4/2025) przygotowany poza repozytorium; bez numerów PESEL,
dat urodzenia i telefonów. Import: `python manage.py import_association_seed <katalog>` (idempotentny).
