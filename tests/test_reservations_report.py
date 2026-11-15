"""Raport „Rezerwacje i osie”: przychód osi na godzinę otwarcia i na zarezerwowaną godzinę, rezerwacje bez wpisu
w książce pobytu (powiązanego albo wpisu rezerwującego w oknie czasu), ranking osób. Dane zmyślone."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.association import stats
from apps.bookings.models import Reservation
from apps.bookings.services import local_dt
from apps.facilities.models import Lane
from apps.logbook import services as logbook

from .conftest import add_credential, approve, make_user
from .test_logbook import permit_data, signature_data_url

pytestmark = pytest.mark.django_db


def _past_reservation(user, lane, days_ago, n, **kw):
    day = timezone.localdate() - timedelta(days=days_ago)
    while not lane.hours_for(day):
        day -= timedelta(days=1)
    oh = lane.hours_for(day)
    start = local_dt(day, oh.opens.hour)
    rule = lane.price_rule_for(day)
    hourly = rule.hourly_amount(2)
    return Reservation.objects.create(number=f"SATOK-R-RAP-{n:04d}", lane=lane, user=user, starts_at=start, ends_at=start + timedelta(hours=2),
                                      persons=2, price_rule=rule, hourly_amount=hourly, amount=hourly * 2, **kw)


def _entry(user, reservation=None, at=None):
    """Wpis zapisany w danej chwili (`now`) — z rezerwacją (strzelanie) albo bez (inny cel)."""
    at = at or reservation.starts_at + timedelta(minutes=10)
    purpose = {"purpose": "strzelanie"} if reservation else {"purpose": "inne", "purpose_note": "test"}
    return logbook.create_entry(data={**permit_data(), **purpose, "has_permit": True}, now=at,
                                signature_png=logbook.decode_signature(signature_data_url()), source="qr_rezerwacji",
                                reservation=reservation, user=user)


def test_report_revenue_per_lane_and_missing_logbook_entries(owner_client):
    ps = approve(make_user("rap@example.com", first_name="Rafał", last_name="Raportowy"))
    add_credential(ps)
    lane = Lane.objects.get(name="Oś 100 m")
    linked = _past_reservation(ps, lane, 3, 1)
    by_time = _past_reservation(ps, lane, 5, 2)
    missing = _past_reservation(ps, lane, 8, 3)
    _past_reservation(ps, lane, 10, 4, status="anulowana")             # odwołana bez kosztów — nie liczy się
    waived = _past_reservation(ps, lane, 12, 5, payment_status="zwolniona")  # zwolniona z opłaty — przychód 0
    _entry(ps, reservation=linked)
    _entry(ps, at=by_time.starts_at - timedelta(minutes=20))           # wpis bez powiązania, ale w oknie rezerwacji
    _entry(ps, at=missing.starts_at - timedelta(hours=3))              # za wcześnie — nie liczy się

    r = stats.reservations_report(30)
    assert r["kpi"]["reservations"] == 4 and r["done_count"] == 4
    assert [x.pk for x in r["missing"]] == [missing.pk, waived.pk] and r["kpi"]["missing_pct"] == 50
    row = next(x for x in r["lanes"] if x["lane"].pk == lane.pk)
    assert row["revenue"] == 3 * linked.amount and row["count"] == 4 and row["hours"] == 8
    assert row["per_booked_hour"] == round(3 * linked.amount / 8) and row["per_open_hour"] == round(row["revenue"] / row["capacity"])
    assert r["ranking"][0]["user"] == ps and r["ranking"][0]["missing"] == 2 and r["ranking"][0]["visits"] == 4
    page = owner_client.get("/panel/raporty/rezerwacje/?okres=30").content.decode()
    assert "Rezerwacje i osie" in page and "Rafał Raportowy" in page and "2 z 4" in page and "Oś 100 m" in page


def test_report_page_periods_and_permission(owner_client):
    from django.test import Client

    from .conftest import login_with_otp

    client = Client()

    for k in ("7", "30", "90", "365", "zly"):
        assert owner_client.get(f"/panel/raporty/rezerwacje/?okres={k}").status_code == 200
    u = make_user("bezuprawnien2@example.com", is_staff=True)
    login_with_otp(client, u)
    assert client.get("/panel/raporty/rezerwacje/").status_code == 403
