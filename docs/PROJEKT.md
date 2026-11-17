# Strzelnica Sątok — dokument projektu (stan na 16.11.2026)

Jeden plik, z którego można podjąć dalszą pracę nad systemem i jego wdrażaniem: zasady pracy, architektura, moduły,
reguły biznesowe, bezpieczeństwo, eksploatacja, wyniki audytu i otwarte sprawy. Szczegóły poszczególnych obszarów są
w dokumentach wymienionych w ostatnim rozdziale — ten plik mówi, co gdzie jest i dlaczego tak.

> Dokument nie zawiera sekretów ani danych osobowych. Wartości z `.env` (hasła, tokeny, klucze) są wyłącznie na serwerze.

---

## 1. Czym jest system

Portal stowarzyszenia zwykłego „STRZELNICA SĄTOK”:
- **strzelcy:** konta, weryfikacja uprawnień prowadzącego strzelanie (PS) i pozwoleń, regulaminy, rezerwacje osi,
  płatności (online, przelew z kodem QR, na miejscu), elektroniczna książka pobytu, kiosk przy wejściu;
- **zarząd (panel):** rezerwacje i blokady osi, wydarzenia/zawody, książka pobytu, weryfikacja dokumentów, kartoteka
  strzelców, płatności i salda, raporty wpływów, **księgowość (uproszczona ewidencja przychodów i kosztów)**,
  stowarzyszenie (członkowie, deklaracje, składki, uchwały i głosowania, zebrania, kluby), statystyki, RODO, kopie zapasowe;
- **migracja z GunTrackera:** archiwum rezerwacji i książki pobytu, sprzątanie kartoteki, wykaz zweryfikowanych PS.

Adresy: produkcja `https://rezerwacje.strzelnicasatok.pl`, kopia testowa `https://test.strzelnicasatok.pl`.
Repozytorium: `Airchi79/strzelnica-portal` (GitHub).

---

## 2. Zasady pracy (obowiązują każdą osobę i każdą sesję AI)

1. **Gałęzie:** zmiany → PR do `main` (CI musi być zielone, scalanie „squash”) → kopia testowa pobiera `main` →
   po sprawdzeniu właściciel pisze **„wdrażamy”** → gałąź `produkcja` przesuwana na sprawdzony `main`:
   `git push origin origin/main:refs/heads/produkcja`. Produkcja nigdy nie pobiera `main`.
2. **Wdraża wyłącznie właściciel** (na serwerze). Nikt inny nie zmienia niczego na serwerach.
3. **Sekrety** (`.env`: klucz Django, hasło bazy, poczta, Tpay/Axepta, token KSeF, token tunelu) — tylko na serwerze;
   ustawiane skryptem `sh deploy/ustaw-env.sh NAZWA` (wartość nie pojawia się na ekranie). Nigdy w czacie ani w repozytorium.
4. **Dane osobowe** (wyciągi, eksporty Stripe/GunTracker, skany legitymacji): analizowane tylko lokalnie, wyniki
   zagregowane/zamaskowane, nigdy w repozytorium. Tekst OCR legitymacji (zawiera PESEL) nie jest zapisywany ani zwracany.
5. **Wyniki testów** podaje się tylko z faktycznie wykonanych testów.
6. Raporty i komunikaty dla właściciela — po polsku.

**Aktualizacja produkcji (serwer QNAP):**
```
cd /share/strzelnica && rm -rf tmp/strzelnica-portal-produkcja && wget -O tmp/strzelnica.tar.gz https://github.com/Airchi79/strzelnica-portal/archive/refs/heads/produkcja.tar.gz && tar -xzf tmp/strzelnica.tar.gz -C tmp && cp -a tmp/strzelnica-portal-produkcja/. /share/strzelnica/ && docker compose up -d --build
```
**Aktualizacja kopii testowej:** `sh /share/CACHEDEV3_DATA/strzelnica-test/deploy/kopia-testowa.sh --kod`
(szczegóły i odświeżanie danych: `docs/KOPIA-TESTOWA.md`). Migracje bazy wykonują się same przy starcie kontenera `app`.

---

## 3. Architektura

**Stos:** Python 3.12, Django 5.2 LTS, PostgreSQL 16, strony renderowane na serwerze + PWA (bez frameworka JS),
gunicorn, whitenoise. Biblioteki: `requirements.txt` (wersje przypięte).

