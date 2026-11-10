# Przeniesienie systemu z Unraid na QNAP (TS-351, QTS 5.2, Container Station 3)

Cel: strzelnica działa na QNAP. Adres strony się nie zmienia. Strzelnica ma **własny tunel Cloudflare „qnap”**
(kontener `tunnel` w tym projekcie) — tunel „unraid” obsługuje inne usługi (kamery, Home Assistant itd.)
i **nie wolno dodawać QNAP jako jego repliki** (ruch innych usług trafiałby losowo na QNAP).
**Do ostatniego kroku Unraid zostaje nietknięty** — powrót to przeniesienie dwóch tras z powrotem.

> Stan na 02.11.2026: przeprowadzka wykonana — aplikacja, baza, kopie i tunel „qnap” działają na QNAP,
> folder `/share/strzelnica` (wolumin `DataVol1`, fizycznie `/share/CACHEDEV3_DATA/strzelnica`), `PUID=1000`, `PGID=100`.
> Na Unraid kontenery strzelnicy są zatrzymane (zapas na tydzień); `cloudflared` na Unraid działa dalej dla innych usług.

Przerwa w działaniu strony: ok. 15–30 minut (od zatrzymania aplikacji na Unraid do uruchomienia na QNAP).

## 0. Przygotowanie QNAP (raz)

- Pula pamięci i wolumin `DataVol1`, Container Station 3 — zrobione.
- *Panel sterowania → Folder udostępniony → Utwórz → Folder udostępniony*: nazwa **`strzelnica`**, wolumin `DataVol1`,
  dostęp **tylko dla Twojego konta** (odczyt/zapis), **bez** dostępu gościa.
- *Panel sterowania → Sieć i usługi plików → Telnet / SSH*: włącz SSH (po przeprowadzce wyłącz).
- Adres QNAP stały: `192.168.1.87` (rezerwacja w routerze albo statyczny IP).

## 1. Logowanie na QNAP przez SSH (z komputera z Windows: PowerShell)

```bash
ssh damian@192.168.1.87
```

Po zalogowaniu przejdź na konto administratora systemu (to samo hasło):

```bash
sudo -i
```

Sprawdź Dockera:

```bash
docker compose version
```

Jeśli pojawi się „command not found”, dodaj ścieżkę Container Station i spróbuj ponownie:

```bash
export PATH="$PATH:$(getcfg container-station Install_Path -f /etc/config/qpkg.conf)/bin"
```

Sprawdź numery użytkownika (potrzebne do `.env`) — zanotuj `uid` i `gid`:

```bash
id damian
```

## 2. Pobranie programu na QNAP

```bash
cd /share/strzelnica && mkdir -p tmp data/private backups
```

```bash
wget -O tmp/strzelnica.tar.gz https://github.com/Airchi79/strzelnica-portal/archive/refs/heads/main.tar.gz && tar -xzf tmp/strzelnica.tar.gz -C tmp && cp -a tmp/strzelnica-portal-main/. /share/strzelnica/
```

## 3. Na Unraid: zatrzymanie aplikacji i świeża kopia bazy

W terminalu Unraid (od tej chwili strona jest niedostępna):

```bash
cd /mnt/user/appdata/strzelnica-portal
```

```bash
docker compose stop app cron
```

```bash
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > /mnt/user/appdata/satok-migracja.dump
```

Sprawdź, że plik ma rozmiar większy niż 0:

```bash
ls -lh /mnt/user/appdata/satok-migracja.dump
```

## 4. Kopiowanie ustawień, dokumentów i bazy z Unraid na QNAP

Nadal w terminalu Unraid (zapyta o hasło do QNAP):

```bash
scp /mnt/user/appdata/strzelnica-portal/.env /mnt/user/appdata/satok-migracja.dump damian@192.168.1.87:/share/strzelnica/
```

