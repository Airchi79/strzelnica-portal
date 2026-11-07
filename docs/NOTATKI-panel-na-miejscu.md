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
