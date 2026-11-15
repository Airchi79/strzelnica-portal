"""Faktury KSeF (import XML/ZIP, przyjęcie do ewidencji, odrzucenie, usunięcie z ewidencji) i generator dokumentów
(zawiadomienia, uchwały, opis zasad, zestawienie przepływów, arkusz CIT-8, dowód wewnętrzny — PDF i DOCX)."""

import io
import zipfile
from datetime import date

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils.functional import empty

from apps.accounting import documents, ksef, services
from apps.accounting.models import AccountingYear, GeneratedDocument, KsefInvoice, Record
from apps.core.models import AssociationSettings
from apps.credentials.models import private_storage

pytestmark = pytest.mark.django_db
OWN_NIP = "1111111111"
KSEF_NO = "2222222222-20260210-0100A0B1C2D3-E4"


@pytest.fixture(autouse=True)
def _private_media(settings, tmp_path):
    settings.PRIVATE_MEDIA_ROOT = tmp_path
    private_storage._wrapped = empty
    yield
    private_storage._wrapped = empty


@pytest.fixture(autouse=True)
def _association(db):
    a = AssociationSettings.load()
    a.nip, a.address = OWN_NIP, "Sątok 1, 00-000 Testowo"
    a.save()


def invoice_xml(number="FV/7/02/2026", gross="1230.00", seller_nip="2222222222", buyer_nip=OWN_NIP, kind="VAT", currency="PLN"):
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Faktura xmlns="http://crd.gov.pl/wzor/2025/06/25/13775/">
  <Naglowek><KodFormularza kodSystemowy="FA (3)" wersjaSchemy="1-0E">FA</KodFormularza><WariantFormularza>3</WariantFormularza></Naglowek>
  <Podmiot1><DaneIdentyfikacyjne><NIP>{seller_nip}</NIP><Nazwa>Hurtownia Testowa sp. z o.o.</Nazwa></DaneIdentyfikacyjne></Podmiot1>
  <Podmiot2><DaneIdentyfikacyjne><NIP>{buyer_nip}</NIP><Nazwa>Stowarzyszenie Testowe</Nazwa></DaneIdentyfikacyjne></Podmiot2>
  <Fa>
    <KodWaluty>{currency}</KodWaluty><P_1>2026-02-10</P_1><P_2>{number}</P_2><P_6>2026-02-09</P_6>
    <P_13_1>1000.00</P_13_1><P_14_1>230.00</P_14_1><P_15>{gross}</P_15><RodzajFaktury>{kind}</RodzajFaktury>
    <FaWiersz><NrWierszaFa>1</NrWierszaFa><P_7>Tarcze strzeleckie</P_7><P_8A>szt.</P_8A><P_8B>100</P_8B><P_11>1000.00</P_11></FaWiersz>
  </Fa>