**Kontenery (`docker-compose.yml`):**
| Usługa | Rola |
|---|---|
| `db` | PostgreSQL 16, dane w `./data/postgres` |
| `app` | gunicorn (3 procesy, `--max-requests 2000` — okresowa wymiana), port tylko `127.0.0.1:8000` (lub `APP_BIND`) |
| `cron` | `python manage.py run_scheduler` — zadania okresowe w jednym procesie (rozdz. 7.3) |
| `backup` | codzienny `pg_dump` + próbne odtworzenie (`deploy/backup.sh`), 7 kopii w `./backups`, stan w `backups/status.json` |
| `tunnel` | (profil `tunel`) Cloudflare Tunnel — jedyna droga z internetu |

**Dane trwałe:** `./data/postgres` (baza), `./data/private` (skany legitymacji, zdjęcia usterek, załączniki uchwał —
podawane wyłącznie przez widoki z kontrolą uprawnień), `./backups`.

**Sieć:** internet → Cloudflare → tunel → `app:8000`. `TRUST_X_FORWARDED_FOR=true` (adres klienta z ostatniego wpisu
`X-Forwarded-For`) — wymagane, by limity prób działały na adres osoby, a nie wspólny adres tunelu.

**Przeglądarka:** CSP bez skryptów wbudowanych (`script-src 'self'`, cały JS w `static/js/*.js`), `connect-src 'self'`,
`frame-ancestors 'none'` (wyjątek: widżet), HSTS, ciasteczka `Secure`/`HttpOnly`, `X-Frame-Options: DENY`.
Service worker (`templates/pwa/sw.js`) przechowuje offline tylko stronę kiosku i zasoby statyczne — bez danych osobowych.

**Transakcje:** `ATOMIC_REQUESTS=True` (każde żądanie w transakcji), kwoty w groszach (liczby całkowite).
Niezmienialność ważnych tabel (dziennik zdarzeń, książka pobytu, historia zapisów ewidencji) — triggery w bazie;
usuwanie po okresie przechowywania tylko z przełącznikiem `set_config('satok.logbook_purge','on', true)` w tej samej transakcji.

---

## 4. Moduły (`apps/`)

| Moduł | Zakres | Ważne pliki |
|---|---|---|
| `accounts` | konta, logowanie e-mailem, 2FA TOTP (panel), limity prób, reset hasła | `views.py`, `ratelimit.py`, `backends.py` |
| `credentials` | legitymacje PS i pozwolenia, weryfikacja przez zarząd, wygasanie, OCR ze zdjęcia/PDF, wykaz zweryfikowanych PS (automatyczne zatwierdzenie) | `views.py`, `forms.py`, `preverified.py` |
| `regulations` | wersjonowane regulaminy, wymuszona akceptacja | |
| `bookings` | rezerwacje osi, blokady, lista oczekujących, rozliczenia, kalendarz ICS, archiwum GunTrackera | `services.py`, `archive.py`, `feed.py` |
| `logbook` | elektroniczna książka pobytu (QR, podpis, wydruki dzienne z łańcuchem sum), archiwum księgi GunTrackera | `services.py`, `daily_print.py`, `now.py` |
| `kiosk` | aplikacja kiosku (parowanie urządzenia, wpis, wyjście, rezerwacja na miejscu: najpierw zdjęcie legitymacji → OCR → formularz) | `views.py`, `onsite.py`, `ocr.py`, `static/js/kiosk*.js` |
| `payments` | wpłaty (Tpay, **Axepta**, przelew, gotówka, BLIK), saldo, zwroty, kod QR przelewu (standard ZBP) | `services.py`, `axepta.py`, `tpay.py`, `gateway.py`, `transfer_qr.py` |
| `reports` | dzienne raporty wpływów `RS/D/RRRR/NNNN` | `services.py` |
| `accounting` | UEPiK: zestawienie, uzupełniające, środki trwałe, KSeF (import + API 2.0), wyciąg BNP, Stripe (eksport GunTrackera), uzgodnienie salda, lista kontrolna miesiąca i zamknięcia roku, generator dokumentów i uchwał | `services.py`, `ksef*.py`, `bank.py`, `stripe_import.py`, `reconciliation.py`, `checklist.py`, `documents.py` |
| `association` | członkowie, deklaracje, składki, uchwały (numeracja, głosowanie Zarządu online, WZC), zebrania, kluby i kody, statystyki | `services.py`, `stats.py` |
| `clients` | kartoteka „osoby bez konta”, duplikaty i scalanie, import, karty QR, sprzątanie po GunTrackerze | `duplicates.py`, `importer.py`, `guntracker_sync.py` |
| `events` | wydarzenia i zawody, zapisy, opłaty startowe | |
| `facilities` | osie, godziny otwarcia (także zbiorczo dla tygodnia i wszystkich osi), cennik, usterki | |
| `audit` | dziennik zdarzeń i rejestr wglądu w dane wrażliwe (append-only) | `services.py`, `context.py` |
| `core` | strona główna, pulpit, panel i menu, uprawnienia, ogłoszenia, FAQ, PWA, push, widżet, harmonogram | `permissions.py`, `panel_menu.py`, `middleware.py`, `management/commands/run_scheduler.py` |

