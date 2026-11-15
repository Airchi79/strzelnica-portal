"""Prowadzący strzelanie zweryfikowani wcześniej (lista z GunTrackera: „Legitymacje Prowadzących Strzelanie, które
zweryfikował Twój zespół” — kolumny Użytkownik, Email, Wgrana, Zweryfikowano).

Zasady (bezpieczeństwo):
- zatwierdzenie automatyczne tylko gdy e-mail konta jest POTWIERDZONY linkiem (właściciel skrzynki) i imię i nazwisko
  konta zgadza się z listą (bez znaczenia wielkość liter, polskie znaki i kolejność) — inaczej zwykła weryfikacja,
- numer, organ i datę wydania osoba wpisuje sama; skan nie jest potrzebny,
- każde automatyczne zatwierdzenie trafia do dziennika zdarzeń i można je cofnąć jak zwykłą weryfikację.

Ten sam plik uzupełnia też brakujące adresy e-mail w kartotece osób bez konta (dopasowanie po imieniu i nazwisku,
tylko jednoznaczne), a konto z potwierdzonym e-mailem łączy się z osobą z kartoteki o tym samym adresie."""

import io
import re
import unicodedata
from datetime import date, datetime

from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction
from django.utils import timezone

from apps.audit.services import log_event

from .models import Credential, PreverifiedOfficer

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 5000
AUTO_NOTE = "Zweryfikowano wcześniej w {source}{when} — zatwierdzono automatycznie (lista PS zweryfikowanych)."
NAME_HEADERS = {"użytkownik", "uzytkownik", "imię i nazwisko", "imie i nazwisko", "klient", "osoba"}
EMAIL_HEADERS = {"email", "e-mail", "adres e-mail", "mail"}
VERIFIED_HEADERS = {"zweryfikowano", "data weryfikacji", "zweryfikowana"}
MONTHS_GENITIVE = {m: i for i, m in enumerate(["stycznia", "lutego", "marca", "kwietnia", "maja", "czerwca", "lipca", "sierpnia",
                                                 "września", "października", "listopada", "grudnia"], start=1)}


class _Rollback(Exception):
    pass


def name_key(*parts):
    """Klucz porównania imienia i nazwiska: bez wielkości liter, polskich znaków, kolejności i nadmiarowych spacji."""
    text = " ".join(str(p or "") for p in parts).replace("ł", "l").replace("Ł", "L")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).casefold()
    return tuple(sorted(re.findall(r"[a-z]+", text)))


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value or "").strip()
    m = re.search(r"(\d{1,2})\s+([a-ząćęłńóśźż]+)\s+(\d{4})", text.lower())  # „5 października 2025” (eksport GunTrackera)
    if m and m.group(2) in MONTHS_GENITIVE:
        try:
            return date(int(m.group(3)), MONTHS_GENITIVE[m.group(2)], int(m.group(1)))
        except ValueError:
            return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", text)
        if not m:
            return None
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def read_rows(uploaded):
    """[(nr wiersza, imię i nazwisko, e-mail, data weryfikacji)] — nagłówek szukany w pierwszych 10 wierszach."""
    from openpyxl import load_workbook

    if not (uploaded.name or "").lower().endswith(".xlsx"):
        raise ValidationError("Wybierz plik XLSX z GunTrackera („Zweryfikowane legitymacje prowadzącego”).")
    if uploaded.size > MAX_BYTES:
        raise ValidationError("Plik jest za duży.")
    try:
        wb = load_workbook(io.BytesIO(uploaded.read()), read_only=True, data_only=True)
    except Exception as e:  # uszkodzony lub nie-XLSX plik
        raise ValidationError("To nie jest poprawny plik Excel (XLSX).") from e
    ws = wb.worksheets[0]
    ws.reset_dimensions()
    rows = list(ws.iter_rows(values_only=True, max_row=MAX_ROWS))
    header_at = None
    for i, row in enumerate(rows[:10]):
        labels = [str(v or "").strip().lower() for v in row]
        if any(lbl in NAME_HEADERS for lbl in labels) and any(lbl in EMAIL_HEADERS for lbl in labels):
            header_at = i
            break
    if header_at is None:
        raise ValidationError("Nie znaleziono kolumn „Użytkownik” i „Email” — użyj eksportu listy zweryfikowanych legitymacji PS.")
    labels = [str(v or "").strip().lower() for v in rows[header_at]]
    i_name = next(i for i, lbl in enumerate(labels) if lbl in NAME_HEADERS)
    i_email = next(i for i, lbl in enumerate(labels) if lbl in EMAIL_HEADERS)
    i_when = next((i for i, lbl in enumerate(labels) if lbl in VERIFIED_HEADERS), None)
    out = []
    for n, row in enumerate(rows[header_at + 1:], start=header_at + 2):
        if not any(v not in (None, "") for v in row):
            continue
        name = " ".join(str(row[i_name] or "").split())[:200] if i_name < len(row) else ""
        email = str(row[i_email] or "").strip().lower() if i_email < len(row) else ""
        when = _date(row[i_when]) if i_when is not None and i_when < len(row) else None
        out.append((n, name, email, when))
    return out


