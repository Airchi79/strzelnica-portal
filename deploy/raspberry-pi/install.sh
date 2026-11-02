#!/bin/sh
# Kiosk książki pobytu na Raspberry Pi (Raspberry Pi OS Bookworm z pulpitem, Wayland/labwc).
# Uruchom jako zwykły użytkownik (nie root):  sh install.sh https://rezerwacje.strzelnicasatok.pl/kiosk/
set -eu

URL="${1:-https://rezerwacje.strzelnicasatok.pl/kiosk/}"
case "$URL" in https://*/kiosk/) ;; *) echo "Podaj adres kiosku, np. https://rezerwacje.strzelnicasatok.pl/kiosk/"; exit 1 ;; esac
if [ "$(id -u)" = "0" ]; then echo "Uruchom jako zwykły użytkownik (skrypt sam użyje sudo tam, gdzie trzeba)."; exit 1; fi

echo "== Aktualizacja systemu i instalacja przeglądarki"
sudo apt-get update
sudo apt-get -y full-upgrade
sudo apt-get -y install chromium-browser || sudo apt-get -y install chromium

echo "== Ekran nie wygasa, automatyczne logowanie do pulpitu"
sudo raspi-config nonint do_blanking 1 || true
sudo raspi-config nonint do_boot_behaviour B4 || true

BROWSER=$(command -v chromium-browser || command -v chromium)
mkdir -p "$HOME/.config/labwc" "$HOME/.local/bin"

cat > "$HOME/.local/bin/satok-kiosk" <<SCRIPT
#!/bin/sh
# Uruchamia przeglądarkę w trybie kiosku i wznawia ją po ewentualnym zamknięciu.
while true; do
  "$BROWSER" --kiosk --noerrdialogs --disable-infobars --no-first-run --disable-session-crashed-bubble \\
    --overscroll-history-navigation=0 --disable-pinch --check-for-update-interval=31536000 \\
    --ozone-platform=wayland --password-store=basic "$URL"
  sleep 3
done
SCRIPT
chmod +x "$HOME/.local/bin/satok-kiosk"

AUTOSTART="$HOME/.config/labwc/autostart"
touch "$AUTOSTART"
grep -q satok-kiosk "$AUTOSTART" || echo "$HOME/.local/bin/satok-kiosk &" >> "$AUTOSTART"

echo "== Gotowe. Uruchom ponownie: sudo reboot"
echo "Po starcie pojawi się ekran parowania — wpisz kod z panelu (Książka pobytu → Kioski)."
