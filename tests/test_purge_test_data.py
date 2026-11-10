"""Czyszczenie danych z okresu testów (polecenie purge_test_data): co znika, co zostaje, bezpieczniki."""

import io
from datetime import date, timedelta

import pytest
from django.core.management import CommandError, call_command
from django.utils import timezone

from apps.accounts.models import User
from apps.association.models import AccessCode, Club, Fee, Member
from apps.audit.models import AuditLog, SensitiveAccessLog
from apps.audit.services import log_event, log_sensitive_access
from apps.bookings import services as bookings
from apps.bookings.models import ArchivedReservation, Block, Reservation
from apps.clients.models import Person
from apps.core import go_live
from apps.core.models import Announcement, Notification, OutgoingEmail
from apps.credentials.models import Credential
from apps.events.models import Event
from apps.facilities.models import Issue, Lane
from apps.kiosk import cashbox, onsite, services as kiosk_services
from apps.kiosk.models import CashDeclaration, OnsiteBooking
from apps.logbook import daily_print, services as logbook
from apps.logbook.models import ArchivedEntry, DailyPrint, Entry, EntryCorrection
from apps.payments import services as payments
from apps.payments.models import LedgerEntry, Payment, Wallet
from apps.regulations.models import RegulationVersion
from apps.reports import services as reports
from apps.reports.models import SalesReport

from .conftest import add_credential, approve, make_user
from .test_logbook import permit_data, signature_data_url

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(owner):
    """Dane jak po kilku tygodniach prób: rezerwacje, kiosk, skarbonka, książka, płatności, raport, wydarzenia itd."""
    lane = Lane.objects.get(name="Oś 100 m")
    add_credential(owner, number="PS-WLASCICIEL")
    shooter = make_user("strzelec@example.com", first_name="Piotr", last_name="Testowy", phone="600 700 800")
    add_credential(shooter, number="PS 777")
    approve(shooter)
    member_owner = Member.objects.create(user=owner, first_name="Adam", last_name="Właściciel", number=1, joined_on=date(2024, 1, 1))
    member_shooter = Member.objects.create(user=shooter, first_name="Piotr", last_name="Testowy", number=2, joined_on=date(2024, 1, 1))
    Fee.objects.create(member=member_shooter, kind="skladka", year=2026, amount=20000, due_on=date(2026, 1, 31))
    # kiosk + skarbonka + rozliczenie
    device = kiosk_services.create_device("Kiosk", owner)[0]
    now = bookings.local_dt(timezone.localdate() + timedelta(days=7), 10) + timedelta(minutes=20)
    flow, _ = onsite.start_qr(device, logbook.personal_token(shooter))
    onsite.accept_statement(flow)
    onsite.book(flow, lane.pk, 10, 11, 1, now=now)
    flow.refresh_from_db()
    d = cashbox.declare(flow, True, now=now)
    cashbox.settle(d.amount, [d.pk], owner)
    # książka pobytu z korektą i wydrukiem dziennym
    day = timezone.localdate() - timedelta(days=2)
    start, _ = daily_print.day_bounds(day)
    e = logbook.create_entry(data={**permit_data(), "purpose": "obserwator", "has_permit": True},
                             signature_png=logbook.decode_signature(signature_data_url()), source="qr_wejscia", now=start + timedelta(hours=10),
                             user=shooter)
    logbook.register_exit(e, now=start + timedelta(hours=11))
    logbook.correct_entry(e, {"last_name": "Nowakowska"}, "literówka w nazwisku", owner)
    daily_print.run()
    # płatność ręczna, saldo, raport wpływów
    p = payments.register_manual_payment(shooter, 5000, "gotowka", owner)
    Payment.objects.filter(pk=p.pk).update(completed_at=timezone.now() - timedelta(days=2))
    reports.generate_missing()
    # pozostałe moduły
    club = Club.objects.create(name="Klub Testowy")
    AccessCode.objects.create(club=club, valid_from=timezone.localdate(), valid_until=timezone.localdate() + timedelta(days=60))
    Event.objects.create(title="Zawody testowe", starts_at=now, ends_at=now + timedelta(hours=3), registration_until=now - timedelta(days=1))
    Announcement.objects.create(title="Testowe ogłoszenie", visible_from=timezone.now())
    Issue.objects.create(title="Testowa usterka", reported_by=shooter)
    Block.objects.create(title="Testowa blokada", starts_at=now + timedelta(days=1), ends_at=now + timedelta(days=1, hours=2), created_by=owner)
    Person.objects.create(first_name="Ola", last_name="Panelowa", permit_number="AB1", permit_issuer="KWP", source="panel")
    # dane zaimportowane z GunTrackera — zostają
    Person.objects.create(first_name="Ewa", last_name="Zimportowana", permit_number="CD2", permit_issuer="KWP", source="guntracker")
    archived_block = Block.objects.create(title="GunTracker: rezerwacja", starts_at=now + timedelta(days=3),
                                          ends_at=now + timedelta(days=3, hours=2))
    ArchivedReservation.objects.create(number="GT1", starts_at=archived_block.starts_at, ends_at=archived_block.ends_at,
                                       row_hash="r1", block=archived_block)
    ArchivedEntry.objects.create(row_hash="a1", started_at=timezone.now() - timedelta(days=400), first_name="Jan", last_name="Archiwalny")
    log_event("test.zdarzenie", summary="próba")
    log_sensitive_access(shooter, "test", "próba")
    return {"owner": owner, "shooter": shooter, "member_owner": member_owner, "member_shooter": member_shooter,
            "archived_block": archived_block}


