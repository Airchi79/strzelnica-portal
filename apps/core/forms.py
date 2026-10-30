from django import forms

from .models import AssociationSettings
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