```bash
scp -r /mnt/user/appdata/strzelnica-portal/data/private/. damian@192.168.1.87:/share/strzelnica/data/private/
```

> Jeśli `scp` odmówi, skopiuj te pliki przez Eksplorator Windows: udział Unraid `appdata\strzelnica-portal`
> → udział QNAP `\\192.168.1.87\strzelnica` (plik `.env`, folder `data\private` i plik `satok-migracja.dump`).

## 5. Na QNAP: ustawienia i odtworzenie bazy

W sesji SSH na QNAP (jako `sudo -i`). W pliku `.env` zmień `PUID` i `PGID` na wartości z `id damian`
(np. `PUID=1000`, `PGID=100`):

```bash
cd /share/strzelnica && vi .env
```

> W `vi`: strzałkami do linii, `i` — edycja, `Esc`, potem `:wq` i Enter — zapis.

Uprawnienia do folderów (podstaw swoje numery zamiast 1000:100):

```bash
chown -R 1000:100 /share/strzelnica/data/private /share/strzelnica/backups
```

Uruchom samą bazę i poczekaj ok. 20 sekund:

```bash
docker compose up -d db
```

Odtwórz dane z kopii:

```bash
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --clean --if-exists' < satok-migracja.dump
```

Zbuduj i uruchom całość (pierwszy raz potrwa kilka–kilkanaście minut — QNAP jest wolniejszy od Unraid):

```bash
docker compose up -d --build
```

Sprawdź stan — `app` ma być `healthy`:

```bash
docker compose ps
```

> Jeśli port 8000 jest zajęty na QNAP, ustaw w `.env` `APP_PORT=8010` i w kroku 6 użyj portu 8010.

## 6. Tunel „qnap” i przeniesienie adresów

1. Cloudflare → **Networks → Tunnels → Create a tunnel** → **Cloudflared** → nazwa `qnap` → **Create Tunnel**.
   Na ekranie instalacji (zakładka **Docker**) skopiuj **tylko** długi ciąg po `--token` — **nie wklejaj go do czatu**,
   polecenia `docker run` nie uruchamiaj.
2. Na QNAP dopisz token do `.env` (zamiast `WKLEJ_TOKEN`, zachowaj apostrofy) i uruchom tunel:

```bash
echo 'TUNNEL_TOKEN=WKLEJ_TOKEN' >> /share/strzelnica/.env
```

```bash
cd /share/strzelnica && docker compose --profile tunel up -d && sleep 15 && docker compose --profile tunel ps tunnel
```

3. W Cloudflare tunel `qnap` ma status **Healthy**.
4. Tunel **unraid → Routes**: usuń tylko trasy `rezerwacje.strzelnicasatok.pl` i `rezerwacje.airchi.pl`.
5. Tunel **qnap → Routes → Add route → Published application**: `rezerwacje` / `strzelnicasatok.pl`, typ **HTTP**,
   URL **`app:8000`** (potwierdź nadpisanie rekordu DNS); to samo dla `rezerwacje` / `airchi.pl`.
6. Sprawdź stronę w telefonie (na danych komórkowych). Jeśli przeglądarka pokazuje „Brak połączenia z internetem” —
   to zapamiętana strona z chwili przełączenia: **Ctrl+F5**.

**Powrót na Unraid:** trasy z tunelu `qnap` z powrotem do `unraid` (URL `192.168.1.9:8000`),
a na Unraid `docker compose start db app cron` (tamtejszy tunel łączy się po adresie IP — na Unraid w `.env` musi być
`APP_BIND=0.0.0.0`, inaczej aplikacja po aktualizacji nasłuchuje tylko na 127.0.0.1).

## 7. Sprzątanie na Unraid (po tygodniu bez problemów)

Kontenerów `cloudflared` i innych usług na Unraid **nie wyłączaj**. Usuń tylko kontenery i obraz strzelnicy
(obraz zawiera kopię danych z czasu budowy):