Migracje — ostatnie (16.11.2026): accounting 0010, accounts 0003, association 0004, audit 0002, bookings 0028,
clients 0007, core 0015, credentials 0006, events 0002, facilities 0006, kiosk 0006, logbook 0009, payments 0013,
regulations 0003, reports 0002.

---

## 5. Reguły biznesowe i ustalenia właściciela

### 5.1 Numeracje (wszystkie nadawane automatycznie)
| Co | Format | Gdzie |
|---|---|---|
| Uchwała | `Nr X/RRRR`, **osobno** dla Zarządu, WZC, członków nadzwyczajnych i zebrania założycielskiego | `association.services._next_number` / `next_resolution_number` |
| Członek | kolejny numer (max + 1) | `association.services.next_member_number` |
| Kod strzelca (tytuł przelewu, karta QR) | `SATOK-K-NNNNNL` (litera kontrolna) | `payments.services.topup_code` |
| Rezerwacja | `SATOK-R-RRRR-NNNN` | `bookings.services._next_number` |
| Raport wpływów | `RS/D/RRRR/NNNN` | `reports.services` |
| Dowód wewnętrzny | `DW/N/RRRR` | `accounting.documents` |

Uchwały: przy przyjęciu w systemie numer nadaje się sam; formularz „uchwała z dokumentu” pokazuje kolejne wolne numery
(puste pole = numer automatyczny); generator dokumentów podpowiada numer, a po przyjęciu w rejestrze dokument dostaje
numer z rejestru. W rejestrze: 1–4/2025 (zebranie założycielskie), 1/2026 Zarządu (prowadzenie ewidencji / opis zasad).

### 5.2 Stowarzyszenie (decyzje z 31.10.2026, `docs/SPEC-etap-5.md`)
Składka 200 zł/rok do 31.01, wpisowe 50 zł, członkowie nadzwyczajni zwolnieni; cennik osi równy dla wszystkich (§ 40);
Zarząd głosuje online (remis — Prezes); WZC — głosy ważone, pełnomocnictwa; skutki uchwał wykonywane automatycznie.

### 5.3 Legitymacje i kiosk
- Uprawnienia PS weryfikuje obsługa; osoby z wykazu zweryfikowanych PS (import z GunTrackera) są zatwierdzane
  automatycznie po potwierdzeniu e-maila i zgodności imienia i nazwiska.
- Odczyt danych legitymacji: zdjęcie (Tesseract, język polski) albo PDF (warstwa tekstowa + obraz strony) → numer,
  organ (PZSS / LOK / PZŁ / inny), daty. Tekst nie jest zapisywany. Limit 10 odczytów / 10 min (konto), 6 na przebieg kiosku.
- Kiosk — rezerwacja na miejscu: regulamin → **najpierw zdjęcie legitymacji** → formularz uzupełniony z odczytu do
  sprawdzenia → rezerwacja; bez aparatu lub przy nieudanym odczycie — wpisanie ręczne.

### 5.4 Płatności
- Operator online: **Axepta BNP Paribas** (domyślny, `ONLINE_PAYMENTS_PROVIDER`), Tpay jako alternatywa
  (`docs/AXEPTA.md`). Dotychczasowe płatności online szły przez Stripe/GunTracker (konto Express — **bez API**;
  dane wyłącznie z ręcznego eksportu raz w miesiącu).
