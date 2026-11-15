"""Wykaz środków trwałych i WNiP (§ 3 rozporządzenia, Dz. U. 2025 poz. 1880) i odpisy amortyzacyjne
(art. 16d, 16h ust. 1 pkt 1, 16i ustawy o CIT)."""

import io
from datetime import date
from decimal import Decimal

import pytest
from django.core.exceptions import ValidationError

from apps.accounting import assets, services
from apps.accounting.models import AccountingYear, DepreciationPosting, FixedAsset, Record
from apps.audit.models import AuditLog

pytestmark = pytest.mark.django_db


def _data(**kw):
    d = {"kind": "st", "name": "Kulochwyt stalowy osi 25 m", "kst": "291", "acquisition": "nabycie",
         "acquired_on": date(2026, 1, 10), "in_use_on": date(2026, 1, 15), "entered_on": date(2026, 1, 15),
         "acquisition_doc": "faktura FV 1/2026", "in_use_doc": "OT 1/2026", "initial_value": 1_200_000, "updated_value": None,
         "method": "liniowa", "rate": Decimal("10.00"), "prior_depreciation": None, "prior_until_year": None, "notes": ""}
    d.update(kw)
    return d


def test_linear_plan_starts_month_after_entry(owner):
    a = assets.create(_data(), by=owner)
    assert a.number == 1
    f = assets.year_figures(a, 2026)
    # 12 000 zł × 10 % / 12 = 100 zł miesięcznie, od lutego (miesiąc po wprowadzeniu) — 11 miesięcy
    assert f["months"][1] == 0 and f["months"][2] == 10000 and f["year"] == 110000 and f["accumulated"] == 110000
    rows, prior = assets.schedule(a)
    assert prior == 0 and rows[0] == {"year": 2026, "amount": 110000, "accumulated": 110000}
    assert rows[-1]["accumulated"] == 1_200_000 and rows[-1]["year"] == 2036  # zrównanie z wartością początkową
    assert sum(r["amount"] for r in rows) == 1_200_000
    assert AuditLog.objects.filter(action="ksiegowosc.srodek_trwaly").exists()


def test_disposal_ends_depreciation_in_that_month(owner):
    a = assets.create(_data(), by=owner)
    assets.dispose(a, by=owner, day=date(2026, 6, 20), kind="likwidacja", reason="zniszczony", doc="LT 1/2026")
    f = assets.year_figures(a, 2026)
    assert f["months"][6] == 10000 and f["months"][7] == 0 and f["year"] == 50000  # luty–czerwiec
    assert a in assets.active_in(2026) and a not in assets.active_in(2027)
    with pytest.raises(ValidationError, match="powód"):
        assets.cancel_disposal(a, by=owner, reason="")
    assets.cancel_disposal(a, by=owner, reason="pomyłka")
    with pytest.raises(ValidationError, match="Podaj przyczynę likwidacji"):
        assets.dispose(a, by=owner, day=date(2026, 6, 20), kind="likwidacja")


def test_prior_depreciation_for_assets_from_before_system(owner):
    a = assets.create(_data(acquired_on=date(2020, 3, 1), in_use_on=date(2020, 3, 1), entered_on=date(2020, 3, 1),
                            prior_depreciation=700_000, prior_until_year=2025), by=owner)
    f = assets.year_figures(a, 2026)
    assert f["year"] == 120000 and f["accumulated"] == 820000
    rows, prior = assets.schedule(a)
    assert prior == 700_000 and rows[0]["year"] == 2026 and rows[-1]["accumulated"] == 1_200_000


def test_validation_rules(owner):
    with pytest.raises(ValidationError, match="10 000 zł"):
        assets.create(_data(method="jednorazowo", rate=Decimal(0)), by=owner)
    with pytest.raises(ValidationError, match="stawkę"):
        assets.create(_data(rate=Decimal(0)), by=owner)
    with pytest.raises(ValidationError, match="najwcześniej w miesiącu"):
        assets.create(_data(entered_on=date(2025, 12, 31)), by=owner)
    with pytest.raises(ValidationError, match="razem z rokiem"):
        assets.create(_data(prior_depreciation=100), by=owner)
    small = assets.create(_data(name="Stół", initial_value=300_000, method="jednorazowo", rate=Decimal(5)), by=owner)
    assert small.rate == 0 and assets.year_figures(small, 2026)["year"] == 0
    late = assets.create(_data(name="Wiata", entered_on=date(2026, 3, 2)), by=owner)
    assert assets.late_entry(late) and assets.year_figures(late, 2026)["months"][3] == 0  # ujawnienie — od kwietnia


