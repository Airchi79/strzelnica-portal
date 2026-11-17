import re

from django import forms
from django.contrib.auth import password_validation
from django.contrib.auth.forms import PasswordResetForm, SetPasswordForm

from .models import User

POSTAL_RE = re.compile(r"^\d{2}-\d{3}$")
PHONE_RE = re.compile(r"^\+?[\d ]{9,16}$")


class ProfileFieldsMixin:
    def clean_postal_code(self):
        value = self.cleaned_data.get("postal_code", "").strip()
        if value and not POSTAL_RE.match(value):
            raise forms.ValidationError("Podaj kod w formacie 00-000.")
        return value

    def clean_phone(self):
        value = self.cleaned_data.get("phone", "").strip()
        if value and not PHONE_RE.match(value):
            raise forms.ValidationError("Podaj poprawny numer telefonu.")
        return value


class RegistrationForm(ProfileFieldsMixin, forms.ModelForm):
    password1 = forms.CharField(label="Hasło", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    password2 = forms.CharField(label="Powtórz hasło", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))

    class Meta:
        model = User
        fields = ["email", "first_name", "last_name", "phone"]
        widgets = {"email": forms.EmailInput(attrs={"autocomplete": "email"})}

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise forms.ValidationError("Konto z tym adresem e-mail już istnieje.")
        return email

    def clean(self):
        data = super().clean()
        p1, p2 = data.get("password1"), data.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "Hasła nie są identyczne.")
        if p1:
            candidate = User(email=data.get("email", ""), first_name=data.get("first_name", ""), last_name=data.get("last_name", ""))
            try:
                password_validation.validate_password(p1, candidate)
            except forms.ValidationError as e:
                self.add_error("password1", e)
        return data

    def save(self, commit=True):
        user = super().save(commit=False)
        user.set_password(self.cleaned_data["password1"])
        if commit:
            user.save()
        return user


class LoginForm(forms.Form):
    email = forms.EmailField(label="E-mail", widget=forms.EmailInput(attrs={"autocomplete": "email", "autofocus": True}))
    password = forms.CharField(label="Hasło", widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))


class ProfileForm(ProfileFieldsMixin, forms.ModelForm):
    class Meta:
        model = User
        fields = ["first_name", "last_name", "phone", "street", "postal_code", "city"]


class PanelProfileForm(ProfileForm):
    """Konto panelu: bez adresu (potrzebny tylko strzelcowi, np. do wpisu w książce pobytu)."""

    class Meta(ProfileForm.Meta):
        fields = ["first_name", "last_name", "phone"]


class OTPTokenForm(forms.Form):
    token = forms.CharField(
        label="Kod z aplikacji",
        max_length=8,
        widget=forms.TextInput(attrs={"autocomplete": "one-time-code", "inputmode": "numeric", "autofocus": True}),
    )

    def clean_token(self):
        token = self.cleaned_data["token"].replace(" ", "")
        if not token.isdigit() or len(token) != 6:
            raise forms.ValidationError("Kod ma 6 cyfr.")
        return token


class ResendVerificationForm(forms.Form):
    email = forms.EmailField(label="E-mail")


class DeactivateForm(forms.Form):
    password = forms.CharField(label="Potwierdź hasłem", widget=forms.PasswordInput(attrs={"autocomplete": "current-password"}))


class StrictSetPasswordForm(SetPasswordForm):
    pass


class DirectPasswordResetForm(PasswordResetForm):
    """Reset hasła: wiadomość oznaczona nagłówkiem — w kopii testowej trafia na adres podany przez osobę (a nie na adres
    zbiorczy testu), jak na produkcji. Link może wykorzystać tylko właściciel skrzynki."""

    def send_mail(self, subject_template_name, email_template_name, context, from_email, to_email, html_email_template_name=None):
        from django.core.mail import EmailMultiAlternatives
        from django.template import loader

        from apps.core.mail_redirect import DIRECT_HEADER

        subject = "".join(loader.render_to_string(subject_template_name, context).splitlines())
        body = loader.render_to_string(email_template_name, context)
        message = EmailMultiAlternatives(subject, body, from_email, [to_email], headers={DIRECT_HEADER: "1"})
        try:
            message.send()
        except Exception:  # noqa: BLE001 — jak w Django: błąd poczty nie ujawnia, czy konto istnieje
            import logging

            logging.getLogger(__name__).exception("Nie wysłano e-maila resetu hasła (użytkownik %s)", context["user"].pk)