def test_report_only_by_default(world):
    out = io.StringIO()
    call_command("purge_test_data", "--zostaw", world["owner"].email, "--bez-kopii", stdout=out)
    text = out.getvalue()
    assert "nic nie usunięto" in text and "Rezerwacje" in text
    assert Reservation.objects.exists() and Entry.objects.exists() and User.objects.count() == 2


def test_purge_removes_test_data_and_keeps_imports_and_owner(world):
    owner = world["owner"]
    assert Reservation.objects.exists() and Payment.objects.exists() and SalesReport.objects.exists() and DailyPrint.objects.exists()
    regs = RegulationVersion.objects.count()
    out = io.StringIO()
    call_command("purge_test_data", "--zostaw", owner.email, "--wykonaj", "--bez-kopii", stdout=out)
    assert "Usunięto rekordów" in out.getvalue()
    for model in (Reservation, Entry, EntryCorrection, DailyPrint, Payment, LedgerEntry, Wallet, SalesReport, OnsiteBooking,
                  CashDeclaration, Event, Announcement, Issue, Club, AccessCode, Fee, Notification, OutgoingEmail, SensitiveAccessLog):
        assert not model.objects.exists(), model
    # konta: tylko właściciel (bez danych strzelca); członkowie zostają (bez powiązania z usuniętym kontem)
    assert list(User.objects.all()) == [owner]
    assert not Credential.objects.exists()  # legitymacja właściciela też — konto panelu nie jest kontem strzelca
    world["member_shooter"].refresh_from_db()
    assert world["member_shooter"].user is None and Member.objects.get(pk=world["member_owner"].pk).user == owner
    # import GunTrackera i jego blokady zostają; kartoteka z panelu znika
    assert ArchivedReservation.objects.count() == 1 and ArchivedEntry.objects.count() == 1
    assert list(Block.objects.all()) == [world["archived_block"]]
    assert list(Person.objects.values_list("last_name", flat=True)) == ["Zimportowana"]
    assert RegulationVersion.objects.count() == regs
    # dziennik: tylko zapis o czyszczeniu
    assert list(AuditLog.objects.values_list("action", flat=True)) == [go_live.PURGE_EVENT]
    # numeracja od nowa, a blokady w bazie znów działają
    lane = Lane.objects.get(name="Oś 100 m")
    newcomer = make_user("nowy@example.com")  # pierwszy prawdziwy strzelec po uruchomieniu
    add_credential(newcomer, number="PS 1")
    approve(newcomer)
    r = bookings.create_reservation(newcomer, lane, timezone.localdate() + timedelta(days=10), 10, 11, 1)
    assert r.number.endswith("-0001")
    from django.db import DatabaseError, transaction

    with pytest.raises(DatabaseError), transaction.atomic():
        AuditLog.objects.all().delete()


def test_end_of_tests_blocks_further_purges(world):
    owner = world["owner"]
    call_command("purge_test_data", "--zostaw", owner.email, "--wykonaj", "--koniec-testow", "--bez-kopii", stdout=io.StringIO())
    assert AuditLog.objects.filter(action=go_live.END_OF_TESTS).exists()
    with pytest.raises(CommandError, match="zablokowane"):
        call_command("purge_test_data", "--zostaw", owner.email, "--wykonaj", "--bez-kopii", stdout=io.StringIO())


def test_refuses_without_backup_non_owner_and_real_tpay(world):
    owner, shooter = world["owner"], world["shooter"]
    with pytest.raises(CommandError, match="kopii"):  # w testach brak pliku stanu kopii
        call_command("purge_test_data", "--zostaw", owner.email, "--wykonaj", stdout=io.StringIO())
    with pytest.raises(CommandError, match="superużytkownik"):
        call_command("purge_test_data", "--zostaw", shooter.email, "--wykonaj", "--bez-kopii", stdout=io.StringIO())
    # nieudana / anulowana próba poza sandboxem — bez pieniędzy, nie blokuje
    Payment.objects.create(user=shooter, amount=3500, method=Payment.Method.TPAY, status=Payment.Status.FAILED, raw={"sandbox": False})
    assert go_live.real_tpay_payments() == []
    Payment.objects.create(user=shooter, amount=1000, method=Payment.Method.TPAY, status=Payment.Status.COMPLETED,
                           raw={"powiadomienie": {"test_mode": "0"}}, completed_at=timezone.now())
    with pytest.raises(CommandError, match="prawdziwe płatności Tpay"):
        call_command("purge_test_data", "--zostaw", owner.email, "--wykonaj", "--bez-kopii", stdout=io.StringIO())
    assert Reservation.objects.exists() and User.objects.count() == 2  # nic nie usunięto
    call_command("purge_test_data", "--zostaw", owner.email, "--wykonaj", "--bez-kopii", "--z-prawdziwymi-platnosciami",
                 stdout=io.StringIO())
    assert not Payment.objects.exists()
