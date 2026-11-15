from decimal import Decimal, InvalidOperation

from django import forms
from django.conf import settings
from django.utils import timezone

from apps.credentials.forms import ALLOWED_SIGNATURES

from .models import CATEGORY_ORDER, DEFAULT_LABELS, AccountingSettings, KsefInvoice, Record

COST_COLUMNS = [
    ("cost_deductible", "Kol. 6 — koszt uzyskania przychodów (art. 15 ustawy o CIT)"),
    ("cost_other", "Kol. 7 — koszt niestanowiący kosztu uzyskania przychodów (art. 16 ust. 1 ustawy o CIT, wydatki z dochodu wolnego)"),
]
ALL_COLUMNS = [("revenue", "Kol. 5 — przychód")] + COST_COLUMNS
DESCRIPTION_HINTS = {
    Record.Kind.REVENUE: ["Składki członkowskie", "Darowizna", "Dotacja", "Dochody z majątku stowarzyszenia — udostępnianie strzelnicy",
                          "Przychody z udostępniania strzelnicy (GunTracker) — zestawienie miesięczne", "Odsetki bankowe"],
    Record.Kind.COST: ["Energia elektryczna", "Woda i ścieki", "Wywóz odpadów", "Materiały i wyposażenie strzelnicy", "Tarcze",
                       "Usługi księgowe", "Opłaty bankowe", "Prowizja operatora płatności (Axepta)", "Ubezpieczenie",
                       "Podatek od nieruchomości", "Naprawy i konserwacja", "Hosting i domena", "Opłaty administracyjne i pocztowe"],
}
MAX_AMOUNT = Decimal(100_000_000)


def parse_amount(value, *, allow_zero=False, allow_negative=False):
    raw = (value or "").replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        amount = Decimal(raw)
    except InvalidOperation as e:
        raise forms.ValidationError("Podaj kwotę, np. 125,50.") from e
    if not amount.is_finite() or amount.as_tuple().exponent < -2:
        raise forms.ValidationError("Kwota może mieć najwyżej dwa miejsca po przecinku.")
    if (amount < 0 and not allow_negative) or (amount == 0 and not allow_zero):
        raise forms.ValidationError("Kwota musi być większa od zera.")
    if abs(amount) >= MAX_AMOUNT:
        raise forms.ValidationError("Kwota jest za duża.")
    return int(amount * 100)


def format_amount(grosze):
    if grosze is None:
        return ""
    return f"{grosze / 100:.2f}".replace(".", ",")


