import datetime
import threading
from datetime import timedelta

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.db import connection
from django.utils import timezone

from apps.accounts.models import User
from apps.audit.models import AuditLog
from apps.bookings import services
from apps.bookings.models import Block, BookingSettings, Reservation
from apps.core.models import Notification
from apps.facilities.models import Lane
from apps.regulations.models import RegulationDocument, RegulationVersion

from .conftest import approve, add_credential, grant, login_with_otp, make_user

pytestmark = pytest.mark.django_db


def next_weekday(offset_days=7):
    return timezone.localdate() + timedelta(days=offset_days)


@pytest.fixture
def ps(db):
    user = make_user("ps@example.com")
    add_credential(user)
    approve(user)
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


def test_free_cancellation_before_24h(ps, lane):
    day = next_weekday(10)
    r = book(ps, lane, day=day)
    now = r.starts_at - timedelta(hours=24, minutes=1)
    r = services.cancel_reservation(r, ps, "zmiana planów", now=now)
    assert r.status == Reservation.Status.CANCELLED and not r.late_cancellation
    assert r.payment_status == Reservation.Payment.NONE


def test_late_cancellation_only_by_staff_optionally_keeping_fee(ps, lane, owner):
    """G5: klient anuluje sam do 24 h przed terminem; później tylko obsługa (bez opłaty albo z zachowaniem należności)."""
    r = book(ps, lane)
    now = r.starts_at - timedelta(hours=23)
    with pytest.raises(ValidationError, match="tylko obsługa"):
        services.cancel_reservation(r, ps, now=now)
    assert not r.can_user_cancel(now) and r.can_user_cancel(r.starts_at - timedelta(hours=25))
    r = services.cancel_reservation(r, owner, "późne odwołanie", as_board=True, now=now, keep_fee=True)
    assert r.late_cancellation and r.payment_status == Reservation.Payment.DUE


def test_user_cannot_cancel_started_or_foreign(ps, lane):
    r = book(ps, lane)
    with pytest.raises(ValidationError, match="tylko obsługa"):
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
    r = services.set_payment(r, "waive", owner)
    assert r.payment_status == Reservation.Payment.WAIVED
    with pytest.raises(ValidationError):
        services.set_payment(r, "waive", owner)
    r = services.set_payment(r, "unwaive", owner)
    assert r.payment_status == Reservation.Payment.DUE
    with pytest.raises(KeyError):
        services.set_payment(r, "paid", owner)  # opłacenie tylko przez wpłatę na saldo


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
    from apps.payments.services import balance_of, register_manual_payment

    register_manual_payment(ps, r.amount, "gotowka", User.objects.get(is_superuser=True), reservation=r)
    r.refresh_from_db()
    assert r.payment_status == Reservation.Payment.PAID and r.paid_amount == r.amount
    owner_client.post(f"/panel/rezerwacje/{r.pk}/", {"action": "persons", "p-persons": "5"})
    r.refresh_from_db()
    assert r.persons == 5 and r.amount == 2 * 20000
    assert r.payment_status == Reservation.Payment.DUE and balance_of(ps) == 0  # dopłata, saldo puste
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
    owner_client.post("/panel/rezerwacje/ustawienia/", {
        "free_cancel_hours": 12, "max_advance_days": 60, "max_persons": 20, "online_payment_minutes": 20, "approval_payment_hours": 12,
        "offline_instructions": "Przelew na konto.",
        **{f"{g}-{k}": "on" for g in ("czlonek", "powracajacy", "nowy") for k in ("pay_online",)},
        "czlonek-pay_pass": "on", "nowy-requires_approval": "on",
    })
    cfg = BookingSettings.load()
    assert (cfg.free_cancel_hours, cfg.max_advance_days, cfg.max_persons, cfg.online_payment_minutes) == (12, 60, 20, 20)
    from apps.bookings.models import GroupRule

    assert not GroupRule.for_group("powracajacy").pay_offline and GroupRule.for_group("czlonek").pay_pass
    # Grupa bez żadnego sposobu płatności jest odrzucana.
    response = owner_client.post("/panel/rezerwacje/ustawienia/", {
        "free_cancel_hours": 12, "max_advance_days": 60, "max_persons": 20, "online_payment_minutes": 20, "approval_payment_hours": 12,
        "czlonek-pay_online": "on", "powracajacy-pay_online": "on",
    })
    assert response.status_code == 200 and "co najmniej jeden sposób" in response.content.decode()


def test_slot_states_respect_dst_day(ps, lane):
    # Ostatnia niedziela października — zmiana czasu; godziny 9–18 muszą pozostać pełnymi godzinami lokalnymi.
    day = datetime.date(2026, 10, 25)
    slots = services.day_slots(lane, day, now=services.local_dt(day, 0) - timedelta(days=1))
    assert [s.hour for s in slots] == list(range(9, 18))
    assert all((s.end - s.start) == timedelta(hours=1) for s in slots)


