# Kopia testowa i wdrażanie: test → produkcja

Dwie kopie systemu na tym samym QNAP:

| | Produkcja | Kopia testowa |
|---|---|---|
| Adres | rezerwacje.strzelnicasatok.pl | test.strzelnicasatok.pl |
| Folder | `/share/strzelnica` | `/share/strzelnica-test` |
| Wersja programu | gałąź **`produkcja`** (tylko sprawdzone zmiany) | gałąź **`main`** (każda nowa zmiana) |
| Baza i pliki | własne | własne (kopia danych z produkcji z chwili utworzenia / odświeżenia) |
| E-maile | do adresatów | **wszystkie na biuro@strzelnicasatok.pl** (temat: `[TEST → adresat]`) |
| Tpay | wg `.env` | **tylko testowy** (sandbox) — inaczej kopia nie wystartuje |
| Powiadomienia push | do urządzeń strzelców | tylko do urządzeń zarejestrowanych w kopii testowej |
| Kiosk | sparowany telefon przy wejściu | osobno sparowany (np. drugi telefon / ten sam w innej karcie) |

Każda strona kopii testowej ma u góry czerwony pasek **WERSJA TESTOWA**, a tytuł karty zaczyna się od `[TEST]`.

## Jak pracujemy

1. Zmiany trafiają do gałęzi `main` → aktualizujesz **test**: `sh /share/strzelnica/deploy/kopia-testowa.sh --kod`
2. Sprawdzasz na test.strzelnicasatok.pl.
3. Gdy jest dobrze, piszesz „wdrażamy” → gałąź `produkcja` zostaje przesunięta na sprawdzoną wersję →
   aktualizujesz **produkcję** (polecenie niżej).

Produkcja nigdy nie pobiera `main`, więc nic niesprawdzonego nie trafi na nią przypadkiem.

**Aktualizacja produkcji** (zawsze to polecenie, zamiast dawnego z `main`):

```
cd /share/strzelnica && wget -O tmp/strzelnica.tar.gz https://github.com/Airchi79/strzelnica-portal/archive/refs/heads/produkcja.tar.gz && tar -xzf tmp/strzelnica.tar.gz -C tmp && cp -a tmp/strzelnica-portal-produkcja/. /share/strzelnica/ && docker compose up -d --build
```

## Pierwsze utworzenie kopii testowej

1. **Tunel Cloudflare** (raz): Cloudflare → Networks → Tunnels → **qnap** → Routes → **Add route** → Published application:
   `test` / `strzelnicasatok.pl`, typ **HTTP**, URL **`192.168.1.87:8001`** (adres QNAP w sieci domowej i port testu).
2. **Zalecane — dostęp tylko dla Ciebie**: Cloudflare → Zero Trust → Access → Applications → Add → Self-hosted →
   `test.strzelnicasatok.pl`, polityka „Allow” dla Twojego adresu e-mail (logowanie kodem z e-maila). Bez tego kopia
   testowa jest dostępna dla każdego, kto zna adres.
3. **Na QNAP** (zakłada folder, `.env` testu, kopiuje bazę i pliki z produkcji, uruchamia test):

```
sh /share/strzelnica/deploy/kopia-testowa.sh
```

Jeśli adres QNAP w sieci domowej jest inny niż 192.168.1.87, poprzedź polecenie: `LAN_IP=192.168.1.xx sh …`
(i ten sam adres wpisz w trasie tunelu).

Skrypt:
- tworzy `/share/strzelnica-test/.env` z ustawień produkcji (bez tokenu tunelu, z nowym kluczem), a przy każdym
  uruchomieniu wymusza ustawienia testu: `SITE_ENVIRONMENT=test`, `EMAIL_REDIRECT_TO`, `TPAY_SANDBOX=true`, adres
  `test.strzelnicasatok.pl`, osobny obraz (`IMAGE_NAME`) i port 8001;
- produkcję tylko odczytuje (zrzut bazy, kopia plików prywatnych) — niczego w niej nie zmienia;
- po wgraniu danych uruchamia `prepare_test_copy`: usuwa subskrypcje push (telefony członków) i kolejkę e-maili
  skopiowane z produkcji oraz rozparowuje kioski.

## Odświeżenie danych testu z produkcji

To samo polecenie co przy tworzeniu: `sh /share/strzelnica/deploy/kopia-testowa.sh` — dane testu zostaną
**zastąpione** aktualnymi danymi produkcji (próby z testu znikną). Kiosk testowy trzeba potem sparować ponownie.

## Logowanie w teście

Konta i hasła są takie jak na produkcji w chwili kopiowania (logowanie 2FA też — ten sam kod z aplikacji).
Konto panelu działa wyłącznie jako panel; do prób jako strzelec załóż w teście konto strzelca na inny adres —
potwierdzenie przyjdzie na biuro@ z dopiskiem, do kogo było adresowane.

## Usunięcie kopii testowej

```
cd /share/strzelnica-test && docker compose down && cd / && rm -rf /share/strzelnica-test
```
oraz trasa `test` w tunelu Cloudflare.
