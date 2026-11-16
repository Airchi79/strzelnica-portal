"""Porządkowanie danych przeniesionych z GunTrackera na podstawie zbiorczego pliku przygotowanego ręcznie
(XLSX, arkusze „Rezerwacje” i „Klienci”; arkusz „rezerwacje statystyka” jest tylko podsumowaniem i nie jest czytany).

Kroki (każdy można włączyć osobno; podgląd wycofuje transakcję — nic nie zapisuje i nic nie wysyła):
1. kartoteka „Osoby bez konta”: klienci z arkusza „Klienci” — dane z pliku zastępują dane w kartotece (puste pole
   w pliku nie kasuje danych), duplikaty tej samej osoby są scalane (karta QR osoby głównej zostaje ważna);
   opcjonalnie usuwa pozostałe osoby z importów GunTrackera (spoza listy, bez konta). Osób dodanych ręcznie w panelu
   i połączonych z kontem nie usuwa,
2. lista PS zweryfikowanych wcześniej: osoby ze statusem „Zweryfikowana” (e-mail, data weryfikacji); wpisy spoza
   pliku, jeszcze niewykorzystane, są usuwane,
3. archiwum rezerwacji GunTrackera: czyszczone w całości (z blokadami osi założonymi przez import) i wgrywane z pliku,
4. archiwum książki pobytu GunTrackera: czyszczone i wgrywane z osobnego eksportu „Księga pobytu” (XLSX); osoby z księgi
   dodawane do kartoteki tylko na życzenie (domyślnie kartoteka = lista klientów)."""

import io
import re
from datetime import datetime, time

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from apps.audit.services import log_event

from .duplicates import MERGE_FIELDS, merge, name_key, suggested_primary
from .importer import normalize_permit
from .models import Person

MAX_BYTES = 10 * 1024 * 1024
TIME_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*[–-]\s*(\d{1,2}):(\d{2})\s*$")
LANE_RE = re.compile(r"oś\s*(\d+)\s*m(?:\s*\((\d+)\))?", re.IGNORECASE)
CLIENT_COLS = {"klient": "client", "telefon": "phone", "e-mail": "email", "ulica i numer": "street", "kod pocztowy": "postcode",
               "miejscowość": "city", "numer pozwolenia": "permit_number", "organ wydający": "permit_issuer",
               "adres (pełny ze źródła)": "address", "legitymacja prowadzącego — status": "ps_status",
               "data weryfikacji legitymacji": "ps_verified"}
RES_COLS = {"data": "day", "godzina": "hours", "klient": "client", "numer rezerwacji": "number", "oś": "lane", "stanowisko": "positions",
            "prowadzący": "officer", "księga": "logbook", "płatność": "payment"}


class _Rollback(Exception):
    pass


def _cell(v):
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return " ".join(str(v if v is not None else "").split())


def _sheet(wb, name, columns, required):
    ws = next((w for w in wb.worksheets if w.title.strip().lower() == name), None)
    if ws is None:
        raise ValidationError(f"Brak arkusza „{name.capitalize()}”.")
    rows = list(ws.iter_rows(values_only=True))
    for i, row in enumerate(rows[:10]):
        labels = [_cell(v).lower() for v in row]
        if all(r in labels for r in required):
            index = {columns[lbl]: j for j, lbl in enumerate(labels) if lbl in columns}
            out = []
            for n, values in enumerate(rows[i + 1:], start=i + 2):
                if not any(v not in (None, "") for v in values):
                    continue
                out.append((n, {f: values[j] if j < len(values) else None for f, j in index.items()}))
            return out
    raise ValidationError(f"Arkusz „{name.capitalize()}”: nie znaleziono nagłówka z kolumnami {', '.join(required)}.")


def read(uploaded):
    from openpyxl import load_workbook

    if not (uploaded.name or "").lower().endswith(".xlsx"):
        raise ValidationError("Wybierz plik XLSX.")
    if uploaded.size > MAX_BYTES:
        raise ValidationError("Plik jest za duży.")
    try:
        wb = load_workbook(io.BytesIO(uploaded.read()), data_only=True)
    except Exception as e:  # uszkodzony / nie-XLSX
        raise ValidationError("To nie jest poprawny plik Excel (XLSX).") from e
    return (_sheet(wb, "klienci", CLIENT_COLS, ["klient", "telefon", "e-mail"]),
            _sheet(wb, "rezerwacje", RES_COLS, ["data", "godzina", "klient", "numer rezerwacji", "oś"]))