def test_update_and_delete_rules(owner):
    a = assets.create(_data(), by=owner)
    with pytest.raises(ValidationError, match="powód"):
        assets.update(a, _data(rate=Decimal(20)), by=owner, reason="")
    assets.update(a, _data(rate=Decimal("20.00")), by=owner, reason="zła stawka KŚT")
    log = AuditLog.objects.get(action="ksiegowosc.srodek_trwaly_zmiana")
    assert log.changes["rate"] == ["10.00", "20.00"]
    record = assets.post_depreciation(2026, 2, by=owner)
    assert record.cost_deductible == 20000 and record.date == date(2026, 2, 28)
    with pytest.raises(ValidationError, match="już ujęte"):
        assets.delete(a, by=owner, reason="pomyłka")
    services.remove_record(record, by=owner, reason="test")
    assets.delete(a, by=owner, reason="pomyłka")
    assert not FixedAsset.objects.exists()


def test_post_depreciation_month_and_year_rules(owner):
    assets.create(_data(), by=owner)
    with pytest.raises(ValidationError, match="Brak odpisów"):
        assets.post_depreciation(2026, 1, by=owner)
    rec = assets.post_depreciation(2026, 3, by=owner, column="cost_other")
    assert rec.cost_other == 10000 and rec.kind == Record.Kind.COST and DepreciationPosting.objects.get().month == 3
    with pytest.raises(ValidationError, match="już ujęte"):
        assets.post_depreciation(2026, 3, by=owner)
    with pytest.raises(ValidationError, match="część roku"):
        assets.post_depreciation(2026, None, by=owner)
    with pytest.raises(ValidationError, match="po zakończeniu"):
        assets.post_depreciation(2099, None, by=owner)


def test_closed_year_blocks_asset_changes(owner):
    a = assets.create(_data(acquired_on=date(2025, 2, 1), in_use_on=date(2025, 2, 1), entered_on=date(2025, 2, 1)), by=owner)
    AccountingYear.objects.create(year=2025, closed_at="2026-03-31T10:00:00Z")
    with pytest.raises(ValidationError, match="zamknięty"):
        assets.update(a, _data(acquired_on=date(2025, 2, 1), in_use_on=date(2025, 2, 1), entered_on=date(2025, 2, 1),
                               name="Inna nazwa"), by=owner, reason="x")


def test_panel_flow_and_exports(owner_client):
    from openpyxl import load_workbook

    post = {"kind": "st", "name": "Kontener biurowy", "kst": "103", "acquisition": "nabycie", "acquired_on": "2026-01-10",
            "in_use_on": "2026-01-15", "entered_on": "2026-01-15", "acquisition_doc": "faktura FV 9/2026", "in_use_doc": "OT 1/2026",
            "initial_value": "24 000,00", "updated_value": "", "method": "liniowa", "rate": "10", "prior_depreciation": "",
            "prior_until_year": "", "notes": ""}
    r = owner_client.post("/panel/ksiegowosc/srodki-trwale/nowy/", post)
    assert r.status_code == 302
    a = FixedAsset.objects.get()
    assert a.initial_value == 2_400_000 and a.rate == Decimal("10.00")
    page = owner_client.get("/panel/ksiegowosc/srodki-trwale/?rok=2026").content.decode()
    assert "Kontener biurowy" in page and "Ujmij odpisy" in page
    detail = owner_client.get(f"/panel/ksiegowosc/srodki-trwale/{a.pk}/").content.decode()
    assert "Plan odpisów" in detail and "2036" in detail
    r = owner_client.post(f"/panel/ksiegowosc/srodki-trwale/{a.pk}/zmien/", {**post, "rate": "20", "reason": ""})
    assert r.status_code == 200  # powód wymagany
    r = owner_client.post(f"/panel/ksiegowosc/srodki-trwale/{a.pk}/zmien/", {**post, "rate": "20", "reason": "stawka"})
    assert r.status_code == 302 and FixedAsset.objects.get().rate == 20
    r = owner_client.post("/panel/ksiegowosc/srodki-trwale/odpisy/", {"year": "2026", "month": "2", "column": "cost_deductible"})
    assert Record.objects.get().cost_deductible == 40000
    pdf = owner_client.get("/panel/ksiegowosc/srodki-trwale/wydruk.pdf?rok=2026")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    xlsx = owner_client.get("/panel/ksiegowosc/srodki-trwale/wydruk.xlsx?rok=2026")
    ws = load_workbook(io.BytesIO(xlsx.content)).active
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    assert rows[5][:11] == list(range(1, 12))
    assert rows[6][4] == "Kontener biurowy" and rows[6][5] == "103" and rows[6][11] == 4400.0  # 20 % od 24 000 zł × 11/12
    r = owner_client.post(f"/panel/ksiegowosc/srodki-trwale/{a.pk}/wykresl/", {"kind": "zbycie", "day": "2026-03-10",
                                                                               "ksef_number": "1111111111-20260310-0100A0B1C2D3-E4"})
    assert FixedAsset.objects.get().disposal == "zbycie"