# --- zatwierdzanie -------------------------------------------------------------------------


def entry_for(user):
    """Wpis z listy pasujący do konta: e-mail potwierdzony i zgodne imię i nazwisko."""
    if not user.email_verified:
        return None
    entry = PreverifiedOfficer.objects.filter(email__iexact=user.email).first()
    if entry is None or not name_key(entry.name) or name_key(entry.name) != name_key(user.first_name, user.last_name):
        return None
    return entry


def _approve(credential, entry):
    from . import services

    when = f" ({entry.verified_on:%d.%m.%Y})" if entry.verified_on else ""
    services.review(credential, None, True, AUTO_NOTE.format(source=entry.source, when=when))


def apply(user, *, by=None):
    """Zatwierdza oczekujące uprawnienia PS konta z listy (i — wg wpisu — klienta). Zwraca liczbę zatwierdzonych."""
    entry = entry_for(user)
    if entry is None:
        return 0
    pending = list(Credential.objects.filter(user=user, kind=Credential.Kind.RANGE_OFFICER, status=Credential.Status.PENDING))
    for credential in pending:
        _approve(credential, entry)
    if pending:
        if entry.approve_client:
            from apps.clients.services import profile_for, set_approved

            if not profile_for(user).approved_at:
                set_approved(user, True, by)
        PreverifiedOfficer.objects.filter(pk=entry.pk).update(applied_user=user, applied_at=timezone.now())
        log_event("dokument.ps_zweryfikowany_wczesniej", user, f"{entry.source}: zatwierdzono uprawnienia PS ({len(pending)})", actor=by)
    return len(pending)


def link_persons(user):
    """Osoba z kartoteki (bez konta) z tym samym, potwierdzonym adresem e-mail → połączenie z kontem."""
    from apps.clients.models import Person

    if not user.email_verified or not user.email:
        return 0
    linked = 0
    for person in Person.objects.filter(email__iexact=user.email, user__isnull=True):
        person.user = user
        person.save(update_fields=["user"])
        log_event("klient.osoba_polaczona", user, f"{person.full_name} — połączono z kontem (ten sam e-mail)")
        linked += 1
    return linked


def on_email_verified(user):
    link_persons(user)
    apply(user)


# --- import listy ----------------------------------------------------------------------------