</Faktura>""".encode()


def upload(name, content):
    return SimpleUploadedFile(name, content)


def test_parse_fa3_invoice():
    data = ksef.parse(invoice_xml(), f"{KSEF_NO}.xml")
    assert data["ksef_number"] == KSEF_NO and data["invoice_number"] == "FV/7/02/2026" and data["issue_date"] == date(2026, 2, 10)
    assert (data["net"], data["vat"], data["gross"]) == (100000, 23000, 123000)
    assert data["seller_name"] == "Hurtownia Testowa sp. z o.o." and data["items"][0]["name"] == "Tarcze strzeleckie"


def test_parser_rejects_bad_and_dangerous_xml():
    with pytest.raises(ValidationError, match="poprawny plik XML"):
        ksef.parse(b"<Faktura><Fa>", "x.xml")
    with pytest.raises(ValidationError, match="ustrukturyzowana"):
        ksef.parse(b"<Inne/>", "x.xml")
    bomb = b'<?xml version="1.0"?><!DOCTYPE Faktura [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]><Faktura>&b;</Faktura>'
    with pytest.raises(ValidationError):
        ksef.parse(bomb, "x.xml")
    external = b'<?xml version="1.0"?><!DOCTYPE Faktura [<!ENTITY e SYSTEM "file:///etc/passwd">]><Faktura>&e;</Faktura>'
    with pytest.raises(ValidationError):
        ksef.parse(external, "x.xml")


def test_import_xml_and_zip_with_duplicates_and_direction(owner):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("faktury/" + KSEF_NO + ".xml", invoice_xml())  # ta sama faktura co niżej — duplikat
        z.writestr("sprzedaz.xml", invoice_xml(number="S/1/2026", seller_nip=OWN_NIP, buyer_nip="3333333333"))
        z.writestr("opis.txt", "pomijany")
    created, dup, errors = ksef.import_files([upload(f"{KSEF_NO}.xml", invoice_xml())], by=owner, own_nip=OWN_NIP)
    assert len(created) == 1 and created[0].direction == KsefInvoice.Direction.PURCHASE and created[0].xml.read().startswith(b"<?xml")
    created, dup, errors = ksef.import_files([upload("paczka.zip", buf.getvalue()), upload("zly.txt", b"x")], by=owner, own_nip=OWN_NIP)
    assert len(created) == 1 and dup == 1 and len(errors) == 1
    assert created[0].direction == KsefInvoice.Direction.SALE


def test_accept_reject_and_remove_from_ewidencja(owner):
    inv = ksef.import_files([upload(f"{KSEF_NO}.xml", invoice_xml())], by=owner, own_nip=OWN_NIP)[0][0]
    with pytest.raises(ValidationError, match="kolumnę"):
        ksef.accept(inv, by=owner, day=date(2026, 2, 10), column="revenue", description="Tarcze")
    record = ksef.accept(inv, by=owner, day=date(2026, 2, 10), column="cost_deductible", description="Tarcze strzeleckie")
    inv.refresh_from_db()
    assert inv.status == KsefInvoice.Status.ACCEPTED and inv.record == record
    assert record.kind == Record.Kind.KSEF and record.cost_deductible == 123000 and record.ksef_number == KSEF_NO
    assert record.counterparty == "Hurtownia Testowa sp. z o.o." and record.document == "FV/7/02/2026"
    with pytest.raises(ValidationError, match="w ewidencji"):
        ksef.reject(inv, by=owner, note="x")
    services.remove_record(record, by=owner, reason="faktura prywatna członka")
    inv.refresh_from_db()
    assert inv.status == KsefInvoice.Status.NEW and inv.record is None and "prywatna" in inv.status_note
    ksef.reject(inv, by=owner, note="nie dotyczy stowarzyszenia")
    assert KsefInvoice.objects.get().status == KsefInvoice.Status.REJECTED
    ksef.reopen(inv, by=owner)
    # przywrócenie zapisu łączy go znów z fakturą
    services.restore_record(record, by=owner)
    inv.refresh_from_db()
    assert inv.status == KsefInvoice.Status.ACCEPTED and inv.record == record


def test_correction_invoice_negative_and_foreign_currency(owner):
    kor = ksef.import_files([upload("kor.xml", invoice_xml(number="KOR/1", gross="-123.00", kind="KOR"))], by=owner, own_nip=OWN_NIP)[0][0]
    rec = ksef.accept(kor, by=owner, day=date(2026, 2, 11), column="cost_deductible", description="Korekta")
    assert rec.cost_deductible == -12300
    eur = ksef.import_files([upload("eur.xml", invoice_xml(number="E/1", currency="EUR"))], by=owner, own_nip=OWN_NIP)[0][0]
    with pytest.raises(ValidationError, match="walucie obcej"):
        ksef.accept(eur, by=owner, day=date(2026, 2, 11), column="cost_deductible", description="x")


def test_ksef_panel_flow(owner_client):
    r = owner_client.post("/panel/ksiegowosc/ksef/import/", {"pliki": [upload(f"{KSEF_NO}.xml", invoice_xml())]})
    assert r.status_code == 302
    inv = KsefInvoice.objects.get()
    page = owner_client.get("/panel/ksiegowosc/ksef/").content.decode()
    assert "FV/7/02/2026" in page and "Do decyzji (1)" in page
    detail = owner_client.get(f"/panel/ksiegowosc/ksef/{inv.pk}/").content.decode()
    assert "Tarcze strzeleckie" in detail and "1230,00" in detail
    r = owner_client.post(f"/panel/ksiegowosc/ksef/{inv.pk}/", {"date": "2026-02-10", "document": "FV/7/02/2026",
                                                                "description": "Tarcze strzeleckie", "column": "cost_deductible"})
    assert r.status_code == 302 and Record.objects.get().cost_deductible == 123000
    r = owner_client.post(f"/panel/ksiegowosc/ksef/{inv.pk}/usun-z-ewidencji/", {"reason": "pomyłka"})
    assert Record.objects.get().is_removed and KsefInvoice.objects.get().status == KsefInvoice.Status.NEW
    xml = owner_client.get(f"/panel/ksiegowosc/ksef/{inv.pk}/xml/")
    assert xml.status_code == 200


# --- generator dokumentów ---------------------------------------------------------------


def _docx_text(content):
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        return z.read("word/document.xml").decode()


DOC_POSTS = {
    "uchwala_wybor": {"numer": "1/2026", "data": "2026-02-01", "miejscowosc": "Sątok", "organ": "Zebranie Członków", "rok": "2026",
                      "przychody_poprzedni": "45 000,00", "zawiadamia": "Zarząd", "podpisy": "Przewodniczący\nProtokolant"},
    "zawiadomienie_wybor": {"data": "2026-02-10", "miejscowosc": "Sątok", "urzad": "Urząd Skarbowy w Testowie\nul. Testowa 1",
                            "rok": "2026", "uchwala": "nr 1/2026 z 1.02.2026", "podpisy": "Jan Testowy"},
    "zawiadomienie_rezygnacja": {"powod": "warunki", "data": "2026-02-10", "urzad": "US Testowo", "ostatni_miesiac": "2026"},
    "opis_zasad": {"data": "2026-01-05", "od_dnia": "2026-01-01", "start_programu": "2026-01-01", "wersja": "moduł Księgowość 1.0",
                   "przechowywanie": "siedziba", "osoby": "Jan Testowy\nEwa Testowa"},
    "przeplywy": {"rok": "2026", "kol4": "500,00", "kol5": "", "kol6": "200,00"},
    "cit8": {"rok": "2026", "cele": "działalność w zakresie kultury fizycznej i sportu", "wolny": ""},
    "dowod_wewnetrzny": {"rodzaj": "detal", "data": "2026-02-12", "data_zdarzenia": "2026-02-12", "przedmiot": "Taśma ostrzegawcza",
                         "ilosc": "2 szt.", "cena": "12,50", "wartosc": "25,00", "podstawa": "paragon nr 123", "sporzadzil": "Jan Testowy"},
}
EXPECTED_TEXT = {
    "uchwala_wybor": "45 000,00 zł", "zawiadomienie_wybor": "art. 10a ust. 3", "zawiadomienie_rezygnacja": "art. 10a ust. 5 pkt 2",
    "opis_zasad": "Ewa Testowa", "przeplywy": "300,00", "cit8": "kultury fizycznej", "dowod_wewnetrzny": "DW/1/2026",
}


@pytest.mark.parametrize("key", list(DOC_POSTS))
def test_every_document_generates_pdf_and_docx(owner_client, owner, key):
    _ = services.add_record(kind=Record.Kind.REVENUE, day=date(2026, 1, 5), document="WB 1", description="Darowizna",
                            amount=100000, column="revenue", by=owner)
    assert owner_client.get(f"/panel/ksiegowosc/dokumenty/nowy/{key}/").status_code == 200
    r = owner_client.post(f"/panel/ksiegowosc/dokumenty/nowy/{key}/", DOC_POSTS[key])
    assert r.status_code == 302, r.content.decode()[:2000]
    obj = GeneratedDocument.objects.get()
    pdf = owner_client.get(f"/panel/ksiegowosc/dokumenty/{obj.pk}.pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    docx = owner_client.get(f"/panel/ksiegowosc/dokumenty/{obj.pk}.docx")
    text = _docx_text(docx.content)
    assert EXPECTED_TEXT[key] in text, key


def test_flows_document_saves_year_data_and_carries_col7(owner_client, owner):
    owner_client.post("/panel/ksiegowosc/dokumenty/nowy/przeplywy/", {"rok": "2026", "kol4": "500,00", "kol5": "", "kol6": "200,00"})
    y = AccountingYear.objects.get(year=2026)
    assert (y.free_income, y.free_prior, y.free_spent) == (50000, None, 20000)
    nxt = services.flows(2027)
    assert nxt["col5"] == 30000 and nxt["col5_from_prev"]


def test_cit_sheet_amounts(owner):
    services.add_record(kind=Record.Kind.REVENUE, day=date(2026, 1, 5), document="WB 1", description="x", amount=100000,
                        column="revenue", by=owner)
    services.add_record(kind=Record.Kind.COST, day=date(2026, 1, 6), document="FV 1", description="y", amount=40000,
                        column="cost_deductible", by=owner)
    ctx = documents.context(owner)
    obj = documents.create(documents.DOCS["cit8"], {"rok": 2026, "cele": "sport", "wolny": None}, by=owner, ctx=ctx)
    _, blocks = documents.blocks_for(obj)
    table = next(v for k, v in blocks if k == "table")
    amounts = [row[1] for row in table["rows"]]
    assert amounts[:5] == ["1 000,00 zł", "400,00 zł", "600,00 zł", "600,00 zł", "0,00 zł"]


def test_internal_proof_numbering_and_prefill_link(owner_client):
    for _ in range(2):
        owner_client.post("/panel/ksiegowosc/dokumenty/nowy/dowod_wewnetrzny/", DOC_POSTS["dowod_wewnetrzny"])
    numbers = sorted(GeneratedDocument.objects.values_list("data___number", flat=True))
    assert numbers == ["DW/1/2026", "DW/2/2026"]
    obj = GeneratedDocument.objects.order_by("pk").first()
    page = owner_client.get(f"/panel/ksiegowosc/dokumenty/{obj.pk}/").content.decode()
    assert "Wpisz koszt do ewidencji" in page and "amount=25%2C00" in page and "document=DW%2F1%2F2026" in page
    form = owner_client.get("/panel/ksiegowosc/dodaj/koszt/?document=DW%2F1%2F2026&amount=25%2C00&description=Ta%C5%9Bma").content.decode()
    assert 'value="DW/1/2026"' in form and 'value="25,00"' in form


# --- wizualizacja faktury ----------------------------------------------------------------


RICH_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Faktura xmlns="http://crd.gov.pl/wzor/2025/06/25/13775/">
  <Naglowek><KodFormularza kodSystemowy="FA (3)" wersjaSchemy="1-0E">FA</KodFormularza><WariantFormularza>3</WariantFormularza></Naglowek>
  <Podmiot1><DaneIdentyfikacyjne><NIP>2222222222</NIP><Nazwa>Hurtownia Testowa sp. z o.o.</Nazwa></DaneIdentyfikacyjne>
    <Adres><KodKraju>PL</KodKraju><AdresL1>ul. Testowa 1</AdresL1><AdresL2>00-001 Testowo</AdresL2></Adres></Podmiot1>
  <Podmiot2><DaneIdentyfikacyjne><NIP>1111111111</NIP><Nazwa>Stowarzyszenie Testowe</Nazwa></DaneIdentyfikacyjne></Podmiot2>
  <Fa><KodWaluty>PLN</KodWaluty><P_1>2026-02-10</P_1><P_1M>Testowo</P_1M><P_2>FV/9/2026</P_2><P_6>2026-02-09</P_6>
    <P_13_1>100.00</P_13_1><P_14_1>23.00</P_14_1><P_13_7>50.00</P_13_7><P_15>173.00</P_15>
    <Adnotacje><P_16>2</P_16><P_17>2</P_17><P_18>2</P_18><P_18A>1</P_18A><Zwolnienie><P_19N>1</P_19N></Zwolnienie></Adnotacje>
    <RodzajFaktury>VAT</RodzajFaktury>
    <FaWiersz><NrWierszaFa>1</NrWierszaFa><P_7>Tarcze &lt;papierowe&gt;</P_7><P_8A>szt.</P_8A><P_8B>100</P_8B><P_9A>1.00</P_9A><P_11>100.00</P_11><P_12>23</P_12></FaWiersz>
    <FaWiersz><NrWierszaFa>2</NrWierszaFa><P_7>Szkolenie</P_7><P_8A>usł.</P_8A><P_8B>1</P_8B><P_9A>50.00</P_9A><P_11>50.00</P_11><P_12>zw</P_12></FaWiersz>
    <Platnosc><TerminPlatnosci><Termin>2026-02-24</Termin></TerminPlatnosci><FormaPlatnosci>6</FormaPlatnosci>
      <RachunekBankowy><NrRB>11222233334444555566667777</NrRB><NazwaBanku>Bank Testowy</NazwaBanku></RachunekBankowy></Platnosc>
  </Fa>
  <Stopka><Informacje><StopkaFaktury>Dziękujemy za zakupy</StopkaFaktury></Informacje><Rejestry><REGON>123456789</REGON></Rejestry></Stopka>
</Faktura>""".encode()


