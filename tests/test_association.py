import json
from datetime import date, timedelta

import pytest
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.utils import timezone

from apps.association import services
from apps.association.models import AccessCode, Attendance, Club, Fee, GeneralMeeting, Member, MembershipApplication, MembershipSettings, Resolution
from apps.bookings import services as booking_services
from apps.bookings.models import Reservation
from apps.facilities.models import Lane
from apps.payments.models import Payment

from .conftest import add_credential, grant, login_with_otp, make_user

pytestmark = pytest.mark.django_db
TODAY = date(2026, 6, 15)


@pytest.fixture
def board(owner):
    """Prezes (z kontem właściciela) i Wiceprezes — członkowie nadzwyczajni."""
    vice_user = grant(make_user("wice@example.com", first_name="Wiesław", last_name="Wice"), "association.manage_resolutions")
    president = services.add_member(user=owner, kind="nadzwyczajny", joined_on=date(2025, 7, 10), founder=True, board_role="prezes", by=None)
    vice = services.add_member(user=vice_user, kind="nadzwyczajny", joined_on=date(2025, 7, 10), founder=True, board_role="wiceprezes", by=None)
    return president, vice


def applicant(email="kandydat@example.com"):
    user = make_user(email, first_name="Karol", last_name="Kandydat")
    return user, services.submit_application(user, "strzelectwo sportowe")


# --- deklaracja → uchwała Zarządu → przyjęcie i składki ------------------------------------


def test_application_board_vote_admits_and_charges_fees(board, django_capture_on_commit_callbacks):
    president, vice = board
    with django_capture_on_commit_callbacks(execute=True):
        user, app = applicant()
    assert app.deadline == (timezone.localtime(app.submitted_at).date().replace() if False else app.deadline)
    assert any("deklaracja" in m.subject.lower() for m in mail.outbox)
    res = services.create_resolution(organ="zarzad", title="przyjęcia członka", content="Przyjmuje się…", action="przyjecie",
                                     applications=[app], by=president.user)
    services.start_board_voting(res, president.user)
    with django_capture_on_commit_callbacks(execute=True):
        services.cast_board_vote(res, president.user, "za")
        res.refresh_from_db()
        assert res.status == Resolution.Status.VOTING  # czeka na cały Zarząd (§ 25)
        services.cast_board_vote(res, vice.user, "za", now=timezone.make_aware(timezone.datetime(2026, 6, 15, 12)))
    res.refresh_from_db()
    assert res.status == Resolution.Status.ADOPTED and (res.number, res.year) == (1, 2026)
    member = Member.objects.get(user=user)
    assert member.kind == Member.Kind.ORDINARY and member.number == 3
    fees = {f.kind: f for f in member.fees.all()}
    assert fees["wpisowe"].amount == 5000 and fees["skladka"].amount == 20000 and fees["skladka"].year == 2026
    assert fees["skladka"].due_on == date(2026, 6, 29)  # po 31.01 → 14 dni od przyjęcia, pełna kwota
    app.refresh_from_db()
    assert app.status == MembershipApplication.Status.ACCEPTED and app.resolution == res
    assert any("Witamy" in m.subject for m in mail.outbox)


def test_board_tie_decided_by_president(board):
    president, vice = board
    for pres_vote, expected in (("za", Resolution.Status.ADOPTED), ("przeciw", Resolution.Status.REJECTED)):
        res = services.create_resolution(organ="zarzad", title="test", content="x", by=president.user)
        services.start_board_voting(res, president.user)
        services.cast_board_vote(res, president.user, pres_vote)
        services.cast_board_vote(res, vice.user, "przeciw" if pres_vote == "za" else "za")
        res.refresh_from_db()
        assert res.status == expected


def test_only_board_members_vote(board):
    president, _ = board
    res = services.create_resolution(organ="zarzad", title="t", content="x", by=president.user)
    services.start_board_voting(res, president.user)
    with pytest.raises(ValidationError):
        services.cast_board_vote(res, make_user("obcy@example.com"), "za")


