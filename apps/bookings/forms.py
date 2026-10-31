from django import forms
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from apps.credentials.forms import DateInput
from apps.credentials.models import Credential
from apps.facilities.models import Lane

from .models import BookingSettings

HOUR_START = [(h, f"{h:02d}:00") for h in range(0, 24)]
HOUR_END = [(h, f"{h:02d}:00") for h in range(1, 25)]


def active_lanes():
    return Lane.objects.filter(is_active=True, facility__is_active=True).select_related("facility")


class ReservationForm(forms.Form):
    lane = forms.ModelChoiceField(label="Oś", queryset=Lane.objects.none(), empty_label=None)
    day = forms.DateField(label="Dzień", widget=DateInput())
    start_hour = forms.TypedChoiceField(label="Od", choices=HOUR_START, coerce=int)
    end_hour = forms.TypedChoiceField(label="Do", choices=HOUR_END, coerce=int)
    persons = forms.IntegerField(label="Liczba osób", min_value=1, initial=1, widget=forms.NumberInput(attrs={"inputmode": "numeric"}))
    notes = forms.CharField(label="Uwagi dla zarządu", max_length=500, required=False, widget=forms.Textarea(attrs={"rows": 2}))
    access_code = forms.CharField(
        label="Kod dostępu klubu", max_length=16, required=False,
        help_text="Tylko dla klubów z umową — rezerwacja jest wtedy rozliczana wg umowy z klubem.",
        widget=forms.TextInput(attrs={"autocomplete": "off", "autocapitalize": "characters"}),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lane"].queryset = active_lanes()
        self.fields["persons"].max_value = BookingSettings.load().max_persons
        self.fields["persons"].widget.attrs["max"] = self.fields["persons"].max_value
        self.fields["day"].widget.attrs["min"] = timezone.localdate().isoformat()


def range_officers():
    today = timezone.localdate()
    User = get_user_model()
    return (
        User.objects.filter(
            Q(credentials__valid_until__isnull=True) | Q(credentials__valid_until__gte=today),
            is_active=True,
            credentials__kind=Credential.Kind.RANGE_OFFICER,
            credentials__status=Credential.Status.VERIFIED,
        )
        .distinct()
        .order_by("last_name", "first_name")
    )


class BoardReservationForm(ReservationForm):
    user = forms.ModelChoiceField(label="Rezerwujący (zweryfikowany PS)", queryset=get_user_model().objects.none())

    field_order = ["user", "lane", "day", "start_hour", "end_hour", "persons", "access_code", "notes"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = range_officers()
        self.fields["user"].label_from_instance = lambda u: f"{u.full_name} ({u.email})"
        self.fields["day"].widget.attrs.pop("min", None)


class CancelForm(forms.Form):
    reason = forms.CharField(label="Powód (opcjonalnie)", max_length=255, required=False)


class BoardCancelForm(forms.Form):
    reason = forms.CharField(label="Powód anulowania (strzelec zobaczy go w e-mailu)", max_length=255)


class PersonsForm(forms.Form):
    persons = forms.IntegerField(label="Liczba osób", min_value=1)


class BlockForm(forms.Form):
    title = forms.CharField(label="Tytuł", max_length=120, help_text="Np. Zawody klubowe, Prace porządkowe.")
    reason = forms.CharField(label="Opis", max_length=255, required=False)
    lanes = forms.ModelMultipleChoiceField(label="Osie", queryset=Lane.objects.none(), widget=forms.CheckboxSelectMultiple)
    day_from = forms.DateField(label="Od dnia", widget=DateInput())
    day_to = forms.DateField(label="Do dnia", widget=DateInput())
    start_hour = forms.TypedChoiceField(label="Od godziny", choices=HOUR_START, coerce=int, initial=0)
    end_hour = forms.TypedChoiceField(label="Do godziny", choices=HOUR_END, coerce=int, initial=24)
    weekly = forms.BooleanField(
        label="Powtarzaj co tydzień",
        required=False,
        help_text="Zaznaczone: blokada w każdy taki dzień tygodnia jak „od dnia”, w podanych godzinach, aż do „do dnia”. "
        "Niezaznaczone: jedna ciągła blokada od pierwszego dnia i godziny do ostatniego dnia i godziny.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lanes"].queryset = Lane.objects.select_related("facility")

    def clean(self):
        data = super().clean()
        d1, d2, h1, h2 = data.get("day_from"), data.get("day_to"), data.get("start_hour"), data.get("end_hour")
        if d1 and d2:
            if d2 < d1:
                self.add_error("day_to", "Data końcowa nie może być wcześniejsza.")
            elif (d2 - d1).days > 366:
                self.add_error("day_to", "Blokada może obejmować najwyżej rok.")
            if h1 is not None and h2 is not None:
                same_day_or_weekly = data.get("weekly") or d1 == d2
                if same_day_or_weekly and h2 <= h1:
                    self.add_error("end_hour", "Godzina końcowa musi być późniejsza niż początkowa.")
        return data


class BookingSettingsForm(forms.ModelForm):
    class Meta:
        model = BookingSettings
        fields = ["free_cancel_hours", "max_advance_days", "max_persons"]

    def clean_max_persons(self):
        value = self.cleaned_data["max_persons"]
        if value < 1:
            raise forms.ValidationError("Co najmniej 1.")
        return value
