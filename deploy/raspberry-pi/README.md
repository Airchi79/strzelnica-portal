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

## Wariant strzelnicy Sątok: ściana kontenera biurowego, LTE, zasilanie solarne

**Układ.** Na zewnątrz, w skrzynce z daszkiem, tylko **ekran i czytnik**. Raspberry Pi, router LTE, regulator solarny,
akumulator i przetwornica są **w środku kontenera**, tuż za ścianą (10 cm). Przez ścianę: przepust (rurka Ø32–40 mm,
uszczelniona pianką/silikonem, wylot skierowany w dół), a w nim taśma ekranu, przewód zasilania ekranu i przewód USB czytnika.
Dzięki temu na zewnątrz nie ma komputera ani zasilania, a elektronika w środku jest sucha.

**Dodatkowo do kupienia (wariant zewnętrzny):**

| # | Element | Uwagi |
|---|---|---|
| 9 | Taśma ekranu Raspberry Pi do Pi 5, **500 mm** (zamiast krótkiej z zestawu) | przechodzi przez ścianę do skrzynki |
| 10 | Skrzynka natynkowa IP65 z poliwęglanu, ok. 300×200×130 mm, z otwieranymi drzwiczkami | ekran w wycięciu w drzwiczkach, uszczelka wokół |
| 11 | Daszek nad skrzynką (blacha lub płyta) z wysięgiem ok. 15–20 cm | ekran dotykowy nie działa dobrze w deszczu |
| 12 | Zawór odpowietrzający z membraną (np. M12, IP68) i saszetki żelu krzemionkowego | ogranicza skraplanie się wody w skrzynce |
| 13 | Router LTE na 12 V, np. **Teltonika RUT241** (zasilanie 9–30 V DC) + karta SIM z pakietem danych | Pi łączone kablem Ethernet |
| 14 | **Regulator solarny MPPT** do paneli (np. Victron SmartSolar 75/15) | dobór zależy od danych paneli — patrz niżej |
| 15 | **Akumulator LiFePO4 12 V, 50–100 Ah**, z BMS **blokującym ładowanie poniżej 0 °C** (lub z grzałką) | w nieogrzewanym kontenerze to warunek konieczny |
| 16 | Przetwornica 12 V → 5 V, **min. 5 A**, wyjście USB‑C (do Raspberry Pi 5) | dobrej jakości, z zabezpieczeniem |
| 17 | Bezpieczniki samochodowe w oprawkach (akumulator, panele, odbiorniki), przewody 2,5–4 mm² | |

W tym wariancie pozycję 4 (obudowa na Pi i ekran) zastępuje skrzynka (10), a Pi leży w środku w zwykłej obudowie na Pi 5.
Zasilacz 27 W (2) przyda się do pierwszej instalacji i testów w domu; na strzelnicy Pi zasila przetwornica (16).
Przetwornica 5 V bez negocjacji USB‑PD jest traktowana przez Pi 5 jak zasilacz 3 A (prąd portów USB ograniczony do 0,6 A —
czytnikowi to wystarcza). Jeśli przetwornica ma pewne 5 A, można dopisać `usb_max_current_enable=1` w `/boot/firmware/config.txt`.

**Bilans energii przy pracy całą dobę (szacunek do weryfikacji pomiarem):** Pi 5 z ekranem i czytnikiem ok. 6–8 W, router LTE
ok. 2–4 W, straty przetwornic ok. 15% — razem ok. **280–330 Wh na dobę**. Z wygaszaniem ekranu przy bezczynności
(`sh install.sh <adres> wygaszaj`) zużycie spada, ale realnie nadal ok. 200–250 Wh na dobę.
Akumulator LiFePO4 12 V 100 Ah (ok. 1000 Wh do wykorzystania) to ok. **4–5 dni** pracy bez słońca; 2 × 100 Ah — ok. 8–10 dni.
W grudniu i styczniu 1 Wp panelu daje w Polsce średnio poniżej 1 Wh na dobę, więc do pracy całą dobę zimą potrzeba rzędu
**300–500 Wp paneli** skierowanych na południe, stromo (ok. 60°). Dwa panele z latarni mogą zimą nie wystarczyć — decyzja po danych z tabliczek.
Jeśli zabraknie: większy akumulator, dodatkowy panel albo zimowy harmonogram nocnego wyłączania (`harmonogram.sh`) tylko w listopadzie–lutym.

**Internet:** router LTE z kartą Play albo Orange — wybierz sieć, która przy ścianie kontenera ma lepszy sygnał (sprawdź telefonem z każdą kartą).
Kiosk przesyła niewiele danych (kilkaset MB miesięcznie z zapasem); przy słabym sygnale — antena zewnętrzna LTE do routera.

**Ograniczenia warunków zewnętrznych:** Raspberry Pi i ekran Touch Display 2 nie są urządzeniami przemysłowymi na mróz.
W skrzynce jest tylko ekran (nagrzewa się podświetleniem), ale przy silnym mrozie ekran może reagować wolno, a w rękawiczkach dotyk
nie działa. Ekran nie może stać w pełnym słońcu bez osłony (nagrzewanie). Na takie dni zostaje wpis telefonem z kodu QR przy wejściu.

**Harmonogram pracy** (opcjonalnie — gdy kiosk ma nie pracować całą dobę, np. zimą; wymaga baterii RTC):

```bash
sh harmonogram.sh 08:30 18:30
```

Kiosk wyłączy się o 18:30 i sam włączy o 8:30 (zegar RTC). Wpisy zrobione bez zasięgu LTE poczekają w kolejce na urządzeniu.

## Złożenie (bez lutowania)

1. Przyklej/zatrzaśnij **Active Cooler** na Pi 5 (złącze wentylatora do gniazda „FAN”).
2. Wepnij **baterię RTC** do złącza „BAT” obok gniazda USB‑C.
3. Połącz **Touch Display 2** z Pi taśmą z zestawu (w wariancie zewnętrznym: taśmą 500 mm przez przepust w ścianie) (złącze DSI/„CAM/DISP 1”, styki zgodnie z instrukcją ekranu) i przewodem zasilania ekranu (5V/GND z zestawu).
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
