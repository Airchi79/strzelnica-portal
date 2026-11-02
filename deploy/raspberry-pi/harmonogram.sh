#!/bin/sh
# Harmonogram pracy kiosku zasilanego z akumulatora: wyłączenie po zamknięciu strzelnicy i samoczynne
# włączenie rano (zegar RTC Raspberry Pi 5 — wymaga baterii RTC). Oszczędza ok. 2/3 energii na dobę.
# Użycie (jako zwykły użytkownik):  sh harmonogram.sh 08:30 18:30
set -eu
ON="${1:-08:30}"; OFF="${2:-18:30}"
for t in "$ON" "$OFF"; do
  echo "$t" | grep -Eq '^([01][0-9]|2[0-3]):[0-5][0-9]$' || { echo "Podaj godziny jako GG:MM, np. 08:30 18:30"; exit 1; }
done

echo "== Najniższy pobór prądu po wyłączeniu (EEPROM: POWER_OFF_ON_HALT=1, WAKE_ON_GPIO=0)"
TMP=$(mktemp)
sudo rpi-eeprom-config > "$TMP"
sed -i '/^POWER_OFF_ON_HALT=/d; /^WAKE_ON_GPIO=/d' "$TMP"
printf 'POWER_OFF_ON_HALT=1\nWAKE_ON_GPIO=0\n' >> "$TMP"
sudo rpi-eeprom-config --apply "$TMP"
rm -f "$TMP"

echo "== Skrypt wyłączenia z budzikiem na $ON"
sudo tee /usr/local/sbin/satok-sleep >/dev/null <<SCRIPT
#!/bin/sh
# Ustawia budzik RTC na najbliższe $ON i wyłącza urządzenie.
next=\$(date -d "today $ON" +%s)
[ "\$next" -le "\$(date +%s)" ] && next=\$(date -d "tomorrow $ON" +%s)
echo 0 > /sys/class/rtc/rtc0/wakealarm
echo "\$next" > /sys/class/rtc/rtc0/wakealarm
systemctl poweroff
SCRIPT
sudo chmod 755 /usr/local/sbin/satok-sleep

sudo tee /etc/systemd/system/satok-sleep.service >/dev/null <<UNIT
[Unit]
Description=Kiosk Strzelnica Satok - wylaczenie na noc
[Service]
Type=oneshot
ExecStart=/usr/local/sbin/satok-sleep
UNIT
sudo tee /etc/systemd/system/satok-sleep.timer >/dev/null <<UNIT
[Unit]
Description=Kiosk Strzelnica Satok - wylaczenie o $OFF
[Timer]
OnCalendar=*-*-* $OFF:00
[Install]
WantedBy=timers.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now satok-sleep.timer
echo "== Gotowe: kiosk wyłączy się o $OFF i włączy o $ON. Zmiana godzin: uruchom skrypt ponownie z nowymi godzinami."
echo "Wyłączenie harmonogramu:  sudo systemctl disable --now satok-sleep.timer"
