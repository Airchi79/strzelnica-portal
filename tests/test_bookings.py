import datetime
import threading
from datetime import timedelta

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone

from apps.audit.models import AuditLog
from apps.bookings import services
from apps.bookings.models import Block, BookingSettings, Reservation
from apps.core.models import Notification
from apps.facilities.models import Lane
from apps.regulations.models import RegulationDocument, RegulationVersion

from .conftest import add_credential, grant, login_with_otp, make_user

pytestmark = pytest.mark.django_db


def next_weekday(offset_days=7):
    return timezone.localdate() + timedelta(days=offset_days)


@pytest.fixture
def ps(db):
    user = make_user("ps@example.com")
    add_credential(user)
    return user


@pytest.fixture
def lane(db):
    return Lane.objects.get(name="Oś 100 m")


def book(user, lane, day=None, start=10, end=12, persons=3, **kw):
    return services.create_reservation(user, lane, day or next_weekday(), start, end, persons, **kw)


# --- reguły ----------------------------------------------------------------


def test_create_reservation_prices_numbers_and_notifies(ps, lane, owner, django_capture_on_commit_callbacks):
    with django_capture_on_commit_callbacks(execute=True):
        r = book(ps, lane, persons=3)
    assert r.amount == 2 * 18000 and r.hourly_amount == 18000  # 3 × 60 zł < ryczałt 200 zł
    assert r.number == f"SATOK-R-{next_weekday().year}-0001"
    assert r.status == Reservation.Status.CONFIRMED and r.payment_status == Reservation.Payment.DUE
    recipients = {addr for m in mail.outbox for addr in m.to}
    assert ps.email in recipients and owner.email in recipients
    assert Notification.objects.filter(recipient=owner, url=f"/panel/rezerwacje/{r.pk}/").exists()
    assert AuditLog.objects.filter(action="rezerwacja.utworzenie", object_id=str(r.pk)).exists()
    r2 = book(ps, Lane.objects.get(name="Oś 35 m"), persons=4, start=9, end=10)
    assert r2.number.endswith("-0002") and r2.amount == 10000


def test_only_verified_range_officer_can_book(lane):
    user = make_user("bez@example.com")
    with pytest.raises(ValidationError, match="prowadzącego strzelanie"):
        book(user, lane)
    add_credential(user, kind="pozwolenie")
    with pytest.raises(ValidationError):
        book(user, lane)
    add_credential(user, valid_until=timezone.localdate() - timedelta(days=1), number="PS-OLD")
    with pytest.raises(ValidationError):
        book(user, lane)


def test_pending_regulation_blocks_booking(ps, lane):
    doc = RegulationDocument.objects.get(slug="regulamin-strzelnicy")
    RegulationVersion.objects.create(document=doc, number=1, content="x", effective_from=timezone.now(), published_at=timezone.now())
    with pytest.raises(ValidationError, match="regulaminy"):
        book(ps, lane)


def test_no_overlap_on_same_lane_but_other_lane_ok(ps, lane):
    book(ps, lane, start=10, end=12)
    with pytest.raises(ValidationError, match="zajęty"):
        book(ps, lane, start=11, end=13)
    book(ps, lane, start=12, end=13)  # styk przedziałów jest dozwolony
    book(ps, Lane.objects.get(name="Oś 35 m"), start=10, end=12)


def test_cancelled_reservation_frees_the_slot(ps, lane):
    r = book(ps, lane)
    services.cancel_reservation(r, ps)
    book(ps, lane)


@pytest.mark.parametrize(
    "start,end,msg",
    [(8, 10, "czynna w godzinach"), (17, 19, "czynna w godzinach"), (12, 12, "później niż początek"), (12, 11, "później niż początek")],
)
def test_opening_hours_and_order(ps, lane, start, end, msg):
    with pytest.raises(ValidationError, match=msg):
        book(ps, lane, start=start, end=end)


def test_whole_day_without_length_limit(ps, lane):
    r = book(ps, lane, start=9, end=18, persons=10)
    assert r.hours == 9 and r.amount == 9 * 20000


def test_closed_day_and_inactive_lane(ps, lane):
    day = next_weekday()
    lane.opening_hours.filter(weekday=day.weekday()).delete()
    with pytest.raises(ValidationError, match="zamknięta"):
        book(ps, lane, day=day)
    other = Lane.objects.get(name="Oś 35 m")
    other.is_active = False
    other.save()
    with pytest.raises(ValidationError, match="niedostępna"):
        book(ps, other)


