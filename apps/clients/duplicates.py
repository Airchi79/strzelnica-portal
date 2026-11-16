"""Porządkowanie kartoteki osób bez konta: klucz imienia i nazwiska (bez wielkości liter, polskich znaków i kolejności),
grupy możliwych duplikatów (to samo imię i nazwisko albo ten sam numer pozwolenia), scalanie i eksport do XLSX.

Do osób z kartoteki nie odwołują się inne rekordy — scalenie przenosi brakujące dane do osoby głównej, a pozostałe usuwa.
Wydrukowana karta QR usuniętej osoby przestaje działać (ostrzeżenie w panelu)."""

import io
import re
import unicodedata

from django.core.exceptions import ValidationError
from django.db import transaction

from apps.audit.services import log_event

from .models import Person

MERGE_FIELDS = ["permit_number", "permit_issuer", "address", "email", "phone"]
LABELS = {"permit_number": "nr pozwolenia", "permit_issuer": "organ", "address": "adres", "email": "e-mail", "phone": "telefon"}


def name_key(*parts):
    """Klucz porównania imienia i nazwiska: bez wielkości liter, polskich znaków, kolejności i nadmiarowych spacji."""
    text = " ".join(str(p or "") for p in parts).replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return tuple(sorted(re.findall(r"[a-z]+", text)))


def person_key(p):
    return name_key(p.first_name, p.last_name)


def _same_text(a, b):
    return name_key(a) == name_key(b) and bool(name_key(a))


def groups(persons=None):
    """Grupy możliwych duplikatów (≥ 2 osoby): to samo imię i nazwisko albo ten sam numer pozwolenia."""
    persons = list(persons if persons is not None else Person.objects.all())
    parent = {p.pk: p.pk for p in persons}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    first = {}
    for p in persons:
        for key in [("n",) + person_key(p)] + ([("p", p.permit_number)] if p.permit_number else []):
            if key == ("n",):
                continue
            if key in first:
                union(p.pk, first[key])
            else:
                first[key] = p.pk
    by_root = {}
    for p in persons:
        by_root.setdefault(find(p.pk), []).append(p)
    out = [sorted(g, key=lambda p: p.pk) for g in by_root.values() if len(g) > 1]
    return sorted(out, key=lambda g: (g[0].last_name.casefold(), g[0].first_name.casefold()))


def suggested_primary(group):
    """Osoba z kontem, potem z wydrukowaną kartą QR (karta zostaje ważna — dane kopii i tak są przenoszone), potem
    najpełniejsza (pozwolenie z organem, adres, kontakt), potem najstarsza."""
    def score(p):
        return (bool(p.user_id), bool(p.card_printed_at), bool(p.permit_number and p.permit_issuer), bool(p.address), bool(p.email),
                bool(p.phone), -p.pk)
    return max(group, key=score)


def merge(primary, others, *, by):
    """Scala `others` w `primary`: puste pola uzupełnia, różniące się dane dopisuje do uwag, konto przenosi; usuwa others.
    Zwraca (uzupełnione_pola, różnice[str], karty_unieważnione)."""
    others = [o for o in others if o.pk != primary.pk]
    if not others:
        raise ValidationError("Zaznacz osoby do scalenia z osobą główną.")
    with transaction.atomic():
        primary = Person.objects.select_for_update().get(pk=primary.pk)
        others = list(Person.objects.select_for_update().filter(pk__in=[o.pk for o in others]))
        users = {o.user_id for o in others if o.user_id} | ({primary.user_id} if primary.user_id else set())
        if len(users) > 1:
            raise ValidationError("Te osoby są połączone z różnymi kontami — to nie jest ta sama osoba, nie scalam.")
        filled, differences, cards = [], [], 0
        for o in others:
            for f in MERGE_FIELDS:
                mine, theirs = getattr(primary, f) or "", getattr(o, f) or ""
                if not theirs:
                    continue
                if not mine:
                    setattr(primary, f, theirs)
                    filled.append(f)
                elif not (_same_text(mine, theirs) if f not in ("email", "permit_number") else mine.lower() == theirs.lower()):
                    differences.append(f"{LABELS[f]}: {theirs}")
            if o.user_id and not primary.user_id:
                primary.user_id = o.user_id
            if o.note and o.note not in primary.note:
                differences.append(o.note)
            if o.card_printed_at:
                cards += 1
        if differences:
            note = (primary.note + " | " if primary.note else "") + "Scalono — inne dane: " + "; ".join(differences)
            primary.note = note[:255]
        primary.save()
        names = ", ".join(f"#{o.pk}" for o in others)
        for o in others:
            o.delete()
    log_event("klient.scalenie_osob", primary, f"{primary.full_name}: scalono {len(others)} ({names})", actor=by)
    return sorted(set(filled)), differences, cards


EXPORT_HEADER = ["ID", "Imię", "Nazwisko", "Numer pozwolenia", "Organ wydający", "Adres", "Telefon", "E-mail", "Konto w systemie",
                 "Pochodzenie", "Dodano", "Karta wydrukowana", "Uwagi", "Grupa możliwego duplikatu"]


def export_xlsx(persons):
    from openpyxl import Workbook
    from openpyxl.styles import Font
    from openpyxl.utils import get_column_letter

    persons = list(persons)
    group_of = {}
    for n, g in enumerate(groups(persons), start=1):
        for p in g:
            group_of[p.pk] = n
    wb = Workbook()
    ws = wb.active
    ws.title = "Osoby bez konta"
    ws.append(EXPORT_HEADER)
    for cell in ws[1]:
        cell.font = Font(bold=True)
    for p in sorted(persons, key=lambda p: (p.last_name.casefold(), p.first_name.casefold(), p.pk)):
        ws.append([p.pk, p.first_name, p.last_name, p.permit_number, p.permit_issuer, p.address, p.phone, p.email,
                   "tak" if p.user_id else "", p.get_source_display(), p.created_at.strftime("%Y-%m-%d") if p.created_at else "",
                   p.card_printed_at.strftime("%Y-%m-%d") if p.card_printed_at else "", p.note, group_of.get(p.pk, "")])
    for i, width in enumerate([7, 14, 18, 16, 22, 34, 16, 28, 10, 20, 11, 12, 30, 12], start=1):
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