```bash
cd /mnt/user/appdata/strzelnica-portal && docker compose down && docker image rm strzelnica-portal:latest
```

Dane na Unraid zostają (folder `appdata/strzelnica-portal`) — **nie usuwaj ich przez co najmniej tydzień**.
Po tym czasie Unraid można wyłączyć.

## 8. Po przeprowadzce

- **Wyłącz SSH** na QNAP.
- **Kopia poza QNAP** — rozdział 9 poniżej.
- Migawki woluminu `DataVol1` (codziennie 3:00) dodatkowo chronią przed przypadkowym usunięciem i ransomware.
- Aktualizacja programu na QNAP — produkcja pobiera wyłącznie gałąź `produkcja` (sprawdzoną w kopii testowej,
  zob. `docs/KOPIA-TESTOWA.md`):

```bash
cd /share/strzelnica && wget -O tmp/strzelnica.tar.gz https://github.com/Airchi79/strzelnica-portal/archive/refs/heads/produkcja.tar.gz && tar -xzf tmp/strzelnica.tar.gz -C tmp && cp -a tmp/strzelnica-portal-produkcja/. /share/strzelnica/ && docker compose up -d --build
```

## 9. Kopia zapasowa poza QNAP (Hybrid Backup Sync)

**Co kopiujemy:** `/share/strzelnica/backups` (codzienne kopie bazy, 7 ostatnich — kilka MB) i
`/share/strzelnica/data/private` (skany dokumentów). **Nie** kopiujemy `data/postgres` (pliki działającej bazy — kopia byłaby
niespójna; od tego są pliki z `backups`). Razem kilkanaście MB — kopia trwa sekundy.

**Plik `.env`** (hasło bazy, klucz aplikacji, hasło poczty) jest potrzebny do odtworzenia systemu. Zapisz jego treść raz
w menedżerze haseł (np. Bitwarden, KeePass) i aktualizuj po każdej zmianie. Nie wysyłaj go e-mailem ani czatem.

**Gdzie:** najlepiej **oba** miejsca — dysk USB (szybkie odtworzenie) i chmura (pożar, kradzież, przepięcie zabiera też USB).
Kopie zawierają dane osobowe — **zawsze z szyfrowaniem** (hasło szyfrowania zapisz w menedżerze haseł; bez niego kopii nie odczytasz).

### A. Dysk USB
1. Podłącz dysk (pendrive 32 GB+ albo dysk zewnętrzny) do portu USB z tyłu QNAP.
2. *Pamięć masowa i migawki → Zewnętrzna pamięć masowa* → dysk → **Formatuj** → system plików **NTFS** lub **exFAT**
   (czytelny także w Windows) → etykieta np. `KOPIA-STRZELNICA`. Formatowanie usuwa zawartość dysku.
3. *App Center* → zainstaluj **Hybrid Backup Sync 3** (jeśli go nie ma) → otwórz.
4. **Kopia zapasowa i przywracanie → Utwórz → Nowe zadanie kopii zapasowej**.
5. **Źródło:** zaznacz `strzelnica/backups` i `strzelnica/data/private`.
6. **Miejsce docelowe:** **Lokalny NAS** → folder na dysku USB (np. `KOPIA-STRZELNICA/strzelnica`).
7. **Harmonogram:** codziennie, **04:30** (po migawce o 3:00).
8. **Zasady / opcje:** włącz **szyfrowanie po stronie klienta** (hasło!), **wersjonowanie** — „Inteligentne wersjonowanie”
   albo 30 wersji.
9. **Powiadomienia:** e-mail przy błędzie zadania (wymaga ustawionej poczty w *Panel sterowania → Centrum powiadomień*;
   można użyć tego samego konta co aplikacja: serwer LH, port 465, SSL).
10. **Utwórz** → **Uruchom teraz** → status „Ukończono”.