def test_current_hour_bookable_but_not_past(ps, lane):
    day = next_weekday()
    now = services.local_dt(day, 10) + timedelta(minutes=20)
    r = services.create_reservation(ps, lane, day, 10, 11, 1, now=now)  # rezerwacja „na miejscu” w trakcie godziny
    assert r.pk
    with pytest.raises(ValidationError, match="minął"):
        services.create_reservation(ps, lane, day, 9, 10, 1, now=now)


def test_max_advance_and_persons(ps, lane):
    cfg = BookingSettings.load()
    with pytest.raises(ValidationError, match="dni naprzód"):
        book(ps, lane, day=timezone.localdate() + timedelta(days=cfg.max_advance_days + 1))
    with pytest.raises(ValidationError, match="Liczba osób"):
        book(ps, lane, persons=cfg.max_persons + 1)
    with pytest.raises(ValidationError, match="Liczba osób"):
        book(ps, lane, persons=0)


def test_dynamic_lane_flat_rate(ps):
    r = book(ps, Lane.objects.get(kind="dynamiczna"), persons=1, start=9, end=11)
    assert r.amount == 30000


def test_price_rule_frozen_at_booking(ps, lane):
    from apps.facilities.models import PriceRule

    day = next_weekday()
    r = book(ps, lane, day=day, persons=2)
    PriceRule.objects.create(lane=lane, per_person_hourly=9000, flat_hourly=30000, valid_from=timezone.localdate())
    owner = make_user("adm@example.com")
    services.change_persons(r, 3, owner)
    r.refresh_from_db()
    assert r.hourly_amount == 18000  # nadal cennik z chwili rezerwacji


# --- anulowanie -----------------------------------------------------------


def test_free_cancellation_before_48h(ps, lane):
    day = next_weekday(10)
    r = book(ps, lane, day=day)
    now = r.starts_at - timedelta(hours=48, minutes=1)
    r = services.cancel_reservation(r, ps, "zmiana planów", now=now)
    assert r.status == Reservation.Status.CANCELLED and not r.late_cancellation
    assert r.payment_status == Reservation.Payment.NONE


def test_late_cancellation_keeps_amount_due(ps, lane):
    r = book(ps, lane)
    now = r.starts_at - timedelta(hours=47)
    r = services.cancel_reservation(r, ps, now=now)
    assert r.late_cancellation and r.payment_status == Reservation.Payment.DUE


def test_user_cannot_cancel_started_or_foreign(ps, lane):
    r = book(ps, lane)
    with pytest.raises(ValidationError, match="rozpoczęła"):
        services.cancel_reservation(r, ps, now=r.starts_at + timedelta(minutes=1))
    other = make_user("x@example.com")
    with pytest.raises(ValidationError, match="Twoja"):
        services.cancel_reservation(r, other)


def test_board_cancellation_is_free_even_late(ps, lane, owner):
    r = book(ps, lane)
    r = services.cancel_reservation(r, owner, "awaria", as_board=True, now=r.starts_at - timedelta(hours=1))
    assert not r.late_cancellation and r.payment_status == Reservation.Payment.NONE


def test_payment_transitions(ps, lane, owner):
    r = book(ps, lane)
    r = services.set_payment(r, "paid", owner)
    assert r.payment_status == Reservation.Payment.PAID
    with pytest.raises(ValidationError):
        services.set_payment(r, "paid", owner)
    r = services.set_payment(r, "due", owner)
    r = services.set_payment(r, "waive", owner)
    assert r.payment_status == Reservation.Payment.WAIVED


# --- blokady --------------------------------------------------------------


def test_block_prevents_booking_and_handles_conflicts(ps, lane, owner, django_capture_on_commit_callbacks):
    day = next_weekday()
    existing = book(ps, lane, day=day, start=14, end=16)
    occ = services.block_occurrences(day, day, 13, 18, weekly=False)
    assert [r.pk for r in services.block_conflicts([lane], occ)] == [existing.pk]
    with django_capture_on_commit_callbacks(execute=True):
        blocks, conflicts, cancelled = services.create_blocks("Zawody", "", [lane], occ, owner, cancel_conflicts=True)
    existing.refresh_from_db()
    assert existing.status == Reservation.Status.CANCELLED and existing.payment_status == Reservation.Payment.NONE
    assert any(ps.email in m.to and "anulowana" in m.subject for m in mail.outbox)
    with pytest.raises(ValidationError, match="zablokowana"):
        book(ps, lane, day=day, start=17, end=18)
    book(ps, lane, day=day, start=10, end=13)  # przed blokadą wolno
    states = {s.hour: s.state for s in services.day_slots(lane, day)}
    assert states[13] == "blokada" and states[10] == "zajete" and states[9] == "wolne"