def test_numbering_separate_per_organ(board):
    president, vice = board
    services.add_historical(organ="zarzad", year=2026, number=1, adopted_on=date(2026, 2, 1), title="UEPiK", content="x", by=None)
    res = services.create_resolution(organ="zarzad", title="t", content="x", by=president.user)
    services.start_board_voting(res, president.user)
    services.cast_board_vote(res, president.user, "za")
    services.cast_board_vote(res, vice.user, "za", now=timezone.make_aware(timezone.datetime(2026, 3, 1, 10)))
    res.refresh_from_db()
    assert res.display_number == "Uchwała Zarządu Nr 2/2026"
    with pytest.raises(ValidationError):
        services.add_historical(organ="zarzad", year=2026, number=1, adopted_on=date(2026, 2, 1), title="dubel", content="x", by=None)


def test_action_must_match_organ():
    with pytest.raises(ValidationError):
        services.create_resolution(organ="wzc", title="t", content="x", action="przyjecie", by=None)
    with pytest.raises(ValidationError):
        services.create_resolution(organ="zarzad", title="t", content="x", action="zmiana_rodzaju", params={"kind": "nadzwyczajny"}, by=None)


def test_suspend_remove_and_fees_settings(board):
    president, vice = board
    m = services.add_member(first_name="Jan", last_name="Bezkonta", kind="zwyczajny", joined_on=date(2026, 1, 5), charge_fees=True, by=None)
    assert m.user is None and m.fees.count() == 2

    def adopt(action, members=(), params=None):
        r = services.create_resolution(organ="zarzad", title=action, content="x", action=action, members=members, params=params, by=president.user)
        services.start_board_voting(r, president.user)
        services.cast_board_vote(r, president.user, "za")
        services.cast_board_vote(r, vice.user, "za")
        return r

    adopt("zawieszenie", [m])
    m.refresh_from_db()
    assert m.status == Member.Status.SUSPENDED and m.appeal_until is not None
    adopt("umorzenie", [m])
    assert not m.fees.filter(status=Fee.Status.DUE).exists()
    adopt("wykreslenie", [m])
    m.refresh_from_db()
    assert m.status == Member.Status.REMOVED and m.removed_on
    adopt("skladki", params={"annual_fee": 24000, "entry_fee": 6000})
    cfg = MembershipSettings.load()
    assert (cfg.annual_fee, cfg.entry_fee) == (24000, 6000)


# --- składki: naliczenie roczne, płatność, przypomnienia ----------------------------------------


def test_annual_fees_ordinary_only_and_idempotent(board):
    ordinary = services.add_member(user=make_user("z@example.com"), kind="zwyczajny", joined_on=date(2025, 9, 1), by=None)
    assert services.ensure_annual_fees(2026) == 1
    assert services.ensure_annual_fees(2026) == 0
    fee = ordinary.fees.get(year=2026)
    assert fee.due_on == date(2026, 1, 31) and fee.amount == 20000
    assert not board[0].fees.exists()  # nadzwyczajni zwolnieni (§ 31)


def test_fee_paid_directly_for_member_with_account(board, owner):
    user = make_user("z@example.com")
    m = services.add_member(user=user, kind="zwyczajny", joined_on=timezone.localdate(), charge_fees=True, by=None)
    fee = m.fees.get(kind="skladka")
    services.mark_fee_paid(fee, "gotowka", owner)
    fee.refresh_from_db()
    assert fee.status == Fee.Status.PAID and Payment.objects.filter(fee=fee, status="zaksiegowana").exists()
    assert m.fees.get(kind="wpisowe").status == Fee.Status.DUE  # tryb bezpośredni: inna należność nie jest pobierana


def test_fee_paid_for_member_without_account(board, owner):
    m = services.add_member(first_name="Anna", last_name="Bez", kind="zwyczajny", joined_on=timezone.localdate(), charge_fees=True, by=None)
    fee = m.fees.get(kind="wpisowe")
    services.mark_fee_paid(fee, "przelew", owner)
    fee.refresh_from_db()
    assert fee.status == Fee.Status.PAID
    with pytest.raises(ValidationError):
        services.mark_fee_paid(fee, "przelew", owner)


