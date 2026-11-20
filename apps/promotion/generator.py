"""Generator publikacji: polecenie zarządu + dane z systemu (osie, cennik, wolne terminy, wydarzenia) → teksty na
Facebook, Instagram, TikTok i stronę, przez API Claude (Anthropic). Odpowiedź w ustalonym formacie JSON (structured outputs).

Model nie dostaje danych osobowych — wyłącznie dane publiczne (te same, które pokazuje publiczny kalendarz i API).
Klucz API tylko w .env serwera (ANTHROPIC_API_KEY); bez klucza generator zgłasza czytelny błąd."""

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

BETAS = ["server-side-fallback-2026-07-01"]
TIMEOUT_SECONDS = 50  # mieści się w limicie czasu żądania serwera (60 s)

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Krótki tytuł roboczy publikacji (do listy w panelu)."},
        "facebook": {"type": "string", "description": "Post na Facebook: 3–6 krótkich akapitów, konkretne informacje, link na końcu."},
        "instagram": {"type": "string", "description": "Opis pod zdjęcie na Instagram: krótszy, emocje i konkret, bez linku w treści (link w bio)."},
        "tiktok": {"type": "string", "description": "Scenariusz filmu 15–30 s (ujęcia, napisy na ekranie) i osobno krótki opis filmu."},
        "website_title": {"type": "string", "description": "Tytuł wpisu w Aktualnościach na stronie."},
        "website_body": {"type": "string", "description": "Treść wpisu na stronę: akapity rozdzielone pustą linią, link do rezerwacji lub zapisów."},
        "hashtags": {"type": "array", "items": {"type": "string"}, "description": "5–10 hasztagów, w tym stałe hasztagi strzelnicy."},
    },
    "required": ["title", "facebook", "instagram", "tiktok", "website_title", "website_body", "hashtags"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """Jesteś redaktorem mediów społecznościowych Strzelnicy Sątok — strzelnicy sportowej prowadzonej przez stowarzyszenie zwykłe
„STRZELNICA SĄTOK” (Sątok koło Bierutowa, woj. dolnośląskie). Przygotowujesz szkice publikacji, które zarząd sprawdza przed publikacją.

Zasady, których przestrzegasz zawsze:
- Piszesz po polsku, poprawnie i naturalnie. Styl i ton opisuje sekcja „Styl” w danych.
- Używasz wyłącznie faktów z sekcji „Dane strzelnicy” i z polecenia. Nie wymyślasz godzin, cen, liczby miejsc, nazwisk, nagród
  ani wydarzeń. Jeśli czegoś brakuje w danych, piszesz ogólnie albo pomijasz ten element.
- Treści dotyczą sportu strzeleckiego, treningu, bezpieczeństwa, wydarzeń i życia stowarzyszenia. Nie promujesz sprzedaży broni ani
  amunicji, nie pokazujesz broni jako narzędzia przemocy, nie używasz języka militarnego wobec ludzi. Podkreślasz zasady
  bezpieczeństwa i obecność prowadzącego strzelanie, gdy to pasuje do tematu.
- Nie podajesz danych osobowych strzelców ani członków.
- Linki: do rezerwacji używasz dokładnie adresu z pola „rezerwacja”, do wydarzeń — adresu wydarzenia z danych. Nie wymyślasz innych adresów.
- Hasztagi: zawsze dołącz stałe hasztagi strzelnicy, dodaj kilka pasujących do tematu.
- Facebook i strona mogą być dłuższe; Instagram krótszy; TikTok to scenariusz krótkiego filmu (ujęcia i napisy) plus opis.
- Unikasz przesady, wielu wykrzykników i emotikon — najwyżej kilka emotikon w całym poście."""


class GenerationError(Exception):
    pass


def _client():
    import anthropic

    key = getattr(settings, "ANTHROPIC_API_KEY", "")
    if not key:
        raise GenerationError("Brak klucza API Claude na serwerze (ANTHROPIC_API_KEY w .env) — generowanie jest wyłączone.")
    return anthropic.Anthropic(api_key=key, timeout=TIMEOUT_SECONDS, max_retries=1)


def context_data(event=None, days=7):
    """Dane publiczne dla modelu: strzelnica, osie z cennikiem, wolne terminy, wydarzenia, link do rezerwacji."""
    from apps.core import public_api
    from apps.core.models import AssociationSettings
    from apps.events.models import Event

    from .models import PromotionSettings

    ps = PromotionSettings.load()
    assoc = AssociationSettings.load()
    today = timezone.localdate()
    lanes = []
    for lane in public_api._lanes():
        rule = lane.price_rule_for(today)
        lanes.append({"oś": lane.name, "długość_m": lane.length_m, "tylko_wyłączność": lane.exclusive_only,
                      "cena_za_osobę_godz_zł": public_api._zl(rule.per_person_hourly) if rule else None,
                      "wyłączność_godz_zł": public_api._zl(rule.flat_hourly) if rule else None})
    free = []
    from apps.bookings import services
    from apps.bookings.models import BookingSettings

    lead = BookingSettings.load().min_lead_hours
    for lane in public_api._lanes():
        for i in range(days):
            d = today + timedelta(days=i)
            ranges = public_api._ranges(d, services.day_slots(lane, d, lead_hours=lead, holds=True))
            if ranges:
                free.append({"oś": lane.name, "data": d.isoformat(), "dzień": public_api.WEEKDAYS[d.weekday()],
                             "wolne": [f"{r['od']}–{r['do']}" for r in ranges]})
    events = []
    for e in Event.objects.filter(status=Event.Status.PUBLISHED, ends_at__gte=timezone.now()).order_by("starts_at")[:5]:
        events.append(_event_data(e))
    data = {
        "strzelnica": {"nazwa": "Strzelnica Sątok", "stowarzyszenie": assoc.name, "adres": assoc.range_place or assoc.address,
                       "telefon": assoc.contact_phone, "e-mail": assoc.contact_email, "strona": "https://strzelnicasatok.pl"},
        "rezerwacja": ps.booking_url(),
        "stałe_informacje": ps.facts,
        "osie_i_cennik": lanes,
        "zasada_ceny": "za godzinę płaci się mniejszą z kwot: liczba osób × cena za osobę albo cena wyłączności",
        "wolne_terminy_najbliższe_dni": free,
        "nadchodzące_wydarzenia": events,
        "dzisiaj": today.isoformat(),
    }
    if event is not None:
        data["wydarzenie_główne"] = _event_data(event)
    return data, ps


def _event_data(e):
    from django.urls import reverse

    taken = e.taken()
    return {"nazwa": e.title, "opis": e.description, "początek": timezone.localtime(e.starts_at).strftime("%Y-%m-%d %H:%M"),
            "koniec": timezone.localtime(e.ends_at).strftime("%Y-%m-%d %H:%M"), "opłata_zł": e.fee / 100,
            "limit_miejsc": e.capacity, "zapisanych": taken,
            "wolnych_miejsc": None if e.capacity is None else max(e.capacity - taken, 0),
            "zapisy_do": timezone.localtime(e.registration_until).strftime("%Y-%m-%d %H:%M"),
            "adres_zapisów": settings.SITE_URL.rstrip("/") + reverse("events:detail", args=[e.pk])}


def generate(brief, event=None):
    """Zwraca (dane publikacji dict, nazwa modelu, zużycie dict). Wyjątek GenerationError — czytelny opis problemu."""
    import anthropic

    data, ps = context_data(event)
    user = (f"Polecenie zarządu:\n{brief.strip()}\n\n"
            f"Styl:\n{ps.tone}\n\nStałe hasztagi: {ps.hashtags}\n\n"
            f"Dane strzelnicy (JSON):\n{json.dumps(data, ensure_ascii=False, indent=1)}")
    client = _client()
    model = getattr(settings, "PROMOTION_MODEL", "claude-opus-5-5")
    try:
        response = client.beta.messages.create(
            model=model,
            max_tokens=16000,
            betas=BETAS,
            fallbacks="default",
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.AuthenticationError as e:
        raise GenerationError("Klucz API Claude jest nieprawidłowy albo wygasł — wymień go w .env serwera.") from e
    except anthropic.RateLimitError as e:
        raise GenerationError("Przekroczony limit API Claude (albo miesięczny limit wydatków) — spróbuj później.") from e
    except anthropic.APIStatusError as e:
        logger.warning("Promocja: błąd API Claude %s (request %s)", e.status_code, getattr(e, "request_id", ""))
        raise GenerationError(f"Błąd usługi Claude ({e.status_code}) — spróbuj ponownie za chwilę.") from e
    except anthropic.APIConnectionError as e:
        raise GenerationError("Brak połączenia z usługą Claude — sprawdź internet serwera i spróbuj ponownie.") from e
    if response.stop_reason == "refusal":
        raise GenerationError("Model odmówił przygotowania tej treści — zmień polecenie (np. bez treści o sprzedaży broni).")
    if response.stop_reason == "max_tokens":
        raise GenerationError("Odpowiedź modelu została ucięta — skróć polecenie i spróbuj ponownie.")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        result = json.loads(text)
    except ValueError as e:
        raise GenerationError("Nieoczekiwana odpowiedź modelu — spróbuj ponownie.") from e
    usage = {"input": response.usage.input_tokens, "output": response.usage.output_tokens,
             "cache_read": getattr(response.usage, "cache_read_input_tokens", 0) or 0}
    return result, getattr(response, "model", model), usage