- Przelew tradycyjny: tytuł = numer rezerwacji albo kod `SATOK-K-…`; kod QR (standard ZBP). Na tym samym telefonie
  płaci się przyciskami „kopiuj” (rachunek, odbiorca, tytuł, kwota) — część aplikacji banków (np. Alior) skanuje
  wyłącznie aparatem; zapis kodu QR w galerii to opcja dla aplikacji, które wczytują obraz.

### 5.5 Księgowość (`docs/KSIEGOWOSC.md`)
- UEPiK wg art. 10a udpp i rozporządzenia MFiG (Dz. U. 2025 poz. 1880); zapisy trwałe, zmiany jako nowe wersje z powodem.
- **Rutyna miesięczna** — Księgowość → „Lista miesiąca”: faktury KSeF (pobierane automatycznie co godzinę), faktura
  prowizyjna (GunTracker, sposób „potrącenie” nadawany automatycznie), eksport Stripe i „Ujmij w ewidencji”, wyciąg BNP
  (XLSX) i rozstrzygnięcie operacji, zwroty za faktury opłacone prywatnie, uzgodnienie salda (różnica niewyjaśniona = 0).
  Różnicę faktura ↔ prowizje można oznaczyć „Różnica wyjaśniona” (z notatką; zmiana kwot unieważnia wyjaśnienie).
- **Przejście na Axeptę:** Księgowość → Ustawienia → „płatności online przez Stripe (GunTracker) do dnia”. Po tej dacie
  lista nie prosi o eksport Stripe, a sprawdza wpłaty Axepta w systemie i fakturę prowizyjną BNP Paribas z KSeF.
- **Zamknięcie roku** — zakładka „Zamknięcie roku”: saldo na 31.12, środki trwałe, forma działalności zapisów,
  przepływy (zał. 2), zamknięcie roku, uchwała WZC o przeznaczeniu dochodu, CIT-8 z CIT-8/O do 31.03.
- Stan: październik 2026 rozliczony; saldo uzgodnione co do grosza; różnica 4,80 zł (faktura GT80163/2026/005 vs
  prowizje netto Stripe 215,14 zł) wyjaśniona — do sprawdzenia przy fakturze za listopad.

### 5.6 Archiwum GunTrackera w statystykach
„Rezerwacje i osie”, „Obłożenie godzinowe” i „Statystyki miesiąca” liczą rezerwacje z archiwum (oś przypisana przy imporcie); blokady
założone za przyszłe rezerwacje GunTrackera liczą się jako zajęte godziny. Przychód archiwalny to **szacunek z cennika**
(eksport nie podaje kwot); „Książka papierowa” i „Dodano” = wpis.

---

## 6. Bezpieczeństwo

| Obszar | Rozwiązanie |
|---|---|
| Panel | `panel_required(perm)`: zalogowany, `is_staff`, **2FA**, uprawnienie; konto panelu nie działa jako konto strzelca; sesja panelu 12 h |
| Uprawnienia | 23 uprawnienia (`apps/core/permissions.py`), rola „Dyżurny”; nadawanie wymaga `accounts.manage_permissions` |
| Test dostępu | `tests/test_access_matrix.py` — każdy adres `/panel/…` (GET/POST) niedostępny bez logowania i dla konta strzelca |
| Logowanie | 5 prób / 15 min na e-mail (20 na adres IP), wyrównany czas odpowiedzi, przekierowanie tylko w obrębie domeny |
| Formularze publiczne | limity e-maili (3/adres, 20/IP na godzinę), rejestracja 10/IP na godzinę |
| Płatności | powiadomienia Axepta — SHA-256 z kluczem sklepu (`compare_digest`), Tpay — JWS z łańcuchem certyfikatów; zgodność kwoty, waluty, zamówienia |
| Kiosk | urządzenie parowane 8-cyfrowym kodem (limit prób), token w ciasteczku `HttpOnly, SameSite=Strict`, API wymaga nagłówka `X-Kiosk`, limit 400 żądań/h |
| Pliki | typ po sygnaturze (PDF/JPG/PNG), 5 MB; obrazy: limit 50 Mpx przed rozpakowaniem, ponowne zapisanie bez EXIF; PDF renderowany ze skalą wg dłuższego boku; XML (KSeF, SVG) przez `defusedxml` |
| Wyjścia sieciowe | push tylko do listy dostawców (https, port 443); KSeF, Tpay, Axepta — stałe adresy |
| Dziennik | wszystkie zmiany i wgląd w dane wrażliwe — `audit` (append-only w bazie) |
| Zależności | `pip-audit` 16.11.2026: brak znanych podatności |

