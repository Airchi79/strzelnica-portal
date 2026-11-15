from decimal import Decimal, InvalidOperation

from django import forms
from django.conf import settings
from django.utils import timezone

from apps.credentials.forms import ALLOWED_SIGNATURES

from .models import (CATEGORY_ORDER, DEFAULT_LABELS, AccountingSettings, Activity, FixedAsset, KsefInvoice, KsefSync, PublicTask,
                     Record)

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
# Forma działalności proponowana przy przyjmowaniu faktur i operacji z wyciągu (zdecydowana większość zapisów).
DEFAULT_ACTIVITY = Activity.OTHER


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
        kwargs.setdefault("required", False)
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
    description = forms.CharField(label="Opis zdarzenia (kol. 4)", max_length=255,
                                  widget=forms.TextInput(attrs={"list": "opisy"}))  # podpowiedzi z <datalist id="opisy">
    counterparty = forms.CharField(label="Kontrahent / wystawca dowodu", max_length=160, required=False)
    amount = forms.CharField(label="Kwota [zł]", max_length=18, widget=forms.TextInput(attrs={"inputmode": "decimal"}))
    column = forms.ChoiceField(label="Kolumna zestawienia", choices=COST_COLUMNS, widget=forms.RadioSelect, required=False)
    notes = forms.CharField(label="Uwagi (kol. 8)", max_length=255, required=False)
    ksef_number = forms.CharField(label="Numer KSeF faktury (jeśli jest)", max_length=40, required=False)
    activity = forms.ChoiceField(label="Forma działalności (zestawienie uzupełniające)", required=False,
                                 choices=[("", "— nieprzypisane —")] + Activity.choices)
    task = forms.ModelChoiceField(label="Umowa o realizację zadania publicznego (dotacja)", required=False,
                                  queryset=PublicTask.objects.all(), empty_label="— nie dotyczy —")
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
                "column": record_column(record), "notes": record.notes, "ksef_number": record.ksef_number,
                "activity": record.activity, "task": record.task_id}


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
    description = forms.CharField(label="Opis zdarzenia (kol. 4)", max_length=255, widget=forms.TextInput(attrs={"list": "opisy"}))
    column = forms.ChoiceField(label="Kolumna zestawienia", choices=COST_COLUMNS, widget=forms.RadioSelect)
    notes = forms.CharField(label="Uwagi (kol. 8)", max_length=255, required=False)
    activity = forms.ChoiceField(label="Forma działalności (zestawienie uzupełniające)", required=False,
                                 choices=[("", "— nieprzypisane —")] + Activity.choices, initial=DEFAULT_ACTIVITY)
    task = forms.ModelChoiceField(label="Umowa o realizację zadania publicznego (dotacja)", required=False,
                                  queryset=PublicTask.objects.all(), empty_label="— nie dotyczy —")

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
                "activity": DEFAULT_ACTIVITY,
            })

    def clean_date(self):
        value = self.cleaned_data["date"]
        if value > timezone.localdate():
            raise forms.ValidationError("Data zdarzenia nie może być z przyszłości.")
        return value


class SettingsForm(forms.ModelForm):
    class Meta:
        model = AccountingSettings
        fields = ["auto_from", "auto_activity", "approving_body", "tax_office", "signatories"]
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


def _date(label, required=True, help_text=""):
    return forms.DateField(label=label, required=required, help_text=help_text,
                           widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))