def test_visualization_read_full_invoice():
    from apps.accounting import ksef_view

    v = ksef_view.read(RICH_XML)
    assert v["number"] == "FV/9/2026" and v["kind_label"] == "Faktura podstawowa" and v["place"] == "Testowo"
    assert v["seller"]["address"] == ["ul. Testowa 1", "00-001 Testowo"] and v["buyer"]["nip"] == "1111111111"
    assert [(line["name"], line["qty"], line["unit_net"], line["net"], line["rate"]) for line in v["lines"]] == [
        ("Tarcze <papierowe>", "100", 100, 10000, "23"), ("Szkolenie", "1", 5000, 5000, "zw")]
    assert v["sums"] == [{"label": "stawka podstawowa", "net": 10000, "vat": 2300}, {"label": "zwolnione z podatku", "net": 5000, "vat": None}]
    assert v["total"] == 17300 and v["flags"] == ["mechanizm podzielonej płatności"]
    assert v["payment"]["form"] == "przelew" and v["payment"]["terms"] == ["2026-02-24"]
    assert v["payment"]["accounts"][0]["number"] == "11222233334444555566667777"
    assert v["footer"] == ["Dziękujemy za zakupy"] and v["registers"] == [("REGON", "123456789")]


def test_visualization_qr_link_format(owner):
    import base64
    import hashlib

    from apps.accounting import ksef_view

    inv = ksef.import_files([upload(f"{KSEF_NO}.xml", RICH_XML)], by=owner, own_nip=OWN_NIP)[0][0]
    digest = base64.urlsafe_b64encode(hashlib.sha256(RICH_XML).digest()).decode().rstrip("=")
    assert ksef_view.qr_url(inv) == f"https://qr.ksef.mf.gov.pl/invoice/2222222222/10-02-2026/{digest}"
    assert ksef_view.qr_url(inv, "test").startswith("https://qr-test.ksef.mf.gov.pl/invoice/")
    inv.ksef_number = ""
    assert ksef_view.qr_url(inv) == ""  # bez numeru KSeF — bez kodu (plik spoza KSeF)


