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


def test_new_person_full_flow_waits_for_ps_and_is_confirmed_after_review(device, lane, owner, now):
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
    assert r.status == Reservation.Status.CONFIRMED and flow.status == OnsiteBooking.Status.BOOKED
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
    bookings.approve_reservation(r, by=owner)
    assert onsite.expire_pending(now=now + timedelta(minutes=30)) == 0
    flow.refresh_from_db()
    r.refresh_from_db()
    assert flow.status == OnsiteBooking.Status.BOOKED and r.status == Reservation.Status.CONFIRMED


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
