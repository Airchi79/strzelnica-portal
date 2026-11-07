import datetime as dt

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.bookings import services, waitlist
from apps.bookings.models import WaitlistEntry
from apps.facilities.models import Lane

from .conftest import add_credential, approve, make_user

pytestmark = pytest.mark.django_db


def ps(email):
    u = make_user(email)
    add_credential(u)
    approve(u)
    return u


@pytest.fixture
def slot(db):
    lane = Lane.objects.filter(is_active=True).first()
    day = timezone.localdate() + dt.timedelta(days=3)
    while lane.hours_for(day) is None:
        day += dt.timedelta(days=1)
    return lane, day, lane.hours_for(day).opens.hour


def test_full_flow_offer_hold_and_next(slot, django_capture_on_commit_callbacks):
    lane, day, h = slot
    owner_r, a, b, c = ps("o@example.com"), ps("a@example.com"), ps("b@example.com"), ps("c@example.com")
    r = services.create_reservation(owner_r, lane, day, h, h + 2, 1)
    with pytest.raises(ValidationError, match="wolny"):
        waitlist.join(a, lane, day, h + 3, h + 4)  # wolny termin — rezerwuj od razu
    ea = waitlist.join(a, lane, day, h, h + 1)
    eb = waitlist.join(b, lane, day, h, h + 2)
    with pytest.raises(ValidationError, match="Już czekasz"):
        waitlist.join(a, lane, day, h, h + 1)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        services.cancel_reservation(r, owner_r)
    ea.refresh_from_db(); eb.refresh_from_db()
    assert ea.status == WaitlistEntry.Status.OFFERED and ea.hold_until and eb.status == WaitlistEntry.Status.WAITING
    assert any("Zwolnił się termin" in m.subject and m.to == [a.email] for m in mail.outbox)
    # trzymany termin: inni nie zarezerwują, a slot widzą jako zajęty
    with pytest.raises(ValidationError, match="listy oczekujących"):
        services.create_reservation(c, lane, day, h, h + 1, 1)
    states = {s.hour: s.state for s in services.day_slots(lane, day, holds=True, viewer=c)}
    assert states[h] == "zajete" and states[h + 1] == "wolne"
    assert {s.hour: s.state for s in services.day_slots(lane, day, holds=True, viewer=a)}[h] == "wolne"
    # oferta wygasa → kolejna osoba (b: cały zakres h..h+2 wolny)
    with django_capture_on_commit_callbacks(execute=True):
        assert waitlist.process(now=ea.hold_until + dt.timedelta(minutes=1)) == 1
    ea.refresh_from_db(); eb.refresh_from_db()
    assert ea.status == WaitlistEntry.Status.EXPIRED and eb.status == WaitlistEntry.Status.OFFERED
    # b rezerwuje — oczekiwanie zrealizowane
    services.create_reservation(b, lane, day, h, h + 2, 1)
    eb.refresh_from_db()
    assert eb.status == WaitlistEntry.Status.DONE


def test_waitlist_page_join_and_cancel(client, slot):
    lane, day, h = slot
    other = ps("zaj@example.com")
    services.create_reservation(other, lane, day, h, h + 1, 1)
    u = ps("w@example.com")
    client.force_login(u)
    assert client.get(f"/rezerwacje/lista-oczekujacych/?os={lane.pk}&dzien={day}&od={h}").status_code == 200
    client.post("/rezerwacje/lista-oczekujacych/", {"lane": lane.pk, "day": day.isoformat(), "start_hour": h, "end_hour": h + 1, "persons": 2})
    e = WaitlistEntry.objects.get(user=u)
    assert e.persons == 2
    client.post("/rezerwacje/lista-oczekujacych/", {"action": "cancel", "entry": e.pk})
    e.refresh_from_db()
    assert e.status == WaitlistEntry.Status.CANCELLED


def test_staff_can_book_over_hold(slot, owner, django_capture_on_commit_callbacks):
    lane, day, h = slot
    x, a = ps("x2@example.com"), ps("a2@example.com")
    r = services.create_reservation(x, lane, day, h, h + 1, 1)
    waitlist.join(a, lane, day, h, h + 1)
    with django_capture_on_commit_callbacks(execute=True):
        services.cancel_reservation(r, x)
    assert services.create_reservation(x, lane, day, h, h + 1, 1, created_by=owner).pk  # obsługa decyduje sama


def test_onsite_channel_no_lead_limit_but_respects_holds(slot, django_capture_on_commit_callbacks):
    """Rezerwacja na miejscu (panel na strzelnicy): bez limitu czasu przed terminem, ale z terminami listy oczekujących."""
    from apps.bookings.models import BookingSettings

    lane, day, h = slot
    cfg = BookingSettings.load()
    cfg.min_lead_hours = 24 * 30  # online: najpóźniej 30 dni wcześniej — termin za kilka dni jest za blisko
    cfg.save()
    u = ps("miejsce@example.com")
    with pytest.raises(ValidationError, match="najpóźniej"):
        services.create_reservation(u, lane, day, h, h + 1, 1)
    r = services.create_reservation(u, lane, day, h, h + 1, 1, channel=services.CHANNEL_ONSITE)
    assert r.pk
    a = ps("czeka@example.com")
    waitlist.join(a, lane, day, h, h + 1)
    with django_capture_on_commit_callbacks(execute=True):
        services.cancel_reservation(r, u)
    with pytest.raises(ValidationError, match="listy oczekujących"):
        services.create_reservation(ps("inny@example.com"), lane, day, h, h + 1, 1, channel=services.CHANNEL_ONSITE)
    with pytest.raises(ValueError):
        services.create_reservation(u, lane, day, h + 1, h + 2, 1, channel="sms")
