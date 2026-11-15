"""Zestawienie uzupełniające (§ 4 rozporządzenia, Dz. U. 2025 poz. 1880): formy działalności (art. 10 ust. 1 udpp),
umowy o realizację zadania publicznego (art. 16 ust. 5), darowizny rzeczowe i limit przychodów (art. 10a ust. 1 pkt 5)."""

import io
from datetime import date, timedelta

import pytest
from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.accounting import documents, services, supplementary
from apps.accounting.models import AccountingSettings, Activity, InKindDonation, PublicTask, Record

pytestmark = pytest.mark.django_db


def _rec(owner, kind, amount, column, day=date(2026, 3, 1), **kw):
    return services.add_record(kind=kind, day=day, document="D", description=kw.pop("description", "zapis"), amount=amount,
                               column=column, by=owner, **kw)


def _task(owner, **kw):
    data = {"name": "Szkolenie młodzieży w strzelectwie", "agreement": "1/2026", "grantor": "Gmina Testowo",
            "signed_on": date(2026, 2, 1), "starts_on": date(2026, 2, 1), "ends_on": date(2026, 11, 30), "grant": 500_000, "notes": ""}
    data.update(kw)
    return supplementary.save_task(data, by=owner)


def test_activities_tasks_and_unassigned(owner):
    task = _task(owner)
    _rec(owner, Record.Kind.REVENUE, 500_000, "revenue", description="Dotacja", activity=Activity.UNPAID, task=task)
    _rec(owner, Record.Kind.COST, 120_000, "cost_other", description="Amunicja na szkolenie", activity=Activity.UNPAID, task=task)
    _rec(owner, Record.Kind.REVENUE, 300_000, "revenue", description="Opłaty za osie", activity=Activity.OTHER)
    _rec(owner, Record.Kind.COST, 50_000, "cost_deductible", description="Prąd")
    st = supplementary.build(2026)
    by = {a["key"]: a for a in st["activities"]}
    assert by["nieodplatna"]["revenue"] == 500_000 and by["nieodplatna"]["cost_other"] == 120_000 and by["nieodplatna"]["result"] == 380_000
    assert by["pozostala"]["result"] == 300_000
    assert st["unassigned_count"] == 1 and st["unassigned"]["cost_deductible"] == 50_000
    assert st["total"]["result"] == 500_000 + 300_000 - 120_000 - 50_000
    t = st["tasks"][0]
    assert t["task"] == task and t["year"]["costs"] == 120_000 and t["left"] == 380_000


def test_classify_creates_versions_and_respects_closed_year(owner):
    a = _rec(owner, Record.Kind.COST, 1000, "cost_deductible")
    b = _rec(owner, Record.Kind.COST, 2000, "cost_deductible")
    task = _task(owner)
    n = services.classify(Record.objects.filter(pk__in=[a.pk, b.pk]), by=owner, activity=Activity.COMMON, task=task)
    assert n == 2
    a.refresh_from_db()
    assert a.activity == "wspolne" and a.task == task and a.version == 2
    hist = services.history(a)
    assert hist[-1]["v"].reason == "przypisanie do zestawienia uzupełniającego"
    assert {f for f, _, _ in hist[-1]["changes"]} == {"activity", "task_id"}
    assert services.classify(Record.objects.filter(pk=a.pk), by=owner, activity=Activity.COMMON) == 0  # bez zmian — bez nowej wersji
    services.classify(Record.objects.filter(pk=a.pk), by=owner, task=None)
    a.refresh_from_db()
    assert a.task is None and a.activity == "wspolne"
    with pytest.raises(ValidationError, match="forma"):
        _rec(owner, Record.Kind.COST, 10, "cost_deductible", activity="zla")


