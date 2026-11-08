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
            "capacity": "", "fee_pln": "30", "payment_days": 3, "registration_until": timezone.localtime(start - dt.timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M"),
            "note_label": "", "status": "opublikowane"}
    page = owner_client.post("/panel/wydarzenia/nowe/", data)
    assert page.status_code == 200 and [c.pk for c in page.context["conflicts"]] == [r.pk]
    assert not Event.objects.exists()
    owner_client.post("/panel/wydarzenia/nowe/", {**data, "confirm": "1", "cancel_conflicts": "on"})
    e = Event.objects.get()
    assert e.fee == 3000 and e.block_id
    assert Reservation.objects.get(pk=r.pk).status == Reservation.Status.CANCELLED
    assert AuditLog.objects.filter(action="wydarzenie.zapis").exists()


# --- zwroty przez Tpay ---------------------------------------------------------------------------


def _paid_online(user, reg, surcharge=0, ext="tpay:01EVT"):
    from apps.payments.services import complete_payment

    p = Payment.objects.create(user=user, amount=reg.amount + surcharge, surcharge=surcharge, method=Payment.Method.TPAY,
                               event_registration=reg, external_id=ext, title=reg.label)
    complete_payment(p)
    reg.refresh_from_db()
    assert reg.payment_status == Registration.Payment.PAID and balance_of(user) == 0
    return p


@pytest.fixture
def refunds(monkeypatch):
    from apps.payments import tpay

    calls = []
    monkeypatch.setattr(tpay, "refund_transaction", lambda tid, amount=None: calls.append((tid, amount)) or {"result": "success"})
    return calls


def test_self_withdrawal_refunds_online_payment(owner, refunds, django_capture_on_commit_callbacks):
    e = make_event(owner)
    a = shooter("zwrot@example.com")
    reg = services.register(a, e)
    _paid_online(a, reg, surcharge=150)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        reg = services.cancel_registration(reg, a)
    assert refunds == [("01EVT", 5000)]  # zwrot częściowy: bez opłaty za płatność online
    assert reg.refund_info["refunded"] == 5000 and balance_of(a) == 0
    body = [m for m in mail.outbox if m.to == [a.email]][0].body
    assert "wycofałeś zapis" in body and "Zwrot 50\xa0zł" in body and "nie podlega zwrotowi" in body


def test_staff_withdraw_with_and_without_refund(owner_client, owner, refunds, django_capture_on_commit_callbacks):
    e = make_event(owner, capacity=5)
    a, b = shooter("a1@example.com"), shooter("b1@example.com")
    ra, rb = services.register(a, e), services.register(b, e)
    _paid_online(a, ra, ext="tpay:01A")
    _paid_online(b, rb, ext="tpay:01B")
    page = owner_client.get(f"/panel/wydarzenia/{e.pk}/").content.decode()
    assert "Wycofaj bez zwrotu" in page
    with django_capture_on_commit_callbacks(execute=True):
        owner_client.post(f"/panel/wydarzenia/{e.pk}/", {"action": "withdraw", "reg": ra.pk})
        owner_client.post(f"/panel/wydarzenia/{e.pk}/", {"action": "withdraw_keep", "reg": rb.pk})
    assert refunds == [("01A", None)]
    assert balance_of(a) == 0 and balance_of(b) == 5000  # b: nadpłata do decyzji zarządu


def test_cancel_event_refund_checkbox(owner_client, owner, refunds, django_capture_on_commit_callbacks):
    from apps.payments.services import register_manual_payment

    e = make_event(owner, capacity=5)
    a, c = shooter("a2@example.com"), shooter("c2@example.com")
    ra, rc = services.register(a, e), services.register(c, e)
    _paid_online(a, ra, ext="tpay:01C")
    register_manual_payment(c, 5000, Payment.Method.CASH, owner, registration=rc)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        owner_client.post(f"/panel/wydarzenia/{e.pk}/", {"action": "cancel_event", "reason": "Burza", "refund": "1"})
    e.refresh_from_db()
    assert e.status == Event.Status.CANCELLED and refunds == [("01C", None)]
    assert balance_of(a) == 0 and balance_of(c) == 5000  # gotówka — zwrot ręczny
    assert any("Zwrot do wykonania ręcznie" in m.subject for m in mail.outbox)


def test_cancel_event_without_refund_keeps_overpayment(owner_client, owner, refunds):
    e = make_event(owner)
    a = shooter("a3@example.com")
    ra = services.register(a, e)
    _paid_online(a, ra, ext="tpay:01D")
    owner_client.post(f"/panel/wydarzenia/{e.pk}/", {"action": "cancel_event", "reason": "Burza"})
    assert refunds == [] and balance_of(a) == 5000


