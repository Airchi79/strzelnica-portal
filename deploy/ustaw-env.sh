#!/bin/sh
# Ustawia (lub dopisuje) zmienną w pliku .env. Wartość nie trafia do historii poleceń.
# Użycie (w folderze projektu):  sh deploy/ustaw-env.sh TPAY_CLIENT_ID
# Wklejanie: prawy przycisk myszy albo Ctrl+Shift+V. Puste wiersze (np. przed wklejonym tekstem) są pomijane;
# przed zapisem skrypt pokazuje początek i koniec wartości i prosi o potwierdzenie.
set -eu
NAME="${1:-}"
FILE="${2:-.env}"
case "$NAME" in ''|*[!A-Z0-9_]*) echo "Podaj nazwę zmiennej, np.: sh deploy/ustaw-env.sh TPAY_CLIENT_ID"; exit 1 ;; esac
[ -f "$FILE" ] || { echo "Brak pliku $FILE — uruchom w folderze projektu (np. /share/strzelnica)."; exit 1; }

clean() { printf '%s' "$1" | tr -d '\r\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//'; }

echo "Wklej wartość $NAME (prawy przycisk myszy lub Ctrl+Shift+V) i naciśnij Enter. Ctrl+C — przerwij."
VALUE=""
TRIES=0
while [ -z "$VALUE" ]; do
  IFS= read -r LINE || { echo "Przerwano — nic nie zmieniono."; exit 1; }
  VALUE=$(clean "$LINE")
  TRIES=$((TRIES + 1))
  [ "$TRIES" -ge 20 ] && { echo "Nie otrzymano wartości — nic nie zmieniono."; exit 1; }
done

LEN=${#VALUE}
if [ "$LEN" -gt 8 ]; then
  SHOW=$(V="$VALUE" awk 'BEGIN { v = ENVIRON["V"]; print substr(v, 1, 4) "…" substr(v, length(v) - 3) }')
else
  SHOW="(krótka wartość)"
fi
# Wyczyść z ekranu wklejoną wartość (przewiń terminal), zostaw tylko skrót.
printf '\033[1A\033[2K' 2>/dev/null || true
echo "Otrzymano: $SHOW, długość $LEN znaków."
printf "Zapisać %s? [t/n]: " "$NAME"
IFS= read -r OK || OK=n
case "$OK" in t|T|tak|TAK|y|Y) ;; *) echo "Nie zapisano."; exit 1 ;; esac

TMP="$FILE.tmp.$$"
# Wartość przekazana przez zmienną środowiskową (ENVIRON), więc znaki specjalne nie są interpretowane.
V="$VALUE" N="$NAME" awk 'BEGIN { n = ENVIRON["N"]; v = ENVIRON["V"]; done = 0 }
  index($0, n "=") == 1 { if (!done) { print n "=" v; done = 1 } ; next }
  { print }
  END { if (!done) print n "=" v }' "$FILE" > "$TMP"
chmod 600 "$TMP"
mv "$TMP" "$FILE"
echo "Zapisano $NAME w $FILE."