---

## 7. Eksploatacja

### 7.1 Zmienne `.env` (tylko nazwy)
`DJANGO_SECRET_KEY, DJANGO_ALLOWED_HOSTS, DJANGO_CSRF_TRUSTED_ORIGINS, SITE_URL, TRUST_X_FORWARDED_FOR, APP_PORT, APP_BIND,
PUID, PGID, POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, EMAIL_* , DEFAULT_FROM_EMAIL, ERROR_EMAIL, TPAY_*, AXEPTA_*,
ONLINE_PAYMENTS_PROVIDER, TUNNEL_TOKEN, KSEF_TOKEN, KSEF_ENVIRONMENT`; kopia testowa dodatkowo `SITE_ENVIRONMENT=test`,
`EMAIL_REDIRECT_TO`, `IMAGE_NAME`. Wzór: `.env.example`.

### 7.2 Kopie zapasowe
Codziennie `backups/satok-RRRR-MM-DD.dump` (7 ostatnich), każda sprawdzana odtworzeniem do bazy `satok_verify`;
stan i alarm w panelu (System → Kopie zapasowe). **Poza serwerem:** Hybrid Backup Sync na dysk USB / chmurę —
`backups` i `data/private`, z szyfrowaniem (`docs/WDROZENIE-QNAP.md`, rozdz. 9). Plik `.env` przechowywać osobno
(menedżer haseł) — bez niego odtworzenie wymaga ustawienia nowych sekretów.

### 7.3 Zadania okresowe (`run_scheduler`)
- co minutę: `expire_reservations`, `process_waitlist`, `process_refunds`, `send_outbox`;
- co 5 minut: `send_announcements`;
- co godzinę: `send_reminders`, `settle_reservations`, `expire_credentials`, `purge_logbook`, `daily_logbook_print`,
  `association_tasks`, `sales_reports`, `accounting_sync`, `ksef_sync`, `check_backups`.

Jeden proces (Django ładowane raz); błąd zadania nie zatrzymuje kolejnych; restart po 24 h; ręcznie:
`docker compose exec app python manage.py <polecenie>`. Logi: `docker compose logs --tail 200 cron`.

### 7.4 Kiosk (urządzenia)
Android: `deploy/android-kiosk/README.md`; Raspberry Pi: `deploy/raspberry-pi/` (instalacja, harmonogram ekranu).
Parowanie: panel → Książka pobytu → Kioski → kod parowania (ważny krótko).

### 7.5 Diagnostyka
`docker compose ps` (app i db „healthy”, cron i backup „Up”), `docker compose logs --tail 200 app`,
stan: `/healthz`. Błędy 500 → e-mail na `ERROR_EMAIL`.

---

## 8. Testy i CI

- `pytest` na PostgreSQL (blokady i triggery bazy są częścią testów). Stan 17.11.2026: **744 testy, wszystkie zaliczone.**
- CI (`.github/workflows/testy.yml`, każdy PR do `main`): instalacja zależności + Tesseract (pol), `makemigrations --check`,
  `manage.py check`, pełne testy.
- Lokalnie: `export DJANGO_DEBUG=1 POSTGRES_HOST=localhost; python manage.py migrate; python -m pytest`.

---

## 9. Audyt 16.11.2026

**Sprawdzone, bez zastrzeżeń:** kontrola dostępu do panelu (test automatyczny wszystkich adresów), logowanie i 2FA,
CSRF (kiosk: nagłówek + SameSite=Strict), brak SQL z danych użytkownika, escapowanie wyjścia (2 przejrzane `mark_safe`),
podpisy powiadomień płatności, XML przez `defusedxml`, adresy push z listy dozwolonych, ochrona przed podwójną
rezerwacją (test współbieżny), stała liczba zapytań do bazy na głównych stronach (brak N+1), service worker bez danych
osobowych, `manage.py check --deploy` (jedyna uwaga: HSTS preload — świadomie wyłączony), `pip-audit`, `bandit`, `ruff`.

