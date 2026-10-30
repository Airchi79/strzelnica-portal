import getpass

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Tworzy konto właściciela systemu (pełne uprawnienia, wymagane logowanie dwuskładnikowe)."

    def add_arguments(self, parser):
        parser.add_argument("--email", required=True)
        parser.add_argument("--first-name", required=True)
        parser.add_argument("--last-name", required=True)

    def handle(self, *args, email, first_name, last_name, **options):
        User = get_user_model()
        if User.objects.filter(email__iexact=email).exists():
            raise CommandError("Konto z tym adresem już istnieje.")
        password = getpass.getpass("Hasło: ")
        if password != getpass.getpass("Powtórz hasło: "):
            raise CommandError("Hasła nie są identyczne.")
        candidate = User(email=email, first_name=first_name, last_name=last_name)
        try:
            validate_password(password, candidate)
        except ValidationError as e:
            raise CommandError(" ".join(e.messages))
        User.objects.create_superuser(email=email, password=password, first_name=first_name, last_name=last_name)
        self.stdout.write(self.style.SUCCESS("Utworzono konto właściciela. Przy pierwszym logowaniu skonfiguruj aplikację 2FA."))
