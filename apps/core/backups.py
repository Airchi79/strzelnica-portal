"""Stan kopii zapasowych bazy: wynik ostatniego sprawdzenia zapisany przez kontener „backup” (deploy/backup.sh)."""

import json
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

MAX_AGE = timedelta(hours=36)  # kopia robiona co dobę; po 36 h bez udanego sprawdzenia — alarm


def status_path():
    return Path(getattr(settings, "BACKUP_DIR", "/backups")) / "status.json"


def read_status(now=None):
    """Zwraca słownik: state (ok / blad / stara / brak), message, checked_at, file, size, counts, files."""
    now = now or timezone.now()
    path = status_path()
    files = sorted(path.parent.glob("satok-*.dump"), reverse=True) if path.parent.exists() else []
    listing = [{"name": f.name, "size": f.stat().st_size,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.get_current_timezone())} for f in files[:10]]
    if not path.exists():
        return {"state": "brak", "message": "Brak wyniku sprawdzenia kopii — kontener „backup” jeszcze go nie zapisał "
                "albo folder kopii nie jest podłączony do aplikacji.", "files": listing}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        checked = datetime.fromisoformat(data["checked_at"].replace("Z", "+00:00"))
    except (ValueError, KeyError, OSError):
        return {"state": "blad", "message": "Plik wyniku sprawdzenia jest nieczytelny.", "files": listing}
    base = {"checked_at": checked, "file": data.get("file", ""), "size": data.get("size", 0), "counts": data.get("counts", {}),
            "files": listing}
    if not data.get("ok"):
        return {**base, "state": "blad", "message": f"Ostatnia kopia NIE przeszła sprawdzenia: {data.get('error') or 'nieznany błąd'}."}
    if now - checked > MAX_AGE:
        return {**base, "state": "stara", "message": f"Ostatnia poprawna kopia jest z {timezone.localtime(checked):%d.%m.%Y %H:%M} — "
                "od ponad 36 godzin nie powstała nowa. Sprawdź kontener „backup”."}
    return {**base, "state": "ok", "message": f"Kopia {data.get('file')} odtworzona i sprawdzona {timezone.localtime(checked):%d.%m.%Y %H:%M}."}