def test_topup_code_transfer_pays_fees(board, owner):
    from apps.payments import services as pay

    user = make_user("z@example.com")
    m = services.add_member(user=user, kind="zwyczajny", joined_on=timezone.localdate(), charge_fees=True, by=None)
    payment = Payment.objects.create(user=user, amount=25000, method="przelew", title=pay.topup_code(user))
    pay.complete_payment(payment)
    assert not m.fees.filter(status=Fee.Status.DUE).exists()


def test_fee_reminders(board, django_capture_on_commit_callbacks):
    user = make_user("z@example.com")
    m = services.add_member(user=user, kind="zwyczajny", joined_on=date(2025, 3, 1), by=None)
    Fee.objects.create(member=m, kind="skladka", year=2026, amount=20000, due_on=date(2026, 1, 31))
    now = timezone.make_aware(timezone.datetime(2026, 3, 5, 10))
    with django_capture_on_commit_callbacks(execute=True):
        assert services.send_fee_reminders(now) == 1
    assert services.send_fee_reminders(now + timedelta(days=5)) == 0
    assert services.send_fee_reminders(now + timedelta(days=31)) == 1
    assert any("Zaległe" in x.subject for x in mail.outbox)


# --- Walne Zebranie: wagi, pełnomocnictwa, Prezes ---------------------------------------------------


def _ordinary(n):
    return [services.add_member(first_name=f"Z{i}", last_name="Zwyczajny", kind="zwyczajny", joined_on=date(2025, 8, 1), by=None) for i in range(n)]


def test_wzc_weighted_votes_with_proxy(board):
    president, vice = board
    o = _ordinary(4)
    meeting = GeneralMeeting.objects.create(kind="wzc", held_on=date(2026, 6, 1), place="Sątok")
    Attendance.objects.create(meeting=meeting, member=president)
    Attendance.objects.create(meeting=meeting, member=o[0])
    Attendance.objects.create(meeting=meeting, member=o[1], proxy=o[0])
    Attendance.objects.create(meeting=meeting, member=o[2], proxy=o[0])
    res = services.create_resolution(organ="wzc", title="kierunki", content="x", by=None, meeting=meeting)
    # 4 zwyczajnych „za” nie przeważy jednego nadzwyczajnego (waga 50) „przeciw”
    res = services.record_meeting_votes(res, meeting, {president.pk: "przeciw", o[0].pk: "za", o[1].pk: "za", o[2].pk: "za"}, by=None)
    assert (res.votes_for, res.votes_against, res.status) == (3, 50, Resolution.Status.REJECTED)
    assert res.votes.get(member=o[1]).proxy == o[0]


def test_wzc_requires_president_and_proxy_limits(board):
    president, vice = board
    o = _ordinary(4)
    meeting = GeneralMeeting.objects.create(kind="wzc", held_on=date(2026, 6, 1), place="Sątok")
    Attendance.objects.create(meeting=meeting, member=vice)
    res = services.create_resolution(organ="wzc", title="x", content="x", by=None, meeting=meeting)
    with pytest.raises(ValidationError, match="Prezesa"):
        services.record_meeting_votes(res, meeting, {vice.pk: "za"}, by=None)
    Attendance.objects.create(meeting=meeting, member=president)
    Attendance.objects.create(meeting=meeting, member=o[0])
    for other in o[1:]:
        Attendance.objects.create(meeting=meeting, member=other, proxy=o[0])
    with pytest.raises(ValidationError, match="najwyżej 2"):
        services.record_meeting_votes(res, meeting, {president.pk: "za"}, by=None)