def test_panel_overview_and_days_view(owner_client, ps, lane, owner):
    from apps.logbook.models import Entry

    day = next_weekday()
    r = book(ps, lane, day=day)
    home = owner_client.get("/panel/").content.decode()
    assert "Płatności" in home and "Nowa rezerwacja" in home and "Braki w książce pobytu" in home
    body = owner_client.get(f"/panel/rezerwacje/dniami/?od={day:%Y-%m-%d}").content.decode()
    assert r.number in body and "do zapłaty" in body
    assert r.number in owner_client.get("/panel/rezerwacje/dniami/?filtr=do_zaplaty").content.decode()
    assert r.number in owner_client.get(f"/panel/rezerwacje/dniami/?q={ps.last_name}").content.decode()
    assert r.number not in owner_client.get("/panel/rezerwacje/dniami/?filtr=braki_ksiega").content.decode()
    past = Reservation.objects.create(
        number="SATOK-R-TEST-9", lane=lane, user=ps, starts_at=timezone.now() - timedelta(days=1, hours=2),
        ends_at=timezone.now() - timedelta(days=1, hours=1), persons=1, price_rule=r.price_rule, hourly_amount=6000, amount=6000,
    )
    assert past.number in owner_client.get("/panel/rezerwacje/dniami/?filtr=braki_ksiega").content.decode()
    assert Entry.objects.count() == 0


# --- punkt 4: zasady grup klientów (G2–G5) ------------------------------------------------


@pytest.fixture
def newbie(db):
    user = make_user("nowy@example.com", first_name="Nowy", last_name="Klient")
    add_credential(user)
    return user


def test_new_client_reservation_waits_for_approval(newbie, lane, owner, client, django_capture_on_commit_callbacks):
    from django.core import mail

    with django_capture_on_commit_callbacks(execute=True):
        r = book(newbie, lane)
    assert r.status == Reservation.Status.PENDING_APPROVAL and r.client_group == "nowy" and r.pay_by is None
    assert any("czeka na zatwierdzenie" in m.subject for m in mail.outbox)
    # Termin jest zablokowany dla innych.
    with pytest.raises(ValidationError):
        book(owner_ps(), lane)
    # Klient może wycofać rezerwację czekającą na zatwierdzenie.
    assert r.can_user_cancel()
    client.force_login(newbie)
    body = client.get(f"/rezerwacje/{r.pk}/").content.decode()
    assert "czeka na zatwierdzenie" in body.lower() and "Zapłać online" not in body


def owner_ps():
    user = make_user("drugi@example.com")
    add_credential(user)
    return approve(user)


def test_panel_approve_sets_payment_deadline_for_online_only(newbie, lane, owner_client, settings):
    settings.TPAY_ENABLED = True
    r = book(newbie, lane)
    assert "Czekają na zatwierdzenie" in owner_client.get("/panel/").content.decode()
    assert r.number in owner_client.get("/panel/rezerwacje/dniami/?filtr=do_zatwierdzenia").content.decode()
    owner_client.post(f"/panel/rezerwacje/{r.pk}/", {"action": "approve"})
    r.refresh_from_db()
    assert r.status == Reservation.Status.CONFIRMED and r.approved_at and r.approved_by.is_superuser
    hours = BookingSettings.load().approval_payment_hours
    assert r.pay_by is not None and abs((r.pay_by - r.approved_at) - timedelta(hours=hours)) < timedelta(seconds=5)
    assert services.payment_options(r) == {"online": True, "offline": False, "instructions": BookingSettings.load().offline_instructions}


def test_panel_reject(newbie, lane, owner_client):
    r = book(newbie, lane)
    owner_client.post(f"/panel/rezerwacje/{r.pk}/", {"action": "reject", "c-reason": "brak miejsc"})
    r.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED and r.cancel_reason == "brak miejsc" and r.payment_status == Reservation.Payment.NONE


def test_online_only_group_expires_unpaid(newbie, lane, settings):
    from apps.bookings.models import GroupRule

    settings.TPAY_ENABLED = True
    GroupRule.objects.filter(group="nowy").update(requires_approval=False)
    r = book(newbie, lane)
    minutes = BookingSettings.load().online_payment_minutes
    assert r.status == Reservation.Status.CONFIRMED and r.pay_by is not None
    assert services.expire_unpaid(now=r.created_at + timedelta(minutes=minutes - 1)) == 0
    # Trwająca płatność Tpay wstrzymuje zwolnienie.
    from apps.payments.models import Payment

    pay = Payment.objects.create(user=newbie, amount=r.amount, method=Payment.Method.TPAY, reservation=r, title=r.number)
    assert services.expire_unpaid(now=r.pay_by + timedelta(minutes=1)) == 0
    Payment.objects.filter(pk=pay.pk).update(status=Payment.Status.FAILED)
    assert services.expire_unpaid(now=r.pay_by + timedelta(minutes=1)) == 1
    r.refresh_from_db()
    assert r.status == Reservation.Status.CANCELLED and "terminie" in r.cancel_reason