def _split_name(full):
    parts = full.split()
    return (parts[0], " ".join(parts[1:])) if len(parts) > 1 else (full, "")


def _phone(raw):
    from apps.bookings.archive import format_phone

    return format_phone(raw) if raw else ""


def _lane(label, lanes):
    """„Oś 25 m (2)” → „Oś 25 m nr 2”, „Oś 100m” → „Oś 100 m”, „Oś 35 m” → „Oś 35 m”."""
    m = LANE_RE.search(label or "")
    if not m:
        return None
    wanted = f"oś {m.group(1)} m nr {m.group(2)}" if m.group(2) else f"oś {m.group(1)} m"
    return next((lane for lane in lanes if lane.name.lower() == wanted), None)


def _day(value):
    if isinstance(value, datetime):
        return value.date()
    if hasattr(value, "year") and hasattr(value, "month"):
        return value
    from apps.credentials.preverified import _date

    return _date(value)


def parse_clients(rows):
    from apps.credentials.preverified import _date

    clients, problems = [], []
    for n, r in rows:
        name = _cell(r.get("client"))
        if not name_key(name):
            problems.append(f"Klienci, wiersz {n}: brak imienia i nazwiska — pominięto")
            continue
        email = _cell(r.get("email")).lower()
        if email:
            try:
                validate_email(email)
            except ValidationError:
                problems.append(f"Klienci, wiersz {n}: {name} — błędny e-mail, pominięto e-mail")
                email = ""
        address = _cell(r.get("address"))
        if not address:
            street, code, city = _cell(r.get("street")), _cell(r.get("postcode")), _cell(r.get("city"))
            address = ", ".join(x for x in [street, " ".join(x for x in [code, city] if x)] if x)
        first, last = _split_name(name)
        clients.append({
            "row": n, "name": name, "first_name": first[:80], "last_name": last[:120], "email": email[:254],
            "phone": _phone(_cell(r.get("phone")))[:32], "address": address[:255],
            "permit_number": normalize_permit(_cell(r.get("permit_number")))[:64], "permit_issuer": _cell(r.get("permit_issuer"))[:160],
            "ps_verified": _cell(r.get("ps_status")).lower() == "zweryfikowana", "ps_date": _date(r.get("ps_verified")),
        })
    return clients, problems


def parse_reservations(rows, phones):
    from apps.bookings.archive import row_hash
    from apps.facilities.models import Lane

    lanes = list(Lane.objects.all())
    tz = timezone.get_current_timezone()
    items, problems = [], []
    for n, r in rows:
        day, t = _day(r.get("day")), TIME_RE.match(_cell(r.get("hours")))
        number, client = _cell(r.get("number")), _cell(r.get("client"))
        if not day or not t or not number or not client:
            problems.append(f"Rezerwacje, wiersz {n}: brak daty, godziny, numeru lub klienta — pominięto")
            continue
        h1, m1, h2, m2 = (int(x) for x in t.groups())
        start = timezone.make_aware(datetime.combine(day, time(h1, m1)), tz)
        end = timezone.make_aware(datetime.combine(day, time(23, 59) if (h2, m2) == (24, 0) else time(h2, m2)), tz)
        if end <= start:
            problems.append(f"Rezerwacje, wiersz {n}: koniec przed początkiem — pominięto")
            continue
        label = _cell(r.get("lane"))
        item = {"number": number[:40], "starts_at": start, "ends_at": end, "client": client[:160], "lane_label": label[:80],
                "positions": _cell(r.get("positions"))[:40], "range_officer": _cell(r.get("officer"))[:160],
                "logbook_info": re.sub(r"^Dodano(\d)", r"Dodano \1", _cell(r.get("logbook")))[:80],
                "payment": _cell(r.get("payment"))[:80], "contact": phones.get(name_key(client), "")[:40],
                "lane": _lane(label, lanes)}
        item["row_hash"] = row_hash(item)
        items.append((n, item))
    return items, problems


# --- kroki -------------------------------------------------------------------------------------------------------------