# --- termin opłaty startowej: przypomnienie, wycofanie po terminie, miejsce dla listy rezerwowej ------------------------


def test_payment_deadline_set_on_registration_and_capped(owner):
    e = make_event(owner, capacity=5)  # zapisy zamykają się za 9 dni
    reg = services.register(shooter("t1@example.com"), e)
    assert abs((reg.pay_by - timezone.now()) - dt.timedelta(days=3)) < dt.timedelta(minutes=1)
    Event.objects.filter(pk=e.pk).update(registration_until=timezone.now() + dt.timedelta(hours=10))
    e.refresh_from_db()
    assert services.payment_deadline(e, timezone.now()) == e.registration_until  # nie później niż zamknięcie zapisów
    Event.objects.filter(pk=e.pk).update(registration_until=timezone.now() + dt.timedelta(minutes=20))
    e.refresh_from_db()
    now = timezone.now()
    assert services.payment_deadline(e, now) == now + services.MIN_PAYMENT_WINDOW  # co najmniej 2 h
    Event.objects.filter(pk=e.pk).update(payment_days=0)
    e.refresh_from_db()
    assert services.payment_deadline(e, now) is None  # 0 — bez terminu
    free = make_event(owner, fee=0)
    assert services.register(shooter("t2@example.com"), free).pay_by is None


def test_unpaid_registration_expires_and_reserve_gets_place_with_own_deadline(owner, django_capture_on_commit_callbacks):
    e = make_event(owner, capacity=1)
    a, b = shooter("t3@example.com"), shooter("t4@example.com")
    ra, rb = services.register(a, e), services.register(b, e)
    assert rb.status == Registration.Status.RESERVE and rb.pay_by is None
    later = ra.pay_by + dt.timedelta(minutes=1)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert services.expire_unpaid_registrations(now=later) == 1
    ra.refresh_from_db()
    rb.refresh_from_db()
    assert ra.status == Registration.Status.CANCELLED
    assert rb.status == Registration.Status.REGISTERED and rb.pay_by > timezone.now() + dt.timedelta(days=2)
    subjects = {m.to[0]: m.subject for m in mail.outbox}
    assert "Wycofano zapis" in subjects["t3@example.com"] and "Zwolniło się miejsce" in subjects["t4@example.com"]
    assert "nie została wniesiona w terminie" in [m for m in mail.outbox if m.to == ["t3@example.com"]][0].body


def test_paid_or_pending_registration_not_expired(owner):
    e = make_event(owner, capacity=5)
    a, b = shooter("t5@example.com"), shooter("t6@example.com")
    ra, rb = services.register(a, e), services.register(b, e)
    _paid_online(a, ra, ext="tpay:01T5")
    Payment.objects.create(user=b, amount=rb.amount, method=Payment.Method.TPAY, event_registration=rb, title=rb.label)
    Registration.objects.filter(pk__in=[ra.pk, rb.pk]).update(pay_by=timezone.now() - dt.timedelta(minutes=1))  # termin minął
    assert services.expire_unpaid_registrations() == 0  # opłacony + płatność online w toku (ostatnie 30 min)
    assert services.expire_unpaid_registrations(now=timezone.now() + dt.timedelta(minutes=40)) == 1
    ra.refresh_from_db()
    assert ra.status == Registration.Status.REGISTERED


def test_event_payment_reminder_once(owner, django_capture_on_commit_callbacks):
    e = make_event(owner, capacity=5)
    reg = services.register(shooter("t7@example.com"), e)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert services.send_payment_reminders(now=reg.pay_by - dt.timedelta(hours=30)) == 0
        assert services.send_payment_reminders(now=reg.pay_by - dt.timedelta(hours=20)) == 1
        assert services.send_payment_reminders(now=reg.pay_by - dt.timedelta(hours=19)) == 0
    assert [m.subject for m in mail.outbox] == ["[Strzelnica Sątok] Przypomnienie: opłata startowa — Zawody klubowe"]


def test_event_page_shows_payment_deadline(client, owner):
    e = make_event(owner, capacity=5)
    u = shooter("t8@example.com")
    services.register(u, e)
    client.force_login(u)
    page = client.get(f"/wydarzenia/{e.pk}/").content.decode()
    assert "zapłać do" in page and "zapis zostanie wycofany" in page