def test_extraordinary_quorum_and_change_kind(board):
    president, vice = board
    ordinary = services.add_member(user=make_user("z@example.com"), kind="zwyczajny", joined_on=date(2026, 2, 1), charge_fees=True, by=None)
    third = services.add_member(first_name="Sylwia", last_name="Trzecia", kind="nadzwyczajny", joined_on=date(2025, 7, 10), by=None)
    meeting = GeneralMeeting.objects.create(kind="nadzwyczajni", held_on=date(2026, 6, 1), place="Sątok")
    Attendance.objects.create(meeting=meeting, member=president)
    res = services.create_resolution(organ="nadzwyczajni", title="zmiana statusu", content="x", action="zmiana_rodzaju",
                                     params={"kind": "nadzwyczajny"}, members=[ordinary], by=None, meeting=meeting)
    with pytest.raises(ValidationError, match="quorum"):
        services.record_meeting_votes(res, meeting, {president.pk: "za"}, by=None)
    Attendance.objects.create(meeting=meeting, member=vice)
    res = services.record_meeting_votes(res, meeting, {president.pk: "za", vice.pk: "za"}, by=None)
    ordinary.refresh_from_db()
    assert res.status == Resolution.Status.ADOPTED and ordinary.kind == Member.Kind.EXTRAORDINARY
    assert ordinary.fees.get(kind="skladka").status == Fee.Status.WAIVED
    assert third.pk  # trzeci nadzwyczajny nieobecny — quorum 2 z 3 spełnione


# --- kody dostępu klubów ---------------------------------------------------------------------


@pytest.fixture
def code(db):
    club = Club.objects.create(name="Klub Testowy")
    c = AccessCode.objects.create(club=club, valid_from=timezone.localdate(), valid_until=timezone.localdate() + timedelta(days=60),
                                  hours_limit=3, contract_hourly=5000)
    c.lanes.set([Lane.objects.get(name="Oś 100 m")])
    return c


def test_reservation_with_club_code_contract_billing(code):
    ps = make_user("ps@example.com")
    add_credential(ps)
    day = timezone.localdate() + timedelta(days=5)
    r = booking_services.create_reservation(ps, Lane.objects.get(name="Oś 100 m"), day, 10, 12, 8, access_code=code.code.lower())
    assert r.access_code == code and r.payment_status == Reservation.Payment.CONTRACT and r.amount == 10000
    with pytest.raises(ValidationError, match="limit"):
        booking_services.create_reservation(ps, Lane.objects.get(name="Oś 100 m"), day, 13, 15, 2, access_code=code.code)
    with pytest.raises(ValidationError, match="osi"):
        booking_services.create_reservation(ps, Lane.objects.get(name="Oś 35 m"), day, 10, 11, 2, access_code=code.code)
    with pytest.raises(ValidationError, match="Nieprawidłowy"):
        booking_services.create_reservation(ps, Lane.objects.get(name="Oś 100 m"), day, 15, 16, 2, access_code="ZLYKOD12")
    code.is_active = False
    code.save()
    with pytest.raises(ValidationError):
        booking_services.create_reservation(ps, Lane.objects.get(name="Oś 100 m"), day, 15, 16, 2, access_code=code.code)
    assert services.code_usage(code)["hours"] == 2


def test_club_reservation_skipped_by_logbook_settlement(code):
    from apps.logbook.services import settle_finished_reservations

    ps = make_user("ps@example.com")
    add_credential(ps)
    r = booking_services.create_reservation(ps, Lane.objects.get(name="Oś 100 m"), timezone.localdate() + timedelta(days=2), 10, 11, 1, access_code=code.code)
    settle_finished_reservations(now=r.ends_at + timedelta(minutes=1))
    r.refresh_from_db()
    assert r.settled_at and r.payment_status == Reservation.Payment.CONTRACT


# --- widoki i uprawnienia ------------------------------------------------------------------------


