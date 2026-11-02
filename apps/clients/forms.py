from django import forms
from django.utils import timezone

from apps.core.dates import add_months
from apps.facilities.models import Lane

from .models import ClientPass


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        super().__init__(format="%Y-%m-%d", **kwargs)


class ProfileForm(forms.Form):
    notes = forms.CharField(label="Notatki obsługi", required=False, max_length=4000, widget=forms.Textarea(attrs={"rows": 3}),
                            help_text="Widoczne tylko w panelu. Nie wpisuj tu danych wrażliwych.")
    tags = forms.CharField(label="Tagi", required=False, max_length=400, help_text="Oddziel przecinkami, np. „sędzia, zawody, VIP”.")

    def clean_tags(self):
        names = [t.strip() for t in self.cleaned_data["tags"].split(",") if t.strip()]
        if any(len(n) > 40 for n in names):
            raise forms.ValidationError("Tag może mieć najwyżej 40 znaków.")
        if len(names) > 15:
            raise forms.ValidationError("Najwyżej 15 tagów.")
        return names


class PassForm(forms.Form):
    kind = forms.ChoiceField(label="Rodzaj", choices=ClientPass.Kind.choices, initial=ClientPass.Kind.PACK)
    entries_limit = forms.IntegerField(label="Liczba wejść (karnet)", min_value=1, max_value=500, required=False, initial=5)
    valid_from = forms.DateField(label="Ważny od", widget=DateInput())
    valid_until = forms.DateField(label="Ważny do", widget=DateInput())
    max_hours = forms.IntegerField(label="Maks. godzin na wejście", min_value=1, max_value=12, initial=2)
    max_persons = forms.IntegerField(label="Maks. osób na wejście", min_value=1, max_value=20, initial=1)
    lanes = forms.ModelMultipleChoiceField(label="Dozwolone osie (puste = wszystkie)", queryset=Lane.objects.none(), required=False,
                                           widget=forms.CheckboxSelectMultiple)
    price = forms.DecimalField(label="Cena [zł]", min_value=0, max_value=100000, decimal_places=2, initial=0)
    paid_with = forms.ChoiceField(label="Zapłata", choices=ClientPass.PaidWith.choices, required=False,
                                  help_text="Kod można wykorzystać dopiero po opłaceniu (lub oznaczeniu jako bezpłatny).")
    note = forms.CharField(label="Uwagi", max_length=255, required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lanes"].queryset = Lane.objects.filter(is_active=True).order_by("order")
        today = timezone.localdate()
        self.fields["valid_from"].initial = today
        self.fields["valid_until"].initial = add_months(today, 3)

    def clean(self):
        d = super().clean()
        if d.get("valid_from") and d.get("valid_until") and d["valid_until"] < d["valid_from"]:
            self.add_error("valid_until", "Koniec ważności nie może być przed początkiem.")
        if d.get("kind") == ClientPass.Kind.PACK and not d.get("entries_limit"):
            self.add_error("entries_limit", "Podaj liczbę wejść karnetu.")
        if d.get("price") is not None:
            d["price_gr"] = int(round(d["price"] * 100))
        return d


class PassPaidForm(forms.Form):
    paid_with = forms.ChoiceField(label="Zapłata", choices=[c for c in ClientPass.PaidWith.choices if c[0]])
