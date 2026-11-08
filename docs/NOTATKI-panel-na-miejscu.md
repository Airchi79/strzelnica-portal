# Panel na strzelnicy (rezerwacja na miejscu) — punkty zaczepienia w kodzie

Notatka dla prac nad panelem na miejscu (osobny etap). Opisuje, co już jest gotowe po stronie systemu.

## Rezerwacja „na miejscu”

- `apps.bookings.services.create_reservation(user, lane, day, start_hour, end_hour, persons, ..., channel=CHANNEL_ONSITE)`
  — `CHANNEL_ONSITE = "na_miejscu"`. Różnica wobec rezerwacji online (`CHANNEL_ONLINE`, domyślnie):
  - **bez** limitu „rezerwacja online najpóźniej [h] przed rozpoczęciem” (`BookingSettings.min_lead_hours`);
  - **z** poszanowaniem terminów trzymanych dla listy oczekujących;
  - pozostałe reguły bez zmian: godziny otwarcia, blokady, `max_advance_days`, liczba osób, zasady grup strzelców
    (zatwierdzanie, sposoby płatności), prowadzący od strzelnicy (`needs_officer`), kody wejścia.
- Obsługa w panelu zarządu (`created_by` ≠ `user`) rezerwuje bez limitu czasu i ponad terminy listy oczekujących.
- Wycena przed potwierdzeniem: `services.quote(..., user=..., enforce_lead=False, respect_holds=True)`.
- Kto może rezerwować: `services.check_can_book(user)` (lista powodów) i `services.booking_readiness(user)` (lista kontrolna).

## Inne gotowe elementy

- Kiosk (książka pobytu przy wejściu): `apps/kiosk` — parowanie urządzeń, praca offline, podpis.
- Książka pobytu: `apps/logbook/services.py` (wpis, automatyczne zamykanie pobytów), „Teraz na strzelnicy”: `apps/logbook/now.py`.
- Płatności na miejscu: `apps.payments.services.register_manual_payment(user, amount, method, by, reservation=...)`;
  online: widok `payments:tpay_start` (doliczana opłata za płatność online wg ustawień — `online_fee`).
- Powiadomienia: `apps.core.notifications.notify_user / notify_board` (e-mail + dzwonek + push).

## Do decyzji przy panelu na miejscu

- Weryfikacja tożsamości i dokumentów „do kamery” (kto zatwierdza: obsługa zdalnie czy automat) i zapis tego w dzienniku.
- Sprzedaż na miejscu / vouchery — po konsultacji z księgową (stowarzyszenie zwykłe).

## Rezerwacja na miejscu w kiosku — etap 1 (część serwerowa)

Decyzje właściciela (listopad 2026): tylko osoby z uprawnieniami PS; konto zakładane w tle po kodzie z e-maila;
najpierw weryfikacja legitymacji, potem płatność; bez weryfikacji w ciągu **15 minut** rezerwacja anuluje się sama
bez opłat; zdjęcie legitymacji usuwane po weryfikacji; tylko dziś, od bieżącej godziny; kod e-mail (nie SMS).

- Logika: `apps/kiosk/onsite.py` (model przebiegu `OnsiteBooking`), API: `/kiosk/api/na-miejscu/…`
  (`terminy`, `start`, `kod`, `regulamin`, `oswiadczenie`, `legitymacja`, `rezerwacja`, `stan`).
- `create_reservation(..., channel=CHANNEL_ONSITE, ps_pending=True)` — rezerwacja „do zatwierdzenia” bez prowadzącego
  od strzelnicy, gdy legitymacja czeka na weryfikację.
- Zatwierdzenie legitymacji w module dokumentów potwierdza czekającą rezerwację, odrzucenie ją anuluje
  (`credential_reviewed`). Wygaszanie i czyszczenie danych: `expire_reservations` (co minutę).
- Treść oświadczenia: `onsite.STATEMENT_TEXT` — do akceptacji przez właściciela.
- Etap 2 (płatność): po weryfikacji legitymacji `onsite.approve_for_payment` — termin trzymany do płatności online
  (czas z ustawień „płatność online”), także w trakcie trwającej godziny; kiosk pokazuje kod QR z linkiem
  `/platnosci/rezerwacja/<token>/` (podpisany, ważny 2 h) — płatność Tpay na telefonie osoby, bez logowania.
  Wspólna logika z płatnością z konta: `payments.services.reservation_online_amount` i `start_tpay`.
  Nieopłacona w czasie — zwalniana przez `expire_reservations`. Termin trzymany dla rezerwacji w bieżącej godzinie
  liczony do jej końca (`bookings.hold_limit`), nie do początku.
- Etap 3 (ekrany kiosku): `static/js/kiosk-onsite.js` + sekcje `os-*` w `templates/kiosk/kiosk.html`.
  Kamera z ramką: ocena klatki w przeglądarce (jasność, odblask, ostrość, krawędzie karty przy ramce, bezruch),
  zdjęcie samo po ok. 1 s stabilnego ustawienia, przycisk ręczny po 12 s. Progi w `LIMITS` — do dostrojenia na
  urządzeniu: adres `/kiosk/#diag` pokazuje wskaźniki pod podglądem kamery.
- Etap 4 (panel obsługi): strona weryfikacji dokumentu pokazuje baner „Osoba czeka przy kiosku” z odliczaniem
  i danymi podanymi w kiosku; takie legitymacje są pierwsze w kolejce; kafelek „Czekają przy kiosku” na stronie
  głównej panelu; jedno pilne powiadomienie zamiast trzech; `approve_reservation` dla rezerwacji czekającej
  w kiosku działa wg zasad kiosku (także w trwającej godzinie, potem płatność). W kiosku wystawca jako przyciski
  PZSS / PZŁ / LOK / Inny, bez pola „ważna do” (uprawnienia PS są bezterminowe; wygaśnięcia, np. po skreśleniu
  z PZSS, obsługa nie jest w stanie sprawdzić — weryfikator PZSS sprawdza dziś tylko licencje zawodnicze).
- Etap 5 (do zrobienia) — automatyczne porównanie danych z legitymacji (OCR, Tesseract na serwerze), decyzje:
  zgodność → zatwierdzenie od razu, oznaczone „automatycznie” do przejrzenia; niezgodność → zwykła weryfikacja
  (15 min); automat tylko akceptuje (odmowę podejmuje człowiek — art. 22 RODO). Legitymacje LOK i PZSS zawierają
  PESEL: zdjęcie usuwane od razu także po zatwierdzeniu automatycznym, zapisywane tylko odczytane dane bez PESEL.
  Odczyt niezależny od układu (wzory: PZŁ „Nr …/PS”, „Kol. …”; LOK „0079/BZGLOK/2022”; PZSS „98655/2024”).
