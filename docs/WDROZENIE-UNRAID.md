# Wdrożenie systemu Strzelnica Sątok na Unraid (serwer domowy)

Instrukcja krok po kroku. Polecenia wpisujesz w terminalu Unraid (ikona `>_` w prawym górnym rogu panelu
WWW albo SSH). Każdy blok kodu to jedno polecenie do skopiowania.

**Czas:** ok. 1 godziny. **Potrzebne:** Unraid 6.12 lub nowszy z włączonym Dockerem, własna domena
(np. `rezerwacje.strzelnica-satok.pl`), skrzynka e-mail do wysyłki powiadomień.

---

## Jak to będzie działać

```
Telefon / komputer strzelca
        │  https://rezerwacje.twoja-domena.pl
        ▼
Cloudflare (certyfikat HTTPS, ochrona)
        │  zaszyfrowany tunel — bez otwierania portów w routerze
        ▼
Unraid w domu: kontener cloudflared ──► kontener app (port 8000)
                                         ├── db (PostgreSQL)
                                         ├── cron (zadania co godzinę)
                                         └── backup (kopia bazy raz na dobę)
```

Dlaczego Cloudflare Tunnel: system musi być dostępny z internetu (strzelcy z telefonów, kody QR,
potwierdzenia płatności Tpay), a tunel nie wymaga otwierania portów w routerze, nie ujawnia domowego
adresu IP i działa także u operatorów bez publicznego adresu IP. Jest bezpłatny.
Alternatywa (Nginx Proxy Manager + przekierowanie portów) — na końcu, w punkcie 12.

---

## 1. Doinstaluj Docker Compose

1. Panel Unraid → **Apps** (Community Applications) → wyszukaj **Docker Compose Manager** → **Install**.
2. Sprawdź w terminalu:

```bash
docker compose version
```

Powinna się pokazać wersja (np. `Docker Compose version v2…`).

## 2. Utwórz folder projektu

Folder w udziale `appdata` (domyślnie na dysku cache — szybko i bez wybudzania macierzy):

```bash
mkdir -p /mnt/user/appdata/strzelnica-portal
```

```bash
cd /mnt/user/appdata/strzelnica-portal
```

> Jeśli udział `appdata` nie jest ustawiony jako „tylko cache” (Shares → appdata → Primary storage: Cache,
> Secondary: brak), ustaw go tak. Baza danych działa najlepiej na dysku cache.

## 3. Pobierz kod z GitHuba

Repozytorium jest publiczne, więc wystarczy pobrać archiwum:

```bash
wget -O /tmp/strzelnica.tar.gz https://github.com/Airchi79/strzelnica-portal/archive/refs/heads/main.tar.gz
```

```bash
tar -xzf /tmp/strzelnica.tar.gz --strip-components=1 -C /mnt/user/appdata/strzelnica-portal
```

```bash
ls /mnt/user/appdata/strzelnica-portal
```

Powinieneś zobaczyć m.in. `docker-compose.yml`, `Dockerfile`, `.env.example`, `apps`, `docs`.

> **Jeśli zmienisz repozytorium na prywatne** (zalecane), pobierz ZIP w przeglądarce (GitHub → Code →
> Download ZIP), rozpakuj na komputerze i skopiuj zawartość do udziału sieciowego
> `\\TOWER\appdata\strzelnica-portal` (w Eksploratorze Windows). Dalsze kroki bez zmian.

## 4. Przygotuj domenę i tunel Cloudflare

1. Załóż bezpłatne konto na https://dash.cloudflare.com i dodaj swoją domenę (Cloudflare poda dwa adresy
   serwerów nazw — ustaw je u rejestratora domeny; zmiana działa zwykle w ciągu kilku godzin).
2. W Cloudflare: **Zero Trust → Networks → Tunnels → Create a tunnel** → typ **Cloudflared** → nazwa np.
   `strzelnica` → wybierz środowisko **Docker** i **skopiuj token** (długi ciąg po `--token`).
3. Jeszcze w kreatorze, zakładka **Public Hostname**:
   - Subdomain: `rezerwacje` · Domain: Twoja domena
   - Service: **HTTP** · URL: `ADRES_IP_UNRAID:8000` (np. `192.168.1.10:8000`)
   - Zapisz.

Adres IP Unraid znajdziesz w panelu (Dashboard → nazwa serwera) albo poleceniem `hostname -I`.

## 5. Uzupełnij plik `.env`

```bash
cd /mnt/user/appdata/strzelnica-portal
```

```bash
cp .env.example .env
```

Wygeneruj tajny klucz i hasło bazy (zapisz wynik — wkleisz go w następnym kroku):