def test_weekly_block_series_and_delete(owner, lane):
    day = next_weekday()
    occ = services.block_occurrences(day, day + timedelta(days=21), 17, 19, weekly=True)
    assert len(occ) == 4
    blocks, _, _ = services.create_blocks("Trening klubu", "", [lane], occ, owner, cancel_conflicts=False)
    assert Block.objects.filter(series=blocks[0].series).count() == 4
    services.delete_block_series(blocks[0], owner, whole_series=False)
    assert Block.objects.count() == 3
    services.delete_block_series(blocks[1], owner, whole_series=True)
    assert Block.objects.count() == 0


def test_keep_conflicts_notifies_user(ps, lane, owner, django_capture_on_commit_callbacks):
    day = next_weekday()
    r = book(ps, lane, day=day)
    with django_capture_on_commit_callbacks(execute=True):
        services.create_blocks("Prace", "", [lane], services.block_occurrences(day, day, 0, 24, False), owner, cancel_conflicts=False)
    r.refresh_from_db()
    assert r.status == Reservation.Status.CONFIRMED
    assert any("Blokada osi" in m.subject for m in mail.outbox)


# --- przypomnienia ----------------------------------------------------------


def test_reminders_sent_once(ps, lane, django_capture_on_commit_callbacks):
    r = book(ps, lane)
    now = r.starts_at - timedelta(hours=20)
    mail.outbox.clear()
    with django_capture_on_commit_callbacks(execute=True):
        assert services.send_reminders(now=now) == 1
    assert services.send_reminders(now=now) == 0
    assert mail.outbox[0].to == [ps.email]
    assert services.send_reminders(now=r.starts_at - timedelta(hours=30)) == 0


# --- współbieżność ------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_concurrent_bookings_only_one_wins():
    from django.core.management import call_command

    call_command("createcachetable", verbosity=0)
    user = make_user("ps2@example.com")
    add_credential(user)
    lane = Lane.objects.get(name="Oś 25 m nr 1")
    day = next_weekday()
    results, barrier = [], threading.Barrier(8)

    def worker():
        try:
            barrier.wait()
            services.create_reservation(user, lane, day, 10, 11, 2)
            results.append("ok")
        except ValidationError:
            results.append("zajete")
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count("ok") == 1 and results.count("zajete") == 7
    assert Reservation.objects.filter(lane=lane).count() == 1


# --- widoki -----------------------------------------------------------------


def test_public_calendar_hides_personal_data(client, ps, lane):
    day = next_weekday()
    book(ps, lane, day=day)
    body = client.get(f"/rezerwacje/?dzien={day:%Y-%m-%d}").content.decode()
    assert "zajęte" in body and ps.last_name not in body and ps.email not in body


def test_booking_flow_via_views(client, ps, lane, django_capture_on_commit_callbacks):
    client.force_login(ps)
    day = next_weekday()
    data = {"lane": lane.pk, "day": day.isoformat(), "start_hour": 10, "end_hour": 12, "persons": 4, "notes": "", "action": "preview"}
    response = client.post("/rezerwacje/nowa/", data)
    body = response.content.decode()
    assert "Sprawdź i potwierdź" in body and "400 zł" in body
    assert not Reservation.objects.exists()
    with django_capture_on_commit_callbacks(execute=True):
        response = client.post("/rezerwacje/nowa/", {**data, "action": "confirm"})
    r = Reservation.objects.get()
    assert response.url == f"/rezerwacje/{r.pk}/"
    assert "SATOK-R" in client.get(response.url).content.decode()
    response = client.post("/rezerwacje/nowa/", {**data, "action": "confirm"})
    assert "właśnie zajęty" in response.content.decode()
    assert client.get("/rezerwacje/moje/").status_code == 200
    client.post(f"/rezerwacje/{r.pk}/", {"reason": ""})
    r.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED


def test_user_without_ps_sees_reasons(client, user):
    client.force_login(user)
    assert "prowadzącego strzelanie" in client.get("/rezerwacje/nowa/").content.decode()


def test_user_cannot_view_foreign_reservation(client, ps, lane):
    r = book(ps, lane)
    other = make_user("o@example.com")
    client.force_login(other)
    assert client.get(f"/rezerwacje/{r.pk}/").status_code == 404


