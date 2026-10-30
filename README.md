# Strzelnica Sątok — system rezerwacji i obsługi stowarzyszenia

System dla Stowarzyszenia STRZELNICA SĄTOK: konta strzelców, weryfikacja uprawnień, regulaminy,
osie i cennik, a w kolejnych etapach rezerwacje, elektroniczna książka pobytu, płatności i obsługa stowarzyszenia.
Specyfikacja: [`docs/SPEC-etap-1-2.md`](docs/SPEC-etap-1-2.md).

**Stos:** Python 3.12+, Django 5.2 LTS, PostgreSQL 16, strony renderowane na serwerze + PWA.

## Uruchomienie produkcyjne (Docker)

```bash
cp .env.example .env          # uzupełnij sekrety i adres
docker compose up -d --build
docker compose exec app python manage.py create_owner --email ty@example.pl --first-name Imię --last-name Nazwisko
```

Aplikacja nasłuchuje na porcie 8000. HTTPS zapewnia reverse proxy (np. Nginx Proxy Manager, Caddy).
Przy pierwszym logowaniu właściciel konfiguruje aplikację uwierzytelniającą (2FA — wymagana w panelu zarządu).
Kopie bazy trafiają codziennie do `./backups` (7 ostatnich).

## Praca deweloperska

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
export DJANGO_DEBUG=1          # baza: PostgreSQL na localhost, użytkownik/hasło/baza: satok
python manage.py migrate && python manage.py createcachetable
python manage.py runserver
```

Testy (wymagają PostgreSQL — sprawdzają m.in. blokady na poziomie bazy):

```bash
python -m pytest
```

## Struktura

| Moduł | Zakres |
|---|---|
| `apps/accounts` | konta, logowanie e-mailem, 2FA (TOTP), limit prób logowania, reset hasła |
| `apps/credentials` | pozwolenia i uprawnienia PS, weryfikacja przez zarząd, automatyczne wygasanie |
| `apps/regulations` | wersjonowane regulaminy, publikacja, wymuszona akceptacja |
| `apps/facilities` | obiekty, osie, godziny otwarcia, cennik (kwoty w groszach) |
| `apps/audit` | dziennik zdarzeń i rejestr wglądu w dane wrażliwe (niezmienialne — trigger w bazie) |
| `apps/core` | strona główna, pulpit, panel zarządu, uprawnienia, PWA, powiadomienia e-mail |

## Zadania okresowe

`python manage.py expire_credentials` — raz na dobę (w Dockerze robi to usługa `cron`).