```bash
python3 -c "import secrets; print('DJANGO_SECRET_KEY=' + secrets.token_urlsafe(50)); print('POSTGRES_PASSWORD=' + secrets.token_urlsafe(24))"
```

> Gdy Unraid nie ma `python3`, użyj: `openssl rand -base64 48` (dwa razy — klucz i hasło).

Otwórz plik do edycji:

```bash
nano .env
```

Zmień (strzałki — poruszanie, `Ctrl+O` `Enter` — zapis, `Ctrl+X` — wyjście):

| Ustawienie | Co wpisać |
|---|---|
| `DJANGO_SECRET_KEY` | wygenerowany klucz |
| `DJANGO_ALLOWED_HOSTS` | `rezerwacje.twoja-domena.pl` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | `https://rezerwacje.twoja-domena.pl` |
| `SITE_URL` | `https://rezerwacje.twoja-domena.pl` |
| `POSTGRES_PASSWORD` | wygenerowane hasło (ustaw **przed** pierwszym uruchomieniem) |
| `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD` | dane serwera poczty (od dostawcy skrzynki) |
| `DEFAULT_FROM_EMAIL` | np. `Strzelnica Sątok <rezerwacje@twoja-domena.pl>` |
| `TPAY_*` | na razie puste — płatności online uruchomisz później (punkt 10) |

**Poczta:** bez działającej poczty nie przyjdą linki aktywacyjne kont. Najprościej użyć skrzynki w Twojej
domenie (dane SMTP u dostawcy hostingu poczty). Gmail też działa: `smtp.gmail.com`, port `587`, login = adres,
hasło = **hasło aplikacji** (Konto Google → Bezpieczeństwo → Hasła do aplikacji).

Zabezpiecz plik — tylko administrator może go czytać:

```bash
chmod 600 .env
```

## 6. Utwórz foldery na dane

```bash
mkdir -p data/postgres data/private backups
```

```bash
chown 99:100 data/private
```

| Folder | Zawartość |
|---|---|
| `data/postgres` | baza danych |
| `data/private` | prywatne pliki (skany dokumentów do weryfikacji — usuwane po weryfikacji) |
| `backups` | codzienne kopie bazy (7 ostatnich) |

## 7. Zbuduj i uruchom

```bash
docker compose up -d --build
```

Pierwsze budowanie trwa kilka minut. Po ok. minucie sprawdź stan:

```bash
docker compose ps
```

Poprawnie: `app` i `db` mają status **(healthy)**, `cron` i `backup` — **Up**.
Test z sieci domowej (w przeglądarce): `http://ADRES_IP_UNRAID:8000/healthz` → odpowiedź `ok`.

Gdy coś nie działa — podgląd logów:

```bash
docker compose logs --tail 50 app
```

## 8. Uruchom tunel Cloudflare

Kontener `cloudflared` uruchom poleceniem (wklej swój token z punktu 4 w miejsce `TWÓJ_TOKEN`):

```bash
docker run -d --name cloudflared --restart unless-stopped cloudflare/cloudflared:latest tunnel --no-autoupdate run --token TWÓJ_TOKEN
```

> Możesz też zainstalować **cloudflared** z zakładki Apps i wkleić token w jego ustawieniach.

Po chwili w Cloudflare (Tunnels) status tunelu zmieni się na **Healthy**. Otwórz w telefonie
`https://rezerwacje.twoja-domena.pl` — powinna się pokazać strona główna strzelnicy.

## 9. Utwórz konto właściciela i skonfiguruj system

```bash
cd /mnt/user/appdata/strzelnica-portal
```

```bash
docker compose exec app python manage.py create_owner --email TWOJ@EMAIL.PL --first-name Imię --last-name Nazwisko
```

Podasz dwukrotnie hasło (min. 12 znaków; znaki nie są wyświetlane).

Następnie w przeglądarce:

1. **Zaloguj się** → zeskanuj kod QR aplikacją uwierzytelniającą (Aegis, Google/Microsoft Authenticator)
   i wpisz 6-cyfrowy kod. To logowanie dwuskładnikowe — chroni panel zarządu.
2. **Panel zarządu → Stowarzyszenie:** nazwa, adres, NIP, REGON, numer rachunku, e-mail powiadomień.
3. **Panel → Regulaminy:** wklej treść regulaminu strzelnicy, regulaminu rezerwacji i informacji RODO →
   **Publikuj**.
