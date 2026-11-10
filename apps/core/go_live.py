"""Czyszczenie danych z okresu testów przed uruchomieniem systemu (jednorazowo, przez właściciela).

Usuwa wszystko, co powstało w tym systemie podczas prób: rezerwacje, książkę pobytu (wpisy, korekty, wydruki dzienne),
płatności i salda, raporty wpływów, kiosk (rezerwacje na miejscu, skarbonka), wydarzenia, ogłoszenia, usterki, blokady
osi, karnety, kluby i kody, składki, deklaracje członkowskie, powiadomienia i e-maile, dziennik zdarzeń oraz konta
użytkowników — poza jednym wskazanym kontem właściciela. Numeracja rezerwacji, wpisów i raportów zaczyna się od nowa.

Zostaje: dane zaimportowane z GunTrackera (archiwum książki pobytu i rezerwacji wraz z ich blokadami osi, kartoteka
osób z importu), członkowie, uchwały, zebrania i głosowania, regulaminy i ich wersje, ustawienia, osie, godziny otwarcia,
cennik, kioski (sparowane urządzenia), FAQ, wygląd, konto właściciela z jego uprawnieniami, legitymacją i logowaniem 2FA.

Bezpieczniki: bez `wykonaj` tylko raport; całość w jednej transakcji (błąd = nic nie usunięto); wymagana świeża,
sprawdzona kopia bazy; prawdziwe płatności Tpay (nie testowe) blokują czyszczenie, chyba że jawnie dopuszczone;
po oznaczeniu końca testów (`koniec_testow`) czyszczenia nie da się już uruchomić.
"""

from collections import OrderedDict

from django.apps import apps
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.db.models import Q

END_OF_TESTS = "system.koniec_testow"
PURGE_EVENT = "system.czyszczenie_danych_testowych"

# Tabele z blokadą zmian i usuwania w bazie (trigger) — na czas czyszczenia wyłączane w tej samej transakcji.
GUARDED = [
    ("audit_auditlog", "auditlog_append_only"),
    ("audit_sensitiveaccesslog", "sensitiveaccesslog_append_only"),
    ("payments_ledgerentry", "ledgerentry_append_only"),
    ("reports_salesreport", "sales_report_guard"),
]


def _m(label):
    return apps.get_model(label)


def plan(keep_user):
    """Kolejność usuwania (zależności PROTECT) i zbiory rekordów. Zwraca OrderedDict: opis → queryset."""
    User = get_user_model()
    archived_blocks = _m("bookings.ArchivedReservation").objects.exclude(block=None).values("block")
    steps = OrderedDict([
        ("Kiosk: oświadczenia o wpłacie do skarbonki", _m("kiosk.CashDeclaration").objects.all()),
        ("Kiosk: rozliczenia skarbonki", _m("kiosk.CashBoxSettlement").objects.all()),
        ("Kiosk: rezerwacje na miejscu", _m("kiosk.OnsiteBooking").objects.all()),
        ("Książka pobytu: korekty wpisów", _m("logbook.EntryCorrection").objects.all()),
        ("Książka pobytu: wydruki dzienne", _m("logbook.DailyPrint").objects.all()),
        ("Książka pobytu: wpisy", _m("logbook.Entry").objects.all()),
        ("Książka pobytu: numeracja wpisów", _m("logbook.EntrySequence").objects.all()),
        ("Zwroty Tpay", _m("payments.TpayRefund").objects.all()),
        ("Operacje na saldach", _m("payments.LedgerEntry").objects.all()),
        ("Wpłaty", _m("payments.Payment").objects.all()),
        ("Importy wyciągów bankowych", _m("payments.BankImport").objects.all()),
        ("Salda strzelców", _m("payments.Wallet").objects.all()),
        ("Zapisy na wydarzenia", _m("events.Registration").objects.all()),
        ("Wydarzenia", _m("events.Event").objects.all()),
        ("Lista oczekujących", _m("bookings.WaitlistEntry").objects.all()),
        ("Rezerwacje", _m("bookings.Reservation").objects.all()),
        ("Blokady osi (bez blokad zaimportowanych rezerwacji GunTrackera)", _m("bookings.Block").objects.exclude(pk__in=archived_blocks)),
        ("Numeracja rezerwacji", _m("bookings.NumberSequence").objects.all()),
        ("Karnety", _m("clients.ClientPass").objects.all()),
        ("Kody wejścia", _m("association.AccessCode").objects.all()),
        ("Kluby", _m("association.Club").objects.all()),
        ("Składki członkowskie", _m("association.Fee").objects.all()),
        ("Deklaracje członkowskie", _m("association.MembershipApplication").objects.all()),
        ("Kartoteka: osoby dodane w panelu (import GunTrackera zostaje)", _m("clients.Person").objects.exclude(source="guntracker")),
        ("Ogłoszenia: doręczenia", _m("core.AnnouncementDelivery").objects.all()),
        ("Ogłoszenia", _m("core.Announcement").objects.all()),
        ("Powiadomienia", _m("core.Notification").objects.all()),
        ("Kopie wysłanych e-maili", _m("core.OutgoingEmail").objects.all()),
        ("Zgłoszenia usterek", _m("facilities.Issue").objects.all()),
        ("Raporty wpływów", _m("reports.SalesReport").objects.all()),
        ("Numeracja raportów", _m("reports.ReportSequence").objects.all()),
        ("Konta użytkowników (poza kontem właściciela)", User.objects.exclude(pk=keep_user.pk)),
        ("Dziennik panelu administracyjnego Django", _m("admin.LogEntry").objects.all()),
        ("Rejestr dostępu do danych", _m("audit.SensitiveAccessLog").objects.all()),
        ("Dziennik zdarzeń", _m("audit.AuditLog").objects.all()),
    ])
    return steps


