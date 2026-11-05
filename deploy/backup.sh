#!/bin/sh
# Codzienna kopia bazy + sprawdzenie, czy da się ją odtworzyć.
# Uruchamiane w kontenerze „backup” (obraz postgres:16). Wynik: /backups/status.json — czyta go aplikacja
# (panel → Przegląd, alarm dla zarządu, gdy kopia jest uszkodzona albo zbyt stara).
#
# Sprawdzenie: pg_restore do tymczasowej bazy satok_verify na tym samym serwerze, porównanie liczby wierszy
# w kluczowych tabelach z bazą produkcyjną, usunięcie tymczasowej bazy. Baza produkcyjna nie jest zmieniana.

set -u
export PGPASSWORD="$POSTGRES_PASSWORD"
DB="$POSTGRES_DB"
U="$POSTGRES_USER"
DIR="${BACKUP_DIR:-/backups}"
KEEP="${BACKUP_KEEP:-7}"
VERIFY_DB="satok_verify"
TABLES="accounts_user bookings_reservation logbook_entry payments_payment payments_ledgerentry credentials_credential association_member"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }

write_status() {  # $1 ok(true/false) $2 plik $3 rozmiar $4 opis błędu $5 liczniki JSON
  tmp="$DIR/.status.json.tmp"
  printf '{"ok": %s, "file": "%s", "size": %s, "checked_at": "%s", "error": "%s", "counts": {%s}}\n' \
    "$1" "$2" "${3:-0}" "$(now)" "$4" "$5" > "$tmp" && mv "$tmp" "$DIR/status.json"
}

count_rows() {  # $1 baza — wypisuje "tabela":liczba, oddzielone przecinkami
  out=""
  for t in $TABLES; do
    n=$(psql -U "$U" -d "$1" -tAc "SELECT count(*) FROM $t" 2>/dev/null) || n=-1
    out="$out${out:+, }\"$t\": ${n:--1}"
  done
  printf '%s' "$out"
}

run_once() {
  file="$DIR/satok-$(date +%F).dump"
  if ! pg_dump -U "$U" -d "$DB" -Fc -f "$file.tmp"; then
    rm -f "$file.tmp"; write_status false "" 0 "pg_dump nie powiódł się" ""; return 1
  fi
  live=$(count_rows "$DB")
  mv "$file.tmp" "$file"
  size=$(wc -c < "$file")
  ls -1t "$DIR"/satok-*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm --

  if ! pg_restore --list "$file" > /dev/null 2>&1; then
    write_status false "$(basename "$file")" "$size" "plik kopii jest uszkodzony (pg_restore --list)" ""; return 1
  fi
  dropdb -U "$U" --if-exists "$VERIFY_DB" > /dev/null 2>&1
  if ! createdb -U "$U" "$VERIFY_DB"; then
    write_status false "$(basename "$file")" "$size" "nie można utworzyć bazy testowej" ""; return 1
  fi
  if ! pg_restore -U "$U" -d "$VERIFY_DB" --no-owner --exit-on-error "$file" > /dev/null 2>"$DIR/.restore.log"; then
    err=$(tail -n 1 "$DIR/.restore.log" | tr '"\\' "''" | cut -c1-200)
    dropdb -U "$U" --if-exists "$VERIFY_DB" > /dev/null 2>&1
    write_status false "$(basename "$file")" "$size" "odtworzenie nie powiodło się: $err" ""; return 1
  fi
  restored=$(count_rows "$VERIFY_DB")
  dropdb -U "$U" --if-exists "$VERIFY_DB" > /dev/null 2>&1
  rm -f "$DIR/.restore.log"
  case "$restored" in *-1*) write_status false "$(basename "$file")" "$size" "w odtworzonej kopii brakuje tabel" "$restored"; return 1 ;; esac
  # kopia i liczniki „na żywo” są z tej samej chwili (± sekundy) — różnica większa niż kilka wierszy oznacza problem
  for t in $TABLES; do
    a=$(printf '%s' "$live" | sed -n "s/.*\"$t\": \(-\{0,1\}[0-9]*\).*/\1/p")
    b=$(printf '%s' "$restored" | sed -n "s/.*\"$t\": \(-\{0,1\}[0-9]*\).*/\1/p")
    if [ -n "$a" ] && [ -n "$b" ] && [ "$b" -lt $((a - 5)) ]; then
      write_status false "$(basename "$file")" "$size" "w kopii brakuje danych w tabeli $t ($b z $a)" "$restored"; return 1
    fi
  done
  write_status true "$(basename "$file")" "$size" "" "$restored"
  echo "$(now) kopia $(basename "$file") ($size B) odtworzona poprawnie"
}

if [ "${1:-}" = "--once" ]; then
  run_once; exit $?
fi
sleep "${BACKUP_START_DELAY:-300}"
while true; do
  run_once || echo "$(now) KOPIA ZAPASOWA: BŁĄD — szczegóły w $DIR/status.json"
  sleep 86400
done