4. **Panel → Osie i cennik:** sprawdź osie, godziny otwarcia (obecnie 9–18) i ceny.
5. **Panel → Książka pobytu → Ustawienia i kod QR → Plakat do druku:** wydrukuj i wywieś przy wejściu.
6. Załóż próbne konto strzelca (inny e-mail), dodaj uprawnienia PS, zweryfikuj je w panelu i wykonaj
   próbną rezerwację — sprawdzisz przy okazji, czy dochodzą e-maile.

## 10. Płatności online Tpay (gdy będziesz gotowy)

1. Panel sandbox Tpay: **Integracja → API** — skopiuj *Client ID* i *Secret*;
   **Powiadomienia → Bezpieczeństwo** — skopiuj *kod zabezpieczający*.
2. Wpisz je do `.env` (`nano .env`): `TPAY_CLIENT_ID`, `TPAY_CLIENT_SECRET`, `TPAY_SECURITY_CODE`,
   `TPAY_SANDBOX=true`.
3. Zastosuj zmiany:

```bash
docker compose up -d
```

4. Wykonaj płatność testową (Saldo i płatności → Zapłać online). Po przejściu na produkcję zmień dane
   na produkcyjne i ustaw `TPAY_SANDBOX=false`.

Adres powiadomień Tpay system przekazuje sam (`https://twoja-domena/platnosci/tpay/powiadomienie/`) —
działa przez tunel Cloudflare, nie trzeba niczego otwierać.

## 11. Codzienna obsługa

**Aktualizacja do nowej wersji** (dane zostają; zmiany w bazie wykonują się automatycznie):

```bash
cd /mnt/user/appdata/strzelnica-portal
```

```bash
wget -O /tmp/strzelnica.tar.gz https://github.com/Airchi79/strzelnica-portal/archive/refs/heads/main.tar.gz && tar -xzf /tmp/strzelnica.tar.gz --strip-components=1 -C /mnt/user/appdata/strzelnica-portal
```

```bash
docker compose up -d --build
```

(Archiwum nie zawiera pliku `.env` ani folderów `data`/`backups`, więc ich nie nadpisze.)

**Kopie zapasowe:** codziennie w `backups/` (plik `satok-RRRR-MM-DD.dump`, 7 ostatnich). Skopiuj je także
poza serwer — np. wtyczką **Appdata Backup** albo zadaniem kopiującym folder `backups` na macierz lub
dysk zewnętrzny. Kopiowanie samego folderu `data/postgres` w trakcie pracy bazy **nie** jest
wiarygodną kopią — do odtwarzania używaj plików `.dump`.

**Odtworzenie bazy z kopii** (zastępuje bieżące dane!):

```bash
docker compose stop app cron
```

```bash
docker compose exec backup sh -c 'export PGPASSWORD=$POSTGRES_PASSWORD; pg_restore -U $POSTGRES_USER -d $POSTGRES_DB --clean --if-exists /backups/satok-RRRR-MM-DD.dump'
```

```bash
docker compose start app cron
```

**Zatrzymanie / uruchomienie całości:**

```bash
docker compose down
```

```bash
docker compose up -d
```

**Utrata telefonu z aplikacją 2FA:** właściciel może usunąć urządzenie innego członka zarządu w panelu
technicznym (`/techniczny/` → OTP TOTP → Devices). Dla własnego konta:

```bash
docker compose exec app python manage.py shell -c "from django_otp.plugins.otp_totp.models import TOTPDevice; TOTPDevice.objects.filter(user__email='TWOJ@EMAIL.PL').delete()"
```

Przy następnym logowaniu skonfigurujesz aplikację od nowa.

## 12. Alternatywa: Nginx Proxy Manager zamiast tunelu

Gdy nie chcesz używać Cloudflare: zainstaluj **Nginx Proxy Manager** z zakładki Apps, przekieruj w routerze
porty **80 i 443** na adres Unraid, ustaw w NPM *Proxy Host* `rezerwacje.twoja-domena.pl` →
`http://ADRES_IP_UNRAID:8000` z certyfikatem Let's Encrypt (opcja *Force SSL*). Domena musi wskazywać
na Twój publiczny adres IP (przy zmiennym IP — usługa DDNS). Ten wariant ujawnia domowy adres IP
i wymaga publicznego adresu od operatora.

## 13. Bezpieczeństwo — lista kontrolna

- [ ] Port 8000 **nie** jest przekierowany w routerze (dostęp z internetu tylko przez tunel / proxy).
- [ ] `.env` ma uprawnienia `600` i nie trafia do repozytorium ani na czat.
- [ ] Repozytorium na GitHubie ustawione jako prywatne.
- [ ] Logowanie dwuskładnikowe skonfigurowane dla każdego członka zarządu.
- [ ] Kopie z folderu `backups` kopiowane poza serwer.
- [ ] Unraid i wtyczki aktualizowane.
