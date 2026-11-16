"""Obłożenie godzinowe: % godzin osi wobec godzin otwarcia (bez blokad), mapa dzień × godzina, najbardziej oblegane terminy."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.association import stats
from apps.bookings import services
from apps.facilities.models import Lane

from .conftest import add_credential, approve, make_user

pytestmark = pytest.mark.django_db


def _ps():
    u = approve(make_user("oblozenie@example.com"))
    add_credential(u)
    return u


def test_occupancy_counts_and_blocks(owner):
    lane = Lane.objects.get(name="Oś 100 m")
    day = timezone.localdate() + timedelta(days=7)
    oh = lane.hours_for(day)
    assert oh, "oś testowa powinna być czynna"
    ps = _ps()
    services.create_reservation(ps, lane, day, oh.opens.hour, oh.opens.hour + 2, 2)
    first, nxt = timezone.localdate(), timezone.localdate() + timedelta(days=30)
    before = stats.occupancy(first, nxt)
    assert before["booked"] == 2 and before["reservations"] == 1
    row = next(x for x in before["lanes"] if x["lane"].pk == lane.pk)
    assert row["booked"] == 2 and row["capacity"] > 0
    w = day.weekday()
    cell = next(c for c in before["heat"][w][2] if c[0] == oh.opens.hour)
    assert cell[1] >= 1 and cell[3] is not None
    assert before["top"][0]["hour"] in (oh.opens.hour, oh.opens.hour + 1)
    # blokada całego dnia: zmniejsza dostępne godziny osi
    occ = services.block_occurrences(day + timedelta(days=7), day + timedelta(days=7), 0, 24, False)
    services.create_blocks("Zawody", "", [lane], occ, owner, cancel_conflicts=False)
    after = stats.occupancy(first, nxt)
    row2 = next(x for x in after["lanes"] if x["lane"].pk == lane.pk)
    assert row2["capacity"] == row["capacity"] - (oh.closes.hour - oh.opens.hour)


def test_occupancy_page(owner_client):
    for k in ("30", "90", "365", "plan", "zly"):
        r = owner_client.get(f"/panel/raporty/oblozenie/?okres={k}")
        assert r.status_code == 200
        assert "Dzień tygodnia × godzina" in r.content.decode()


def test_occupancy_requires_permission(client):
    from .conftest import login_with_otp

    u = make_user("bezuprawnien@example.com", is_staff=True)
    login_with_otp(client, u)
    assert client.get("/panel/raporty/oblozenie/").status_code == 403


def _archived(lane, day, hour, hours=2, number="GT-1", client="Jan Archiwalny", logbook="Dodano1", payment="Online", positions="1, 2",
              block=None):
    from apps.bookings.models import ArchivedReservation
    from apps.bookings.services import local_dt

    return ArchivedReservation.objects.create(row_hash=f"{number}-{day}-{hour}", number=number, starts_at=local_dt(day, hour),
                                              ends_at=local_dt(day, hour + hours), client=client, lane_label=lane.name, lane=lane,
                                              positions=positions, logbook_info=logbook, payment=payment, block=block)


def test_guntracker_archive_in_occupancy_and_report(owner, owner_client):
    from apps.bookings.models import Block

    lane = Lane.objects.get(name="Oś 100 m")
    today = timezone.localdate()
    day = today - timedelta(days=3)
    while not lane.hours_for(day):
        day -= timedelta(days=1)
    oh = lane.hours_for(day)
    h = oh.opens.hour
    rule = lane.price_rule_for(day)
    a1 = _archived(lane, day, h)  # 2 h, 2 stanowiska, wpis w książce
    _archived(lane, day, h + 2, hours=1, number="GT-2", logbook="", payment="Online", positions="3")  # bez wpisu
    _archived(lane, day, h + 3, hours=1, number="GT-3", client="Jan  archiwalny", logbook="Brak", payment="Kod wejścia")
    # przyszła rezerwacja z GunTrackera ma blokadę osi — liczy się jako zajęta, nie zamknięta
    fut = today + timedelta(days=1)
    while not lane.hours_for(fut):
        fut += timedelta(days=1)
    fh = lane.hours_for(fut).opens.hour
    from apps.bookings.services import local_dt

    block = Block.objects.create(title="GunTracker", starts_at=local_dt(fut, fh), ends_at=local_dt(fut, fh + 1))
    block.lanes.set([lane])
    _archived(lane, fut, fh, hours=1, number="GT-4", block=block)

    occ = stats.occupancy(day, fut + timedelta(days=1))
    row = next(x for x in occ["lanes"] if x["lane"].pk == lane.pk)
    assert occ["reservations"] == 4 and row["booked"] == 5
    full = sum(1 for d in range((fut - day).days + 1) for _ in [0] if lane.hours_for(day + timedelta(days=d)))
    assert row["capacity"] == sum((lane.hours_for(day + timedelta(days=d)).closes.hour - lane.hours_for(day + timedelta(days=d)).opens.hour)
                                  for d in range((fut - day).days + 1) if lane.hours_for(day + timedelta(days=d)))
    assert full >= 2

    rep = stats.reservations_report(30)
    assert rep["archived_count"] == 3  # przyszła (jutro) poza okresem do dziś
    expected = rule.hourly_amount(2) * 2 + rule.hourly_amount(1) * 1  # kod wejścia — 0
    assert rep["estimated"] == expected and rep["kpi"]["revenue"] >= expected
    assert rep["kpi"]["reservations"] == 3 and rep["done_count"] == 3
    assert {m.number for m in rep["missing"]} == {"GT-2", "GT-3"}
    jan = next(x for x in rep["ranking"] if x["name"].startswith("Jan"))
    assert jan["user"] is None and jan["visits"] == 3 and jan["missing"] == 2  # ta sama osoba mimo innej pisowni
    assert a1.number not in {m.number for m in rep["missing"]}
    page = owner_client.get("/panel/raporty/rezerwacje/?okres=30").content.decode()
    assert "archiwum GunTrackera" in page and "szacunek z cennika" in page and "GunTracker GT-2" in page and "(GunTracker)" in page
