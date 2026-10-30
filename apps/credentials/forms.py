from django import forms
from django.conf import settings
from django.utils import timezone

from .models import Credential

ALLOWED_SIGNATURES = {
    b"%PDF-": ".pdf",
    b"\xff\xd8\xff": ".jpg",
    b"\x89PNG\r\n\x1a\n": ".png",
}


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class CredentialForm(forms.ModelForm):
    class Meta:
        model = Credential
        fields = ["kind", "number", "issuer", "issued_on", "valid_until", "details", "scan"]
        widgets = {"issued_on": DateInput(), "valid_until": DateInput()}

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["scan"].help_text = "Opcjonalnie: PDF, JPG lub PNG do 5 MB. Plik zostanie usunięty zaraz po weryfikacji."

    def clean_number(self):
        return " ".join(self.cleaned_data["number"].split()).upper()

    def clean_issued_on(self):
        value = self.cleaned_data["issued_on"]
        if value > timezone.localdate():
            raise forms.ValidationError("Data wydania nie może być z przyszłości.")
        return value

    def clean_scan(self):
        f = self.cleaned_data.get("scan")
        if not f or not hasattr(f, "read") or getattr(f, "_committed", False):
            return f
        if f.size > settings.MAX_UPLOAD_BYTES:
            raise forms.ValidationError("Plik jest za duży (maksymalnie 5 MB).")
        head = f.read(16)
        f.seek(0)
        ext = next((e for sig, e in ALLOWED_SIGNATURES.items() if head.startswith(sig)), None)
        if ext is None:
            raise forms.ValidationError("Dozwolone są tylko pliki PDF, JPG i PNG.")
        f.name = f"skan{ext}"
        return f

    def clean(self):
        data = super().clean()
        issued, until = data.get("issued_on"), data.get("valid_until")
        if issued and until and until < issued:
            self.add_error("valid_until", "Data ważności nie może być wcześniejsza niż data wydania.")
        kind, number = data.get("kind"), data.get("number")
        if self.user and kind and number:
            dup = Credential.objects.filter(user=self.user, kind=kind, number=number).exclude(pk=self.instance.pk)
            if dup.exists():
                self.add_error("number", "Masz już taki dokument w systemie.")
        return data


class ReviewForm(forms.Form):
    decision = forms.ChoiceField(choices=[("approve", "Zatwierdź"), ("reject", "Odrzuć")], widget=forms.HiddenInput)
    note = forms.CharField(label="Uwagi (wymagane przy odrzuceniu)", max_length=255, required=False)

    def clean(self):
        data = super().clean()
        if data.get("decision") == "reject" and not data.get("note", "").strip():
            self.add_error("note", "Podaj powód odrzucenia.")
        return data