class AssetForm(forms.Form):
    kind = forms.ChoiceField(label="Rodzaj", choices=FixedAsset.Kind.choices)
    name = forms.CharField(label="Określenie środka trwałego / WNiP (§ 3 ust. 1 pkt 5)", max_length=255)
    kst = forms.CharField(label="Symbol KŚT (pkt 6)", max_length=20, required=False,
                          help_text="Klasyfikacja Środków Trwałych — przy WNiP zostaw puste.")
    acquisition = forms.ChoiceField(label="Sposób pozyskania", choices=FixedAsset.Acquisition.choices)
    acquired_on = _date("Data nabycia / wytworzenia (pkt 2)")
    in_use_on = _date("Data przyjęcia do używania (pkt 3)")
    entered_on = _date("Data wprowadzenia do wykazu",
                       help_text="Najpóźniej w miesiącu przyjęcia do używania — później oznacza ujawnienie (§ 3 ust. 3).")
    acquisition_doc = forms.CharField(label="Rodzaj i numer dowodu nabycia / wytworzenia (pkt 4 lit. c)", max_length=160, required=False,
                                      help_text="Np. „faktura FV 12/2026 (KSeF …)”, „umowa darowizny z 3.02.2026”.")
    in_use_doc = forms.CharField(label="Numer dowodu przyjęcia do używania (pkt 4 lit. b)", max_length=64, required=False,
                                 help_text="Np. „OT 1/2026”.")
    initial_value = AmountField(label="Wartość początkowa [zł] (pkt 7)", required=True)
    updated_value = AmountField(label="Zaktualizowana wartość początkowa [zł] (pkt 10) — np. po ulepszeniu")
    method = forms.ChoiceField(label="Metoda", choices=FixedAsset.Method.choices, widget=forms.RadioSelect)
    rate = forms.DecimalField(label="Roczna stawka amortyzacyjna [%] (pkt 8)", max_digits=5, decimal_places=2, min_value=0,
                              max_value=100, required=False,
                              help_text="Z Wykazu rocznych stawek amortyzacyjnych (załącznik nr 1 do ustawy o CIT).")
    prior_depreciation = AmountField(label="Odpisy dokonane przed prowadzeniem wykazu w systemie [zł]",
                                     help_text="Tylko dla składników wprowadzonych do wykazu wcześniej (np. przed 2026 r.).")
    prior_until_year = forms.IntegerField(label="…dokonane do końca roku", min_value=1990, max_value=2100, required=False)
    notes = forms.CharField(label="Uwagi", max_length=255, required=False)
    reason = forms.CharField(label="Powód zmiany", max_length=255, required=False)

    def __init__(self, *args, editing=False, **kwargs):
        super().__init__(*args, **kwargs)
        if editing:
            self.fields["reason"].required = True
        else:
            del self.fields["reason"]
        if not self.is_bound and not self.initial.get("method"):
            self.initial["method"] = "liniowa"

    def clean_initial_value(self):
        value = self.cleaned_data["initial_value"]
        if not value:
            raise forms.ValidationError("Wartość początkowa musi być większa od zera.")
        return value

    def clean(self):
        data = super().clean()
        data["rate"] = data.get("rate") or Decimal(0)
        return data

    @staticmethod
    def initial_for(asset):
        return {f: getattr(asset, f) for f in ("kind", "name", "kst", "acquisition", "acquired_on", "in_use_on", "entered_on",
                                               "acquisition_doc", "in_use_doc", "initial_value", "updated_value", "method", "rate",
                                               "prior_depreciation", "prior_until_year", "notes")}


class DisposeForm(forms.Form):
    kind = forms.ChoiceField(label="Rodzaj", choices=FixedAsset.Disposal.choices)
    day = _date("Data likwidacji / zbycia / stwierdzenia niedoboru")
    reason = forms.CharField(label="Przyczyna likwidacji", max_length=255, required=False)
    doc = forms.CharField(label="Numer dowodu wykreślenia (np. protokół likwidacji LT 1/2026)", max_length=64, required=False)
    ksef_number = forms.CharField(label="Numer KSeF faktury zbycia (jeśli wystawiona w KSeF)", max_length=40, required=False)


class PostingForm(forms.Form):
    year = forms.IntegerField(min_value=2000, max_value=2100)
    month = forms.IntegerField(min_value=0, max_value=12)
    column = forms.ChoiceField(choices=[("cost_deductible", "kol. 6"), ("cost_other", "kol. 7")])


class TaskForm(forms.Form):
    name = forms.CharField(label="Nazwa zadania publicznego", max_length=255)
    agreement = forms.CharField(label="Numer umowy", max_length=80, required=False)
    grantor = forms.CharField(label="Zleceniodawca (organ, jednostka)", max_length=160)
    signed_on = _date("Data zawarcia umowy")
    starts_on = _date("Realizacja od")
    ends_on = _date("Realizacja do")
    grant = AmountField(label="Kwota dotacji [zł]", required=True)
    notes = forms.CharField(label="Uwagi", max_length=255, required=False)

    def clean_grant(self):
        value = self.cleaned_data["grant"]
        if not value:
            raise forms.ValidationError("Kwota dotacji musi być większa od zera.")
        return value


class DonationForm(forms.Form):
    date = _date("Data otrzymania")
    description = forms.CharField(label="Przedmiot darowizny", max_length=255,
                                  help_text="Bez danych osobowych darczyńcy (§ 4 ust. 2 rozporządzenia).")
    value = AmountField(label="Wartość [zł]", required=True, help_text="Wartość rynkowa z dnia otrzymania.")
    document = forms.CharField(label="Dowód (np. umowa darowizny nr …, protokół przyjęcia)", max_length=120, required=False)
    statutory = forms.BooleanField(label="Przeznaczona na działalność statutową (nie wlicza się do limitu przychodów)", required=False,
                                   initial=True)
    add_revenue = forms.BooleanField(label="Dodaj też zapis przychodu (kol. 5) w zestawieniu przychodów i kosztów", required=False,
                                     initial=True)
    activity = forms.ChoiceField(label="Forma działalności zapisu przychodu", required=False,
                                 choices=[("", "— nieprzypisane —")] + Activity.choices, initial=Activity.UNPAID)

    def clean_value(self):
        value = self.cleaned_data["value"]
        if not value:
            raise forms.ValidationError("Wartość musi być większa od zera.")
        return value


