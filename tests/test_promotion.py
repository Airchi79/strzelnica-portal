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
          "website_title": "Wolne osie w sobotę", "website_body": "Akapit 1\n\nAkapit 2", "hashtags": ["#StrzelnicaSątok", "#trening"],
          "graphic_headline": "Wolne osie w sobotę", "graphic_subtitle": "Spokojny trening na 100 m",
          "graphic_details": ["Sobota 21.11", "Oś 100 m", "Zapisy online", "Za dużo", "Ponad limit"]}


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


def test_guntracker_period_hides_slot_hours_and_price_rule():
    data, ps = generator.context_data()
    assert ps.booking_target == "guntracker"
    assert data["wolne_terminy_najbliższe_dni"] == [] and data["zasada_ceny"] is None and "GunTracker" in data["uwaga_o_rezerwacjach"]
    ps.booking_target = PromotionSettings.Target.SYSTEM
    ps.save()
    data, _ = generator.context_data()
    assert data["zasada_ceny"] and data["uwaga_o_rezerwacjach"] == "" and data["wolne_terminy_najbliższe_dni"]


def _png(size=(1600, 1200), color=(120, 90, 60)):
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "PNG")
    return buf.getvalue()


def test_graphics_render_both_formats_with_and_without_photo():
    import io

    from PIL import Image

    from apps.promotion import graphics

    post = Post(title="t", graphic_headline="Wolne osie w sobotę — zapraszamy na dłuższy trening z prowadzącym",
                graphic_subtitle="Bezpieczny trening na osi 100 m ąęśćżźółń", graphic_details="Sobota 21.11\nOś 100 m\n10:00–14:00\nZapisy\nNadmiar")
    for fmt, size in graphics.FORMATS.items():
        for photo in (None, _png()):
            img = Image.open(io.BytesIO(graphics.render(post, fmt, photo_bytes=photo, icon_bytes=_png((512, 512)))))
            assert img.size == size and img.format == "PNG"
    graphics.render(post, "kwadrat", photo_bytes=b"to nie jest obraz", icon_bytes=b"zly")  # zły plik — tło domyślne


def test_panel_graphics_photo_and_preview(client, monkeypatch, settings):
    from django.core.files.uploadedfile import SimpleUploadedFile

    settings.ANTHROPIC_API_KEY = "test"
    monkeypatch.setattr(generator, "_client", lambda: FakeClient())
    staff = grant(make_user("grafik@example.com"), "promotion.manage_promotion")
    login_with_otp(client, staff)
    client.post("/panel/promocja/", {"brief": "Zaproś na sobotni trening"})
    post = Post.objects.get()
    assert post.graphic_headline == "Wolne osie w sobotę" and post.graphic_details.count("\n") == 3  # najwyżej 4 wiersze
    page = client.get(f"/panel/promocja/{post.pk}/").content.decode()
    assert "mock-fb" in page and "mock-ig" in page and "mock-tt" in page and "mock-www" in page and "guntracker.pl" in page
    r = client.get(f"/panel/promocja/{post.pk}/grafika/kwadrat.png")
    assert r.status_code == 200 and r["Content-Type"] == "image/png"
    r = client.get(f"/panel/promocja/{post.pk}/grafika/pion.png?pobierz=1")
    assert "attachment" in r["Content-Disposition"]
    assert client.get(f"/panel/promocja/{post.pk}/grafika/inny.png").status_code == 404
    client.post(f"/panel/promocja/{post.pk}/", {"action": "photo", "photo": SimpleUploadedFile("z.png", _png(), "image/png")})
    post.refresh_from_db()
    assert post.photo and post.photo.name.endswith(".jpg")
    assert client.get(f"/panel/promocja/{post.pk}/grafika/kwadrat.png").status_code == 200
    r = client.post(f"/panel/promocja/{post.pk}/", {"action": "photo", "photo": SimpleUploadedFile("z.png", b"tekst", "image/png")}, follow=True)
    assert "zdjęcie" in r.content.decode().lower()
    name = post.photo.name
    client.post(f"/panel/promocja/{post.pk}/", {"action": "photo_remove"})
    post.refresh_from_db()
    assert not post.photo and not post.photo.storage.exists(name)


def test_tiktok_caption():
    assert Post(tiktok="Ujęcie 1: tarcza\nOPIS FILMU: Sobota na 100 m").tiktok_caption == "Sobota na 100 m"
    assert Post(tiktok="Krótki opis").tiktok_caption == "Krótki opis"