def sync_persons(clients, *, by, prune):
    report = {"zaktualizowane": 0, "dodane": 0, "scalone": 0, "usuniete": [], "karty_usuniete": 0, "konflikty": []}
    persons = list(Person.objects.all())
    by_key, by_email, by_permit = {}, {}, {}
    for p in persons:
        by_key.setdefault(name_key(p.first_name, p.last_name), []).append(p)
        if p.email:
            by_email.setdefault(p.email.lower(), []).append(p)
        if p.permit_number:
            by_permit.setdefault(p.permit_number, []).append(p)
    keep = set()
    for c in clients:
        group = {p.pk: p for p in by_key.get(name_key(c["name"]), [])}
        for p in by_email.get(c["email"], []) if c["email"] else []:
            group[p.pk] = p
        for p in by_permit.get(c["permit_number"], []) if c["permit_number"] else []:
            if name_key(p.first_name, p.last_name) == name_key(c["name"]):
                group[p.pk] = p
        group = [p for p in group.values() if p.pk not in keep]
        if group:
            users = {p.user_id for p in group if p.user_id}
            if len(users) > 1:
                report["konflikty"].append(f"{c['name']}: w kartotece kilka osób połączonych z różnymi kontami — bez scalania")
                primary = next(p for p in group if p.user_id)
            else:
                primary = suggested_primary(group)
                others = [p for p in group if p.pk != primary.pk]
                if others:
                    merge(primary, others, by=by)
                    report["scalone"] += len(others)
                primary.refresh_from_db()
            changed = []
            for f in ["first_name", "last_name"] + MERGE_FIELDS:
                if c[f] and getattr(primary, f) != c[f]:
                    setattr(primary, f, c[f])
                    changed.append(f)
            if changed:
                primary.save(update_fields=changed)
                report["zaktualizowane"] += 1
            keep.add(primary.pk)
        else:
            p = Person.objects.create(**{f: c[f] for f in ["first_name", "last_name"] + MERGE_FIELDS}, source=Person.Source.GUNTRACKER,
                                      created_by=by)
            keep.add(p.pk)
            report["dodane"] += 1
    if prune:
        for p in Person.objects.filter(source=Person.Source.GUNTRACKER, user__isnull=True).exclude(pk__in=keep):
            report["usuniete"].append(p.full_name)
            report["karty_usuniete"] += bool(p.card_printed_at)
            p.delete()
    return report


def sync_preverified(clients, *, by):
    from apps.credentials.models import PreverifiedOfficer

    report = {"dodane": 0, "zmienione": 0, "usuniete": 0, "bez_weryfikacji": 0}
    wanted = {}
    for c in clients:
        if not c["ps_verified"]:
            report["bez_weryfikacji"] += 1
            continue
        if c["email"]:
            wanted[c["email"]] = c
    for email, c in wanted.items():
        entry, created = PreverifiedOfficer.objects.get_or_create(email=email, defaults={"name": c["name"], "verified_on": c["ps_date"],
                                                                                         "imported_by": by})
        if created:
            report["dodane"] += 1
        elif (entry.name, entry.verified_on) != (c["name"], c["ps_date"] or entry.verified_on):
            entry.name, entry.verified_on = c["name"], c["ps_date"] or entry.verified_on
            entry.save(update_fields=["name", "verified_on"])
            report["zmienione"] += 1
    stale = PreverifiedOfficer.objects.exclude(email__in=list(wanted)).filter(applied_user__isnull=True)
    report["usuniete"] = stale.count()
    stale.delete()
    return report


def reset_reservations(items, *, by, now=None):
    from apps.bookings.archive import new_stats, store
    from apps.bookings.models import ArchivedReservation, Block

    old = ArchivedReservation.objects.all()
    block_ids = list(old.exclude(block__isnull=True).values_list("block_id", flat=True))
    report = {"usuniete": old.count(), "blokady_usuniete": len(block_ids)}
    old.delete()
    Block.objects.filter(pk__in=block_ids).delete()
    stats = store(items, by=by, now=now, stats=new_stats())
    report.update(stats)
    return report