class ClassifyForm(forms.Form):
    activity = forms.ChoiceField(label="Forma działalności", required=False, choices=[("", "— bez zmiany —")] + Activity.choices)
    task = forms.ModelChoiceField(label="Umowa (dotacja)", required=False, queryset=PublicTask.objects.all(),
                                  empty_label="— bez zmiany —")
    clear_task = forms.BooleanField(label="Odłącz od umowy", required=False)


class KsefSyncForm(forms.ModelForm):
    class Meta:
        model = KsefSync
        fields = ["enabled", "sync_from", "include_sales"]
        widgets = {"sync_from": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d")}

    def clean(self):
        data = super().clean()
        if data.get("enabled") and not data.get("sync_from"):
            self.add_error("sync_from", "Podaj datę, od której pobierać faktury.")
        if data.get("sync_from") and data["sync_from"] > timezone.localdate():
            self.add_error("sync_from", "Data nie może być z przyszłości.")
        return data


class BankBookForm(forms.Form):
    date = forms.DateField(label="Data zdarzenia (kol. 2)", widget=forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"))
    document = forms.CharField(label="Nr dowodu księgowego (kol. 3)", max_length=64)
    description = forms.CharField(label="Opis zdarzenia (kol. 4)", max_length=255, widget=forms.TextInput(attrs={"list": "opisy"}))
    column = forms.ChoiceField(label="Kolumna zestawienia", choices=COST_COLUMNS, widget=forms.RadioSelect)
    activity = forms.ChoiceField(label="Forma działalności", required=False, choices=[("", "— nieprzypisane —")] + Activity.choices,
                                 initial=DEFAULT_ACTIVITY)
    task = forms.ModelChoiceField(label="Umowa o realizację zadania publicznego (dotacja)", required=False,
                                  queryset=PublicTask.objects.all(), empty_label="— nie dotyczy —")
    notes = forms.CharField(label="Uwagi (kol. 8)", max_length=255, required=False)

    def __init__(self, *args, op, suggestion="", **kwargs):
        super().__init__(*args, **kwargs)
        if op.is_inflow:
            self.fields["column"].choices = [("revenue", "Kol. 5 — przychód")]
        if not self.is_bound:
            self.initial.update({"date": op.booked_on, "document": f"Wyciąg bankowy {op.booked_on:%d.%m.%Y}",
                                 "description": suggestion, "notes": op.title[:255],
                                 "column": "revenue" if op.is_inflow else "cost_deductible", "activity": DEFAULT_ACTIVITY})

    def clean_date(self):
        value = self.cleaned_data["date"]
        if value > timezone.localdate():
            raise forms.ValidationError("Data zdarzenia nie może być z przyszłości.")
        return value


class BankBulkForm(forms.Form):
    description = forms.CharField(label="Opis zdarzenia (kol. 4) dla zaznaczonych", max_length=255,
                                  widget=forms.TextInput(attrs={"list": "opisy"}))
    cost_column = forms.ChoiceField(label="Kolumna dla wydatków", choices=COST_COLUMNS, initial="cost_deductible")
    activity = forms.ChoiceField(label="Forma działalności", required=False, choices=[("", "— nieprzypisane —")] + Activity.choices,
                                 initial=DEFAULT_ACTIVITY)
    task = forms.ModelChoiceField(label="Umowa (dotacja)", required=False, queryset=PublicTask.objects.all(), empty_label="— nie dotyczy —")


class BankInvoiceForm(forms.Form):
    invoice = forms.ModelChoiceField(label="Faktura KSeF opłacona tym przelewem", queryset=KsefInvoice.objects.none())

    def __init__(self, *args, op, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["invoice"].queryset = (KsefInvoice.objects.filter(direction=KsefInvoice.Direction.PURCHASE)
                                           .exclude(status=KsefInvoice.Status.REJECTED).exclude(bank_operations__isnull=False)
                                           .order_by("-issue_date"))
        self.fields["invoice"].label_from_instance = (
            lambda i: f"{i.invoice_number} · {i.seller_name} · {i.issue_date:%d.%m.%Y} · {format_amount(i.gross)} zł")


class KsefBulkForm(forms.Form):
    description = forms.CharField(
        label="Opis zdarzenia (kol. 4) dla zaznaczonych", max_length=255, required=False, widget=forms.TextInput(attrs={"list": "opisy"}),
        help_text="Puste — każda faktura dostanie własny opis (pozycje z faktury albo nazwa sprzedawcy).")
    cost_column = forms.ChoiceField(label="Kolumna dla faktur zakupu", choices=COST_COLUMNS, initial="cost_deductible")
    activity = forms.ChoiceField(label="Forma działalności", required=False, choices=[("", "— nieprzypisane —")] + Activity.choices,
                                 initial=DEFAULT_ACTIVITY)
    task = forms.ModelChoiceField(label="Umowa (dotacja)", required=False, queryset=PublicTask.objects.all(), empty_label="— nie dotyczy —")
