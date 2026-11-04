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


# Podpowiedzi zależne od rodzaju dokumentu — serwer ustawia je dla wybranego rodzaju,
# a skrypt credential.js podmienia je na żywo przy zmianie rodzaju.
KIND_HINTS = {
    Credential.Kind.RANGE_OFFICER: {
        "title": "Uprawnienia prowadzącego strzelanie",
        "lead": "Wymagane do samodzielnej rezerwacji osi.",
        "number_label": "Numer legitymacji PS",
        "number_placeholder": "np. PS/123/2019",
        "number_help": "Przepisz numer z legitymacji prowadzącego strzelanie dokładnie tak, jak jest wydrukowany.",
        "issuer_placeholder": "np. PZSS, LOK lub klub strzelecki",
        "issuer_help": "Organizacja, która wydała legitymację (np. klub, w którym zdawałeś egzamin).",
        "valid_help": "Uprawnienia PS są zwykle bezterminowe — zostaw puste, jeśli na legitymacji nie ma daty ważności.",
        "details_placeholder": "np. broń palna i pneumatyczna",
    },
    Credential.Kind.FIREARM_PERMIT: {
        "title": "Pozwolenie na broń",
        "lead": "Potwierdza prawo do posiadania broni.",
        "number_label": "Numer pozwolenia / legitymacji posiadacza broni",
        "number_placeholder": "numer z legitymacji posiadacza broni",
        "number_help": "Przepisz numer z legitymacji posiadacza broni.",
        "issuer_placeholder": "np. Komendant Wojewódzki Policji w Gorzowie Wlkp.",
        "issuer_help": "Organ, który wydał pozwolenie.",
        "valid_help": "Pozwolenie jest zwykle bezterminowe — zostaw puste, jeśli nie ma daty ważności.",
        "details_placeholder": "np. sportowe, kolekcjonerskie",
    },
    Credential.Kind.LICENSE: {
        "title": "Licencja zawodnicza",
        "lead": "Licencja sportowa na bieżący rok.",
        "number_label": "Numer licencji",
        "number_placeholder": "numer z licencji zawodniczej",
        "number_help": "Przepisz numer z licencji.",
        "issuer_placeholder": "np. Polski Związek Strzelectwa Sportowego",
        "issuer_help": "Związek, który wydał licencję.",
        "valid_help": "Licencja jest ważna do daty na dokumencie (zwykle do 31 grudnia) — wpisz ją.",
        "details_placeholder": "np. pistolet, karabin",
    },
    Credential.Kind.OTHER: {
        "title": "Inny dokument",
        "lead": "Np. patent strzelecki lub zaświadczenie.",
        "number_label": "Numer dokumentu",
        "number_placeholder": "numer z dokumentu",
        "number_help": "",
        "issuer_placeholder": "kto wydał dokument",
        "issuer_help": "",
        "valid_help": "Zostaw puste, jeśli dokument jest bezterminowy.",
        "details_placeholder": "czego dotyczy dokument",
    },
}
KIND_ORDER = [Credential.Kind.RANGE_OFFICER, Credential.Kind.FIREARM_PERMIT, Credential.Kind.LICENSE, Credential.Kind.OTHER]


class CredentialForm(forms.ModelForm):
    class Meta:
        model = Credential
        fields = ["kind", "number", "issuer", "issued_on", "valid_until", "details", "scan"]
        widgets = {
            "kind": forms.RadioSelect,
            "issued_on": DateInput(),
            "valid_until": DateInput(),
            # Bez „Obecnie: …/Wyczyść” — skan leży w prywatnym magazynie i nie ma publicznego adresu.
            "scan": forms.FileInput(attrs={"accept": "image/jpeg,image/png,application/pdf,image/*"}),
        }

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        labels = dict(Credential.Kind.choices)
        self.fields["kind"].choices = [(k.value, labels[k]) for k in KIND_ORDER]
        self.fields["issued_on"].widget.attrs["max"] = timezone.localdate().isoformat()
        self.fields["number"].widget.attrs.update({"autocomplete": "off", "autocapitalize": "characters", "spellcheck": "false"})
        self.fields["scan"].label = "Zdjęcie lub skan dokumentu"
        self.fields["scan"].help_text = (
            "Zalecane — przyspiesza weryfikację. Zdjęcie z telefonu (JPG/PNG) albo PDF do 5 MB; "
            "numer i nazwisko muszą być czytelne. Plik zostanie usunięty zaraz po weryfikacji."
        )
        self.apply_hints(self.current_kind())

    def current_kind(self):
        value = self.data.get(self.add_prefix("kind")) if self.is_bound else None
        value = value or self.initial.get("kind") or self.instance.kind or Credential.Kind.RANGE_OFFICER
        return value if value in KIND_HINTS else Credential.Kind.RANGE_OFFICER

    def apply_hints(self, kind):
        h = KIND_HINTS[kind]
        self.fields["number"].label = h["number_label"]
        self.fields["number"].help_text = h["number_help"]
        self.fields["number"].widget.attrs["placeholder"] = h["number_placeholder"]
        self.fields["issuer"].help_text = h["issuer_help"]
        self.fields["issuer"].widget.attrs["placeholder"] = h["issuer_placeholder"]
        self.fields["valid_until"].help_text = h["valid_help"]
        self.fields["details"].help_text = ""
        self.fields["details"].widget.attrs["placeholder"] = h["details_placeholder"]

    @staticmethod
    def hints_json():
        return {k.value: v for k, v in KIND_HINTS.items()}

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
            raise forms.ValidationError(
                "Dozwolone są tylko pliki PDF, JPG i PNG. Zdjęcie w formacie HEIC (iPhone) zrób przyciskiem „Zrób zdjęcie” "
                "— zostanie zapisane jako JPG."
            )
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
    note = forms.CharField(
        label="Uwagi (wymagane przy odrzuceniu)", max_length=255, required=False,
        widget=forms.Textarea(attrs={"rows": 3, "maxlength": 255}),
    )

    def clean(self):
        data = super().clean()
        if data.get("decision") == "reject" and not data.get("note", "").strip():
            self.add_error("note", "Podaj powód odrzucenia.")
        return data


class RevokeForm(forms.Form):
    reason = forms.CharField(
        label="Powód cofnięcia (strzelec dostanie go e-mailem)", max_length=200,
        widget=forms.Textarea(attrs={"rows": 2, "maxlength": 200}),
    )
