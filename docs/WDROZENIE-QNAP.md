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
a na Unraid `docker compose start db app cron`.

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
- **Kopia poza QNAP**: kopie bazy są w `/share/strzelnica/backups` (codziennie, 7 ostatnich). Ustaw w **Hybrid Backup Sync**
  zadanie kopiujące ten folder (i `data/private`) na dysk USB albo do chmury — dyski w QNAP są stare.
- Migawki woluminu `DataVol1` (codziennie 3:00) dodatkowo chronią przed przypadkowym usunięciem i ransomware.
- Aktualizacja programu na QNAP (zamiast poleceń z instrukcji Unraid):

```bash
cd /share/strzelnica && wget -O tmp/strzelnica.tar.gz https://github.com/Airchi79/strzelnica-portal/archive/refs/heads/main.tar.gz && tar -xzf tmp/strzelnica.tar.gz -C tmp && cp -a tmp/strzelnica-portal-main/. /share/strzelnica/ && docker compose up -d --build
```