def reset_logbook(book, *, by, add_persons=False):
    """Archiwum książki pobytu GunTrackera: czyszczone w całości; z eksportu „Księga pobytu” wgrywane od nowa.
    Osoby z księgi (np. strzelający z prowadzącym) trafiają do kartoteki tylko przy `add_persons`."""
    from apps.logbook import archive
    from apps.logbook.models import ArchivedEntry

    from .importer import PersonIndex, find_existing
    from .importer import merge as merge_fields

    from django.db import connection

    report = {"usuniete": ArchivedEntry.objects.count(), "dodane": 0, "pominiete": 0, "osoby": 0, "osoby_uzupelnione": 0}
    # Archiwum książki jest chronione w bazie przed zmianą i usunięciem; usunięcie dopuszcza tylko przełącznik
    # „satok.logbook_purge” ważny do końca tej transakcji (jak przy usuwaniu po okresie przechowywania). Dotyczy wyłącznie
    # kopii księgi z GunTrackera, zastępowanej w tej samej transakcji świeżym eksportem — wpisów z naszej książki nie rusza.
    with connection.cursor() as cur:
        cur.execute("SELECT set_config('satok.logbook_purge', 'on', true)")
    ArchivedEntry.objects.all().delete()
    with connection.cursor() as cur:
        cur.execute("SELECT set_config('satok.logbook_purge', 'off', true)")
    if book is None:
        return report
    items = archive.parse(book)
    index = PersonIndex() if add_persons else None
    seen = set()
    for _, item in items:
        if item["row_hash"] in seen:
            report["pominiete"] += 1
            continue
        seen.add(item["row_hash"])
        ArchivedEntry.objects.create(**item, imported_by=by, import_file=(book.name or "")[:255])
        report["dodane"] += 1
        if add_persons:
            person = {k: item[k] for k in ("first_name", "last_name", "permit_number", "permit_issuer", "address")}
            known = find_existing(person, index)
            if known is None:
                index.add(Person.objects.create(**person, source=Person.Source.GUNTRACKER, created_by=by))
                report["osoby"] += 1
            elif merge_fields(known, person)[0]:
                report["osoby_uzupelnione"] += 1
    if items:
        report["zakres"] = (min(i["started_at"] for _, i in items), max(i["started_at"] for _, i in items))
    return report


def run(uploaded, *, by, preview=True, persons=True, prune=True, preverified=True, reservations=True, logbook=False, book=None,
        book_persons=False, now=None):
    """`logbook` — wyczyść archiwum książki pobytu i (gdy podano `book`, eksport „Księga pobytu”) wgraj je od nowa."""
    client_rows, res_rows = read(uploaded)
    clients, problems = parse_clients(client_rows)
    phones = {name_key(c["name"]): c["phone"] for c in clients if c["phone"]}
    items, res_problems = parse_reservations(res_rows, phones)
    problems += res_problems
    result = {"klienci": len(clients), "rezerwacje": len(items), "problemy": problems, "podglad": preview}
    try:
        with transaction.atomic():
            if persons:
                result["kartoteka"] = sync_persons(clients, by=by, prune=prune)
            if preverified:
                result["ps"] = sync_preverified(clients, by=by)
            if reservations:
                result["archiwum_rezerwacji"] = reset_reservations(items, by=by, now=now)
                result["zakres"] = (min(i["starts_at"] for _, i in items), max(i["starts_at"] for _, i in items)) if items else None
            if logbook:
                result["ksiazka"] = reset_logbook(book, by=by, add_persons=book_persons)
            if preview:
                raise _Rollback
    except _Rollback:
        pass
    if not preview:
        k, ps, ar, kb = result.get("kartoteka", {}), result.get("ps", {}), result.get("archiwum_rezerwacji", {}), result.get("ksiazka", {})
        log_event("klient.porzadkowanie_guntracker", summary=(
            f"{uploaded.name}: kartoteka — zaktualizowane {k.get('zaktualizowane', 0)}, dodane {k.get('dodane', 0)}, scalone "
            f"{k.get('scalone', 0)}, usunięte {len(k.get('usuniete', []))}; PS — dodane {ps.get('dodane', 0)}, usunięte {ps.get('usuniete', 0)}; "
            f"rezerwacje — usunięte {ar.get('usuniete', 0)}, dodane {ar.get('dodane', 0)}; "
            f"książka — usunięte {kb.get('usuniete', 0)}, dodane {kb.get('dodane', 0)}")[:255], actor=by)
    return result