def test_no_expiry_without_tpay_or_for_offline_groups(ps, newbie, lane, settings):
    from apps.bookings.models import GroupRule

    settings.TPAY_ENABLED = False
    GroupRule.objects.filter(group="nowy").update(requires_approval=False)
    assert book(newbie, lane).pay_by is None  # bez Tpay nie zwalniamy
    settings.TPAY_ENABLED = True
    assert book(ps, lane, start=14, end=15).pay_by is None  # powracający: także offline


def test_staff_booking_ignores_group_rules(newbie, lane, owner):
    r = book(newbie, lane, created_by=owner)
    assert r.status == Reservation.Status.CONFIRMED and r.client_group == "" and r.pay_by is None
    assert services.payment_options(r)["offline"]


def test_tpay_refused_when_online_not_allowed(ps, lane, client, settings):
    from apps.bookings.models import GroupRule

    settings.TPAY_ENABLED = True
    GroupRule.objects.filter(group="powracajacy").update(pay_online=False)
    r = book(ps, lane)
    client.force_login(ps)
    body = client.get(f"/rezerwacje/{r.pk}/").content.decode()
    assert "Zapłać online" not in body and r.number in body
    response = client.post("/platnosci/tpay/start/", {"reservation": r.pk})
    assert response.status_code == 302 and response.url == f"/rezerwacje/{r.pk}/"


# --- lista kontrolna „co jeszcze, żeby zarezerwować” ------------------------------------------


def test_booking_readiness_steps(db):
    from apps.credentials.models import Credential

    u = make_user("gotowosc@example.com", verified=False)
    r = services.booking_readiness(u)
    states = {s["key"]: s["state"] for s in r["steps"]}
    assert not r["ready"] and states["email"] == "missing" and states["ps"] == "missing" and states["profile"] == "tip"
    u.email_verified_at = timezone.now()
    u.save()
    c = add_credential(u, status=Credential.Status.PENDING)
    states = {s["key"]: s["state"] for s in services.booking_readiness(u)["steps"]}
    assert states["email"] == "done" and states["ps"] == "waiting"
    Credential.objects.filter(pk=c.pk).update(status=Credential.Status.VERIFIED)
    r = services.booking_readiness(u)
    # Gotowość zgodna z check_can_book (dane kontaktowe są tylko zalecane).
    assert r["ready"] == (not services.check_can_book(u))


def test_dashboard_and_cannot_book_show_checklist(client):
    u = make_user("nowy-klient@example.com")
    client.force_login(u)
    body = client.get("/pulpit/").content.decode()
    assert "Co jeszcze, żeby zarezerwować" in body and "Dodaj uprawnienia PS" in body and "?rodzaj=ps" in body
    body = client.get("/rezerwacje/nowa/").content.decode()
    assert "Nie możesz jeszcze rezerwować" in body and "Uprawnienia prowadzącego strzelanie" in body
    form = client.get("/dokumenty/dodaj/?rodzaj=ps").content.decode()
    assert 'value="ps"' in form and 'checked' in form.split('value="ps"')[1].split('>')[0]


def test_dashboard_ready_state(client, ps):
    client.force_login(ps)
    body = client.get("/pulpit/").content.decode()
    assert "Możesz rezerwować osie" in body


def test_slots_json_and_new_form(client, ps, lane):
    day = next_weekday()
    book(ps, lane, day=day, start=10, end=12, persons=1)
    data = client.get(f"/rezerwacje/sloty/?os={lane.pk}&dzien={day.isoformat()}").json()
    states = {s["hour"]: s["state"] for s in data["slots"]}
    assert states[10] == "zajete" and states[11] == "zajete" and states[12] == "wolne"
    assert data["price"]["flat"] == 20000 and data["price"]["per_person"] == 6000
    body = str(data)
    assert "ps@example.com" not in body and "Kowalski" not in body  # bez danych osobowych
    assert client.get("/rezerwacje/sloty/?os=99999").status_code == 404
    client.force_login(ps)
    page = client.get(f"/rezerwacje/nowa/?os={lane.pk}&dzien={day.isoformat()}").content.decode()
    assert 'class="lane-card"' in page and "Wybierz godziny" in page and "data-slots-url" in page


def test_reservation_page_calendar_and_share(client, ps, lane):
    r = book(ps, lane)
    client.force_login(ps)
    body = client.get(f"/rezerwacje/{r.pk}/").content.decode()
    assert "Rezerwacja potwierdzona" in body and "Dodaj do kalendarza" in body and "calendar.google.com" in body
    ics = client.get(f"/rezerwacje/{r.pk}/kalendarz.ics")
    text = ics.content.decode()
    assert ics["Content-Type"].startswith("text/calendar") and "BEGIN:VEVENT" in text and r.number in text
    assert f"DTSTART:{r.starts_at.astimezone(datetime.timezone.utc):%Y%m%dT%H%M%SZ}" in text and "\r\n" in text
    other = make_user("obcy@example.com")
    client.force_login(other)
    assert client.get(f"/rezerwacje/{r.pk}/kalendarz.ics").status_code == 404