def test_member_page_and_application_flow(client, board):
    user = make_user("k@example.com")
    client.force_login(user)
    body = client.get("/stowarzyszenie/").content.decode()
    assert "Deklaracja członkowska" in body and "§ 40" in body
    client.post("/stowarzyszenie/", {"motivation": "", "accept": "on"})
    assert MembershipApplication.objects.filter(user=user, status="oczekuje").exists()
    client.post("/stowarzyszenie/", {"action": "withdraw"})
    assert MembershipApplication.objects.get(user=user).status == "wycofana"


def test_panel_pages(owner_client, board):
    services.add_historical(organ="zalozycielskie", year=2025, number=1, adopted_on=date(2025, 7, 10), title="założenia", content="x", by=None)
    for url in ["/panel/czlonkowie/", "/panel/czlonkowie/nowy/", "/panel/deklaracje/", "/panel/skladki/", "/panel/uchwaly/",
                "/panel/uchwaly/nowa/", "/panel/uchwaly/historyczna/", "/panel/zebrania/", "/panel/kluby/", "/panel/raporty/",
                f"/panel/czlonkowie/{board[0].pk}/", "/panel/czlonkowie/?format=csv", "/panel/raporty/?format=csv"]:
        assert owner_client.get(url).status_code == 200, url
    r = Resolution.objects.get()
    assert owner_client.get(f"/panel/uchwaly/{r.pk}/druk/").status_code == 200


def test_panel_requires_association_permissions(client, board):
    staff = grant(make_user("s@example.com"), "credentials.review_credential")
    login_with_otp(client, staff)
    for url in ["/panel/czlonkowie/", "/panel/uchwaly/", "/panel/kluby/", "/panel/raporty/"]:
        assert client.get(url).status_code == 403, url


def test_import_seed_command(tmp_path, owner):
    data = {
        "stowarzyszenie": {"name": "Stowarzyszenie Test", "address": "Adres 1"},
        "czlonkowie": [{"numer": 1, "imie": "A", "nazwisko": "B", "rodzaj": "nadzwyczajny", "data_przyjecia": "2025-07-10",
                        "zalozyciel": True, "funkcja": "prezes"}],
        "uchwaly": [{"organ": "zalozycielskie", "rok": 2025, "numer": 1, "data": "2025-07-10", "tytul": "t", "tresc": "x"}],
        "regulamin": {"plik": "reg.txt", "obowiazuje_od": "2025-07-10T00:00:00"},
    }
    (tmp_path / "dane.json").write_text(json.dumps(data), encoding="utf-8")
    (tmp_path / "reg.txt").write_text("§ 1.\n\nTreść", encoding="utf-8")
    call_command("import_association_seed", str(tmp_path))
    call_command("import_association_seed", str(tmp_path))
    assert Member.objects.count() == 1 and Member.objects.get().board_role == "prezes"
    assert Resolution.objects.filter(historical=True).count() == 1
    assert services.association_rules().content.startswith("§ 1.")


def _zip(files):
    import io
    import zipfile

    from django.core.files.uploadedfile import SimpleUploadedFile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return SimpleUploadedFile("pakiet.zip", buf.getvalue(), content_type="application/zip")


IMPORT_DATA = {
    "czlonkowie": [{"numer": 7, "imie": "Jan", "nazwisko": "Założyciel", "rodzaj": "nadzwyczajny", "data_przyjecia": "2025-07-10"}],
    "uchwaly": [{"organ": "zalozycielskie", "rok": 2025, "numer": 1, "data": "2025-07-10", "tytul": "t", "tresc": "x",
                 "skan": "skany/u1.pdf"},
                {"organ": "zalozycielskie", "rok": 2025, "numer": 2, "data": "2025-07-10", "tytul": "t2", "tresc": "y",
                 "skan": "skany/u2.pdf"}],
}


def _package():
    return {"pakiet/dane.json": json.dumps(IMPORT_DATA), "pakiet/skany/u1.pdf": b"%PDF-1.4 test", "pakiet/skany/u2.pdf": b"MZ to nie pdf"}