### B. Chmura — Dysk Google (zalecana druga kopia, bez kosztów)
Danych jest kilkanaście MB, więc wystarczy darmowe 15 GB konta Google. Najlepiej **osobne konto Google stowarzyszenia**
(np. `kopia.strzelnicasatok@gmail.com`) zamiast prywatnego — dostęp nie zależy wtedy od jednej osoby. Hasło do konta
i hasło szyfrowania kopii zapisz w menedżerze haseł zarządu.

1. **Konto Google:** zaloguj się (albo załóż konto) i włącz weryfikację dwuetapową (*Konto Google → Bezpieczeństwo*).
2. QNAP → *App Center* → **Hybrid Backup Sync 3** (zainstaluj, jeśli go nie ma) → otwórz.
3. **Dodaj miejsce docelowe:** w HBS 3 *Pamięć masowa* (lub *Miejsca docelowe*) → **Dodaj / Utwórz** → **Google Drive** →
   **Dodaj konto / Uwierzytelnij** → otworzy się logowanie Google → zaloguj się kontem z punktu 1 → **Zezwól** na dostęp HBS 3.
4. **Kopia zapasowa i przywracanie → Utwórz → Nowe zadanie kopii zapasowej**.
5. **Źródło:** zaznacz `strzelnica/backups` i `strzelnica/data/private` (to samo co w punkcie A; **bez** `data/postgres`).
6. **Miejsce docelowe:** konto Google Drive z punktu 3 → **Utwórz folder** `strzelnica-kopia` → wybierz go.
7. **Harmonogram:** codziennie **05:00** (po nocnej kopii bazy i po kopii na USB).
8. **Zasady / opcje:**
   - **Szyfrowanie po stronie klienta — obowiązkowo** (kopie zawierają dane osobowe; Google widzi tylko zaszyfrowane pliki).
     Ustaw długie hasło i od razu zapisz je w menedżerze haseł — **bez niego kopii nie odtworzysz**.
   - **Wersjonowanie:** „Inteligentne wersjonowanie” albo 30 wersji (pozwala wrócić do kopii sprzed kilku tygodni).
9. **Powiadomienia:** e-mail przy błędzie zadania (jak w punkcie A.9).
10. **Utwórz** → **Uruchom teraz** → status „Ukończono”.
11. **Sprawdź na Dysku Google** (drive.google.com, konto z punktu 1): jest folder `strzelnica-kopia` z plikami — ich nazwy
    i treść są nieczytelne (zaszyfrowane), tak ma być.
12. Wykonaj próbę odtworzenia z punktu C, wybierając zadanie z Dysku Google.

Nazwy przycisków w HBS 3 mogą się nieznacznie różnić zależnie od wersji — szukaj odpowiedników „Miejsce docelowe / Google Drive /
Nowe zadanie kopii zapasowej”. Inne chmury działają tak samo: **OneDrive** albo **Backblaze B2** (płatność za GB — przy tej
ilości danych grosze miesięcznie).

### C. Próba odtworzenia (raz, potem co kilka miesięcy)
W HBS 3 → zadanie → **Przywróć** → wybierz najnowszą wersję → przywróć jeden plik `.dump` do **innego** folderu
(np. `Public/test-przywracania`) → sprawdź, że plik ma rozmiar podobny jak w `backups`. Kopia, której nie dało się
odtworzyć, nie jest kopią. Po teście usuń przywrócony plik.

### D. Automatyczne sprawdzanie kopii (od listopada 2026)
Kontener `backup` co dobę robi kopię i **od razu ją odtwarza** do tymczasowej bazy `satok_verify`, porównuje liczbę
danych z bazą produkcyjną i usuwa tymczasową bazę. Wynik: panel → *System → Kopie zapasowe* (oraz kafelek w *Przeglądzie*).
Gdy kopia jest uszkodzona albo od 36 h nie powstała nowa — zarząd dostaje alarm e-mailem (raz na dobę).

