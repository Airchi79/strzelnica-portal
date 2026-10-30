from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend


class EmailBackend(ModelBackend):
    """Logowanie e-mailem (bez rozróżniania wielkości liter)."""

    def authenticate(self, request, username=None, password=None, email=None, **kwargs):
        email = (email or username or "").strip().lower()
        if not email or password is None:
            return None
        User = get_user_model()
        try:
            user = User.objects.get(email__iexact=email)
        except User.DoesNotExist:
            User().set_password(password)  # wyrównanie czasu odpowiedzi
            return None
        if user.check_password(password) and self.user_can_authenticate(user):
            return user
        return None
