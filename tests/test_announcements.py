import datetime as dt

import pytest
from django.core import mail
from django.core.management import call_command
from django.utils import timezone

from apps.core import announcements
from apps.core.models import Announcement

from .conftest import add_credential, approve, make_user

pytestmark = pytest.mark.django_db


def test_banner_visible_only_in_window(client):
    now = timezone.now()
    Announcement.objects.create(title="Strzelnica zamknięta w sobotę", level="zamkniete", visible_from=now - dt.timedelta(hours=1))
    Announcement.objects.create(title="Stare", visible_from=now - dt.timedelta(days=5), visible_until=now - dt.timedelta(days=1))
    Announcement.objects.create(title="Przyszłe", visible_from=now + dt.timedelta(days=1))
    html = client.get("/").content.decode()
    assert "Strzelnica zamknięta w sobotę" in html and "Stare" not in html and "Przyszłe" not in html


def test_create_with_email_requires_confirmation_and_sends_in_batches(owner_client, monkeypatch):
    for i in range(3):
        make_user(f"u{i}@example.com")
    make_user("niepotwierdzony@example.com", verified=False)
    data = {"title": "Nowy regulamin", "body": "Prosimy o akceptację.", "level": "info",
            "visible_from": timezone.localtime().strftime("%Y-%m-%dT%H:%M"), "visible_until": "", "email_audience": "wszyscy",
            "booked_from": "", "booked_to": ""}
    page = owner_client.post("/panel/ogloszenia/nowe/", data)
    assert page.status_code == 200 and page.context["preview_count"] >= 3  # + właściciel
    assert not Announcement.objects.exists()
    owner_client.post("/panel/ogloszenia/nowe/", {**data, "action": "confirm_email"})
    a = Announcement.objects.get()
    assert a.email_queued and not mail.outbox
    monkeypatch.setattr(announcements, "BATCH", 2)
    call_command("send_announcements")
    assert len(mail.outbox) == 2 and Announcement.objects.get().email_queued
    call_command("send_announcements")
    call_command("send_announcements")
    a.refresh_from_db()
    assert not a.email_queued and a.emailed_at and a.emailed_count == len(mail.outbox)
    assert not any("niepotwierdzony" in m.to[0] for m in mail.outbox)
    assert len({m.to[0] for m in mail.outbox}) == len(mail.outbox)  # nikt dwa razy
    call_command("send_announcements")
    assert a.emailed_count == len(mail.outbox)


def test_booked_audience_only_people_with_reservations(owner_client):
    from apps.bookings import services as booking_services
    from apps.facilities.models import Lane

    ps = make_user("ps-ogl@example.com")
    add_credential(ps)
    approve(ps)
    make_user("bez-rez@example.com")
    lane = Lane.objects.filter(is_active=True).first()
    day = timezone.localdate() + dt.timedelta(days=3)
    while lane.hours_for(day) is None:
        day += dt.timedelta(days=1)
    booking_services.create_reservation(ps, lane, day, lane.hours_for(day).opens.hour, lane.hours_for(day).opens.hour + 1, 1)
    a = Announcement(title="Zamknięte", visible_from=timezone.now(), email_audience="rezerwujacy", booked_from=day, booked_to=day)
    assert [u.email for u in announcements.recipients(a)] == [ps.email]


def test_panel_requires_permission(client):
    from .conftest import grant, login_with_otp

    staff = grant(make_user("x@example.com"), "bookings.manage_reservations")
    login_with_otp(client, staff)
    assert client.get("/panel/ogloszenia/").status_code == 403
