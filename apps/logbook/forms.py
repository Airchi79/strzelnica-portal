from django import forms
from django.utils import timezone

from apps.bookings.models import Reservation

from .models import STATEMENT_TEXT, Entry, LogbookSettings


class ReservationChoiceField(forms.ModelChoiceField):
    def label_from_instance(self, r):
        # Bez nazwisk — wybór widoczny dla każdego, kto otworzy formularz przy wejściu.
        return f"{r.lane} · {timezone.localtime(r.starts_at):%H:%M}–{timezone.localtime(r.ends_at):%H:%M}"


class EntryForm(forms.Form):
    purpose = forms.ChoiceField(label="Cel pobytu", choices=Entry.Purpose.choices, widget=forms.RadioSelect)
    reservation = ReservationChoiceField(
        label="Rezerwacja (oś i godziny)", queryset=Reservation.objects.none(), required=False, empty_label="— wybierz —"
    )
    purpose_note = forms.CharField(label="Krótki opis (np. rodzaj prac)", max_length=160, required=False)
    first_name = forms.CharField(label="Imię", max_length=80, widget=forms.TextInput(attrs={"autocomplete": "given-name"}))
    last_name = forms.CharField(label="Nazwisko", max_length=120, widget=forms.TextInput(attrs={"autocomplete": "family-name"}))
    has_permit = forms.ChoiceField(
        label="Czy masz pozwolenie na broń?", choices=[("tak", "Tak"), ("nie", "Nie")], widget=forms.RadioSelect
    )
    permit_number = forms.CharField(label="Numer pozwolenia na broń", max_length=64, required=False)
    permit_issuer = forms.CharField(
        label="Organ, który wydał pozwolenie", max_length=160, required=False, help_text="Np. Komendant Wojewódzki Policji we Wrocławiu."
    )
    address = forms.CharField(
        label="Adres zamieszkania", max_length=255, required=False, widget=forms.TextInput(attrs={"autocomplete": "street-address"})
    )
    statement = forms.BooleanField(label=STATEMENT_TEXT)
    signature = forms.CharField(widget=forms.HiddenInput, required=False)

    def __init__(self, *args, reservations=None, fixed_reservation=None, purposes=None, shooting_without_reservation=False, **kwargs):
        super().__init__(*args, **kwargs)
        self.fixed_reservation = fixed_reservation
        # Kiosk na strzelnicy: strzelanie także bez rezerwacji (np. poza godzinami) — z podanym prowadzącym strzelanie.
        self.shooting_without_reservation = shooting_without_reservation
        if purposes:
            self.fields["purpose"].choices = [c for c in Entry.Purpose.choices if c[0] in purposes]
            if len(purposes) == 1:
                self.fields["purpose"].initial = purposes[0]
        if fixed_reservation is not None:
            del self.fields["reservation"]
        else:
            self.fields["reservation"].queryset = reservations if reservations is not None else Reservation.objects.none()

    def clean(self):
        data = super().clean()
        if data.get("has_permit") == "tak":
            for f in ("permit_number", "permit_issuer"):
                if not data.get(f, "").strip():
                    self.add_error(f, "Podaj numer pozwolenia i organ, który je wydał.")
        elif data.get("has_permit") == "nie" and not data.get("address", "").strip():
            self.add_error("address", "Bez pozwolenia na broń wymagany jest adres zamieszkania.")
        purpose = data.get("purpose")
        if "reservation" in self.fields and purpose == Entry.Purpose.SHOOTING and not data.get("reservation"):
            if not self.shooting_without_reservation:
                self.add_error("reservation", "Przy strzelaniu wybierz rezerwację, w ramach której strzelasz.")
            elif not data.get("purpose_note", "").strip():
                self.add_error("purpose_note", "Strzelanie bez rezerwacji: podaj, kto prowadzi strzelanie (imię i nazwisko).")
        if purpose in (Entry.Purpose.WORK, Entry.Purpose.OTHER) and not data.get("purpose_note", "").strip():
            self.add_error("purpose_note", "Opisz krótko cel pobytu.")
        data["has_permit"] = data.get("has_permit") == "tak"
        return data


class StaffEntryForm(EntryForm):
    """Wpis przez obsługę: osoba podpisuje się na urządzeniu obsługi; godzina wejścia może być wcześniejsza."""

    entered_at = forms.DateTimeField(
        label="Godzina wejścia", widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        input_formats=["%Y-%m-%dT%H:%M"],
    )
    field_order = ["entered_at", "purpose", "reservation"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["statement"].label = "Osoba potwierdza: " + STATEMENT_TEXT[0].lower() + STATEMENT_TEXT[1:]
        self.fields["has_permit"].label = "Czy osoba ma pozwolenie na broń?"
        if "reservation" in self.fields:
            self.fields["reservation"].label_from_instance = lambda r: (
                f"{r.number} · {r.lane} · {timezone.localtime(r.starts_at):%H:%M}–{timezone.localtime(r.ends_at):%H:%M} · {r.user.full_name}"
            )


class LogbookSettingsForm(forms.ModelForm):
    class Meta:
        model = LogbookSettings
        fields = ["retention_years", "window_before_minutes", "max_entries_per_hour", "auto_exit_hours"]

    def clean_retention_years(self):
        value = self.cleaned_data["retention_years"]
        if value < 1 or value > 50:
            raise forms.ValidationError("Podaj od 1 do 50 lat.")
        return value


class EntryCorrectionForm(forms.Form):
    """Korekta wpisu przez obsługę — pola wypełnione bieżącymi wartościami (po wcześniejszych korektach)."""

    first_name = forms.CharField(label="Imię", max_length=80)
    last_name = forms.CharField(label="Nazwisko", max_length=120)
    permit_number = forms.CharField(label="Numer pozwolenia na broń", max_length=64, required=False)
    permit_issuer = forms.CharField(label="Organ, który wydał pozwolenie", max_length=160, required=False)
    address = forms.CharField(label="Adres", max_length=255, required=False)
    purpose_note = forms.CharField(label="Opis celu / prowadzący strzelanie", max_length=160, required=False)
    exited_at = forms.DateTimeField(
        label="Wyjście", required=False, input_formats=["%Y-%m-%dT%H:%M"],
        widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
    )
    reason = forms.CharField(label="Powód korekty", max_length=255, help_text="Np. „literówka w nazwisku”, „osoba wyszła o 21:30”.")

    def __init__(self, *args, entry=None, **kwargs):
        super().__init__(*args, **kwargs)
        if entry is not None:
            for name in ("first_name", "last_name", "permit_number", "permit_issuer", "address", "purpose_note"):
                self.fields[name].initial = getattr(entry, name)
            self.fields["exited_at"].initial = timezone.localtime(entry.exited_at) if entry.exited_at else None
            drop = ["address"] if entry.has_permit else ["permit_number", "permit_issuer"]
            for name in drop:
                del self.fields[name]