def test_visualization_in_panel_and_pdf(owner_client, owner):
    inv = ksef.import_files([upload(f"{KSEF_NO}.xml", RICH_XML)], by=owner, own_nip=OWN_NIP)[0][0]
    page = owner_client.get(f"/panel/ksiegowosc/ksef/{inv.pk}/").content.decode()
    assert "Faktura podstawowa nr FV/9/2026" in page and "ul. Testowa 1" in page and "Tarcze &lt;papierowe&gt;" in page
    assert "11222233334444555566667777" in page and "mechanizm podzielonej płatności" in page
    assert "data:image/svg+xml;base64," in page and "qr.ksef.mf.gov.pl/invoice/2222222222/10-02-2026/" in page
    pdf = owner_client.get(f"/panel/ksiegowosc/ksef/{inv.pk}/pdf/")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF") and "faktura-FV-9-2026.pdf" in pdf["Content-Disposition"]
    # uszkodzony plik w magazynie — strona działa (podsumowanie z bazy)
    with inv.xml.open("wb") as f:
        f.write(b"<zepsuty")
    page = owner_client.get(f"/panel/ksiegowosc/ksef/{inv.pk}/").content.decode()
    assert "Nie udało się wyświetlić treści faktury" in page


# --- przyjmowanie wielu faktur naraz -----------------------------------------------------


