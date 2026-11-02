from datetime import timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.association import services as association_services
from apps.association.models import Member
from apps.audit.models import AuditLog
from apps.bookings import services as booking_services
from apps.bookings.models import Reservation
from apps.clients import services
from apps.clients.models import ClientPass
from apps.facilities.models import Lane

from .conftest import approve, add_credential, grant, login_with_otp, make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def ps(db):
    user = make_user("ps@example.com", first_name="Piotr", last_name="Strzelec")
    add_credential(user)
    approve(user)
    return user


@pytest.fixture(autouse=True)
def passes_for_returning(db):
    from apps.bookings.models import GroupRule

    GroupRule.objects.filter(group="powracajacy").update(pay_pass=True)


def day(offset=5):
    return timezone.localdate() + timedelta(days=offset)


def issue(user, owner=None, **kw):
    params = {"kind": ClientPass.Kind.PACK, "entries_limit": 2, "valid_from": timezone.localdate(), "valid_until": day(60),
              "by": owner, "price": 15000, "paid_with": "gotowka"}
    params.update(kw)
    return services.issue_pass(user, **params)


LANE = "Oś 25 m nr 1"


def book(user, code, start=10, end=11, persons=1, offset=5, lane=LANE):
    return booking_services.create_reservation(user, Lane.objects.get(name=lane), day(offset), start, end, persons, access_code=code)


def test_pass_pays_reservation_and_counts_entries(ps):
    p = issue(ps)
    assert p.code.startswith("W") and len(p.code) == 8
    r = book(ps, p.code.lower())
    assert r.client_pass == p and r.payment_status == Reservation.Payment.PASS and r.amount == 0
    assert services.pass_uses(p) == 1 and services.pass_remaining(p) == 1
    book(ps, p.code, start=12, end=13)
    with pytest.raises(ValidationError, match="wszystkie wejścia"):
        book(ps, p.code, start=14, end=15)


def test_free_cancellation_returns_entry_late_does_not(ps):
    p = issue(ps, entries_limit=1)
    r = book(ps, p.code, offset=10)
    booking_services.cancel_reservation(r, ps)
    assert services.pass_uses(p) == 0
    r2 = book(ps, p.code, offset=10, start=12, end=13)
    Reservation.objects.filter(pk=r2.pk).update(late_cancellation=True, status=Reservation.Status.CANCELLED)
    assert services.pass_uses(p) == 1


def test_pass_limits(ps):
    other = make_user("inny@example.com")
    add_credential(other)
    p = issue(ps, max_hours=1, max_persons=2, lanes=[Lane.objects.get(name=LANE)])
    with pytest.raises(ValidationError, match="innej osoby"):
        book(other, p.code)
    with pytest.raises(ValidationError, match="1 h"):
        book(ps, p.code, end=12)
    with pytest.raises(ValidationError, match="2 os"):
        book(ps, p.code, persons=3)
    with pytest.raises(ValidationError, match="osi"):
        book(ps, p.code, lane="Oś 100 m")
    with pytest.raises(ValidationError, match="ważny"):
        book(ps, p.code, offset=90)
    unpaid = issue(ps, paid_with="")
    with pytest.raises(ValidationError, match="opłacony"):
        book(ps, unpaid.code)
    services.mark_pass_paid(unpaid, "przelew", None)
    assert book(ps, unpaid.code, start=15, end=16).client_pass == unpaid
    services.deactivate_pass(p, None)
    with pytest.raises(ValidationError, match="nieaktywny"):
        book(ps, p.code)


def test_single_and_subscription_kinds(ps):
    single = issue(ps, kind=ClientPass.Kind.SINGLE, entries_limit=9)
    sub = issue(ps, kind=ClientPass.Kind.SUBSCRIPTION, entries_limit=9)
    assert single.entries_limit == 1 and sub.entries_limit is None
    for h in (9, 10, 11, 12):
        book(ps, sub.code, start=h, end=h + 1)
    assert services.pass_uses(sub) == 4 and services.pass_remaining(sub) is None


def test_change_persons_respects_pass(ps, owner):
    p = issue(ps, max_persons=2)
    r = book(ps, p.code)
    booking_services.change_persons(r, 2, owner)
    with pytest.raises(ValidationError, match="najwyżej 2"):
        booking_services.change_persons(r, 3, owner)
    r.refresh_from_db()
    assert r.amount == 0 and r.payment_status == Reservation.Payment.PASS


