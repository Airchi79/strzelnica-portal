"""Import archiwalnej książki pobytu z GunTrackera (eksport „Księga pobytu” XLSX). Wpisy trafiają do osobnego archiwum
tylko do odczytu; ponowny import tego samego wiersza jest pomijany (suma kontrolna wiersza)."""

import hashlib
import json
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import log_event
from apps.core.xlsx import read_table, text

from .models import ArchivedEntry

REQUIRED = ["Rozpoczęcie", "Imię", "Nazwisko"]


def _dt(value, row, field, required=True):
    if value in (None, ""):
        if required:
            raise ValidationError(f"Wiersz {row}: brak daty w kolumnie {field}.")
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M"):
            try:
                dt = datetime.strptime(str(value).strip(), fmt)
                break
            except ValueError:
                continue
        else:
            raise ValidationError(f"Wiersz {row}: nieprawidłowa data „{value}” w kolumnie {field}.")
    return timezone.make_aware(dt) if timezone.is_naive(dt) else dt


def parse(uploaded):
    _, rows = read_table(uploaded, REQUIRED)
    items = []
    for row, r in rows:
        item = {
            "started_at": _dt(r.get("rozpoczęcie"), row, "Rozpoczęcie"),
            "ended_at": _dt(r.get("zakończenie"), row, "Zakończenie", required=False),
            "first_name": text(r.get("imię"), 80), "last_name": text(r.get("nazwisko"), 120),
            "permit_number": "".join(text(r.get("numer pozwolenia"), 64).split()).upper(),
            "permit_issuer": text(r.get("organ wydający"), 160), "address": text(r.get("adres"), 255),
            "lane": text(r.get("oś"), 80), "position": text(r.get("stanowisko"), 40), "entry_type": text(r.get("typ wpisu"), 60),
            "confirmation": text(r.get("potwierdzenie"), 120), "added_by": text(r.get("dodał administrator"), 120),
        }
        if not item["first_name"] or not item["last_name"]:
            raise ValidationError(f"Wiersz {row}: brak imienia lub nazwiska.")
        try:
            item["duration_minutes"] = int(r.get("czas (min)")) if r.get("czas (min)") not in (None, "") else None
        except (TypeError, ValueError):
            item["duration_minutes"] = None
        key = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in item.items()}
        item["row_hash"] = hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        items.append((row, item))
    return items


def run(uploaded, *, by=None, preview=False, add_persons=True):
    from apps.clients.importer import find_existing, merge
    from apps.clients.models import Person

    items = parse(uploaded)
    stats = {"dodane": 0, "pominiete": 0, "osoby": 0, "osoby_uzupelnione": 0}
    with transaction.atomic():
        existing = set(ArchivedEntry.objects.filter(row_hash__in=[i["row_hash"] for _, i in items]).values_list("row_hash", flat=True))
        for _, item in items:
            if item["row_hash"] in existing:
                stats["pominiete"] += 1
                continue
            existing.add(item["row_hash"])
            ArchivedEntry.objects.create(**item, imported_by=by, import_file=(uploaded.name or "")[:255])
            stats["dodane"] += 1
            if add_persons:
                person = {k: item[k] for k in ("first_name", "last_name", "permit_number", "permit_issuer", "address")}
                known = find_existing(person)
                if known is None:
                    Person.objects.create(**person, source=Person.Source.GUNTRACKER, created_by=by)
                    stats["osoby"] += 1
                elif merge(known, person)[0]:  # tylko uzupełnienie pustych pól — bez nadpisywania
                    stats["osoby_uzupelnione"] += 1
        if preview:
            transaction.set_rollback(True)
    stats["zakres"] = (min(i["started_at"] for _, i in items), max(i["started_at"] for _, i in items)) if items else None
    if not preview and stats["dodane"]:
        log_event("ksiazka.import_archiwum", summary=f"{uploaded.name}: {stats['dodane']} wpisów archiwalnych, {stats['osoby']} nowych osób, "
                                                       f"{stats['osoby_uzupelnione']} uzupełnionych", actor=by)
    return stats
