import base64
import json
import re
from datetime import timedelta
from io import BytesIO

import pytest
from django.core.exceptions import ValidationError
from django.test import Client
from django.utils import timezone
from PIL import Image

from apps.accounts.models import User
from apps.bookings import services as bookings
from apps.bookings.models import Reservation
from apps.core.models import OutgoingEmail
from apps.credentials import services as credential_services
from apps.credentials.models import Credential
from apps.facilities.models import Lane
from apps.kiosk import onsite, services
from apps.kiosk.models import OnsiteBooking

from .conftest import add_credential, approve, make_user

pytestmark = pytest.mark.django_db
H = {"HTTP_X_KIOSK": "1"}


def photo_data_url(size=(1000, 640)):
    img = Image.new("RGB", size, "white")
    out = BytesIO()
    img.save(out, "JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


@pytest.fixture
def device(owner):
    d, _ = services.create_device("Tablet przy wejściu", owner)
    return d


@pytest.fixture
def lane(db):
    return Lane.objects.get(name="Oś 100 m")


@pytest.fixture
def now():
    """10:20 za tydzień (ten sam dzień tygodnia — oś czynna); termin w przyszłości, więc zatwierdzenie jest możliwe."""
    return bookings.local_dt(timezone.localdate() + timedelta(days=7), 10) + timedelta(minutes=20)


def last_code(email):
    msg = OutgoingEmail.objects.filter(recipients__contains=[email.lower()]).order_by("-pk").first()
    return re.search(r"\b(\d{6})\b", msg.body).group(1)


def verified_flow(device, email="nowy@example.com"):
    flow, secret = onsite.start(device, email, "Nowak", "Jan", "600 700 800")  # celowo: imię/nazwisko jak wpisze osoba
    flow = onsite.verify_code(flow, last_code(email))
    onsite.accept_statement(flow)
    return flow


def test_new_person_full_flow_waits_for_ps_and_awaits_payment_after_review(device, lane, owner, now):
    flow = verified_flow(device)
    user = User.objects.get(email="nowy@example.com")
    assert flow.user == user and user.email_verified and not user.has_usable_password() and user.phone == "600 700 800"
    with pytest.raises(ValidationError, match="legitymacji"):
        onsite.book(flow, lane.pk, 10, 12, 2, now=now)
    credential = onsite.submit_ps(flow, "ps 123", "PZSS", "2020-05-01", "", photo_data_url())
    assert credential.status == Credential.Status.PENDING and credential.scan and credential.number == "PS 123"
    r = onsite.book(flow, lane.pk, 10, 12, 2, now=now)
    flow.refresh_from_db()
    assert r.status == Reservation.Status.PENDING_APPROVAL and not r.with_officer and r.officer_hourly == 0
    assert flow.status == OnsiteBooking.Status.AWAITING and flow.awaiting_until == now + timedelta(minutes=onsite.VERIFY_MINUTES)
    assert onsite.state(flow, now=now)["seconds_left"] == onsite.VERIFY_MINUTES * 60
    credential_services.review(credential, owner, approve=True)
    r.refresh_from_db()
    flow.refresh_from_db()
    credential.refresh_from_db()
    # po weryfikacji: termin trzymany do płatności (etap 2), przebieg w kiosku zakończony rezerwacją
    assert r.status == Reservation.Status.HOLD and flow.status == OnsiteBooking.Status.BOOKED
    assert not credential.scan  # zdjęcie usunięte po weryfikacji
    with pytest.raises(ValidationError, match="już złożona"):
        onsite.book(flow, lane.pk, 12, 13, 1, now=now)


def test_rejected_ps_cancels_reservation(device, lane, owner, now):
    flow = verified_flow(device)
    credential = onsite.submit_ps(flow, "PS-9", "PZSS", "2020-05-01", "", photo_data_url())
    r = onsite.book(flow, lane.pk, 10, 11, 1, now=now)
    credential_services.review(credential, owner, approve=False, note="nieczytelne")
    r.refresh_from_db()
    flow.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED and flow.status == OnsiteBooking.Status.REJECTED


def test_no_review_within_15_minutes_cancels_without_fee(device, lane, now):
    flow = verified_flow(device)
    onsite.submit_ps(flow, "PS-9", "PZSS", "2020-05-01", "", photo_data_url())
    r = onsite.book(flow, lane.pk, 10, 11, 1, now=now)
    assert onsite.expire_pending(now=now + timedelta(minutes=14)) == 0
    assert onsite.expire_pending(now=now + timedelta(minutes=16)) == 1
    r.refresh_from_db()
    flow.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED and r.payment_status == Reservation.Payment.NONE
    assert flow.status == OnsiteBooking.Status.EXPIRED and "15 minut" in r.cancel_reason


def test_staff_approving_reservation_in_panel_also_ends_waiting(device, lane, owner, now):
    flow = verified_flow(device)
    onsite.submit_ps(flow, "PS-9", "PZSS", "2020-05-01", "", photo_data_url())
    r = onsite.book(flow, lane.pk, 10, 11, 1, now=now)
    bookings.approve_reservation(r, by=owner)  # z widoku rezerwacji — zasady kiosku: termin trzymany do płatności
    assert onsite.expire_pending(now=now + timedelta(minutes=30)) == 0
    flow.refresh_from_db()
    r.refresh_from_db()
    assert flow.status == OnsiteBooking.Status.BOOKED and r.status == Reservation.Status.HOLD


def test_existing_verified_range_officer_books_immediately_for_payment(device, lane, now):
    ps = make_user("ps@example.com", first_name="Piotr", last_name="Prowadzący")
    add_credential(ps)
    approve(ps)
    flow = verified_flow(device, email="PS@example.com")
    assert flow.user == ps  # istniejące konto — bez nowego
    with pytest.raises(ValidationError, match="zweryfikowane"):
        onsite.submit_ps(flow, "PS-2", "PZSS", "2020-05-01", "", photo_data_url())
    r = onsite.book(flow, lane.pk, 10, 11, 1, now=now)
    flow.refresh_from_db()
    assert r.user == ps and r.status == Reservation.Status.HOLD and flow.status == OnsiteBooking.Status.BOOKED


def test_statement_and_code_rules(device, lane, now):
    flow, _ = onsite.start(device, "x@example.com", "Jan", "Nowak", "600700800")
    with pytest.raises(ValidationError, match="potwierdź adres"):
        onsite.accept_statement(flow)
    for i in range(onsite.CODE_ATTEMPTS):
        with pytest.raises(ValidationError):
            onsite.verify_code(flow, "000000" if last_code("x@example.com") != "000000" else "111111")
    with pytest.raises(ValidationError, match="Zbyt wiele"):
        onsite.verify_code(flow, last_code("x@example.com"))
    flow2, _ = onsite.start(device, "y@example.com", "Jan", "Nowak", "600700800")
    flow2 = onsite.verify_code(flow2, last_code("y@example.com"))
    onsite.submit_ps(flow2, "PS-9", "PZSS", "2020-05-01", "", photo_data_url())
    with pytest.raises(ValidationError, match="oświadczenie"):
        onsite.book(flow2, lane.pk, 10, 11, 1, now=now)


def test_expired_code_and_start_validation(device):
    with pytest.raises(ValidationError, match="e-mail"):
        onsite.start(device, "zly", "Jan", "Nowak", "600700800")
    with pytest.raises(ValidationError, match="telefonu"):
        onsite.start(device, "a@example.com", "Jan", "Nowak", "12")
    flow, _ = onsite.start(device, "a@example.com", "Jan", "Nowak", "600700800")
    with pytest.raises(ValidationError, match="wygasł"):
        onsite.verify_code(flow, last_code("a@example.com"), now=timezone.now() + timedelta(minutes=onsite.CODE_MINUTES + 1))


def test_photo_validation(device):
    flow, _ = onsite.start(device, "a@example.com", "Jan", "Nowak", "600700800")
    flow = onsite.verify_code(flow, last_code("a@example.com"))
    with pytest.raises(ValidationError, match="Brak zdjęcia"):
        onsite.submit_ps(flow, "PS-1", "PZSS", "2020-05-01", "", "")
    with pytest.raises(ValidationError, match="rozdzielczość"):
        onsite.submit_ps(flow, "PS-1", "PZSS", "2020-05-01", "", photo_data_url((300, 200)))
    with pytest.raises(ValidationError, match="Nieprawidłowe"):
        onsite.submit_ps(flow, "PS-1", "PZSS", "2020-05-01", "", "data:image/jpeg;base64,AAAA")
    with pytest.raises(ValidationError, match="przyszłości"):
        onsite.submit_ps(flow, "PS-1", "PZSS", (timezone.localdate() + timedelta(days=3)).isoformat(), "", photo_data_url())


def test_inactive_account_cannot_be_used(device):
    u = make_user("stary@example.com")
    u.is_active = False
    u.save()
    flow, _ = onsite.start(device, "stary@example.com", "Jan", "Nowak", "600700800")
    with pytest.raises(ValidationError, match="nie można użyć"):
        onsite.verify_code(flow, last_code("stary@example.com"))


def test_ps_pending_only_onsite(lane):
    u = make_user("z@example.com")
    with pytest.raises(ValueError):
        bookings.create_reservation(u, lane, timezone.localdate() + timedelta(days=7), 10, 11, 1, ps_pending=True)


def test_purge(device):
    flow, _ = onsite.start(device, "a@example.com", "Jan", "Nowak", "600700800")
    assert onsite.purge(now=timezone.now() + timedelta(hours=2)) == 0
    assert onsite.purge(now=timezone.now() + timedelta(days=2)) == 1
    assert not OnsiteBooking.objects.filter(pk=flow.pk).exists()


# --- API kiosku -----------------------------------------------------------------------------------------------


@pytest.fixture
def kiosk(owner):
    d, code = services.create_device("Kiosk", owner)
    c = Client()
    c.post("/kiosk/parowanie/", {"code": code})
    return d, c


def post(c, path, data):
    return c.post(path, json.dumps(data), content_type="application/json", **H)


def test_api_flow_and_isolation(kiosk, owner):
    d, c = kiosk
    assert c.get("/kiosk/api/na-miejscu/terminy/", **H).status_code == 200
    r = post(c, "/kiosk/api/na-miejscu/start/", {"email": "api@example.com", "first_name": "Ala", "last_name": "Kot", "phone": "600700800"})
    assert r.status_code == 200, r.content
    ref, secret = r.json()["ref"], r.json()["secret"]
    assert post(c, "/kiosk/api/na-miejscu/kod/", {"ref": ref, "secret": "zly", "code": "1"}).status_code == 404
    other_dev, code = services.create_device("Inny", owner)
    other = Client()
    other.post("/kiosk/parowanie/", {"code": code})
    assert post(other, "/kiosk/api/na-miejscu/stan/", {"ref": ref, "secret": secret}).status_code == 404
    r = post(c, "/kiosk/api/na-miejscu/kod/", {"ref": ref, "secret": secret, "code": last_code("api@example.com")})
    assert r.status_code == 200 and r.json()["state"]["status"] == "potwierdzony" and r.json()["state"]["ps"] == "brak"
    r = post(c, "/kiosk/api/na-miejscu/oswiadczenie/", {"ref": ref, "secret": secret, "accept": True})
    assert r.json()["state"]["statement"] is True
    r = post(c, "/kiosk/api/na-miejscu/legitymacja/", {"ref": ref, "secret": secret, "number": "PS 5", "issuer": "PZSS",
                                                       "issued_on": "2021-01-01", "photo": photo_data_url()})
    assert r.status_code == 200 and r.json()["state"]["ps"] == "weryfikacja"
    r = post(c, "/kiosk/api/na-miejscu/rezerwacja/", {"ref": ref, "secret": secret, "lane": 999999, "start_hour": 10,
                                                      "end_hour": 11, "persons": 1})
    assert r.status_code == 400 and "oś" in r.json()["error"]
    assert c.post("/kiosk/api/na-miejscu/stan/", "{}", content_type="application/json").status_code == 400  # bez nagłówka


# --- etap 2: płatność kodem QR na własnym telefonie ---------------------------------------------------------------


def _fake_tpay(monkeypatch, captured):
    from apps.payments import tpay

    def fake_create(**kw):
        captured.update(kw)
        return {"transaction_id": "01K", "title": "TR-K", "payment_url": "https://secure.sandbox.tpay.com/?title=TR-K"}

    monkeypatch.setattr(tpay, "create_transaction", fake_create)


def test_after_ps_review_reservation_waits_for_payment_with_qr(device, lane, owner, now, settings):
    settings.TPAY_ENABLED = True
    flow = verified_flow(device)
    credential = onsite.submit_ps(flow, "PS-9", "PZSS", "2020-05-01", "", photo_data_url())
    r = onsite.book(flow, lane.pk, 10, 12, 2, now=now)
    credential_services.review(credential, owner, approve=True)
    r.refresh_from_db()
    flow.refresh_from_db()
    assert r.status == Reservation.Status.HOLD and r.pay_by and not r.needs_approval and r.approved_by == owner
    st = onsite.state(flow)
    assert flow.status == OnsiteBooking.Status.BOOKED
    assert st["pay"]["url"].startswith(settings.SITE_URL) and st["pay"]["qr"].startswith("data:image/svg+xml")
    assert st["pay"]["amount_due"] == r.amount and st["reservation"]["paid"] is False


def test_current_hour_onsite_booking_holds_until_end_not_start(device, lane):
    ps = make_user("ps@example.com")
    add_credential(ps)
    approve(ps)
    day = timezone.localdate() + timedelta(days=7)
    inside = bookings.local_dt(day, 10) + timedelta(minutes=40)  # trwająca godzina 10–11
    flow = verified_flow(device, email="ps@example.com")
    r = onsite.book(flow, lane.pk, 10, 11, 1, now=inside)
    assert r.status == Reservation.Status.HOLD and r.pay_by > inside  # nie wygasa od razu
    r = bookings.start_online_payment(r, now=inside)
    assert inside < r.pay_by <= r.ends_at


def test_onsite_approval_allowed_during_reservation(device, lane, owner):
    day = timezone.localdate() + timedelta(days=7)
    booked_at = bookings.local_dt(day, 10) + timedelta(minutes=5)
    flow = verified_flow(device)
    onsite.submit_ps(flow, "PS-9", "PZSS", "2020-05-01", "", photo_data_url())
    r = onsite.book(flow, lane.pk, 10, 11, 1, now=booked_at)
    r = bookings.approve_reservation(r, by=owner, now=booked_at + timedelta(minutes=5))  # w trakcie godziny — dozwolone
    assert r.status == Reservation.Status.HOLD and r.pay_by <= r.ends_at
    with pytest.raises(ValidationError, match="nie czeka"):
        onsite.approve_for_payment(r, owner, now=booked_at + timedelta(minutes=6))


def test_unpaid_after_approval_released(device, lane, owner, now):
    flow = verified_flow(device)
    credential = onsite.submit_ps(flow, "PS-9", "PZSS", "2020-05-01", "", photo_data_url())
    r = onsite.book(flow, lane.pk, 10, 11, 1, now=now)
    credential_services.review(credential, owner, approve=True)
    r.refresh_from_db()
    bookings.expire_unpaid(now=r.pay_by + timedelta(minutes=1))
    r.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED


def test_guest_pay_link_starts_tpay_without_login(client, device, lane, now, settings, monkeypatch):
    from apps.payments.models import Payment

    settings.TPAY_ENABLED = True
    ps = make_user("ps@example.com")
    add_credential(ps)
    approve(ps)
    flow = verified_flow(device, email="ps@example.com")
    r = onsite.book(flow, lane.pk, 10, 11, 1, now=now)
    token = onsite.pay_token(r)
    body = client.get(f"/platnosci/rezerwacja/{token}/").content.decode()
    assert r.number in body and "Zapłać online" in body and ps.email not in body
    captured = {}
    _fake_tpay(monkeypatch, captured)
    response = client.post(f"/platnosci/rezerwacja/{token}/")
    assert response.status_code == 302 and response.url.startswith("https://secure.sandbox.tpay.com/")
    payment = Payment.objects.get()
    assert payment.user == ps and payment.reservation == r and payment.amount >= r.amount
    assert captured["success_url"].endswith(f"/platnosci/rezerwacja/{token}/wynik/?wynik=ok")
    assert client.get(f"/platnosci/rezerwacja/{token}/wynik/?wynik=ok").status_code == 200
    assert client.get("/platnosci/rezerwacja/zly-token/").status_code == 404


def test_guest_pay_refuses_paid_or_cancelled(client, device, lane, now, settings):
    settings.TPAY_ENABLED = True
    ps = make_user("ps@example.com")
    add_credential(ps)
    approve(ps)
    flow = verified_flow(device, email="ps@example.com")
    r = onsite.book(flow, lane.pk, 10, 11, 1, now=now)
    bookings.cancel_reservation(r, by=ps, as_board=True, reason="test")
    body = client.get(f"/platnosci/rezerwacja/{onsite.pay_token(r)}/").content.decode()
    assert "nie czeka na płatność" in body
    client.post(f"/platnosci/rezerwacja/{onsite.pay_token(r)}/")
    from apps.payments.models import Payment

    assert not Payment.objects.exists()


# --- etap 3: dane dla ekranów kiosku ---------------------------------------------------------------------------------


def test_price_and_state_fields_for_screens(device, lane, now):
    q = onsite.price(lane.pk, 10, 12, 2, now=now)
    assert q["amount"] > 0 and "10:00–12:00" in q["label"]
    with pytest.raises(ValidationError):
        onsite.price("x", 10, 12, 2, now=now)
    flow = verified_flow(device)
    st = onsite.state(flow)
    assert st["first_name"] == "Nowak" and st["last_name"] == "Jan" and st["statement"] is True
    assert "275" in st["statement_text"]
    onsite.submit_ps(flow, "PS-9", "PZSS", "2020-05-01", "", photo_data_url())
    r = onsite.book(flow, lane.pk, 10, 12, 2, now=now)
    flow.refresh_from_db()
    assert onsite.state(flow, now=now)["reservation"]["id"] == r.pk and r.amount == q["amount"]


def test_kiosk_page_has_onsite_screens(kiosk):
    _, c = kiosk
    body = c.get("/kiosk/").content.decode()
    assert "data-os-open" in body and "kiosk-onsite.js" in body and "art. 275" in body
    r = c.get("/kiosk/api/na-miejscu/cena/?os=x&od=10&do=11&osoby=1", **H)
    assert r.status_code == 400



# --- etap 4: panel obsługi ---------------------------------------------------------------------------------------------


def _board_subjects():
    return list(OutgoingEmail.objects.filter(recipients__contains=["wlasciciel@example.com"]).values_list("subject", flat=True))


def test_one_urgent_notification_and_panel_banner_first_in_queue(device, lane, owner, owner_client, now):
    other = make_user("inny@example.com")
    older = Credential.objects.create(user=other, kind=Credential.Kind.RANGE_OFFICER, number="X-1", issuer="LOK",
                                      issued_on=timezone.localdate() - timedelta(days=400))
    flow = verified_flow(device)
    credential = onsite.submit_ps(flow, "PS-9", "PZSS (Polski Związek Strzelectwa Sportowego)", "2020-05-01", "", photo_data_url())
    OutgoingEmail.objects.all().delete()
    onsite.book(flow, lane.pk, 10, 11, 1, now=now)
    subjects = _board_subjects()
    assert len(subjects) == 1 and "PILNE" in subjects[0] and onsite.VERIFY_MINUTES == 15
    from apps.credentials.views import _pending_queue

    assert list(_pending_queue().values_list("pk", flat=True))[:2] == [credential.pk, older.pk]
    body = owner_client.get(f"/panel/weryfikacja/{credential.pk}/").content.decode()
    assert "Osoba czeka przy kiosku" in body and "data-countdown-to" in body and "600 700 800" in body
    assert "Osoba czeka przy kiosku" not in owner_client.get(f"/panel/weryfikacja/{older.pk}/").content.decode()
    home = owner_client.get("/panel/").content.decode()
    assert "Czekają przy kiosku" in home and f"/panel/weryfikacja/{credential.pk}/" in home


def test_approve_from_panel_page_confirms_for_payment(device, lane, owner, owner_client, now):
    flow = verified_flow(device)
    credential = onsite.submit_ps(flow, "PS-9", "LOK (Liga Obrony Kraju)", "2020-05-01", "", photo_data_url())
    r = onsite.book(flow, lane.pk, 10, 11, 1, now=now)
    owner_client.post(f"/panel/weryfikacja/{credential.pk}/", {"decision": "approve", "note": ""})
    r.refresh_from_db()
    credential.refresh_from_db()
    assert credential.status == Credential.Status.VERIFIED and r.status == Reservation.Status.HOLD and not credential.scan