Ręczne uruchomienie (np. po zmianach):
```bash
cd /share/strzelnica && docker compose exec backup sh /backup.sh --once
```

### E. Awaryjne odtworzenie bazy z kopii
Gdy baza jest uszkodzona (albo trzeba cofnąć się do wczorajszego stanu). **Odtworzenie zastępuje bieżącą bazę.**
1. Zatrzymaj aplikację (baza zostaje włączona):
```bash
cd /share/strzelnica && docker compose stop app cron
```
2. Wybierz plik kopii (najnowszy jest na górze):
```bash
ls -lt /share/strzelnica/backups/
```
3. Odtwórz (w miejsce `satok-RRRR-MM-DD.dump` wpisz nazwę pliku):
```bash
cd /share/strzelnica && docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --clean --if-exists' < backups/satok-RRRR-MM-DD.dump
```
4. Uruchom aplikację i sprawdź logowanie oraz ostatnie rezerwacje:
```bash
cd /share/strzelnica && docker compose up -d
```
Po awarii całego QNAP: zainstaluj program według punktów 0–2 i 5 na nowym serwerze, wgraj `.env` z menedżera haseł,
przywróć z HBS 3 folder `backups` i `data/private`, a potem wykonaj kroki 3–4 powyżej.

## 10. Uruchomienie systemu: usunięcie danych z okresu testów

Przed zastąpieniem GunTrackera usuń wszystko, co powstało podczas prób: rezerwacje, książkę pobytu (wpisy, korekty,
wydruki dzienne), płatności i salda, raporty wpływów, kiosk i skarbonkę, wydarzenia, ogłoszenia, usterki, blokady osi,
karnety, kluby i kody, składki, deklaracje członkowskie, powiadomienia, e-maile, dziennik zdarzeń oraz wszystkie konta
poza Twoim. Z Twojego konta znikają dane strzelca (legitymacje, kartoteka, akceptacje regulaminów) — konto panelu
działa wyłącznie jako panel. Numeracja rezerwacji, wpisów i raportów zaczyna się od 1.

**Zostaje:** import z GunTrackera (archiwum książki i rezerwacji z ich blokadami osi, kartoteka osób z importu),
członkowie, uchwały, zebrania, regulaminy, ustawienia, osie, godziny, cennik, sparowane kioski, FAQ, wygląd oraz Twoje
konto panelu (uprawnienia, logowanie 2FA, powiązanie z członkiem stowarzyszenia — głosowania zarządu).

1. Zrób świeżą kopię bazy (bez niej polecenie odmówi): `cd /share/strzelnica && docker compose exec backup sh /backup.sh --once`
2. Raport — nic nie usuwa, pokazuje liczby i ewentualne przeszkody:
   ```
   docker compose exec app python manage.py purge_test_data --zostaw TWOJ@ADRES.pl
   ```
3. Usunięcie (można powtarzać, dopóki trwają testy):
   ```
   docker compose exec app python manage.py purge_test_data --zostaw TWOJ@ADRES.pl --wykonaj
   ```
4. Ostatnie czyszczenie w dniu przejścia z GunTrackera — dodatkowo blokuje polecenie na stałe:
   ```
   docker compose exec app python manage.py purge_test_data --zostaw TWOJ@ADRES.pl --wykonaj --koniec-testow
   ```

Polecenie działa w jednej transakcji: przy jakimkolwiek błędzie nic nie zostaje usunięte. Odmawia, gdy: nie ma świeżej
kopii bazy, podane konto nie jest kontem właściciela, w bazie są prawdziwe (nie testowe) płatności Tpay (świadome
usunięcie: `--z-prawdziwymi-platnosciami`) albo uchwała jest powiązana z deklaracją członkowską złożoną w systemie.
Import z GunTrackera najlepiej wykonać **po** ostatnim czyszczeniu (pkt 4) — import nie jest usuwany, ale tak jest prościej
sprawdzić, że dane są kompletne.
