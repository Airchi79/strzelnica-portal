"""Prowadzący od strzelnicy: rezerwacja osoby bez uprawnień PS (opcja w ustawieniach, domyślnie wyłączona)."""

from datetime import timedelta

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.bookings import services
from apps.bookings.models import BookingSettings, Reservation
from apps.facilities.models import Lane

from .conftest import add_credential, approve, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def lane(db):
    return Lane.objects.get(name="Oś 100 m")


@pytest.fixture
def guest(db):
    return approve(make_user("bezps@example.com"))  # bez uprawnień PS (zatwierdzony klient — sama oś nie wymagałaby akceptacji)


@pytest.fixture
def officer(db):
    u = make_user("prowadzacy@example.com")
    add_credential(u)
    return u


def _enable(fee=3000):
    cfg = BookingSettings.load()
    cfg.officer_service, cfg.officer_fee_hourly = True, fee
    cfg.save()


def _day():
    return timezone.localdate() + timedelta(days=7)


def test_disabled_by_default_requires_ps(guest, lane):
    assert not BookingSettings.load().officer_service
    with pytest.raises(ValidationError, match="prowadzącego strzelanie"):
        services.create_reservation(guest, lane, _day(), 10, 12, 2)
    steps = {s["key"]: s for s in services.booking_readiness(guest)["steps"]}
    assert steps["ps"]["state"] == "missing"


def test_booking_with_officer_flow(guest, officer, lane, owner, django_capture_on_commit_callbacks):
    _enable()
    steps = {s["key"]: s for s in services.booking_readiness(guest)["steps"]}
    assert steps["ps"]["state"] == "tip" and "30,00 zł / h" in steps["ps"]["detail"]
    with pytest.raises(ValidationError, match="bez kodu"):
        services.quote(lane, _day(), 10, 12, 2, access_code="WXXXXXXX", user=guest)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        r = services.create_reservation(guest, lane, _day(), 10, 12, 2)
    base = r.hourly_amount * 2
    assert r.with_officer and r.officer_hourly == 3000 and r.amount == base + 6000
    assert r.status == Reservation.Status.PENDING_APPROVAL  # zawsze do zatwierdzenia
    assert "obsługa przydzieli prowadzącego" in [m for m in mail.outbox if m.to == [guest.email]][0].body
    with pytest.raises(ValidationError, match="przydziel prowadzącego"):
        services.approve_reservation(r, owner)
    with pytest.raises(ValidationError, match="uprawnienia PS"):
        services.assign_officer(r, guest, owner)
    services.assign_officer(r, officer, owner)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        r = services.approve_reservation(r, owner)
    assert r.status == Reservation.Status.CONFIRMED and r.range_officer_user == officer
    assert "Prowadzący od strzelnicy: " + officer.full_name in [m for m in mail.outbox if m.to == [guest.email]][0].body
    # zmiana liczby osób zachowuje dopłatę za prowadzącego
    r = services.change_persons(r, 1, owner)
    assert r.amount == r.hourly_amount * 2 + 6000


def test_ps_user_books_normally_when_enabled(lane):
    _enable()
    ps = approve(make_user("ps2@example.com"))
    add_credential(ps)
    r = services.create_reservation(ps, lane, _day(), 10, 11, 1)
    assert not r.with_officer and r.officer_hourly == 0 and r.status == Reservation.Status.CONFIRMED


def test_panel_assign_officer_and_logbook(owner_client, guest, officer, lane):
    _enable(fee=0)
    r = services.create_reservation(guest, lane, _day(), 10, 12, 2)
    page = owner_client.get(f"/panel/rezerwacje/{r.pk}/").content.decode()
    assert "Prowadzący od strzelnicy" in page and "do przydzielenia" in page and officer.full_name in page
    options = page.split("wybierz osobę ze zweryfikowanym PS")[1].split("</select>")[0]
    assert f'value="{officer.pk}"' in options and f'value="{guest.pk}"' not in options  # rezerwujący (bez PS) nie na liście
    owner_client.post(f"/panel/rezerwacje/{r.pk}/", {"action": "assign_officer", "officer": guest.pk})  # bez PS — odrzucone
    r.refresh_from_db()
    assert r.officer_id is None
    owner_client.post(f"/panel/rezerwacje/{r.pk}/", {"action": "assign_officer", "officer": officer.pk})
    owner_client.post(f"/panel/rezerwacje/{r.pk}/", {"action": "approve"})
    r.refresh_from_db()
    assert r.officer_id == officer.pk and r.status == Reservation.Status.CONFIRMED and r.amount == r.hourly_amount * 2


def test_settings_form_and_public_page(owner_client, client):
    response = owner_client.post("/panel/rezerwacje/ustawienia/", {
        **{f: getattr(BookingSettings.load(), f) for f in ["free_cancel_hours", "online_payment_minutes", "approval_payment_hours",
                                                           "hold_minutes", "transfer_days", "transfer_cutoff_hours",
                                                           "offline_instructions", "min_lead_hours", "waitlist_hold_minutes",
                                                           "max_advance_days", "max_persons"]},
        "prepay_before_approval": "on", "officer_service": "on", "officer_fee_pln": "25,50",
        **{f"{g}-{k}": "on" for g in ("czlonek", "powracajacy", "nowy") for k in ("pay_online",)},
    })
    assert response.status_code == 302, response.content.decode()[:3000]
    cfg = BookingSettings.load()
    assert cfg.officer_service and cfg.officer_fee_hourly == 2550
    assert "prowadzącym zapewnionym przez strzelnicę" in client.get("/osie/").content.decode()