def test_logbook_settlement_skips_pass(ps):
    from apps.logbook.services import settle_finished_reservations

    p = issue(ps)
    r = book(ps, p.code, offset=2)
    settle_finished_reservations(now=r.ends_at + timedelta(minutes=1))
    r.refresh_from_db()
    assert r.settled_at and r.payment_status == Reservation.Payment.PASS and r.amount == 0


def test_club_code_still_works_and_unknown_code_rejected(ps):
    with pytest.raises(ValidationError, match="Nieprawidłowy"):
        book(ps, "WXXXXXXX")


def test_client_types(ps):
    newbie = make_user("nowy@example.com")
    member_user = make_user("czlonek@example.com")
    association_services.add_member(number=1, first_name="C", last_name="Z", kind=Member.Kind.ORDINARY,
                                    joined_on=timezone.localdate(), by=None, user=member_user)
    lane = Lane.objects.get(name=LANE)
    start = timezone.now() - timedelta(days=3)
    rule = lane.price_rule_for(start.date())
    Reservation.objects.create(number="SATOK-R-T-1", lane=lane, user=ps, starts_at=start, ends_at=start + timedelta(hours=1), persons=1,
                               price_rule=rule, hourly_amount=100, amount=100)
    users = {u.email: u for u in services.annotate_clients([ps, newbie, member_user])}
    assert users["ps@example.com"].client_type == "powracajacy" and users["ps@example.com"].visits == 1
    assert users["ps@example.com"].ps_status == "wazne"
    assert users["nowy@example.com"].client_type == "nowy" and users["nowy@example.com"].ps_status == "brak"
    assert users["czlonek@example.com"].client_type == "czlonek"
    services.set_approved(newbie, True, None)
    assert services.client_type(newbie) == "powracajacy"


def test_panel_kartoteka_flow(owner_client, ps):
    body = owner_client.get("/panel/uzytkownicy/").content.decode()
    assert "Klienci" in body and "Piotr Strzelec" in body and "ważne" in body
    url = f"/panel/uzytkownicy/{ps.pk}/"
    owner_client.post(url + "kartoteka/", {"notes": "Lubi oś 100 m", "tags": "sędzia, VIP, sędzia"})
    profile = services.profile_for(ps)
    assert profile.notes == "Lubi oś 100 m" and sorted(t.name for t in profile.tags.all()) == ["VIP", "sędzia"]
    owner_client.post(url + "zatwierdzenie/", {"approved": "1"})
    assert services.profile_for(ps).approved_at is not None
    response = owner_client.post(url + "kod-wejscia/", {
        "kind": "karnet", "entries_limit": 5, "valid_from": timezone.localdate().isoformat(), "valid_until": day(30).isoformat(),
        "max_hours": 2, "max_persons": 1, "price": "150.00", "paid_with": "",
    })
    assert response.status_code == 302
    p = ClientPass.objects.get(user=ps)
    assert p.price == 15000 and not p.is_paid
    owner_client.post(f"/panel/kody-wejscia/{p.pk}/", {"action": "paid", "paid_with": "karta"})
    p.refresh_from_db()
    assert p.paid_with == "karta" and p.paid_at
    body = owner_client.get(url).content.decode()
    assert p.code in body and "0 z 5" in body and "Lubi oś 100 m" in body
    assert owner_client.get("/panel/uzytkownicy/?tag=VIP").content.decode().count("Piotr Strzelec") == 1
    assert "Piotr Strzelec" not in owner_client.get("/panel/uzytkownicy/?typ=nowy").content.decode()
    assert AuditLog.objects.filter(action__startswith="kod_wejscia.").count() == 2


def test_panel_permissions(client, ps):
    viewer = grant(make_user("v@example.com"), "accounts.view_users")
    login_with_otp(client, viewer)
    assert client.get(f"/panel/uzytkownicy/{ps.pk}/").status_code == 200
    assert client.post(f"/panel/uzytkownicy/{ps.pk}/kartoteka/", {"notes": "x"}).status_code == 403
    assert client.get(f"/panel/uzytkownicy/{ps.pk}/kod-wejscia/").status_code == 403


def test_dashboard_shows_own_passes(client, ps):
    p = issue(ps)
    client.force_login(ps)
    body = client.get("/pulpit/").content.decode()
    assert p.code in body and "zostało 2 z 2" in body


def test_pass_not_allowed_for_group(ps):
    from apps.bookings.models import GroupRule

    GroupRule.objects.filter(group="powracajacy").update(pay_pass=False)
    p = issue(ps)
    with pytest.raises(ValidationError, match="grupy"):
        book(ps, p.code)
