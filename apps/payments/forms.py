from decimal import Decimal

from django import forms
from django.contrib.auth import get_user_model

from apps.facilities.forms import PLNField

from .models import Payment, PaymentSettings

MIN_TOPUP = Decimal("10")
MAX_TOPUP = Decimal("5000")


class TopupForm(forms.Form):
    amount = PLNField(label="Kwota doładowania [zł]")

    def clean_amount(self):
        value = self.cleaned_data["amount"]
        if value < MIN_TOPUP or value > MAX_TOPUP:
            raise forms.ValidationError(f"Kwota od {MIN_TOPUP} do {MAX_TOPUP} zł.")
        return self.fields["amount"].to_grosze(value)


class UserChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, u):
        return f"{u.full_name} ({u.email})"


def _users():
    return get_user_model().objects.filter(is_active=True).order_by("last_name", "first_name")


class ManualPaymentForm(forms.Form):
    user = UserChoiceField(label="Strzelec", queryset=get_user_model().objects.none())
    amount = PLNField(label="Kwota [zł]")
    method = forms.ChoiceField(
        label="Sposób",
        choices=[(Payment.Method.CASH, "Gotówka"), (Payment.Method.BLIK_PHONE, "BLIK na telefon"), (Payment.Method.TRANSFER, "Przelew (ręcznie)")],
    )
    note = forms.CharField(label="Uwagi", max_length=255, required=False)
    reservation = forms.IntegerField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, onsite_only=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = _users()
        if onsite_only:  # dyżurny: tylko pieniądze przyjęte na miejscu
            self.fields["method"].choices = [(Payment.Method.CASH, "Gotówka"), (Payment.Method.BLIK_PHONE, "BLIK na telefon")]

    def clean_amount(self):
        value = self.cleaned_data["amount"]
        if value <= 0:
            raise forms.ValidationError("Kwota musi być dodatnia.")
        return self.fields["amount"].to_grosze(value)


class CorrectionForm(forms.Form):
    direction = forms.ChoiceField(label="Rodzaj", choices=[("plus", "Uznanie (+)"), ("minus", "Obciążenie (−)")])
    amount = PLNField(label="Kwota [zł]")
    reason = forms.CharField(label="Powód (widoczny w historii salda)", max_length=200)

    def clean(self):
        data = super().clean()
        if data.get("amount") is not None:
            if data["amount"] <= 0:
                self.add_error("amount", "Kwota musi być dodatnia.")
            else:
                grosze = self.fields["amount"].to_grosze(data["amount"])
                data["signed"] = grosze if data.get("direction") == "plus" else -grosze
        return data


class BankImportForm(forms.Form):
    file = forms.FileField(label="Plik wyciągu (CSV z bankowości internetowej)")

    def clean_file(self):
        f = self.cleaned_data["file"]
        if f.size > 5 * 1024 * 1024:
            raise forms.ValidationError("Plik jest za duży (maks. 5 MB).")
        if not f.name.lower().endswith((".csv", ".txt")):
            raise forms.ValidationError("Wgraj plik CSV.")
        return f


class AssignForm(forms.Form):
    user = UserChoiceField(label="Przypisz do strzelca", queryset=get_user_model().objects.none(), required=False)
    reason = forms.CharField(label="Powód pominięcia (np. darowizna, wpłata nie dotyczy rezerwacji)", max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = _users()


class PaymentSettingsForm(forms.ModelForm):
    class Meta:
        model = PaymentSettings
        fields = ["wallet_enabled", "overpayment_pays_reservations"]
