#!/bin/sh
# Kopia testowa na tym samym serwerze: /share/strzelnica-test (osobna baza, pliki, port, obraz Dockera).
# Produkcja (/share/strzelnica) jest tylko ODCZYTYWANA: zrzut bazy (pg_dump) i skopiowanie plików prywatnych.
#
# Użycie (na QNAP, jako admin):
#   sh /share/strzelnica/deploy/kopia-testowa.sh            # pierwsze utworzenie albo odświeżenie danych z produkcji
#   sh /share/strzelnica/deploy/kopia-testowa.sh --kod      # tylko aktualizacja programu w teście (gałąź main)
#
# Zmienne (opcjonalnie): TEST_HOST (test.strzelnicasatok.pl), LAN_IP (adres QNAP w sieci domowej, dla tunelu),
# TEST_PORT (8001), REDIRECT (adres, na który trafiają WSZYSTKIE e-maile z testu).
set -eu
PROD=/share/strzelnica
TEST=/share/strzelnica-test
TEST_HOST="${TEST_HOST:-test.strzelnicasatok.pl}"
LAN_IP="${LAN_IP:-192.168.1.87}"
TEST_PORT="${TEST_PORT:-8001}"
REDIRECT="${REDIRECT:-biuro@strzelnicasatok.pl}"
URL=https://github.com/Airchi79/strzelnica-portal/archive/refs/heads/main.tar.gz

[ "$PROD" != "$TEST" ] || { echo "Błąd: folder testu nie może być folderem produkcji."; exit 1; }
[ -f "$PROD/.env" ] || { echo "Brak $PROD/.env — uruchom na serwerze produkcji."; exit 1; }

code() {
  mkdir -p "$TEST/tmp"
  wget -q -O "$TEST/tmp/kod.tar.gz" "$URL"
  rm -rf "$TEST/tmp/strzelnica-portal-main"
  tar -xzf "$TEST/tmp/kod.tar.gz" -C "$TEST/tmp"
  cp -a "$TEST/tmp/strzelnica-portal-main/." "$TEST/"
  rm -rf "$TEST/tmp/strzelnica-portal-main" "$TEST/tmp/kod.tar.gz"
}

setvar() {  # ustawia zmienną w $TEST/.env (zastępuje albo dopisuje); wartości nie są wypisywane
  grep -v "^$1=" "$TEST/.env" > "$TEST/.env.tmp" || true
  printf '%s=%s\n' "$1" "$2" >> "$TEST/.env.tmp"
  mv "$TEST/.env.tmp" "$TEST/.env"
}

if [ "${1:-}" = "--kod" ]; then
  [ -f "$TEST/.env" ] || { echo "Kopia testowa jeszcze nie istnieje — uruchom skrypt bez --kod."; exit 1; }
  code
  cd "$TEST" && docker compose up -d --build app cron
  echo "Test zaktualizowany do najnowszej wersji (main): https://$TEST_HOST"
  exit 0
fi

mkdir -p "$TEST/data/private" "$TEST/backups" "$TEST/tmp"
code

if [ ! -f "$TEST/.env" ]; then
  echo "Tworzę $TEST/.env na podstawie produkcji (z ustawieniami testu)…"
  grep -v '^TUNNEL_TOKEN=' "$PROD/.env" > "$TEST/.env"
  chmod 600 "$TEST/.env"
  setvar DJANGO_SECRET_KEY "$(head -c 48 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
# Ustawienia testu — za każdym razem (bezpieczeństwo ważniejsze niż ręczne zmiany w .env testu)
setvar SITE_ENVIRONMENT test
setvar EMAIL_REDIRECT_TO "$REDIRECT"
setvar TPAY_SANDBOX true
setvar SITE_URL "https://$TEST_HOST"
setvar DJANGO_ALLOWED_HOSTS "$TEST_HOST"
setvar DJANGO_CSRF_TRUSTED_ORIGINS "https://$TEST_HOST"
setvar IMAGE_NAME strzelnica-portal-test
setvar APP_BIND "$LAN_IP"
setvar APP_PORT "$TEST_PORT"

echo "Zrzut bazy produkcji (tylko odczyt)…"
cd "$PROD" && docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$TEST/tmp/produkcja.dump"
echo "Kopiuję pliki prywatne (uchwały, dokumenty)…"
cp -a "$PROD/data/private/." "$TEST/data/private/"

cd "$TEST"
docker compose stop app cron 2>/dev/null || true
docker compose up -d db
i=0
until docker compose exec -T db sh -c 'pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB"' >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 60 ] || { echo "Baza testu nie wystartowała."; exit 1; }; sleep 2
done
echo "Wgrywam dane do bazy testu…"
docker compose exec -T db sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --clean --if-exists' < tmp/produkcja.dump || true
rm -f tmp/produkcja.dump
docker compose up -d --build app cron backup
i=0
until docker compose exec -T app python manage.py prepare_test_copy; do
  i=$((i + 1)); [ "$i" -lt 30 ] || { echo "Nie udało się przygotować kopii — sprawdź: docker compose logs app"; exit 1; }; sleep 5
done
echo "Gotowe: https://$TEST_HOST (po dodaniu trasy w tunelu Cloudflare). E-maile z testu trafiają na $REDIRECT."
