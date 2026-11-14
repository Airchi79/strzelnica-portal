from decimal import Decimal, InvalidOperation

from django import forms
from django.conf import settings
from django.utils import timezone

from apps.credentials.forms import ALLOWED_SIGNATURES

from .models import CATEGORY_ORDER, DEFAULT_LABELS, AccountingSettings, Record

COLUMN_CHOICES = [
    ("cost_deductible", "Kol. 6 — koszt uzyskania przychodów (art. 15 ustawy o CIT)"),
    ("cost_other", "Kol. 7 — koszt niestanowiący kosztu uzyskania przychodów (art. 16 ust. 1 ustawy o CIT)"),
]
DESCRIPTION_HINTS = {
    Record.Kind.REVENUE: ["Składki członkowskie", "Darowizna", "Dotacja", "Dochody z majątku stowarzyszenia — udostępnianie strzelnicy",
                          "Przychody z udostępniania strzelnicy (GunTracker) — zestawienie miesięczne", "Odsetki bankowe"],
    Record.Kind.COST: ["Energia elektryczna", "Woda i ścieki", "Wywóz odpadów", "Materiały i wyposażenie strzelnicy", "Tarcze",
                       "Usługi księgowe", "Opłaty bankowe", "Prowizja operatora płatności (Axepta)", "Ubezpieczenie",
                       "Podatek od nieruchomości", "Naprawy i konserwacja", "Hosting i domena", "Opłaty administracyjne i pocztowe"],
}


def parse_amount(value):
    raw = (value or "").replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        amount = Decimal(raw)
    except InvalidOperation as e:
        raise forms.ValidationError("Podaj kwotę, np. 125,50.") from e
    if amount <= 0 or amount.as_tuple().exponent < -2:
        raise forms.ValidationError("Kwota musi być dodatnia i mieć najwyżej dwa miejsca po przecinku.")
    if amount >= Decimal("100000000"):
        raise forms.ValidationError("Kwota jest za duża.")
    return int(amount * 100)


class RecordForm(forms.Form):
    date = forms.DateField(label="Data zdarzenia (kol. 2)", widget=forms.DateInput(attrs={"type": "date"}))
    document = forms.CharField(label="Nr dowodu księgowego (kol. 3)", max_length=64,
                               help_text="Np. numer faktury, rachunku, wyciągu bankowego albo dowodu wewnętrznego.")
    description = forms.CharField(label="Opis zdarzenia (kol. 4)", max_length=255)
    counterparty = forms.CharField(label="Kontrahent / wystawca dowodu", max_length=160, required=False)
    amount = forms.CharField(label="Kwota [zł]", max_length=16, widget=forms.TextInput(attrs={"inputmode": "decimal"}))
    column = forms.ChoiceField(label="Kolumna zestawienia", choices=COLUMN_CHOICES, widget=forms.RadioSelect, required=False)
    notes = forms.CharField(label="Uwagi (kol. 8)", max_length=255, required=False)
    attachment = forms.FileField(label="Skan dowodu (PDF, JPG, PNG — do 5 MB)", required=False)

    def __init__(self, *args, kind, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind = kind
        if kind == Record.Kind.REVENUE:
            del self.fields["column"]
        else:
            self.fields["column"].required = True
            self.fields["column"].initial = "cost_deductible"

    def clean_amount(self):
        return parse_amount(self.cleaned_data["amount"])

    def clean_date(self):
        value = self.cleaned_data["date"]
        if value > timezone.localdate():
            raise forms.ValidationError("Data zdarzenia nie może być z przyszłości.")
        return value

    def clean_attachment(self):
        f = self.cleaned_data.get("attachment")
        if not f:
            return f
        if f.size > settings.MAX_UPLOAD_BYTES:
            raise forms.ValidationError("Plik jest za duży (maksymalnie 5 MB).")
        head = f.read(16)
        f.seek(0)
        ext = next((e for sig, e in ALLOWED_SIGNATURES.items() if head.startswith(sig)), None)
        if ext is None:
            raise forms.ValidationError("Dozwolone są tylko pliki PDF, JPG i PNG.")
        f.name = f"dowod{ext}"
        return f

    @property
    def column_value(self):
        return "revenue" if self.kind == Record.Kind.REVENUE else self.cleaned_data["column"]


class CorrectionForm(forms.Form):
    date = forms.DateField(label="Data korekty", widget=forms.DateInput(attrs={"type": "date"}))
    reason = forms.CharField(label="Przyczyna korekty", max_length=255)


class SettingsForm(forms.ModelForm):
    class Meta:
        model = AccountingSettings
        fields = ["auto_from"]
        widgets = {"auto_from": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        labels = self.instance.labels or {}
        for category in CATEGORY_ORDER:
            self.fields[f"label_{category}"] = forms.CharField(
                label=f"Opis zdarzenia: {category.replace('_', ' ')}", max_length=255, required=False,
                initial=labels.get(category, ""), help_text=f"Domyślnie: {DEFAULT_LABELS[category]}",
                widget=forms.TextInput(attrs={"placeholder": DEFAULT_LABELS[category]}))

    def save(self, commit=True):
        obj = super().save(commit=False)
        obj.labels = {c: " ".join(self.cleaned_data[f"label_{c}"].split()) for c in CATEGORY_ORDER
                      if self.cleaned_data.get(f"label_{c}", "").strip()}
        if commit:
            obj.save()
        return obj
