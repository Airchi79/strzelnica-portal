# Etap 6 — panel obsługi wzorowany na GunTrackerze, kiosk i Raspberry Pi

Decyzje właściciela: G1–G6 tak, G7 kiosk na tablecie **i** Raspberry Pi, G8 import z GunTrackera (czeka na pliki eksportu).

## 1. Przegląd i lista rezerwacji dniami
- `/panel/` — kafelki „wymaga uwagi” (zatwierdzenia, płatności, braki w książce, dokumenty, deklaracje, uchwały, wpłaty do przypisania)
  i strumień zdarzeń z filtrami. Kafelki zależą od uprawnień.
- `/panel/rezerwacje/dniami/` — rezerwacje w grupach dni, kolumny: godzina, klient, miejsce, księga, płatność; filtry i wyszukiwanie.

## 2. Książka pobytu
- Grupowanie wg rezerwacji (wpisy bez rezerwacji — wg dnia), filtr „tylko otwarte”, „Na strzelnicy teraz”.
- Numery pozwoleń, adresy i podpis ukryte; „Pokaż dane” zapisuje dostęp w dzienniku wglądu (za każdą osobę).
- Eksport XLSX (z zabezpieczeniem przed formułami), wydruk/PDF jak dotąd.
- Wpis przez obsługę (uprawnienie `logbook.staff_entry`): osoba podpisuje się na urządzeniu obsługi, godzina wejścia do 24 h wstecz.
- Automatyczne zamknięcie pobytu: o końcu rezerwacji albo po N godzinach (ustawienie, domyślnie 12; 0 = wyłączone), oznaczenie „auto”.
- Suma kontrolna wpisu liczona zawsze z czasu UTC.

## 3. Kartoteka klientów i kody wejścia
- „Klienci” (dawniej „Użytkownicy”): typ (członek / powracający / nowy), status uprawnień PS, liczba i data ostatniej wizyty,
  tagi, filtry. Karta klienta: notatki obsługi, tagi, zatwierdzenie, kody wejścia, historia rezerwacji i wpisów, członkostwo.
- Kody wejścia (`W…`): pojedyncze wejście, karnet (N wejść), abonament (bez limitu); ważność, limit godzin i osób na wejście,
  dozwolone osie, cena i sposób zapłaty. Jedno wejście = jedna rezerwacja; bezpłatne anulowanie zwraca wejście.
  Kod działa po opłaceniu i tylko dla grup, które mają włączone „kody wejścia”.

## 4. Zasady grup klientów
| Grupa | Zatwierdzanie | Online | Offline | Kody |
|---|---|---|---|---|
| Członkowie | nie | tak | tak | tak |
| Powracający | nie | tak | tak | nie |
| Nowi | **tak** | tak | nie | nie |

(ustawienia domyślne, zmieniane w *Rezerwacje → Ustawienia*). Rezerwacje tworzone przez obsługę nie podlegają zasadom grup.
- Nowy klient: rezerwacja „czeka na zatwierdzenie” (blokuje termin); obsługa zatwierdza albo odrzuca z powodem.
- Grupa „tylko online”: nieopłacona rezerwacja zwalnia się po 15 min (po zatwierdzeniu — po 24 h); działa tylko przy włączonym Tpay;
  trwająca płatność Tpay wstrzymuje zwolnienie; wpłata za zwolnioną rezerwację trafia do obsługi do decyzji.
- Anulowanie: klient sam do 24 h przed terminem; później tylko obsługa — bez opłaty albo „z zachowaniem należności”.

## 5. Raporty sprzedaży
- Numeracja `RS/D/rok/nnnn` (dzienne) i `RS/M/rok/nnnn` (miesięczne); raport jest zamrożonym obrazem danych z sumą SHA-256,
  baza nie pozwala go zmienić ani usunąć. Dzienne — za dni z operacjami; miesięczne — za zakończone miesiące.
- Zawartość: zaksięgowane wpłaty (rezerwacje, składki, inne), sprzedaż kodów wejścia, zwroty do wpłacających; podsumowania wg kategorii
  i sposobu zapłaty. PDF (czcionka DejaVu w repozytorium) i CSV (UTF-8 z BOM, separator `;`).
- Generowanie automatyczne (zadanie okresowe) i ręczne w panelu; uprawnienie `reports.view_salesreport`.

## 6. Kiosk i menu boczne
- `/kiosk/` — tablet lub Raspberry Pi. Parowanie 8-cyfrowym kodem z panelu (*Książka pobytu → Kioski*), ważnym 15 min;
  urządzenie dostaje losowy token w ciasteczku (HttpOnly, SameSite=Strict), w bazie tylko skrót SHA-256. Wyłączenie w panelu działa natychmiast.
- Wpis do książki (te same reguły co formularz), wyjście (wyszukanie po nazwisku), skanowanie QR kamerą (BarcodeDetector) lub czytnikiem USB.
- Bez sieci: wpisy w kolejce na urządzeniu z identyfikatorem (`client_ref`) — ponowne wysłanie nie tworzy duplikatu; godzina wejścia
  z urządzenia przyjmowana do 72 h wstecz; wpis odrzucony przy synchronizacji jest zgłaszany obsłudze (bez danych osobowych).
- Raspberry Pi: `deploy/raspberry-pi/` (lista części, złożenie, skrypt instalacyjny Chromium w trybie kiosku).
- Panel: na ekranach ≥ 1100 px menu po lewej stronie.

## Zadania okresowe (kontener `cron`)
Co 5 min: `expire_reservations`. Co godzinę: `send_reminders`, `settle_reservations`, `expire_credentials`, `purge_logbook`
(także automatyczne zamykanie pobytów), `association_tasks`, `sales_reports`.
