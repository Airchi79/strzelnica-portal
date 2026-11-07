"""Import rezerwacji z GunTrackera (eksport „Rezerwacje” XLSX) do archiwum. Daty w eksporcie nie mają roku —
rok podaje się przy imporcie. Przyszłe terminy dostają blokadę osi (w kalendarzu publicznym: „niedostępne”)."""

import hashlib
import json
import re
from datetime import datetime

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import log_event
from apps.core.xlsx import read_table, text
from apps.facilities.models import Lane

from .models import ArchivedReservation, Block

REQUIRED = ["Godzina", "Numer", "Klient", "Oś / Instruktor"]  # kolumny eksportu GunTrackera
TIME_RE = re.compile(r"^\s*(\d{1,2})\.(\d{1,2})(?:\.(\d{4}))?\s+(\d{1,2}):(\d{2})\s*[–-]\s*(\d{1,2}):(\d{2})\s*$")
LANE_RE = re.compile(r"oś\s*(\d+)\s*m(?:\s*\((\d+)\))?", re.IGNORECASE)


def parse_time(value, year, row):
    m = TIME_RE.match(str(value or ""))
    if not m:
        raise ValidationError(f"Wiersz {row}: nieprawidłowy termin „{value}” (oczekiwano np. 14.06 15:00 – 17:00).")
    day, month, y, h1, m1, h2, m2 = m.groups()
    y = int(y) if y else year
    try:
        start = datetime(y, int(month), int(day), int(h1), int(m1))
        end = datetime(y, int(month), int(day), int(h2), int(m2)) if (h2, m2) != ("24", "00") else datetime(y, int(month), int(day), 23, 59)
    except ValueError:
        raise ValidationError(f"Wiersz {row}: nieprawidłowa data „{value}”.") from None
    if end <= start:
        raise ValidationError(f"Wiersz {row}: koniec przed początkiem w „{value}”.")
    return timezone.make_aware(start), timezone.make_aware(end)


def match_lane(label, lanes):
    """Eksport rezerwacji GunTrackera dopisuje na końcu liczbę stanowisk w nawiasie:
    „Oś 25 m (1) (5)” → „Oś 25 m nr 1”, „Oś 100m (5)” → „Oś 100 m”, „Oś 35 m (5)” → „Oś 35 m”."""
    core = re.sub(r"\s*\(\d+\)\s*$", "", label or "")
    m = LANE_RE.search(core)
    if not m:
        return None
    distance, nr = m.group(1), m.group(2)
    wanted = f"oś {distance} m nr {nr}" if nr else f"oś {distance} m"
    return next((lane for lane in lanes if lane.name.lower() == wanted), None)


def format_phone(raw):
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    if len(digits) == 11 and digits.startswith("48"):
        return f"+48 {digits[2:5]} {digits[5:8]} {digits[8:]}"
    if len(digits) == 9:
        return f"+48 {digits[:3]} {digits[3:6]} {digits[6:]}"
    return str(raw or "").strip()[:32]


def update_person_contacts(items):
    """Uzupełnia pusty telefon osób z kartoteki (bez konta) na podstawie strzelca rezerwacji („Imię Nazwisko”).
    Tylko jednoznaczne dopasowanie; istniejących telefonów nie nadpisuje. Zwraca (uzupełnione, bez_dopasowania)."""
    from apps.clients.models import Person

    updated, unmatched = 0, set()
    for _, item in items:
        parts = item["client"].split()
        if len(parts) < 2 or not item["contact"]:
            continue
        candidates = Person.objects.filter(first_name__iexact=parts[0], last_name__iexact=" ".join(parts[1:]))
        if not candidates.exists():
            candidates = Person.objects.filter(first_name__iexact=" ".join(parts[1:]), last_name__iexact=parts[0])
        if candidates.count() != 1:
            unmatched.add(item["client"])
            continue
        updated += candidates.filter(phone="").update(phone=format_phone(item["contact"]))
    return updated, len(unmatched)


