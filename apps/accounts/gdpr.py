"""RODO: eksport danych osoby (art. 15 i 20) i usunięcie danych osobowych konta (art. 17) z zachowaniem danych,
które przepisy każą przechowywać (książka pobytu — własny okres przechowywania; rozliczenia — przepisy rachunkowe;
ewidencja członków stowarzyszenia)."""

import secrets

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from apps.audit.services import log_event


def _dt(v):
    return timezone.localtime(v).isoformat(timespec="minutes") if v else None


def export_data(user):
    from apps.audit.models import SensitiveAccessLog
    from apps.association.models import Member, MembershipApplication
    from apps.bookings.models import Reservation, WaitlistEntry
    from apps.clients.models import ClientPass, Person
    from apps.credentials.models import Credential
    from apps.events.models import Registration
    from apps.facilities.models import Issue
    from apps.logbook.models import Entry
    from apps.payments.models import LedgerEntry, Payment
    from apps.regulations.models import Acceptance

    data = {
        "opis": "Dane osobowe przetwarzane w systemie rezerwacji Strzelnicy Sątok (eksport na żądanie, art. 15 i 20 RODO).",
        "wygenerowano": _dt(timezone.now()),
        "konto": {
            "e-mail": user.email, "imię": user.first_name, "nazwisko": user.last_name, "telefon": user.phone,
            "ulica": user.street, "kod_pocztowy": user.postal_code, "miejscowość": user.city,
            "rejestracja": _dt(user.date_joined), "e-mail_potwierdzony": _dt(user.email_verified_at),
            "dostęp_do_panelu": user.is_staff,
        },
        "dokumenty": [{"rodzaj": c.get_kind_display(), "numer": c.number, "organ": c.issuer, "wydano": str(c.issued_on),
                       "ważny_do": str(c.valid_until) if c.valid_until else None, "status": c.get_status_display(),
                       "zdjęcie_w_systemie": bool(c.scan), "uwagi_weryfikacji": c.review_note}
                      for c in Credential.objects.filter(user=user)],
        "akceptacje_regulaminów": [{"dokument": str(a.version), "kiedy": _dt(a.accepted_at), "ip": a.ip}
                                   for a in Acceptance.objects.filter(user=user).select_related("version")],
        "rezerwacje": [{"numer": r.number, "oś": str(r.lane), "od": _dt(r.starts_at), "do": _dt(r.ends_at), "osoby": r.persons,
                        "status": r.get_status_display(), "płatność": r.get_payment_status_display(), "kwota_gr": r.amount,
                        "uwagi": r.notes} for r in Reservation.objects.filter(user=user).select_related("lane")],
        "książka_pobytu": [{"numer": e.number, "wejście": _dt(e.entered_at), "wyjście": _dt(e.exited_at), "cel": e.get_purpose_display(),
                            "imię": e.first_name, "nazwisko": e.last_name, "pozwolenie": e.permit_number, "adres": e.address,
                            "podpis": "zapisany (obraz podpisu przechowywany w książce pobytu)"} for e in Entry.objects.filter(user=user)],
        "płatności": [{"kwota_gr": p.amount, "sposób": p.get_method_display(), "status": p.get_status_display(), "tytuł": p.title,
                       "utworzono": _dt(p.created_at), "zaksięgowano": _dt(p.completed_at)} for p in Payment.objects.filter(user=user)],
        "historia_salda": [{"kiedy": _dt(le.created_at), "opis": le.description, "kwota_gr": le.amount}
                           for le in LedgerEntry.objects.filter(user=user)],
        "kody_wejścia": [{"kod": cp.code, "rodzaj": cp.get_kind_display(), "od": str(cp.valid_from), "do": str(cp.valid_until)}
                         for cp in ClientPass.objects.filter(user=user)],
        "lista_oczekujących": [{"oś": str(w.lane), "od": _dt(w.starts_at), "status": w.get_status_display()}
                               for w in WaitlistEntry.objects.filter(user=user).select_related("lane")],
        "wydarzenia": [{"wydarzenie": r.event.title, "data": _dt(r.event.starts_at), "status": r.get_status_display(), "informacja": r.note}
                       for r in Registration.objects.filter(user=user).select_related("event")],
        "zgłoszenia_usterek": [{"tytuł": i.title, "opis": i.description, "zgłoszono": _dt(i.created_at), "status": i.get_status_display()}
                               for i in Issue.objects.filter(reported_by=user)],
        "członkostwo": [{"numer": m.number, "status": m.get_status_display(), "od": str(m.joined_on) if m.joined_on else None,
                         "adres": m.address, "e-mail": m.email} for m in Member.objects.filter(user=user)],
        "deklaracje": [{"złożono": _dt(a.submitted_at), "status": a.get_status_display()} for a in MembershipApplication.objects.filter(user=user)],
        "kartoteka_osób": [{"imię": p.first_name, "nazwisko": p.last_name, "telefon": p.phone, "e-mail": p.email, "adres": p.address,
                            "pozwolenie": p.permit_number} for p in Person.objects.filter(user=user)],
        "kto_oglądał_moje_dane": [{"kiedy": _dt(s.at), "kto": s.actor.full_name if s.actor else "system", "co": s.resource, "cel": s.reason}
                                  for s in SensitiveAccessLog.objects.filter(subject=user).select_related("actor")],
    }
    log_event("rodo.eksport", user, f"Eksport danych {user.email}", actor=user)
    return data


