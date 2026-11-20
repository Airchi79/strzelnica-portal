"""Promocja: szkice publikacji z API Claude (w testach — sztuczny klient, bez połączeń i kosztów), zatwierdzanie, ustawienia."""

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from apps.promotion import generator, services
from apps.promotion.models import Post, PromotionSettings

from .conftest import add_credential, approve, grant, login_with_otp, make_user

pytestmark = pytest.mark.django_db

RESULT = {"title": "Trening w sobotę", "facebook": "FB tekst https://x", "instagram": "IG tekst", "tiktok": "Ujęcie 1…",
          "website_title": "Wolne osie w sobotę", "website_body": "Akapit 1\n\nAkapit 2", "hashtags": ["#StrzelnicaSątok", "#trening"]}


class FakeClient:
    def __init__(self, stop_reason="end_turn", text=None):
        self.calls = []
        self.stop_reason, self.text = stop_reason, text if text is not None else json.dumps(RESULT, ensure_ascii=False)
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop_reason, model=kw["model"],
                               content=[SimpleNamespace(type="text", text=self.text)],
                               usage=SimpleNamespace(input_tokens=1200, output_tokens=800, cache_read_input_tokens=0))


def test_generate_sends_public_data_and_parses_json(monkeypatch, settings):
    settings.ANTHROPIC_API_KEY = "test"
    u = approve(make_user("tajny@example.com", first_name="Tajny", last_name="Strzelec"))
    add_credential(u)
    client = FakeClient()
    monkeypatch.setattr(generator, "_client", lambda: client)
    result, model, usage = generator.generate("Zaproś na trening w sobotę")
    assert result["facebook"] == "FB tekst https://x" and model == "claude-opus-5-5" and usage["output"] == 800
    kw = client.calls[0]
    body = kw["messages"][0]["content"]
    assert "Zaproś na trening" in body and "osie_i_cennik" in body and "guntracker" in body  # link do GunTrackera do dnia startu
    assert "Tajny" not in body and "tajny@example.com" not in body  # bez danych osobowych
    assert kw["output_config"]["format"]["type"] == "json_schema" and kw["fallbacks"] == "default"
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_generate_errors(monkeypatch, settings):
    settings.ANTHROPIC_API_KEY = ""
    with pytest.raises(generator.GenerationError, match="Brak klucza"):
        generator.generate("cokolwiek")
    settings.ANTHROPIC_API_KEY = "test"
    monkeypatch.setattr(generator, "_client", lambda: FakeClient(stop_reason="refusal"))
    with pytest.raises(generator.GenerationError, match="odmówił"):
        generator.generate("cokolwiek")
    monkeypatch.setattr(generator, "_client", lambda: FakeClient(text="nie-json"))
    with pytest.raises(generator.GenerationError, match="Nieoczekiwana"):
        generator.generate("cokolwiek")


def test_booking_link_switch(settings):
    ps = PromotionSettings.load()
    assert "guntracker.pl" in ps.booking_url()
    ps.booking_target = PromotionSettings.Target.SYSTEM
    ps.save()
    assert ps.booking_url().endswith("/rezerwacje/nowa/")


def test_event_data_in_context(owner):
    from apps.events.models import Event

    now = timezone.now()
    e = Event.objects.create(title="GPA", status=Event.Status.PUBLISHED, starts_at=now + timedelta(days=3), ends_at=now + timedelta(days=3, hours=5),
                             registration_until=now + timedelta(days=2), fee=16000, capacity=40)
    data, _ = generator.context_data(e)
    assert data["wydarzenie_główne"]["opłata_zł"] == 160 and data["wydarzenie_główne"]["wolnych_miejsc"] == 40
    assert "/wydarzenia/" in data["wydarzenie_główne"]["adres_zapisów"] and data["nadchodzące_wydarzenia"][0]["nazwa"] == "GPA"


def test_panel_flow(client, monkeypatch, settings):
    settings.ANTHROPIC_API_KEY = "test"
    monkeypatch.setattr(generator, "_client", lambda: FakeClient())
    staff = grant(make_user("promo@example.com"), "promotion.manage_promotion")
    login_with_otp(client, staff)
    page = client.get("/panel/promocja/").content.decode()
    assert "Przygotuj szkic" in page and "GunTracker" in page
    r = client.post("/panel/promocja/", {"brief": "Zaproś na sobotni trening"})
    post = Post.objects.get()
    assert r.status_code == 302 and r["Location"] == f"/panel/promocja/{post.pk}/"
    assert post.status == Post.Status.DRAFT and post.hashtags == "#StrzelnicaSątok #trening" and post.created_by == staff
    page = client.get(f"/panel/promocja/{post.pk}/").content.decode()
    assert "data-copy-from" in page and "Zatwierdź" in page
    form = {"title": "Trening!", "facebook": "Poprawiony FB", "instagram": "IG", "tiktok": "TT", "website_title": "W",
            "website_body": "B", "hashtags": "#a"}
    client.post(f"/panel/promocja/{post.pk}/", {**form, "action": "approve"})
    post.refresh_from_db()
    assert post.status == Post.Status.APPROVED and post.facebook == "Poprawiony FB" and post.decided_by == staff
    client.post(f"/panel/promocja/{post.pk}/", {**form, "action": "published"})
    post.refresh_from_db()
    assert post.status == Post.Status.PUBLISHED
    client.post(f"/panel/promocja/{post.pk}/", {**form, "facebook": "zmiana po publikacji", "action": "save"})
    post.refresh_from_db()
    assert post.facebook == "Poprawiony FB"  # opublikowanej nie zmienia się
    r = client.post("/panel/promocja/", {"brief": "x"})
    assert "Napisz polecenie" in r.content.decode()
    client.post("/panel/promocja/ustawienia/", {"booking_target": "system", "guntracker_url": "https://www.guntracker.pl/p/strzelnica-satok",
                                               "tone": "rzeczowo", "hashtags": "#a", "facts": ""})
    assert PromotionSettings.load().booking_target == "system"


def test_decide_rules(owner):
    p = Post.objects.create(brief="test")
    with pytest.raises(Exception):
        services.decide(p, "published", by=owner)  # szkic nie może od razu być opublikowany
    services.decide(p, "reject", by=owner)
    services.decide(p, "reopen", by=owner)
    assert Post.objects.get().status == Post.Status.DRAFT


def test_promotion_requires_permission(client):
    other = grant(make_user("inny@example.com"), "logbook.view_logbook")
    login_with_otp(client, other)
    assert client.get("/panel/promocja/").status_code == 403