def test_in_kind_donations_and_revenue_limit(owner):
    _rec(owner, Record.Kind.REVENUE, 95_000_000, "revenue", day=date(2025, 6, 1))
    d = supplementary.add_donation({"date": date(2025, 7, 1), "description": "Kontener biurowy", "value": 10_000_000,
                                    "document": "umowa darowizny 1/2025", "statutory": True, "activity": Activity.UNPAID},
                                   by=owner, add_revenue=True)
    assert d.record.revenue == 10_000_000 and d.record.activity == "nieodplatna"
    supplementary.add_donation({"date": date(2025, 8, 1), "description": "Tarcze", "value": 200_000, "statutory": False}, by=owner)
    st = supplementary.build(2025)
    assert st["total"]["revenue"] == 105_000_000 and st["donations_total"] == 10_200_000 and st["donations_statutory"] == 10_000_000
    assert st["limit_base"] == 95_000_000 and st["limit_ok"]
    assert supplementary.revenue_for_limit(2025) == 95_000_000
    # uchwała o wyborze ewidencji podpowiada przychody roku poprzedniego bez darowizn rzeczowych
    ctx = documents.context(owner)
    assert ctx["revenue_by_year"][2025] == 95_000_000
    with pytest.raises(ValidationError, match="przyczynę"):
        supplementary.delete_donation(d, by=owner, reason="")
    supplementary.delete_donation(d, by=owner, reason="pomyłka")
    assert not supplementary.build(2025)["limit_ok"]  # 105 000 000 gr > limit
    with pytest.raises(ValidationError, match="przyszłości"):
        supplementary.add_donation({"date": timezone.localdate() + timedelta(days=1), "description": "x", "value": 1}, by=owner)


def test_sync_uses_configured_activity(owner):
    from apps.payments.models import Payment

    from .conftest import make_user
    from .test_reports import _pay

    shooter = make_user("klient@example.com")
    day = timezone.localdate() - timedelta(days=2)
    _pay(shooter, owner, 5000, Payment.Method.CASH, day)
    s = AccountingSettings.load()
    s.auto_from, s.auto_activity = day, Activity.OTHER
    s.save()
    rec = services.sync_from_sales_reports()[0]
    assert rec.activity == "pozostala"


def test_panel_supplementary_flow_and_exports(owner_client, owner):
    from openpyxl import load_workbook

    r = owner_client.post("/panel/ksiegowosc/umowy/nowa/", {"name": "Zawody dla młodzieży", "agreement": "2/2026", "grantor": "Powiat",
                                                           "signed_on": "2026-03-01", "starts_on": "2026-03-01", "ends_on": "2026-09-30",
                                                           "grant": "3 000,00", "notes": ""})
    assert r.status_code == 302
    task = PublicTask.objects.get()
    assert task.grant == 300_000
    a = _rec(owner, Record.Kind.COST, 100_000, "cost_other", description="Puchary")
    b = _rec(owner, Record.Kind.REVENUE, 300_000, "revenue", description="Dotacja powiatu")
    page = owner_client.get("/panel/ksiegowosc/uzupelniajace/?rok=2026").content.decode()
    assert "Nieprzypisane (2)" in page and "Zawody dla młodzieży" in page
    r = owner_client.post("/panel/ksiegowosc/uzupelniajace/?rok=2026", {"zapis": [a.pk, b.pk], "activity": "nieodplatna",
                                                                         "task": task.pk, "pokaz": "nieprzypisane"})
    assert r.status_code == 302
    assert set(Record.objects.values_list("activity", flat=True)) == {"nieodplatna"}
    assert set(Record.objects.values_list("task", flat=True)) == {task.pk}
    r = owner_client.post("/panel/ksiegowosc/darowizny-rzeczowe/nowa/", {"date": "2026-04-01", "description": "Stojaki na broń",
                                                                          "value": "1 500,00", "document": "umowa 3/2026",
                                                                          "statutory": "on", "activity": "nieodplatna"})
    assert r.status_code == 302 and InKindDonation.objects.get().record is None  # bez zaznaczenia — bez zapisu przychodu
    pdf = owner_client.get("/panel/ksiegowosc/uzupelniajace/wydruk.pdf?rok=2026")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    xlsx = owner_client.get("/panel/ksiegowosc/uzupelniajace/wydruk.xlsx?rok=2026")
    rows = [list(r) for r in load_workbook(io.BytesIO(xlsx.content)).active.iter_rows(values_only=True)]
    flat = {r[0]: r for r in rows if r and r[0]}
    assert flat["Nieodpłatna działalność pożytku publicznego"][1:5] == ["3 000,00", "0,00", "1 000,00", "2 000,00"]
    assert flat["Przychody do limitu z art. 10a ust. 1 pkt 5 ustawy"][1] == "1 500,00"
    assert any(r[0] and r[0].startswith("Zawody dla młodzieży") for r in rows)
    # forma działalności w formularzu zapisu
    form = owner_client.get(f"/panel/ksiegowosc/zapis/{a.pk}/zmien/").content.decode()
    assert 'value="nieodplatna" selected' in form