def anonymize_blockers(user):
    from apps.association.models import Member
    from apps.bookings.models import Reservation
    from apps.events.models import Registration

    reasons = []
    if user.is_active:
        reasons.append("Konto jest aktywne — najpierw dezaktywacja (przez osobę albo zarząd).")
    if user.is_staff or user.is_superuser:
        reasons.append("Osoba ma uprawnienia w panelu — najpierw je odbierz.")
    if Reservation.objects.filter(user=user, ends_at__gt=timezone.now(), status__in=["potwierdzona", "do_zatwierdzenia", "oczekuje"]).exists():
        reasons.append("Ma przyszłe rezerwacje (także czekające na płatność) — anuluj je.")
    if Reservation.objects.filter(user=user, payment_status="do_zaplaty").exclude(status="anulowana", late_cancellation=False).exists():
        reasons.append("Ma nierozliczone należności za rezerwacje.")
    if Registration.objects.filter(user=user, status__in=["zapisany", "rezerwowy"], event__ends_at__gt=timezone.now()).exists():
        reasons.append("Jest zapisana na nadchodzące wydarzenie.")
    if Member.objects.filter(user=user, status__in=["aktywny", "zawieszony"]).exists():
        reasons.append("Jest aktywnym członkiem stowarzyszenia — najpierw zmiana statusu członkostwa (uchwała).")
    return reasons


@transaction.atomic
def anonymize(user, by):
    """Usuwa dane osobowe z konta. Zostają (bez danych kontaktowych): wpisy książki pobytu (do upływu okresu
    przechowywania), rozliczenia księgowe, ewidencja członków i dziennik zdarzeń."""
    from apps.association.models import Member
    from apps.bookings.models import CalendarFeed, WaitlistEntry
    from apps.clients.models import ClientProfile, Person
    from apps.core.models import Notification, OutgoingEmail, PushSubscription
    from apps.credentials.models import Credential
    from apps.credentials.services import delete_scan
    from apps.facilities.models import Issue
    from django_otp.plugins.otp_totp.models import TOTPDevice

    reasons = anonymize_blockers(user)
    if reasons:
        raise ValidationError(reasons)
    email_before = user.email
    for c in Credential.objects.filter(user=user):
        delete_scan(c)
        c.delete()
    ClientProfile.objects.filter(user=user).delete()
    Person.objects.filter(user=user).delete()
    WaitlistEntry.objects.filter(user=user).delete()
    CalendarFeed.objects.filter(user=user).delete()
    Notification.objects.filter(recipient=user).delete()
    OutgoingEmail.objects.filter(recipients__contains=[email_before]).delete()  # kopie e-maili (skrzynka nadawcza)
    PushSubscription.objects.filter(user=user).delete()
    TOTPDevice.objects.filter(user=user).delete()
    Issue.objects.filter(reported_by=user).update(reported_by=None)
    Member.objects.filter(user=user).update(email="")  # ewidencja członków zostaje, bez danych kontaktowych
    user.first_name = "Usunięte"
    user.last_name = f"konto nr {user.pk}"
    user.email = f"usuniete-{user.pk}-{secrets.token_hex(4)}@anonim.invalid"
    user.phone = user.street = user.postal_code = user.city = ""
    user.qr_secret = secrets.token_hex(16)
    user.set_unusable_password()
    user.anonymized_at = timezone.now()
    user.save()
    user.user_permissions.clear()
    user.groups.clear()
    log_event("rodo.anonimizacja", user, f"Usunięto dane osobowe konta nr {user.pk}", changes={"email_przed": "ukryty"}, actor=by)
    return email_before