class AmountField(forms.CharField):
    """Kwota w złotych („1 234,56”) → grosze (int); pusta → None. Zero dozwolone."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("max_length", 18)
        kwargs.setdefault("widget", forms.TextInput(attrs={"inputmode": "decimal"}))
        super().__init__(*args, **kwargs)

    def prepare_value(self, value):
        return format_amount(value) if isinstance(value, int) else value

    def clean(self, value):
        value = super().clean(value)
        if value in (None, ""):
            return None
        return parse_amount(value, allow_zero=True)


def _check_scan(f):
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


class RecordForm(forms.Form):
    date = forms.DateField(label="Data zdarzenia (kol. 2)", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    document = forms.CharField(label="Nr dowodu księgowego (kol. 3)", max_length=64,
                               help_text="Np. numer faktury, rachunku, wyciągu bankowego albo dowodu wewnętrznego.")
    description = forms.CharField(label="Opis zdarzenia (kol. 4)", max_length=255)
    counterparty = forms.CharField(label="Kontrahent / wystawca dowodu", max_length=160, required=False)
    amount = forms.CharField(label="Kwota [zł]", max_length=18, widget=forms.TextInput(attrs={"inputmode": "decimal"}))
    column = forms.ChoiceField(label="Kolumna zestawienia", choices=COST_COLUMNS, widget=forms.RadioSelect, required=False)
    notes = forms.CharField(label="Uwagi (kol. 8)", max_length=255, required=False)
    ksef_number = forms.CharField(label="Numer KSeF faktury (jeśli jest)", max_length=40, required=False)
    attachment = forms.FileField(label="Skan dowodu (PDF, JPG, PNG — do 5 MB)", required=False)
    reason = forms.CharField(label="Powód zmiany", max_length=255, required=False,
                             help_text="Zapisuje się w historii zapisu razem z poprzednią treścią.")

    def __init__(self, *args, kind, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.kind, self.editing = kind, editing
        if kind in (Record.Kind.REVENUE, Record.Kind.REVENUE_AUTO):
            del self.fields["column"]
        else:
            self.fields["column"].required = True
            self.fields["column"].initial = "cost_deductible"
            if kind in (Record.Kind.KSEF, Record.Kind.CORRECTION):
                self.fields["column"].choices = ALL_COLUMNS
        if editing:
            self.fields["reason"].required = True
            self.fields["attachment"].label = "Nowy skan dowodu (zastąpi obecny)"
        else:
            del self.fields["reason"]

    def clean_amount(self):
        negative_ok = self.kind in (Record.Kind.KSEF, Record.Kind.REVENUE_AUTO, Record.Kind.CORRECTION)
        return parse_amount(self.cleaned_data["amount"], allow_negative=negative_ok)

    def clean_date(self):
        value = self.cleaned_data["date"]
        if value > timezone.localdate():
            raise forms.ValidationError("Data zdarzenia nie może być z przyszłości.")
        return value

    def clean_attachment(self):
        return _check_scan(self.cleaned_data.get("attachment"))

    @property
    def column_value(self):
        if "column" not in self.fields:
            return "revenue"
        return self.cleaned_data["column"]

    @classmethod
    def initial_for(cls, record):
        from .services import record_amount, record_column

        return {"date": record.date, "document": record.document, "description": record.description,
                "counterparty": record.counterparty, "amount": format_amount(record_amount(record)),
                "column": record_column(record), "notes": record.notes, "ksef_number": record.ksef_number}


class ReasonForm(forms.Form):
    reason = forms.CharField(label="Przyczyna", max_length=255)


class KsefImportForm(forms.Form):
    def clean(self):
        files = self.files.getlist("pliki")
        if not files:
            raise forms.ValidationError("Wybierz pliki XML faktur albo archiwum ZIP.")
        if len(files) > 100:
            raise forms.ValidationError("Za dużo plików naraz (najwyżej 100) — spakuj je do archiwum ZIP.")
        self.cleaned_data["files"] = files
        return self.cleaned_data


class KsefAcceptForm(forms.Form):
    date = forms.DateField(label="Data zdarzenia (kol. 2)", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                           help_text="Data poniesienia wydatku albo uzyskania przychodu — domyślnie data wystawienia faktury.")
    document = forms.CharField(label="Nr dowodu księgowego (kol. 3)", max_length=64)
    description = forms.CharField(label="Opis zdarzenia (kol. 4)", max_length=255)
    column = forms.ChoiceField(label="Kolumna zestawienia", choices=COST_COLUMNS, widget=forms.RadioSelect)
    notes = forms.CharField(label="Uwagi (kol. 8)", max_length=255, required=False)

    def __init__(self, *args, invoice, **kwargs):
        super().__init__(*args, **kwargs)
        if invoice.direction == KsefInvoice.Direction.SALE:
            self.fields["column"].choices = [("revenue", "Kol. 5 — przychód")]
        if not self.is_bound:
            self.initial.update({
                "date": invoice.issue_date, "document": invoice.invoice_number[:64],
                "description": (invoice.items_summary or invoice.seller_name)[:255], "notes": f"KSeF {invoice.ksef_number}"[:255]
                if invoice.ksef_number else "",
                "column": "revenue" if invoice.direction == KsefInvoice.Direction.SALE else "cost_deductible",
            })

    def clean_date(self):
        value = self.cleaned_data["date"]
        if value > timezone.localdate():
            raise forms.ValidationError("Data zdarzenia nie może być z przyszłości.")
        return value


class SettingsForm(forms.ModelForm):
    class Meta:
        model = AccountingSettings
        fields = ["auto_from", "approving_body", "tax_office", "signatories"]
        widgets = {"auto_from": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
                   "tax_office": forms.Textarea(attrs={"rows": 3}), "signatories": forms.Textarea(attrs={"rows": 3})}

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
