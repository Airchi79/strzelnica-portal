from django import forms

from apps.core.forms import DateTimeInput
from apps.facilities.forms import PLNField
from apps.facilities.models import Lane

from .models import Event


class EventForm(forms.ModelForm):
    fee_pln = PLNField(label="Opłata startowa [zł]", required=False, help_text="0 lub puste — bezpłatne.")
    cancel_conflicts = forms.BooleanField(
        label="Anuluj kolidujące rezerwacje osi (bez opłat, wpłaty online wrócą przez Tpay, strzelcy dostaną e-mail)", required=False,
        help_text="Bez zaznaczenia rezerwacje zostają, a ich właściciele dostaną informację o blokadzie.",
    )

    class Meta:
        model = Event
        fields = ["title", "description", "starts_at", "ends_at", "lanes", "capacity", "registration_until", "payment_days", "note_label",
                  "status"]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 5}),
            "starts_at": DateTimeInput(), "ends_at": DateTimeInput(), "registration_until": DateTimeInput(),
            "lanes": forms.CheckboxSelectMultiple,
        }
        help_texts = {"lanes": "Zaznaczone osie zostaną zablokowane dla rezerwacji na czas wydarzenia (po opublikowaniu)."}

    field_order = ["title", "description", "starts_at", "ends_at", "lanes", "capacity", "fee_pln", "registration_until", "note_label", "status"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for f in ("starts_at", "ends_at", "registration_until"):
            self.fields[f].input_formats = ["%Y-%m-%dT%H:%M"]
        self.fields["lanes"].queryset = Lane.objects.filter(is_active=True).select_related("facility")
        self.fields["status"].choices = [c for c in Event.Status.choices if c[0] != Event.Status.CANCELLED]  # odwołanie osobnym przyciskiem
        if self.instance.pk:
            self.fields["fee_pln"].initial = self.instance.fee / 100
            if self.instance.registrations.exists():
                self.fields["fee_pln"].disabled = True
                self.fields["fee_pln"].help_text = "Są już zapisy — opłaty nie można zmienić."

    def clean(self):
        data = super().clean()
        a, b, r = data.get("starts_at"), data.get("ends_at"), data.get("registration_until")
        if a and b and b <= a:
            self.add_error("ends_at", "Koniec musi być później niż początek.")
        if a and r and r > a:
            self.add_error("registration_until", "Zapisy muszą się zamknąć najpóźniej z początkiem wydarzenia.")
        return data

    def save(self, commit=True):
        obj = super().save(commit=False)
        if not self.fields["fee_pln"].disabled:
            obj.fee = self.fields["fee_pln"].to_grosze(self.cleaned_data.get("fee_pln")) or 0
        if commit:
            obj.save()
        return obj


class RegisterForm(forms.Form):
    note = forms.CharField(max_length=120, required=False)

    def __init__(self, *args, event=None, **kwargs):
        super().__init__(*args, **kwargs)
        if event and event.note_label:
            self.fields["note"].label = event.note_label
            self.fields["note"].required = True
        else:
            del self.fields["note"]
