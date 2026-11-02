"""Import osób do kartoteki z arkusza XLSX (format eksportu „Klienci” z GunTrackera:
Imię, Nazwisko, Numer pozwolenia, Organ wydający, Adres). Podgląd wycofuje transakcję — nic nie zapisuje."""

import io

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Q

from apps.audit.services import log_event

from .models import Person

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 20000
COLUMNS = {"imię": "first_name", "nazwisko": "last_name", "numer pozwolenia": "permit_number", "organ wydający": "permit_issuer", "adres": "address"}
LIMITS = {"first_name": 80, "last_name": 120, "permit_number": 64, "permit_issuer": 160, "address": 255}


def normalize_permit(value):
    return "".join(str(value or "").split()).upper()


def read_rows(uploaded):
    from openpyxl import load_workbook

    if not (uploaded.name or "").lower().endswith(".xlsx"):
        raise ValidationError("Wybierz plik XLSX (eksport „Klienci” z GunTrackera albo arkusz z tymi samymi kolumnami).")
    if uploaded.size > MAX_BYTES:
        raise ValidationError("Plik jest za duży (najwyżej 20 MB).")
    try:
        wb = load_workbook(io.BytesIO(uploaded.read()), read_only=True, data_only=True)
    except Exception as e:  # uszkodzony lub nie-XLSX plik
        raise ValidationError("To nie jest poprawny plik Excel (XLSX).") from e
    ws = wb.worksheets[0]
    rows = ws.iter_rows(values_only=True)
    header = [str(v or "").strip().lower() for v in next(rows, [])]
    index = {}
    for label, field in COLUMNS.items():
        if label in header:
            index[field] = header.index(label)
    if "first_name" not in index or "last_name" not in index:
        raise ValidationError("Arkusz musi mieć kolumny „Imię” i „Nazwisko” (opcjonalnie: Numer pozwolenia, Organ wydający, Adres).")
    out = []
    for row_no, values in enumerate(rows, start=2):
        if row_no > MAX_ROWS:
            raise ValidationError("Arkusz ma zbyt wiele wierszy.")
        if not any(v not in (None, "") for v in values):
            continue
        item = {f: " ".join(str(values[i] if i < len(values) and values[i] is not None else "").split())[:LIMITS[f]]
                for f, i in index.items()}
        for f in COLUMNS.values():
            item.setdefault(f, "")
        item["permit_number"] = normalize_permit(item["permit_number"])
        out.append((row_no, item))
    return out


def find_existing(item):
    if item["permit_number"]:
        hit = Person.objects.filter(permit_number=item["permit_number"]).first()
        if hit:
            return hit
    return Person.objects.filter(first_name__iexact=item["first_name"], last_name__iexact=item["last_name"]).filter(
        Q(permit_number="") | Q(permit_number=item["permit_number"]) | Q(address__iexact=item["address"])
    ).first()


def run(uploaded, *, by=None, preview=False, source=Person.Source.GUNTRACKER):
    rows = read_rows(uploaded)
    report, added = [], 0
    with transaction.atomic():
        for row_no, item in rows:
            label = f"Wiersz {row_no}: {item['first_name']} {item['last_name']}"
            if not item["first_name"] or not item["last_name"]:
                report.append(("blad", f"Wiersz {row_no}: brak imienia lub nazwiska — pominięto"))
                continue
            if find_existing(item):
                report.append(("pominieto", f"{label} — już w kartotece"))
                continue
            Person.objects.create(**item, source=source, created_by=by)
            added += 1
            if item["permit_number"] and not item["permit_issuer"]:
                report.append(("blad", f"{label} — dodano, ale brak organu wydającego pozwolenie (uzupełnij przed wpisem)"))
            elif not item["permit_number"] and not item["address"]:
                report.append(("blad", f"{label} — dodano, ale brak pozwolenia i adresu (uzupełnij przed wpisem)"))
            else:
                report.append(("ok", label))
        if preview:
            transaction.set_rollback(True)
    if not preview and added:
        log_event("klient.import_osob", summary=f"{uploaded.name}: dodano {added} osób do kartoteki", actor=by)
    return report
