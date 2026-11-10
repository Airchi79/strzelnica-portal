"""Przygotowanie kopii testowej po wgraniu do niej bazy z produkcji (działa wyłącznie w SITE_ENVIRONMENT=test).

    python manage.py prepare_test_copy

Usuwa z kopii to, co mogłoby dotrzeć do prawdziwych osób albo urządzeń: subskrypcje powiadomień push (telefony
członków) i e-maile z kolejki produkcji, a także sparowania kiosków (kiosk testowy paruje się od nowa w panelu). Zapisuje w dzienniku znacznik przygotowania kopii; od tej
chwili push w kopii testowej trafia tylko na urządzenia zarejestrowane w niej po przygotowaniu.
"""

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction


class Command(BaseCommand):
    help = "Przygotowuje kopię testową po skopiowaniu bazy z produkcji (push, kolejka e-maili, kioski). Tylko SITE_ENVIRONMENT=test."

    def handle(self, *args, **options):
        if not settings.IS_TEST_COPY:
            raise CommandError("To polecenie działa wyłącznie w kopii testowej (SITE_ENVIRONMENT=test). Produkcja nie została zmieniona.")
        from apps.audit.services import log_event
        from apps.core.models import OutgoingEmail, PushSubscription
        from apps.kiosk.models import KioskDevice

        with transaction.atomic():
            push, _ = PushSubscription.objects.all().delete()
            outbox, _ = OutgoingEmail.objects.all().delete()
            kiosks = KioskDevice.objects.exclude(token_hash="").update(token_hash="", pairing_code_hash="", paired_at=None)
            log_event("system.kopia_testowa", summary=f"Przygotowano kopię testową: push {push}, e-maile w kolejce {outbox}, kioski {kiosks}")
        self.stdout.write(self.style.SUCCESS(
            f"Kopia testowa gotowa. Usunięto subskrypcji push: {push}, e-maili z kolejki: {outbox}; rozparowano kiosków: {kiosks} (kiosk testowy sparuj w panelu)."))