**Poprawione (PR #70):**
1. Zadania okresowe: ok. 5 800 startów Django na dobę (≈0,7 s CPU i 85 MB każdy) → jeden stały proces. Największa
   oszczędność energii w systemie.
2. Ochrona pamięci przy odczycie zdjęć/PDF legitymacji (obraz-„bomba”, nietypowo wysoka strona PDF).
3. Logo SVG przez `defusedxml`; gunicorn z okresową wymianą procesów; nieużywane importy; dokumentacja harmonogramu.

**Do potwierdzenia przez właściciela (konfiguracja serwera, nie kod):**
- w produkcyjnym `.env` jest `TRUST_X_FORWARDED_FOR=true` (inaczej limity prób są wspólne dla wszystkich za tunelem);
- zadanie Hybrid Backup Sync (kopia poza QNAP) działa i ma ostatni udany przebieg;
- treść `.env` zapisana poza serwerem.

**Uproszczenie panelu (wdrożone 17.11.2026 na prośbę właściciela, PR #72):**
- jedna pozycja „Dziś na strzelnicy” z zakładkami „Teraz na terenie” / „Plan dnia” (osoba tylko z uprawnieniem do
  książki trafia od razu do „Teraz”);
- grupa menu **„Raporty”**: „Statystyki i raporty” (Raport roczny, Statystyki miesiąca, Obłożenie godzinowe,
  Rezerwacje i osie) i „Raporty wpływów” — wspólny pasek zakładek;
- **„Ustawienia”** (System) — jedna strona z kafelkami wszystkich ustawień (dane stowarzyszenia, rezerwacje, książka,
  kioski, płatności, księgowość, osie), widoczne wg uprawnień; każda strona ustawień ma powrót „← Wszystkie ustawienia”;
- **„Archiwum GunTracker”** (System) — jedna pozycja z zakładkami „Rezerwacje” / „Książka pobytu”;
- „Statystyki miesiąca” liczą też archiwum GunTrackera (jak „Rezerwacje i osie”).

**Świadomy kompromis (bez zmian):** blokada logowania po 5 nieudanych próbach dotyczy adresu e-mail — ktoś znający
e-mail może zablokować logowanie tej osoby na 15 minut (alternatywa: CAPTCHA po kilku próbach).

---

## 10. Otwarte sprawy i kalendarz

| Kiedy | Co |
|---|---|
| początek grudnia 2026 | zamknięcie listopada w „Liście miesiąca”; sprawdzić, czy faktura GunTrackera wyrówna 4,80 zł |
| przejście na Axeptę | umowa z BNP, dane sklepu w `.env`, test w sandboxie (`docs/AXEPTA.md`); data końca Stripe w ustawieniach księgowości; po 1. miesiącu — sprawdzić rozpoznanie faktury BNP i wypłat |
| styczeń–marzec 2027 | zamknięcie roku 2026, uchwała WZC 1/2027 o przeznaczeniu dochodu, **wykaz rubryk CIT-8 z kwotami** (do zbudowania po publikacji formularza), CIT-8 do 31.03.2027 |
| odłożone | karnety i wypłata salda (SPEC etap 5) |

---

## 11. Mapa dokumentów

| Plik | Zawartość |
|---|---|
| `README.md` | skrót: uruchomienie, praca deweloperska, struktura, harmonogram |
| `docs/SPEC-etap-1-2.md` … `SPEC-etap-6-panel.md` | specyfikacje etapów (konta, rezerwacje, książka, płatności, stowarzyszenie, panel i kiosk) |
| `docs/NOTATKI-panel-na-miejscu.md` | notatki do obsługi na miejscu |
| `docs/KSIEGOWOSC.md` | księgowość: zakres, KSeF, wyciąg, Stripe, dokumenty |
| `docs/AXEPTA.md` | płatności Axepta: sandbox, uruchomienie produkcyjne, dokumenty |
| `docs/KOPIA-TESTOWA.md` | kopia testowa i ścieżka test → produkcja |
| `docs/WDROZENIE-QNAP.md` | serwer QNAP: instalacja, tunel, kopie poza serwerem |
| `docs/WDROZENIE-UNRAID.md` | poprzedni serwer (historycznie) |
| `docs/projekty-dokumentow/` | wzory dokumentów |
| `deploy/` | skrypty: kopia zapasowa, kopia testowa, `ustaw-env.sh`, kiosk Android / Raspberry Pi |