def test_bulk_accept_common_or_own_description_and_currency(owner_client, owner):
    a = ksef.import_files([upload("a.xml", invoice_xml(number="FV/A", gross="100.00"))], by=owner, own_nip=OWN_NIP)[0][0]
    b = ksef.import_files([upload("b.xml", invoice_xml(number="FV/B", gross="50.00"))], by=owner, own_nip=OWN_NIP)[0][0]
    s = ksef.import_files([upload("s.xml", invoice_xml(number="S/1", gross="30.00", seller_nip=OWN_NIP, buyer_nip="3333333333"))],
                          by=owner, own_nip=OWN_NIP)[0][0]
    eur = ksef.import_files([upload("e.xml", invoice_xml(number="E/1", currency="EUR"))], by=owner, own_nip=OWN_NIP)[0][0]
    page = owner_client.get("/panel/ksiegowosc/ksef/").content.decode()
    assert 'data-check-all="faktura"' in page and "Przyjmij zaznaczone" in page
    assert '<option value="pozostala" selected>' in page  # domyślna forma działalności
    r = owner_client.post("/panel/ksiegowosc/ksef/zbiorczo/", {"faktura": [a.pk, b.pk, s.pk, eur.pk], "description": "Prowizje GunTracker",
                                                               "cost_column": "cost_other", "activity": "pozostala"}, follow=True)
    content = r.content.decode()
    assert "Przyjęto do ewidencji faktur: 3" in content and "E/1" in content and "walucie obcej" in content
    recs = {rec.document: rec for rec in Record.objects.all()}
    assert recs["FV/A"].cost_other == 10000 and recs["FV/B"].cost_other == 5000 and recs["S/1"].revenue == 3000
    assert {r.description for r in recs.values()} == {"Prowizje GunTracker"} and {r.activity for r in recs.values()} == {"pozostala"}
    assert recs["FV/A"].date == a.issue_date and recs["FV/A"].ksef_number == ""
    assert KsefInvoice.objects.get(pk=eur.pk).status == KsefInvoice.Status.NEW
    # bez opisu — opis z pozycji faktury
    c = ksef.import_files([upload("c.xml", invoice_xml(number="FV/C", gross="20.00"))], by=owner, own_nip=OWN_NIP)[0][0]
    records, errors = ksef.accept_many([c], by=owner, description="", cost_column="cost_deductible", activity="pozostala")
    assert errors == [] and records[0].description == "Tarcze strzeleckie" and records[0].cost_deductible == 2000


def test_accept_form_defaults_to_other_activity(owner_client, owner):
    inv = ksef.import_files([upload(f"{KSEF_NO}.xml", invoice_xml())], by=owner, own_nip=OWN_NIP)[0][0]
    page = owner_client.get(f"/panel/ksiegowosc/ksef/{inv.pk}/").content.decode()
    assert '<option value="pozostala" selected>' in page and 'list="opisy"' in page and '<datalist id="opisy">' in page
