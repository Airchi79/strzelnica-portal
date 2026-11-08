from types import SimpleNamespace

import pytest

from apps.payments.transfer_qr import payload, qr_for, recipient_name

ACCOUNT = "PL61109010140000071219812874"


def test_payload_follows_zbp_2d_format():
    text = payload(ACCOUNT, 24050, "SATOK-R-2026-0001", "911-205-89-72", "Stowarzyszenie „STRZELNICA SĄTOK”")
    assert text == "9112058972|PL|61109010140000071219812874|024050|STRZELNICA SATOK|SATOK-R-2026-0001|||"
    assert len(text) <= 160


@pytest.mark.parametrize("name,expected", [
    ("Stowarzyszenie „STRZELNICA SĄTOK”", "STRZELNICA SATOK"),
    ("Klub Strzelecki Żółć i Gęś z bardzo długą nazwą", "Klub Strzelecki Zolc"),
    ("Nazwa|z|kreskami", "Nazwa z kreskami"),
])
def test_recipient_name_ascii_and_short(name, expected):
    assert recipient_name(name) == expected


def test_amount_zero_lets_payer_enter_amount_and_bad_nip_is_omitted():
    assert payload(ACCOUNT, 0, "SATOK-K00012A", "123", "X").startswith("|PL|61109010140000071219812874|000000|X|SATOK-K00012A|")


def test_no_qr_without_valid_account():
    assert payload("", 100, "T") == "" and payload("PL123", 100, "T") == ""
    assert qr_for(SimpleNamespace(bank_account="", nip="", name="x"), 100, "T") == ""


def test_title_and_large_amount():
    text = payload(ACCOUNT, 123456789, "Tytuł ąę " + "x" * 40, "", "A")
    fields = text.split("|")
    assert fields[3] == "123456789" and len(fields[5]) == 32 and fields[5].startswith("Tytul ae")
