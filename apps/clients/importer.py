"""Import osób do kartoteki z arkusza XLSX (format eksportu „Klienci” z GunTrackera: Imię, Nazwisko, Numer pozwolenia,
Organ wydający, Adres; opcjonalnie E-mail i Telefon). Podgląd wycofuje transakcję — nic nie zapisuje.

Osoba, która już jest w kartotece (ten sam nr pozwolenia albo imię i nazwisko), nie jest dodawana drugi raz — import
uzupełnia jej brakujące dane (np. e-mail, telefon, organ wydający). Dane różniące się od kartoteki nie są nadpisywane
(mogły być poprawione w panelu) — import je pokazuje, a nadpisuje tylko w trybie „nadpisz różniące się dane”.
"""

import io

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.db.models import Q

from apps.audit.services import log_event

from .models import Person

MAX_BYTES = 20 * 1024 * 1024
MAX_ROWS = 20000
# nagłówek kolumny (małymi literami) → pole; kilka wariantów nazw dla e-maila i telefonu
COLUMNS = {
    "imię": "first_name", "nazwisko": "last_name", "numer pozwolenia": "permit_number", "organ wydający": "permit_issuer",
    "adres": "address",
    "e-mail": "email", "email": "email", "adres e-mail": "email", "adres email": "email", "mail": "email",
    "telefon": "phone", "nr telefonu": "phone", "numer telefonu": "phone", "telefon kontaktowy": "phone",
}
FIELDS = ["first_name", "last_name", "permit_number", "permit_issuer", "address", "email", "phone"]
LIMITS = {"first_name": 80, "last_name": 120, "permit_number": 64, "permit_issuer": 160, "address": 255, "email": 254, "phone": 32}
# pola uzupełniane / porównywane przy osobie, która już jest w kartotece (imię i nazwisko — bez zmian)
MERGE_FIELDS = ["permit_number", "permit_issuer", "address", "email", "phone"]
LABELS = {"permit_number": "nr pozwolenia", "permit_issuer": "organ wydający", "address": "adres", "email": "e-mail", "phone": "telefon"}


def normalize_permit(value):
    return "".join(str(value or "").split()).upper()


def _cell(value):
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # telefon zapisany w Excelu jako liczba
    return " ".join(str(value if value is not None else "").split())


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
    ws.reset_dimensions()  # GunTracker zapisuje w pliku błędny rozmiar arkusza („A1:A1”)
    rows = ws.iter_rows(values_only=True)
    header = [str(v or "").strip().lower() for v in next(rows, [])]
    index = {}
    for i, label in enumerate(header):
        field = COLUMNS.get(label)
        if field and field not in index:
            index[field] = i
    if "first_name" not in index or "last_name" not in index:
        raise ValidationError("Arkusz musi mieć kolumny „Imię” i „Nazwisko” (opcjonalnie: Numer pozwolenia, Organ wydający, Adres, "
                              "E-mail, Telefon).")
    out = []
    for row_no, values in enumerate(rows, start=2):
        if row_no > MAX_ROWS:
            raise ValidationError("Arkusz ma zbyt wiele wierszy.")
        if not any(v not in (None, "") for v in values):
            continue
        item = {f: _cell(values[i] if i < len(values) else None)[:LIMITS[f]] for f, i in index.items()}
        for f in FIELDS:
            item.setdefault(f, "")
        item["permit_number"] = normalize_permit(item["permit_number"])
        item["email"] = item["email"].lower()
        if item["phone"]:
            from apps.bookings.archive import format_phone

            item["phone"] = format_phone(item["phone"])
        out.append((row_no, item))
    return out


def find_existing(item):
    if item.get("permit_number"):
        hit = Person.objects.filter(permit_number=item["permit_number"]).first()
        if hit:
            return hit
    return Person.objects.filter(first_name__iexact=item["first_name"], last_name__iexact=item["last_name"]).filter(
        Q(permit_number="") | Q(permit_number=item.get("permit_number", "")) | Q(address__iexact=item.get("address", ""))
    ).first()


def _same(field, a, b):
    if field in ("address", "permit_issuer", "email"):
        return " ".join(a.split()).lower() == " ".join(b.split()).lower()
    if field == "phone":
        return "".join(ch for ch in a if ch.isdigit())[-9:] == "".join(ch for ch in b if ch.isdigit())[-9:]
    return a == b


def merge(person, item, overwrite=False):
    """Uzupełnia osobę z kartoteki danymi z pliku. Zwraca (uzupełnione_pola, różniące_się_pola, nadpisane_pola).
    Puste pola są uzupełniane zawsze; różniące się — tylko przy `overwrite`."""
    filled, different, replaced = [], [], []
    for f in MERGE_FIELDS:
        new, old = item.get(f, ""), getattr(person, f) or ""
        if not new or _same(f, new, old):
            continue
        if not old:
            setattr(person, f, new)
            filled.append(f)
        elif overwrite:
            setattr(person, f, new)
            replaced.append(f)
        else:
            different.append(f)
    if filled or replaced:
        person.save(update_fields=filled + replaced)
    return filled, different, replaced


def _names(fields):
    return ", ".join(LABELS[f] for f in fields)


def run(uploaded, *, by=None, preview=False, overwrite=False, source=Person.Source.GUNTRACKER):
    """Statusy w raporcie: ok (dodano), uzupelniono, roznica (różne dane — bez zmian), nadpisano, bez_zmian, blad."""
    rows = read_rows(uploaded)
    report, stats = [], {"dodane": 0, "uzupelnione": 0, "nadpisane": 0}
    with transaction.atomic():
        for row_no, item in rows:
            label = f"Wiersz {row_no}: {item['first_name']} {item['last_name']}"
            if not item["first_name"] or not item["last_name"]:
                report.append(("blad", f"Wiersz {row_no}: brak imienia lub nazwiska — pominięto"))
                continue
            if item["email"]:
                try:
                    validate_email(item["email"])
                except ValidationError:
                    report.append(("blad", f"{label} — niepoprawny e-mail „{item['email']}” pominięto"))
                    item["email"] = ""
            existing = find_existing(item)
            if existing:
                filled, different, replaced = merge(existing, item, overwrite=overwrite)
                if filled:
                    stats["uzupelnione"] += 1
                    report.append(("uzupelniono", f"{label} — uzupełniono: {_names(filled)}"))
                if replaced:
                    stats["nadpisane"] += 1
                    report.append(("nadpisano", f"{label} — zastąpiono danymi z pliku: {_names(replaced)}"))
                if different:
                    report.append(("roznica", f"{label} — w pliku inne dane niż w kartotece ({_names(different)}) — bez zmian"))
                if not (filled or replaced or different):
                    report.append(("bez_zmian", f"{label} — już w kartotece, bez zmian"))
                continue
            Person.objects.create(**item, source=source, created_by=by)
            stats["dodane"] += 1
            if item["permit_number"] and not item["permit_issuer"]:
                report.append(("blad", f"{label} — dodano, ale brak organu wydającego pozwolenie (uzupełnij przed wpisem)"))
            elif not item["permit_number"] and not item["address"]:
                report.append(("blad", f"{label} — dodano, ale brak pozwolenia i adresu (uzupełnij przed wpisem)"))
            else:
                report.append(("ok", label))
        if preview:
            transaction.set_rollback(True)
    if not preview and any(stats.values()):
        log_event("klient.import_osob", summary=f"{uploaded.name}: dodano {stats['dodane']}, uzupełniono {stats['uzupelnione']}, "
                                                 f"nadpisano {stats['nadpisane']} osób w kartotece", actor=by)
    return report