def parse(uploaded, year):
    _, rows = read_table(uploaded, REQUIRED)
    lanes = list(Lane.objects.all())
    items = []
    for row, r in rows:
        start, end = parse_time(r.get("godzina"), year, row)
        contact = r.get("kontakt")
        contact = str(int(contact)) if isinstance(contact, (int, float)) else text(contact, 40)
        item = {
            "number": text(r.get("numer"), 40), "starts_at": start, "ends_at": end, "client": text(r.get("klient"), 160),
            "lane_label": text(r.get("oś / instruktor"), 80), "positions": text(r.get("stanowiska"), 40),
            "range_officer": text(r.get("prowadzący strzelanie"), 160), "logbook_info": text(r.get("księga pobytu"), 80),
            "payment": text(r.get("płatność"), 80), "contact": contact,
        }
        if not item["number"] or not item["client"]:
            raise ValidationError(f"Wiersz {row}: brak numeru lub strzelca.")
        item["lane"] = match_lane(item["lane_label"], lanes)
        key = {k: (v.isoformat() if hasattr(v, "isoformat") else (v.pk if isinstance(v, Lane) else v)) for k, v in item.items()}
        item["row_hash"] = hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        items.append((row, item))
    return items


def run(uploaded, *, year, by=None, preview=False, now=None, update_contacts=True):
    from . import services

    now = now or timezone.now()
    items = parse(uploaded, year)
    stats = {"dodane": 0, "pominiete": 0, "blokady": 0, "bez_osi": [], "konflikty": [], "przyszle": [], "telefony": 0, "bez_kartoteki": 0}
    with transaction.atomic():
        existing = set(ArchivedReservation.objects.filter(row_hash__in=[i["row_hash"] for _, i in items]).values_list("row_hash", flat=True))
        for row, item in items:
            if item["row_hash"] in existing:
                stats["pominiete"] += 1
                continue
            existing.add(item["row_hash"])
            archived = ArchivedReservation.objects.create(**item, imported_by=by)
            stats["dodane"] += 1
            if item["lane"] is None:
                stats["bez_osi"].append(f"wiersz {row}: {item['lane_label']}")
            if item["ends_at"] > now:
                label = f"{item['number']} · {timezone.localtime(item['starts_at']):%d.%m %H:%M}–{timezone.localtime(item['ends_at']):%H:%M} · {item['lane_label']}"
                stats["przyszle"].append(label)
                if item["lane"] is None:
                    continue
                conflicts = list(services.block_conflicts([item["lane"]], [(item["starts_at"], item["ends_at"])]))
                if conflicts:
                    stats["konflikty"].append(f"{label} — koliduje z {', '.join(c.number for c in conflicts)}")
                    continue
                block = Block.objects.create(
                    title=f"GunTracker {item['number']}: {item['client']}"[:120],
                    reason=f"Rezerwacja przeniesiona z GunTrackera; prowadzący: {item['range_officer']}; płatność: {item['payment']}"[:255],
                    starts_at=item["starts_at"], ends_at=item["ends_at"], created_by=by,
                )
                block.lanes.set([item["lane"]])
                archived.block = block
                archived.save(update_fields=["block"])
                stats["blokady"] += 1
        if update_contacts:
            stats["telefony"], stats["bez_kartoteki"] = update_person_contacts(items)
        if preview:
            transaction.set_rollback(True)
    stats["zakres"] = (min(i["starts_at"] for _, i in items), max(i["starts_at"] for _, i in items)) if items else None
    if not preview and (stats["dodane"] or stats["telefony"]):
        log_event("rezerwacja.import_archiwum", summary=f"{uploaded.name}: {stats['dodane']} rezerwacji, {stats['blokady']} blokad, {stats['telefony']} telefonów w kartotece", actor=by)
    return stats
