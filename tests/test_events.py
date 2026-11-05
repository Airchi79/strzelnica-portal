import datetime as dt

import pytest
from django.core import mail
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.bookings.models import Block, Reservation
from apps.events import services
from apps.events.models import Event, Registration
from apps.facilities.models import Lane
from apps.payments.models import LedgerEntry, Payment
from apps.payments.services import balance_of

from .conftest import add_credential, approve, make_user

pytestmark = pytest.mark.django_db


def make_event(owner, capacity=2, fee=5000, lanes=(), **kw):
    start = timezone.now().replace(minute=0, second=0, microsecond=0) + dt.timedelta(days=10)
    e = Event(title="Zawody klubowe", starts_at=start, ends_at=start + dt.timedelta(hours=6), capacity=capacity, fee=fee,
              registration_until=start - dt.timedelta(days=1), status=Event.Status.PUBLISHED, **kw)
    services.save_event(e, list(lanes), owner)
    return e


def shooter(email):
    u = make_user(email)
    approve(u)
    return u


def test_capacity_reserve_list_and_promotion(owner, django_capture_on_commit_callbacks):
    e = make_event(owner, capacity=2)
    a, b, c = shooter("a@example.com"), shooter("b@example.com"), shooter("c@example.com")
    with django_capture_on_commit_callbacks(execute=True):
        ra, rb, rc = services.register(a, e), services.register(b, e), services.register(c, e)
    assert [ra.status, rb.status, rc.status] == ["zapisany", "zapisany", "rezerwowy"]
    assert ra.payment_status == Registration.Payment.DUE and ra.amount == 5000
    with pytest.raises(Exception):
        services.register(a, e)  # drugi raz
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        services.cancel_registration(ra, a)
    rc.refresh_from_db()
    assert rc.status == Registration.Status.REGISTERED
    assert any("Zwolniło się miejsce" in m.subject and m.to == [c.email] for m in mail.outbox)


def test_cash_payment_refund_and_sales_report(owner_client, owner):
    from apps.payments.services import register_manual_payment
    from apps.reports.services import collect

    e = make_event(owner)
    a = shooter("p@example.com")
    reg = services.register(a, e)
    owner_client.post(f"/panel/wydarzenia/{e.pk}/", {"action": "cash", "reg": reg.pk})
    reg.refresh_from_db()
    assert reg.payment_status == Registration.Payment.PAID and reg.paid_amount == 5000
    p = Payment.objects.get(event_registration=reg)
    assert p.method == "gotowka" and LedgerEntry.objects.filter(kind=LedgerEntry.Kind.EVENT_FEE).exists()
    rows = collect(timezone.localdate(), timezone.localdate())
    assert any(r["category"] == "wydarzenie" and r["amount"] == 5000 for r in rows)
    services.cancel_registration(reg, owner, by_staff=True, reason="kontuzja")
    assert balance_of(a) == 5000  # wpłata wraca jako nadpłata


def test_event_blocks_lanes_and_cancel_event_removes_block(owner, django_capture_on_commit_callbacks):
    lane = Lane.objects.filter(is_active=True).first()
    e = make_event(owner, lanes=[lane])
    e.refresh_from_db()
    assert e.block and list(e.block.lanes.all()) == [lane]
    a = shooter("x@example.com")
    services.register(a, e)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        services.cancel_event(e, owner, "Pogoda")
    e.refresh_from_db()
    assert e.status == Event.Status.CANCELLED and e.block is None and not Block.objects.exists()
    assert Registration.objects.filter(status=Registration.Status.CANCELLED).count() == 1
    assert any("Odwołano" in m.subject and m.to == [a.email] for m in mail.outbox)


def test_public_pages_and_registration_flow(client, owner):
    e = make_event(owner, capacity=None, fee=0, note_label="Klasa")
    draft = Event.objects.create(title="Szkic", starts_at=e.starts_at, ends_at=e.ends_at, registration_until=e.registration_until)
    html = client.get("/wydarzenia/").content.decode()
    assert "Zawody klubowe" in html and "Szkic" not in html
    assert client.get(f"/wydarzenia/{draft.pk}/").status_code == 404
    u = shooter("pub@example.com")
    client.force_login(u)
    response = client.post(f"/wydarzenia/{e.pk}/", {"action": "register"})  # brak wymaganej klasy
    assert response.status_code == 200 and not Registration.objects.exists()
    client.post(f"/wydarzenia/{e.pk}/", {"action": "register", "note": "pistolet sportowy"})
    reg = Registration.objects.get()
    assert reg.note == "pistolet sportowy" and reg.payment_status == Registration.Payment.FREE
    assert "Jesteś zapisany" in client.get(f"/wydarzenia/{e.pk}/").content.decode()
    client.post(f"/wydarzenia/{e.pk}/", {"action": "cancel"})
    assert Registration.objects.get().status == Registration.Status.CANCELLED


def test_registration_closed_after_deadline(owner):
    e = make_event(owner)
    e.registration_until = timezone.now() - dt.timedelta(minutes=1)
    e.save()
    with pytest.raises(Exception):
        services.register(shooter("late@example.com"), e)


def test_panel_edit_shows_lane_conflicts(owner_client, owner):
    from apps.bookings import services as booking_services

    lane = Lane.objects.filter(is_active=True).first()
    ps = make_user("ps-ev@example.com")
    add_credential(ps)
    approve(ps)
    day = timezone.localdate() + dt.timedelta(days=12)
    while lane.hours_for(day) is None:
        day += dt.timedelta(days=1)
    h = lane.hours_for(day).opens.hour
    r = booking_services.create_reservation(ps, lane, day, h, h + 1, 1)
    start = booking_services.local_dt(day, h)
    data = {"title": "Zawody", "description": "", "starts_at": timezone.localtime(start).strftime("%Y-%m-%dT%H:%M"),
            "ends_at": timezone.localtime(start + dt.timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M"), "lanes": [lane.pk],
            "capacity": "", "fee_pln": "30", "registration_until": timezone.localtime(start - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            "note_label": "", "status": "opublikowane"}
    page = owner_client.post("/panel/wydarzenia/nowe/", data)
    assert page.status_code == 200 and [c.pk for c in page.context["conflicts"]] == [r.pk]
    assert not Event.objects.exists()
    owner_client.post("/panel/wydarzenia/nowe/", {**data, "confirm": "1", "cancel_conflicts": "on"})
    e = Event.objects.get()
    assert e.fee == 3000 and e.block_id
    assert Reservation.objects.get(pk=r.pk).status == Reservation.Status.CANCELLED
    assert AuditLog.objects.filter(action="wydarzenie.zapis").exists()
