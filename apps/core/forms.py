from django import forms

from django.core.exceptions import ValidationError

from . import branding
from .models import Announcement, AssociationSettings
from .permissions import PANEL_PERMISSIONS


class AssociationSettingsForm(forms.ModelForm):
    class Meta:
        model = AssociationSettings
        fields = ["name", "address", "nip", "regon", "bank_account", "contact_email", "contact_phone", "notify_email"]

    def clean_nip(self):
        nip = "".join(ch for ch in self.cleaned_data["nip"] if ch.isdigit())
        if nip:
            weights = [6, 5, 7, 2, 3, 4, 5, 6, 7]
            if len(nip) != 10 or sum(int(a) * w for a, w in zip(nip, weights)) % 11 != int(nip[9]):
                raise forms.ValidationError("Nieprawidłowy NIP.")
        return nip

    def clean_bank_account(self):
        raw = "".join(ch for ch in self.cleaned_data["bank_account"].upper() if ch.isalnum())
        if not raw:
            return ""
        if raw.isdigit() and len(raw) == 26:
            raw = "PL" + raw
        if len(raw) != 28 or not raw.startswith("PL"):
            raise forms.ValidationError("Podaj polski numer rachunku (26 cyfr).")
        rearranged = raw[4:] + raw[:4]
        if int("".join(str(int(c, 36)) for c in rearranged)) % 97 != 1:
            raise forms.ValidationError("Nieprawidłowa suma kontrolna numeru rachunku.")
        return raw


class UserPermissionsForm(forms.Form):
    permissions = forms.MultipleChoiceField(
        label="Uprawnienia w panelu zarządu", choices=PANEL_PERMISSIONS, widget=forms.CheckboxSelectMultiple, required=False
    )


class LogoForm(forms.Form):
    """Wgranie logo. Dla SVG ikony generuje przeglądarka (ukryte pola), dla PNG/JPG — serwer."""

    logo = forms.FileField(label="Plik logo", help_text="SVG (najlepiej), PNG lub JPG, do 1 MB. Najlepiej kwadratowe, na przezroczystym tle.")
    icon_background = forms.RegexField(
        label="Kolor tła ikony aplikacji", regex=r"^#[0-9a-fA-F]{6}$", initial="#1c2a21",
        widget=forms.TextInput(attrs={"type": "color"}),
        help_text="Tło ikony na telefonie (Android przycina ikonę do koła lub zaokrąglonego kwadratu, iPhone nie obsługuje przezroczystości).",
    )
    icon_192 = forms.CharField(widget=forms.HiddenInput, required=False)
    icon_512 = forms.CharField(widget=forms.HiddenInput, required=False)
    icon_maskable = forms.CharField(widget=forms.HiddenInput, required=False)

    def clean_logo(self):
        f = self.cleaned_data["logo"]
        if f.size > branding.MAX_LOGO_BYTES:
            raise ValidationError("Plik jest za duży (najwyżej 1 MB).")
        data = f.read()
        kind = branding.detect_type(data)
        if not kind:
            raise ValidationError("Dozwolone są pliki SVG, PNG i JPG.")
        if kind == "image/svg+xml":
            return {"type": kind, "data": branding.check_svg(data)}
        img = branding._open_raster(data)
        return {"type": "image/png", "data": branding.raster_to_png(img), "image": img}

    def clean(self):
        data = super().clean()
        logo = data.get("logo")
        if not logo or "icon_background" not in data:
            return data
        if "image" in logo:
            data["icons"] = branding.make_icons(logo["image"], data["icon_background"])
        else:
            try:
                data["icons"] = {k: branding.check_icon_data_url(data.get(k, ""), size) for k, size in branding.ICON_SIZES.items()}
            except ValidationError as e:
                self.add_error("logo", "Ikony z pliku SVG tworzy przeglądarka — wybierz plik ponownie i poczekaj na podgląd. " + e.messages[0])
        return data


class DateTimeInput(forms.DateTimeInput):
    input_type = "datetime-local"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%dT%H:%M", **kwargs)


class AnnouncementForm(forms.ModelForm):
    class Meta:
        model = Announcement
        fields = ["title", "body", "level", "visible_from", "visible_until", "email_audience", "booked_from", "booked_to"]
        widgets = {
            "body": forms.Textarea(attrs={"rows": 4}),
            "visible_from": DateTimeInput(), "visible_until": DateTimeInput(),
            "booked_from": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
            "booked_to": forms.DateInput(attrs={"type": "date"}, format="%Y-%m-%d"),
        }
        help_texts = {
            "email_audience": "E-mail tylko w sprawach korzystania ze strzelnicy (zamknięcie, zmiany godzin, regulamin). Wysyłka w tle, w partiach — może potrwać kilkanaście minut.",
            "booked_from": "Dla „osób z rezerwacją”: np. dzień zamknięcia strzelnicy.",
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in ("visible_from", "visible_until"):
            self.fields[f].input_formats = ["%Y-%m-%dT%H:%M"]
        if self.instance.pk and (self.instance.emailed_at or self.instance.email_queued):
            self.fields["email_audience"].disabled = True  # e-mail raz wysłany — nie wysyłamy drugi raz
            self.fields["email_audience"].help_text = "E-mail już wysłany lub w kolejce — nie zostanie wysłany ponownie."

    def clean(self):
        data = super().clean()
        a, b = data.get("visible_from"), data.get("visible_until")
        if a and b and b <= a:
            self.add_error("visible_until", "Koniec musi być później niż początek.")
        if data.get("email_audience") == Announcement.Audience.BOOKED:
            f, t = data.get("booked_from"), data.get("booked_to")
            if not f or not t:
                self.add_error("booked_from", "Podaj okres rezerwacji, których dotyczy ogłoszenie.")
            elif t < f:
                self.add_error("booked_to", "Data końcowa nie może być wcześniejsza.")
        return data
