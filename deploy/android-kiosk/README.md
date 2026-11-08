# Kiosk na telefonie lub tablecie z Androidem (Samsung)

Kiosk działa w aplikacji „Kiosk” zainstalowanej z przeglądarki Chrome (pełny ekran, bez paska adresu) i **przypiętej**
w Androidzie. Odpiąć ją — i wyjść do ekranu głównego, ustawień czy innych aplikacji — może tylko osoba, która zna
**PIN urządzenia** (admin). Strona kiosku dodatkowo: przycisk Wstecz wraca do ekranu startowego kiosku (nie wychodzi),
linki są zablokowane, przytrzymanie palca nie otwiera menu, ekran nie gaśnie systemowo (zamiast tego czarny wygaszacz
po 3 minutach bezczynności — budzi go dotyk albo skan kodu).

Nazwy opcji poniżej są z nakładki Samsung One UI i mogą się nieznacznie różnić w zależności od wersji.

## 1. Przygotowanie urządzenia (raz)

1. **Bez prywatnych danych:** osobne konto Google tylko dla kiosku (albo bez konta), bez poczty i komunikatorów.
2. **Blokada ekranu:** *Ustawienia → Ekran blokady → Typ blokady ekranu → PIN* (najlepiej 6 cyfr) — znają go tylko admini.
3. **Przypinanie aplikacji:** *Ustawienia → Zabezpieczenia i prywatność → Więcej ustawień zabezpieczeń → Przypnij aplikację*
   → włącz oraz **„Pytaj o PIN przed odpięciem”**.
4. **Wyświetlacz:** *Wygaszanie ekranu* — 10 minut (strona kiosku i tak utrzymuje ekran włączony); jasność automatyczna.
5. **Powiadomienia:** tryb *Nie przeszkadzać* włączony na stałe; na ekranie blokady — bez powiadomień.
6. **Bateria:** *Ustawienia → Bateria → Ochrona baterii* (ładowanie do 80–85% — urządzenie stale podłączone do zasilania);
   *Aplikacje → Chrome → Bateria → Bez ograniczeń*.
7. **Wyłącz zbędne:** panele krawędzi (Edge), podwójne naciśnięcie klawisza bocznego, asystenta głosowego.
8. **Aktualizacje systemu:** bez automatycznej instalacji — po każdym restarcie Android wymaga PIN-u (patrz pkt 4).
9. Jeśli jest dostępna: *Zabezpieczenia → „Zablokuj sieć i zabezpieczenia”* (wyłączenie urządzenia tylko po odblokowaniu).

## 2. Instalacja kiosku

1. W panelu: *Książka pobytu → Kioski → Dodaj urządzenie* — zanotuj kod parowania (ważny 15 minut).
2. W Chrome otwórz `https://rezerwacje.strzelnicasatok.pl/kiosk/` i wpisz kod.
3. Menu Chrome (⋮) → **„Dodaj do ekranu głównego” → „Zainstaluj”**. Na ekranie głównym pojawi się aplikacja **Kiosk**.
4. Otwórz **Kiosk** z ekranu głównego (otworzy się na pełnym ekranie) i zezwól na użycie kamery.
5. W panelu przy urządzeniu ustaw *Kamera do kodów QR → przednia*.
6. **Przypnij:** *Ostatnie aplikacje* → dotknij ikony Kiosku nad jego kartą → **„Przypnij tę aplikację”**.

## 3. Wyjście z kiosku (tylko admin)

- Nawigacja przyciskami: przytrzymaj jednocześnie **Wstecz** i **Ostatnie aplikacje**.
- Nawigacja gestami: przesuń palcem od dołu ekranu w górę i przytrzymaj.

Pojawi się ekran blokady — po wpisaniu PIN-u aplikacja jest odpięta. Powrót do kiosku: otwórz Kiosk i przypnij ponownie (pkt 2.6).

## 4. Po restarcie lub zaniku zasilania

Android po każdym uruchomieniu pokazuje ekran blokady i **wymaga PIN-u** — tego nie da się obejść bez płatnego
oprogramowania do zarządzania urządzeniami. Admin odblokowuje urządzenie, otwiera Kiosk i przypina go ponownie.
Telefon lub tablet ma własny akumulator, więc przy zasilaniu z instalacji solarnej restart powinien być rzadki
(głównie po aktualizacji systemu — dlatego aktualizacje instaluj ręcznie, przy okazji wizyty na strzelnicy).

## 5. Sprawdzenie po instalacji

- Przyciski / gesty Wstecz, Ekran główny, Ostatnie aplikacje — nie wychodzą z kiosku.
- Wstecz w trakcie wpisu lub rezerwacji — wraca do ekranu startowego kiosku.
- Przeciągnięcie paska powiadomień — nic nie otwiera.
- Po 3 minutach bezczynności ekran startowy przechodzi w czarny wygaszacz; dotknięcie go budzi (bez naciśnięcia przycisku pod spodem).
- Wyjście z kiosku wymaga PIN-u.

Jeśli w tych próbach da się wyjść z kiosku bez PIN-u, kolejnym krokiem jest dedykowana przeglądarka kioskowa
(np. Fully Kiosk Browser — licencja jednorazowa, rzędu kilku euro na urządzenie; wymagałaby zmiany sposobu odczytu
kodów QR kamerą, bo działa na innym silniku niż Chrome).