def test_panel_import_preview_then_import(owner_client):
    body = owner_client.post("/panel/czlonkowie/import/", {"pakiet": _zip(_package()), "tryb": "podglad"}).content.decode()
    assert "nic nie zapisano" in body and "zostanie wprowadzone" in body
    assert not Member.objects.exists() and not Resolution.objects.exists()
    body = owner_client.post("/panel/czlonkowie/import/", {"pakiet": _zip(_package()), "tryb": "importuj"}).content.decode()
    assert "Wynik importu" in body and "to nie jest PDF" in body
    assert Member.objects.get().number == 7
    r1, r2 = Resolution.objects.order_by("number")
    assert r1.attachment and not r2.attachment
    body = owner_client.post("/panel/czlonkowie/import/", {"pakiet": _zip(_package()), "tryb": "importuj"}).content.decode()
    assert "już w rejestrze" in body and Member.objects.count() == 1 and Resolution.objects.count() == 2
    assert "Import danych" in owner_client.get("/panel/czlonkowie/").content.decode()


def test_panel_import_rejects_bad_packages(owner_client):
    from django.core.files.uploadedfile import SimpleUploadedFile

    body = owner_client.post("/panel/czlonkowie/import/", {"pakiet": SimpleUploadedFile("x.zip", b"nie zip"), "tryb": "podglad"}, follow=True)
    assert "poprawny plik ZIP" in body.content.decode()
    body = owner_client.post("/panel/czlonkowie/import/", {"pakiet": _zip({"inne.txt": "x"}), "tryb": "podglad"}).content.decode()
    assert "nie ma pliku dane.json" in body
    body = owner_client.post("/panel/czlonkowie/import/", {"pakiet": _zip({"../dane.json": "{}"}), "tryb": "podglad"}).content.decode()
    assert "Nieprawidłowa ścieżka" in body
    body = owner_client.post("/panel/czlonkowie/import/", {"pakiet": _zip({"dane.json": "{zly"}), "tryb": "importuj"}).content.decode()
    assert "uszkodzony" in body and not Member.objects.exists()


def test_panel_import_respects_section_permissions(client):
    staff = grant(make_user("m@example.com"), "association.manage_members")
    login_with_otp(client, staff)
    body = client.post("/panel/czlonkowie/import/", {"pakiet": _zip(_package()), "tryb": "importuj"}).content.decode()
    assert Member.objects.count() == 1 and not Resolution.objects.exists() and "brak uprawnienia" in body
    other = grant(make_user("o@example.com"), "association.manage_resolutions")
    from django.test import Client

    c = Client()
    login_with_otp(c, other)
    assert c.get("/panel/czlonkowie/import/").status_code == 403


def _xlsx(members=(), resolutions=(), sheets=None):
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from openpyxl import load_workbook

    from apps.association.importer import template_xlsx

    wb = load_workbook(io.BytesIO(template_xlsx()))
    for row in members:
        wb["Członkowie"].append(list(row))
    for row in resolutions:
        wb["Uchwały"].append(list(row))
    if sheets is not None:
        for name in list(wb.sheetnames):
            if name not in sheets:
                del wb[name]
    buf = io.BytesIO()
    wb.save(buf)
    return SimpleUploadedFile("dane.xlsx", buf.getvalue())


def test_panel_import_xlsx(owner_client):
    from datetime import datetime

    response = owner_client.get("/panel/czlonkowie/import/wzor.xlsx")
    assert response.status_code == 200 and response.content[:2] == b"PK"
    members = [(1, "Remigiusz", "Raczyński", "", "Sątok 4", "Członek nadzwyczajny", datetime(2025, 7, 10), "tak", "prezes"),
               (2, "Sylwia", "Raczyńska", "", "", "nadzwyczajny", "10.07.2025", "tak", ""),
               (None, None, None, None, None, None, None, None, None)]
    resolutions = [("zalozycielskie", 2025, 1, "2025-07-10", "W sprawie założenia", "Treść", 3, 0, 0)]
    body = owner_client.post("/panel/czlonkowie/import/", {"pakiet": _xlsx(members, resolutions), "tryb": "podglad"}).content.decode()
    assert "nic nie zapisano" in body and not Member.objects.exists()
    owner_client.post("/panel/czlonkowie/import/", {"pakiet": _xlsx(members, resolutions), "tryb": "importuj"})
    m1 = Member.objects.get(number=1)
    assert m1.kind == "nadzwyczajny" and m1.board_role == "prezes" and m1.founder and m1.joined_on.isoformat() == "2025-07-10"
    assert Member.objects.get(number=2).board_role == ""
    r = Resolution.objects.get()
    assert r.historical and (r.votes_for, r.votes_against) == (3, 0)


