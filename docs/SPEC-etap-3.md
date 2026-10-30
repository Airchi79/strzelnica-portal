# Strzelnica Sątok — etap 3: elektroniczna książka rejestru pobytu

**Wersja:** 1.0, 2026-10-31 · **Status:** zbudowany

## 1. Podstawa prawna

Rozporządzenie MSWiA z 15 marca 2000 r. w sprawie wzorcowego regulaminu strzelnic (Dz.U. 2000 nr 18 poz. 234,
zm. Dz.U. 2000 nr 51 poz. 618, Dz.U. 2002 nr 23 poz. 238, Dz.U. 2011 nr 237 poz. 1418), wydane na podstawie
art. 46 ust. 3 ustawy o broni i amunicji. Status wg API Sejmu (30.10.2026): obowiązujący.

Załącznik, rozdz. 1 ust. 1 pkt 3 (brzmienie od zmiany z 2002 r.) — prowadzący strzelanie prowadzi książkę rejestru
pobytu na strzelnicy, w której zamieszcza się:

- a) imię i nazwisko korzystającego ze strzelnicy,
- b) numer pozwolenia na broń oraz nazwę organu, który je wydał, albo adres korzystającego, jeśli nie posiada pozwolenia,
- c) oświadczenie o zapoznaniu się z regulaminem strzelnicy i przepisami bezpieczeństwa, potwierdzone własnoręcznym podpisem.

Wcześniejsze pola (rodzaj i kaliber broni, właściciel broni, godziny przyjścia i wyjścia) zostały usunięte zmianą z 2002 r.

**Zastrzeżenie:** przepisy nie regulują wprost formy elektronicznej książki ani podpisu na ekranie. Zgodność
rozwiązania warto potwierdzić we właściwym wydziale postępowań administracyjnych KWP. System umożliwia wydruk książki.

## 2. Decyzje właściciela (30–31.10.2026)

| Nr | Decyzja |
|---|---|
| D1 | Zbieramy tylko pola a–c oraz automatycznie: datę i godzinę wejścia, cel pobytu. Broni nie zbieramy. Wypis opcjonalny |
| D2 | Podpis palcem na ekranie + wydruk książki do kontroli |
| D3 | Wpis nie musi być związany z rezerwacją. Cel pobytu: strzelanie (wymaga trwającej rezerwacji), obserwator/osoba towarzysząca (rezerwacja opcjonalna), prace/obsługa, inny |
| D4 | Okres przechowywania 5 lat, zmienialny w ustawieniach |
| D5 | Opłata ustalona przy rezerwacji. Gdy strzelających w książce jest więcej niż zadeklarowano — dopłata wg cennika z chwili rezerwacji, nie więcej niż ryczałt. Mniej wpisów nie obniża opłaty |

## 3. Sposoby wpisu

1. **Kod QR rezerwacji** — widoczny dla prowadzącego na stronie rezerwacji; uczestnicy skanują go telefonem.
   Aktywny od 30 min przed początkiem (ustawienie) do końca rezerwacji. Cel: strzelanie lub obserwator.
2. **Kod QR przy wejściu** — plakat do druku z panelu. Token można w każdej chwili wymienić (stary przestaje działać).
   Przy celu „strzelanie” wybór trwającej rezerwacji (lista bez nazwisk).
3. **Stały kod QR strzelca** („Mój kod QR”) — prowadzący skanuje go aparatem i dopisuje osobę do swojej trwającej
   rezerwacji; osoba podpisuje się na telefonie prowadzącego. Kod nie zawiera danych osobowych; można go wymienić.
4. **Czytnik przy wejściu** (etap 6) — przewidziany w modelu danych.

Osoba zalogowana ma dane podstawione z konta (imię, nazwisko, zweryfikowane pozwolenie lub adres). Wpis bez konta jest możliwy.

## 4. Integralność i ochrona danych

- Wpisu nie można zmienić ani usunąć (trigger w PostgreSQL). Wyjątki: jednorazowe zapisanie godziny wyjścia
  oraz usunięcie po okresie przechowywania przez zadanie `purge_logbook`.
- Każdy wpis ma sumę kontrolną SHA-256 danych i podpisu; panel pokazuje wynik weryfikacji.
- Podpis: PNG, sprawdzany po stronie serwera (format, rozmiar, niepusty).
- Limit wpisów z jednego adresu IP na godzinę (domyślnie 30).
- Przegląd, szczegóły i wydruk książki wymagają uprawnienia „Książka pobytu: przegląd i wydruk”; każdy wgląd trafia
  do rejestru wglądów w dane wrażliwe.

## 5. Zadania okresowe

`settle_reservations` (rozliczenie wg książki) i `purge_logbook` (czyszczenie) — w usłudze `cron` co godzinę.
