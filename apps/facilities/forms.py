from decimal import ROUND_HALF_UP, Decimal

from django import forms
from django.forms import BaseInlineFormSet, inlineformset_factory

from apps.credentials.forms import DateInput

from .models import Facility, Lane, OpeningHours, PriceRule


class FacilityForm(forms.ModelForm):
    class Meta:
        model = Facility
        fields = ["name", "kind", "is_active", "order"]
        help_texts = {"is_active": "Wyłączony obiekt i jego osie znikają z rezerwacji (historia zostaje).",
                      "order": "Kolejność na liście (mniejsza liczba — wyżej)."}


class LaneForm(forms.ModelForm):
    class Meta:
        model = Lane
        fields = ["facility", "name", "kind", "length_m", "description", "exclusive_only", "is_active", "order"]
        widgets = {"description": forms.Textarea(attrs={"rows": 3})}
        help_texts = {"is_active": "Wyłączona oś znika z rezerwacji; istniejące rezerwacje zostają.",
                      "order": "Kolejność w kalendarzu (mniejsza liczba — wyżej)."}


class NewLaneForm(LaneForm):
    """Nowa oś: opcjonalnie godziny skopiowane z innej osi i pierwszy cennik."""

    copy_hours_from = forms.ModelChoiceField(label="Godziny otwarcia skopiuj z osi", queryset=Lane.objects.none(), required=False,
                                             empty_label="— bez godzin (ustawię później) —")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["copy_hours_from"].queryset = Lane.objects.select_related("facility").order_by("facility__order", "order")
        self.fields["copy_hours_from"].label_from_instance = lambda lane: f"{lane.facility} · {lane}"
        self.fields["price_per_person"] = PLNField(label="Cennik: stawka za osobę / h [zł]", required=False,
                                                   help_text="Puste = oś tylko z ryczałtem.")
        self.fields["price_flat"] = PLNField(label="Cennik: ryczałt / h [zł]", required=False,
                                             help_text="Puste = bez cennika (dodasz później; bez cennika oś nie przyjmuje rezerwacji).")

    def clean(self):
        d = super().clean()
        if d.get("price_per_person") is not None and d.get("price_flat") is None:
            self.add_error("price_flat", "Podaj ryczałt (maksymalną cenę za godzinę).")
        return d


class TimeInput(forms.TimeInput):
    input_type = "time"

    def __init__(self, **kwargs):
        super().__init__(format="%H:%M", **kwargs)


class OpeningHoursForm(forms.ModelForm):
    class Meta:
        model = OpeningHours
        fields = ["weekday", "opens", "closes"]
        widgets = {"opens": TimeInput(), "closes": TimeInput()}

    def clean(self):
        data = super().clean()
        opens, closes = data.get("opens"), data.get("closes")
        if opens and closes:
            if closes <= opens:
                raise forms.ValidationError("Godzina zamknięcia musi być późniejsza niż otwarcia.")
            if opens.minute or closes.minute:
                raise forms.ValidationError("Rezerwacje są w pełnych godzinach, więc godziny otwarcia też muszą być pełne.")
        return data


class BaseHoursFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        seen = set()
        for form in self.forms:
            if not form.cleaned_data or form.cleaned_data.get("DELETE"):
                continue
            day = form.cleaned_data.get("weekday")
            if day in seen:
                raise forms.ValidationError("Każdy dzień tygodnia może wystąpić tylko raz.")
            seen.add(day)


HoursFormSet = inlineformset_factory(
    Lane, OpeningHours, form=OpeningHoursForm, formset=BaseHoursFormSet, extra=0, can_delete=True, max_num=7
)


class PLNField(forms.DecimalField):
    """Kwota w złotych w formularzu, zapisywana w groszach."""

    def __init__(self, **kwargs):
        super().__init__(max_digits=9, decimal_places=2, min_value=Decimal("0"), localize=True, **kwargs)

    def to_grosze(self, value):
        if value is None:
            return None
        return int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


class PriceRuleForm(forms.ModelForm):
    per_person = PLNField(label="Stawka za osobę / h [zł]", required=False, help_text="Puste = oś tylko z ryczałtem.")
    flat = PLNField(label="Ryczałt / h [zł]")

    class Meta:
        model = PriceRule
        fields = ["valid_from"]
        widgets = {"valid_from": DateInput()}

    def __init__(self, *args, lane=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.lane = lane

    def clean_valid_from(self):
        value = self.cleaned_data["valid_from"]
        if PriceRule.objects.filter(lane=self.lane, valid_from=value).exists():
            raise forms.ValidationError("Cennik od tej daty już istnieje dla tej osi.")
        return value

    def save(self, commit=True):
        rule = super().save(commit=False)
        rule.lane = self.lane
        rule.per_person_hourly = self.fields["per_person"].to_grosze(self.cleaned_data.get("per_person"))
        rule.flat_hourly = self.fields["flat"].to_grosze(self.cleaned_data["flat"])
        if commit:
            rule.save()
        return rule