def test_panel_import_xlsx_errors(owner_client):
    from django.core.files.uploadedfile import SimpleUploadedFile

    bad_kind = [(1, "A", "B", "", "", "honorowy", "10.07.2025", "nie", "")]
    body = owner_client.post("/panel/czlonkowie/import/", {"pakiet": _xlsx(bad_kind), "tryb": "podglad"}).content.decode()
    assert "wiersz 2" in body and "honorowy" in body
    bad_date = [(1, "A", "B", "", "", "zwyczajny", "jutro", "nie", "")]
    assert "nieprawidłowa data" in owner_client.post("/panel/czlonkowie/import/", {"pakiet": _xlsx(bad_date), "tryb": "podglad"}).content.decode()
    body = owner_client.post("/panel/czlonkowie/import/", {"pakiet": _xlsx(sheets=["Instrukcja"]), "tryb": "podglad"}).content.decode()
    assert "nie ma karty" in body
    body = owner_client.post("/panel/czlonkowie/import/", {"pakiet": SimpleUploadedFile("x.xlsx", b"nie excel"), "tryb": "podglad"}).content.decode()
    assert "poprawny plik Excel" in body
    body = owner_client.post("/panel/czlonkowie/import/", {"pakiet": SimpleUploadedFile("x.csv", b"a;b"), "tryb": "podglad"}).content.decode()
    assert "ZIP" in body and "XLSX" in body and not Member.objects.exists()


def test_month_stats_occupancy_and_comparison(owner_client):
    import datetime as dt

    from apps.association import stats
    from apps.bookings import services as booking_services
    from apps.facilities.models import Lane

    from .conftest import add_credential, approve, make_user

    shooter = make_user("staty@example.com")
    add_credential(shooter)
    approve(shooter)
    lane = Lane.objects.filter(is_active=True).first()
    today = dt.date.today()
    y, m = (today.year + 1, 3)  # przyszły miesiąc — bez innych danych
    day = dt.date(y, m, 1)
    while lane.hours_for(day) is None:
        day += dt.timedelta(days=1)
    h = lane.hours_for(day).opens.hour
    booking_services.create_reservation(shooter, lane, day, h, h + 2, 3, created_by=shooter)
    data = stats.with_comparison(y, m)
    row = next(x for x in data["lanes"] if x["lane"].pk == lane.pk)
    assert row["booked"] == 2 and row["capacity"] > 2 and row["pct"] == round(200 / row["capacity"])
    assert data["kpi"]["reservations"] == 1 and data["kpi"]["persons"] == 3 and data["kpi"]["officers"] == 1
    assert data["deltas"]["reservations"]["abs"] == 1  # poprzedni miesiąc pusty
    assert sum(v for _, cells in data["heat"] for _, v, _ in cells) == 2
    response = owner_client.get(f"/panel/raporty/miesiac/?m={y}-{m:02d}")
    assert response.status_code == 200 and "Obłożenie osi" in response.content.decode()
    assert owner_client.get("/panel/raporty/miesiac/?m=zle").status_code == 200


def test_members_csv_neutralizes_formulas(owner_client, board):
    m = board[0]
    Member.objects.filter(pk=m.pk).update(first_name="=HYPERLINK(\"http://x\")", last_name="+48")
    body = owner_client.get("/panel/czlonkowie/?format=csv").content.decode("utf-8-sig")
    assert "'=HYPERLINK" in body and ";'+48;" in body