def real_tpay_payments():
    """Płatności Tpay poza środowiskiem testowym (prawdziwe pieniądze) — dokumentacja księgowa."""
    Payment = _m("payments.Payment")
    return [p for p in Payment.objects.filter(method=Payment.Method.TPAY).exclude(status=Payment.Status.PENDING) if not p.is_test]


def check(keep_user, allow_real_tpay=False, backup_state=None):
    """Warunki uruchomienia. Zwraca listę błędów (pusta = można czyścić)."""
    from apps.audit.models import AuditLog

    errors = []
    if AuditLog.objects.filter(action=END_OF_TESTS).exists():
        errors.append("Koniec testów został już oznaczony — czyszczenie danych jest zablokowane na stałe.")
    if not (keep_user.is_active and keep_user.is_superuser):
        errors.append(f"Konto {keep_user.email} nie jest aktywnym kontem właściciela (superużytkownik).")
    if backup_state is not None and backup_state != "ok":
        errors.append("Brak świeżej, sprawdzonej kopii zapasowej bazy (panel → Przegląd). Najpierw wykonaj kopię.")
    real = real_tpay_payments()
    if real and not allow_real_tpay:
        errors.append(f"W systemie są prawdziwe płatności Tpay ({len(real)}): "
                      + ", ".join(f"{p.amount / 100:.2f} zł z {p.created_at:%d.%m.%Y}" for p in real[:10])
                      + ". To dokumentacja księgowa — usuń je tylko świadomie (opcja dopuszczająca prawdziwe płatności).")
    Resolution = _m("association.Resolution")
    linked = Resolution.objects.filter(target_applications__isnull=False).distinct()
    if linked.exists():
        errors.append("Uchwały powiązane z deklaracjami członkowskimi z systemu: "
                      + ", ".join(str(r) for r in linked[:10])
                      + ". Uchwały zostają, a deklaracje miały być usunięte — wyjaśnij to przed czyszczeniem.")
    return errors


def counts(keep_user):
    return [(label, qs.count()) for label, qs in plan(keep_user).items()]


def run(keep_user, by=None, end_of_tests=False, allow_real_tpay=False, backup_state=None):
    """Usuwa dane testowe. Zwraca listę (opis, liczba). Wyjątek ValidationError — nic nie zostało usunięte."""
    from apps.audit.services import log_event

    errors = check(keep_user, allow_real_tpay, backup_state)
    if errors:
        raise ValidationError(errors)
    User = get_user_model()
    files = []
    result = []
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT set_config('satok.logbook_purge', 'on', true)")
            # ALTER TABLE wymaga braku zaległych (odroczonych) sprawdzeń kluczy obcych w tej transakcji.
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for table, trigger in GUARDED:
                cur.execute(f'ALTER TABLE "{table}" DISABLE TRIGGER "{trigger}"')
        others = User.objects.exclude(pk=keep_user.pk)
        # Dane, które zostają, a wskazują usuwane konta: członek bez konta, uchwała bez autora.
        _m("association.Member").objects.filter(user__in=others).update(user=None)
        _m("association.Resolution").objects.filter(created_by__in=others).update(created_by=None)
        # Pliki na dysku (zdjęcia legitymacji, usterek) — usuwane po zatwierdzeniu transakcji.
        Credential = _m("credentials.Credential")
        files += [c.scan for c in Credential.objects.filter(user__in=others).exclude(scan="")]
        files += [i.photo for i in _m("facilities.Issue").objects.exclude(photo="")]
        for label, qs in plan(keep_user).items():
            n = qs.count()
            qs.delete()
            result.append((label, n))
        with connection.cursor() as cur:
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for table, trigger in GUARDED:
                cur.execute(f'ALTER TABLE "{table}" ENABLE TRIGGER "{trigger}"')
            cur.execute("SET CONSTRAINTS ALL DEFERRED")
        removed = sum(n for _, n in result)
        log_event(PURGE_EVENT, summary=f"Usunięto dane z okresu testów: {removed} rekordów",
                  changes={label: n for label, n in result if n}, actor=by or keep_user)
        if end_of_tests:
            log_event(END_OF_TESTS, summary="Koniec testów — system uruchomiony do użytku; czyszczenie danych zablokowane",
                      actor=by or keep_user)

        def remove_files():
            for f in files:
                try:
                    f.storage.delete(f.name)
                except OSError:
                    pass

        transaction.on_commit(remove_files)
    return result
