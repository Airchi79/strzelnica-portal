#!/bin/sh
# Ustawia (lub dopisuje) zmienną w pliku .env bez pokazywania wartości na ekranie i bez zapisu w historii poleceń.
# Użycie (w folderze projektu):  sh deploy/ustaw-env.sh TPAY_CLIENT_ID
set -eu
NAME="${1:-}"
FILE="${2:-.env}"
case "$NAME" in ''|*[!A-Z0-9_]*) echo "Podaj nazwę zmiennej, np.: sh deploy/ustaw-env.sh TPAY_CLIENT_ID"; exit 1 ;; esac
[ -f "$FILE" ] || { echo "Brak pliku $FILE — uruchom w folderze projektu (np. /share/strzelnica)."; exit 1; }

printf "Wartość %s (nie będzie widoczna; wklej prawym przyciskiem myszy lub Ctrl+Shift+V, potem Enter): " "$NAME"
# Po przerwaniu (Ctrl+C) przywróć wyświetlanie wpisywanych znaków.
trap 'stty echo 2>/dev/null; echo; exit 130' INT TERM
stty -echo 2>/dev/null || true
IFS= read -r VALUE || true
stty echo 2>/dev/null || true
trap - INT TERM
echo
# Usuń znaki Enter/CR i spacje na brzegach (częste przy wklejaniu z przeglądarki).
VALUE=$(printf '%s' "$VALUE" | tr -d '\r\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//')
[ -n "$VALUE" ] || { echo "Pusta wartość — nic nie zmieniono."; exit 1; }

TMP="$FILE.tmp.$$"
# Wartość przekazana przez zmienną środowiskową (ENVIRON), więc znaki specjalne nie są interpretowane.
V="$VALUE" N="$NAME" awk 'BEGIN { n = ENVIRON["N"]; v = ENVIRON["V"]; done = 0 }
  index($0, n "=") == 1 { if (!done) { print n "=" v; done = 1 } ; next }
  { print }
  END { if (!done) print n "=" v }' "$FILE" > "$TMP"
chmod 600 "$TMP"
mv "$TMP" "$FILE"
echo "Zapisano $NAME w $FILE."