def test_panel_requires_booking_permission(client, ps, lane):
    r = book(ps, lane)
    staff = grant(make_user("z@example.com"), "credentials.review_credential")
    login_with_otp(client, staff)
    for url in ["/panel/rezerwacje/", "/panel/rezerwacje/lista/", f"/panel/rezerwacje/{r.pk}/", "/panel/blokady/", "/panel/rezerwacje/nowa/"]:
        assert client.get(url).status_code == 403, url


def test_panel_pages_and_actions(owner_client, ps, lane):
    day = next_weekday()
    r = book(ps, lane, day=day)
    body = owner_client.get(f"/panel/rezerwacje/?dzien={day:%Y-%m-%d}").content.decode()
    assert ps.last_name in body
    assert owner_client.get("/panel/rezerwacje/lista/").status_code == 200
    owner_client.post(f"/panel/rezerwacje/{r.pk}/", {"action": "paid"})
    r.refresh_from_db()
    assert r.payment_status == Reservation.Payment.PAID
    owner_client.post(f"/panel/rezerwacje/{r.pk}/", {"action": "persons", "p-persons": "5"})
    r.refresh_from_db()
    assert r.persons == 5 and r.amount == 2 * 20000
    response = owner_client.post(f"/panel/rezerwacje/{r.pk}/", {"action": "cancel", "c-reason": ""})
    r.refresh_from_db()
    assert r.status == Reservation.Status.CONFIRMED  # powód wymagany
    owner_client.post(f"/panel/rezerwacje/{r.pk}/", {"action": "cancel", "c-reason": "awaria osi"})
    r.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED and r.cancel_reason == "awaria osi"


def test_panel_create_on_behalf(owner_client, ps, lane):
    day = next_weekday()
    response = owner_client.post(
        "/panel/rezerwacje/nowa/",
        {"user": ps.pk, "lane": lane.pk, "day": day.isoformat(), "start_hour": 9, "end_hour": 10, "persons": 1, "notes": ""},
    )
    r = Reservation.objects.get()
    assert response.url == f"/panel/rezerwacje/{r.pk}/" and r.user == ps and r.created_by.is_superuser


def test_panel_block_conflict_flow(owner_client, ps, lane):
    day = next_weekday()
    r = book(ps, lane, day=day, start=10, end=11)
    data = {"title": "Zawody", "reason": "", "lanes": [lane.pk], "day_from": day.isoformat(), "day_to": day.isoformat(), "start_hour": 9, "end_hour": 18}
    response = owner_client.post("/panel/blokady/nowa/", data)
    assert response.status_code == 200 and "koliduje" in response.content.decode()
    assert not Block.objects.exists()
    owner_client.post("/panel/blokady/nowa/", {**data, "decision": "cancel"})
    r.refresh_from_db()
    assert Block.objects.count() == 1 and r.status == Reservation.Status.CANCELLED


def test_csv_export_escapes_formulas(owner_client, lane):
    evil = make_user("evil@example.com", first_name="=HYPERLINK(1)")
    add_credential(evil)
    book(evil, lane)
    response = owner_client.get("/panel/rezerwacje/lista/?format=csv")
    content = response.content.decode("utf-8-sig")
    assert response["Content-Type"].startswith("text/csv")
    assert "'=HYPERLINK" in content and ";=HYPERLINK" not in content


def test_notifications_panel(owner_client, owner, ps, lane, django_capture_on_commit_callbacks):
    r = book(ps, lane)
    n = Notification.objects.get(recipient=owner)
    assert "(1)" in owner_client.get("/pulpit/").content.decode()
    response = owner_client.get(f"/panel/powiadomienia/{n.pk}/")
    assert response.url == f"/panel/rezerwacje/{r.pk}/"
    n.refresh_from_db()
    assert n.read_at is not None


def test_booking_settings_panel(owner_client):
    owner_client.post("/panel/rezerwacje/ustawienia/", {"free_cancel_hours": 24, "max_advance_days": 60, "max_persons": 20})
    cfg = BookingSettings.load()
    assert (cfg.free_cancel_hours, cfg.max_advance_days, cfg.max_persons) == (24, 60, 20)


def test_slot_states_respect_dst_day(ps, lane):
    # Ostatnia niedziela października — zmiana czasu; godziny 9–18 muszą pozostać pełnymi godzinami lokalnymi.
    day = datetime.date(2026, 10, 25)
    slots = services.day_slots(lane, day, now=services.local_dt(day, 0) - timedelta(days=1))
    assert [s.hour for s in slots] == list(range(9, 18))
    assert all((s.end - s.start) == timedelta(hours=1) for s in slots)
