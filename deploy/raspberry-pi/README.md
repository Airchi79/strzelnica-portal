# Kiosk książki pobytu na Raspberry Pi

Urządzenie przy wejściu: ekran dotykowy z formularzem wpisu do książki pobytu i czytnik kodów QR (rezerwacja albo osobisty kod z konta).
Działa na tej samej stronie co kiosk na tablecie: `https://rezerwacje.strzelnicasatok.pl/kiosk/`.
Bez internetu wpisy zapisują się na urządzeniu i wysyłają same po powrocie sieci (najwyżej 72 h wstecz).

## Zestaw do kupienia

| # | Element | Uwagi |
|---|---|---|
| 1 | Raspberry Pi 5, 4 GB | |
| 2 | Oficjalny zasilacz Raspberry Pi 27 W USB‑C | inne zasilacze często powodują restarty |
| 3 | Raspberry Pi Touch Display 2, 7" | podłączany taśmą DSI (w zestawie) |
| 4 | Obudowa na Pi 5 i Touch Display 2 (np. Waveshare) | sprawdź w opisie zgodność z **Display 2** i **Pi 5** |
| 5 | Raspberry Pi Active Cooler | chłodzenie procesora |
| 6 | Karta microSD 32 GB, A2 (Raspberry Pi lub SanDisk Extreme) | |
| 7 | Bateria RTC do Pi 5 (oficjalna) | prawidłowa godzina wpisów, gdy nie ma internetu |
| 8 | Waveshare 2D Barcode Scanner Module (USB) | czyta kody QR także z ekranu telefonu; działa jak klawiatura |

Do instalacji przyda się na chwilę zwykła klawiatura USB.

## Złożenie (bez lutowania)

1. Przyklej/zatrzaśnij **Active Cooler** na Pi 5 (złącze wentylatora do gniazda „FAN”).
2. Wepnij **baterię RTC** do złącza „BAT” obok gniazda USB‑C.
3. Połącz **Touch Display 2** z Pi taśmą z zestawu (złącze DSI/„CAM/DISP 1”, styki zgodnie z instrukcją ekranu) i przewodem zasilania ekranu (5V/GND z zestawu).
4. Zamknij całość w **obudowie**.
5. Podłącz **czytnik QR** do portu USB.
6. Włóż kartę microSD z systemem (krok niżej) i podłącz **zasilacz 27 W**.

## Instalacja systemu i kiosku

1. Na komputerze uruchom **Raspberry Pi Imager**, wybierz *Raspberry Pi 5* → *Raspberry Pi OS (64-bit)* z pulpitem.
   W ustawieniach (koło zębate): nazwa użytkownika i hasło, sieć Wi‑Fi strzelnicy, strefa czasowa *Europe/Warsaw*, układ klawiatury *pl*.
2. Uruchom Pi, otwórz Terminal i wykonaj:

   ```bash
   curl -fsSLO https://raw.githubusercontent.com/Airchi79/strzelnica-portal/main/deploy/raspberry-pi/install.sh
   ```

   ```bash
   sh install.sh https://rezerwacje.strzelnicasatok.pl/kiosk/
   ```

   ```bash
   sudo reboot
   ```

3. Po restarcie pojawi się ekran **Parowanie kiosku**. W panelu zarządu: *Książka pobytu → Kioski → Dodaj urządzenie*,
   wpisz pokazany 8-cyfrowy kod na ekranie Pi (kod ważny 15 minut).
4. Klawiatura ekranowa: w `sudo raspi-config` → *Display Options* włącz klawiaturę ekranową (jeśli tej opcji nie ma w Twojej wersji
   systemu, zainstaluj `sudo apt install squeekboard`).

## Obsługa

- **Wpis**: osoba dotyka „Wpis do książki pobytu” albo przykłada kod QR rezerwacji / „Mój kod QR” do czytnika — formularz otworzy się sam.
- **Wyjście**: „Wyjście ze strzelnicy” → początek nazwiska → wybór osoby.
- Ekran wraca do startu po 2 minutach bezczynności (dane z formularza są czyszczone).
- **Zgubione / skradzione urządzenie**: w panelu *Kioski → Wyłącz* — urządzenie natychmiast traci dostęp.
- **Ponowne parowanie** (np. po reinstalacji): *Kioski → Nowy kod*.