def run(uploaded, *, by, preview=False, approve_client=True, fill_emails=True):
    """Import listy. Raport: [(status, opis)] — statusy: dodano, zmieniono, bez_zmian, zatwierdzono, email, pominieto, blad.
    W podglądzie nic nie jest zapisywane (transakcja wycofana)."""
    from apps.accounts.models import User
    from apps.clients.models import Person

    rows = read_rows(uploaded)
    report = []
    try:
        with transaction.atomic():
            seen = {}
            for n, name, email, when in rows:
                try:
                    validate_email(email)
                except ValidationError:
                    report.append(("blad", f"Wiersz {n}: {name or '—'} — brak lub błędny adres e-mail, pominięto"))
                    continue
                if not name_key(name):
                    report.append(("blad", f"Wiersz {n}: brak imienia i nazwiska, pominięto"))
                    continue
                if email in seen:
                    report.append(("pominieto", f"Wiersz {n}: {name} — ten sam e-mail co w wierszu {seen[email]}"))
                    continue
                seen[email] = n
                entry, created = PreverifiedOfficer.objects.get_or_create(
                    email=email, defaults={"name": name, "verified_on": when, "imported_by": by, "approve_client": approve_client})
                if created:
                    report.append(("dodano", f"{name}"))
                elif (entry.name, entry.verified_on, entry.approve_client) != (name, when or entry.verified_on, approve_client):
                    entry.name, entry.verified_on, entry.approve_client = name, when or entry.verified_on, approve_client
                    entry.save(update_fields=["name", "verified_on", "approve_client"])
                    report.append(("zmieniono", f"{name}"))
                else:
                    report.append(("bez_zmian", f"{name}"))
            # konta, które już są w systemie: oczekujące uprawnienia PS → zatwierdzenie (w podglądzie tylko raport —
            # zatwierdzenie wysyła e-mail do osoby, a tego nie da się wycofać)
            for user in User.objects.filter(email__in=list(seen), email_verified_at__isnull=False):
                if entry_for(user) is None:
                    report.append(("pominieto", f"{user.full_name}: konto z tym e-mailem ma inne imię lub nazwisko niż lista — "
                                                "uprawnienia zatwierdzi obsługa"))
                    continue
                pending = Credential.objects.filter(user=user, kind=Credential.Kind.RANGE_OFFICER, status=Credential.Status.PENDING).count()
                if preview:
                    if pending:
                        report.append(("zatwierdzono", f"{user.full_name}: uprawnienia PS czekające na weryfikację zostaną zatwierdzone"))
                    continue
                link_persons(user)
                if apply(user, by=by):
                    report.append(("zatwierdzono", f"{user.full_name}: zatwierdzono uprawnienia PS czekające na weryfikację"))
            if fill_emails:
                report += _fill_person_emails(Person, list(PreverifiedOfficer.objects.filter(email__in=list(seen))))
            if preview:
                raise _Rollback
    except _Rollback:
        pass
    if not preview:
        counts = {}
        for status, _ in report:
            counts[status] = counts.get(status, 0) + 1
        log_event("dokument.ps_lista_import", None, f"Lista PS zweryfikowanych: {counts}", actor=by)
    return report


def _fill_person_emails(Person, entries):
    """Kartoteka: osoba bez e-maila i bez konta, jednoznacznie dopasowana po imieniu i nazwisku → e-mail z listy."""
    report = []
    by_key = {}
    for e in entries:
        by_key.setdefault(name_key(e.name), []).append(e)
    persons = {}
    for p in Person.objects.all():
        persons.setdefault(name_key(p.first_name, p.last_name), []).append(p)
    used = {e.lower() for e in Person.objects.exclude(email="").values_list("email", flat=True)}
    for key, items in by_key.items():
        matches = persons.get(key, [])
        if not matches:
            continue
        name = items[0].name
        if len(items) > 1 or len(matches) > 1:
            report.append(("pominieto", f"Kartoteka: {name} — kilka osób o tym imieniu i nazwisku, e-mail uzupełnij ręcznie"))
            continue
        person, entry = matches[0], items[0]
        if person.email:
            if person.email.lower() != entry.email:
                report.append(("pominieto", f"Kartoteka: {person.full_name} — ma już inny e-mail, bez zmian"))
            continue
        if person.user_id:
            continue
        if entry.email in used:
            report.append(("pominieto", f"Kartoteka: {person.full_name} — ten e-mail ma już inna osoba w kartotece, bez zmian"))
            continue
        person.email = entry.email
        person.save(update_fields=["email"])
        used.add(entry.email)
        report.append(("email", f"Kartoteka: {person.full_name} — uzupełniono e-mail"))
    return report
