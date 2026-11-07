from django import forms
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.credentials.forms import DateInput
from apps.facilities.forms import PLNField
from apps.facilities.models import Lane

from .models import AccessCode, Club, GeneralMeeting, Member, MembershipApplication, Resolution, Vote
from .services import APPLICATION_STATEMENT

User = get_user_model()


class ApplicationForm(forms.Form):
    motivation = forms.CharField(
        label="Zainteresowania / czym chcesz się zajmować w Stowarzyszeniu", max_length=500, required=False,
        widget=forms.Textarea(attrs={"rows": 3}),
    )
    accept = forms.BooleanField(label=APPLICATION_STATEMENT)


class MemberForm(forms.Form):
    user = forms.ModelChoiceField(
        label="Konto w systemie (opcjonalnie)", queryset=User.objects.none(), required=False,
        help_text="Gdy osoba nie ma konta — zostaw puste i podaj dane poniżej. Konto można połączyć później.",
    )
    first_name = forms.CharField(label="Imię", max_length=80, required=False)
    last_name = forms.CharField(label="Nazwisko", max_length=120, required=False)
    email = forms.EmailField(label="E-mail", required=False)
    address = forms.CharField(label="Adres zamieszkania", max_length=255, required=False)
    number = forms.IntegerField(label="Numer członkowski", min_value=1, required=False, help_text="Puste = kolejny wolny numer.")
    kind = forms.ChoiceField(label="Rodzaj członkostwa", choices=Member.Kind.choices)
    joined_on = forms.DateField(label="Data przyjęcia", widget=DateInput())
    founder = forms.BooleanField(label="Założyciel", required=False)
    board_role = forms.ChoiceField(label="Funkcja w Zarządzie", choices=Member.BoardRole.choices, required=False)
    charge_fees = forms.BooleanField(
        label="Nalicz wpisowe i składkę za rok przyjęcia", required=False,
        help_text="Zaznacz tylko dla nowo przyjmowanych członków zwyczajnych. Członkowie nadzwyczajni są zwolnieni ze składek.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = User.objects.filter(is_active=True, member__isnull=True).order_by("last_name")
        self.fields["user"].label_from_instance = lambda u: f"{u.full_name} ({u.email})"

    def clean(self):
        data = super().clean()
        if not data.get("user") and not (data.get("first_name") and data.get("last_name")):
            raise forms.ValidationError("Wybierz konto albo podaj imię i nazwisko.")
        return data


class MemberEditForm(forms.ModelForm):
    class Meta:
        model = Member
        fields = ["first_name", "last_name", "email", "address", "founder"]


class LinkAccountForm(forms.Form):
    user = forms.ModelChoiceField(label="Połącz z kontem", queryset=User.objects.none())

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["user"].queryset = User.objects.filter(is_active=True, member__isnull=True).order_by("last_name")
        self.fields["user"].label_from_instance = lambda u: f"{u.full_name} ({u.email})"


class BoardRoleForm(forms.Form):
    board_role = forms.ChoiceField(label="Funkcja w Zarządzie", choices=Member.BoardRole.choices, required=False)


class CreateAccountForm(forms.Form):
    email = forms.EmailField(label="E-mail")
    first_name = forms.CharField(label="Imię", max_length=80)
    last_name = forms.CharField(label="Nazwisko", max_length=120)


class FeePaymentForm(forms.Form):
    method = forms.ChoiceField(label="Sposób", choices=[("gotowka", "Gotówka"), ("blik", "BLIK na telefon"), ("przelew", "Przelew (ręcznie)")])
    note = forms.CharField(label="Uwagi", max_length=255, required=False)


class MemberMultipleChoice(forms.ModelMultipleChoiceField):
    def label_from_instance(self, m):
        return f"{m.number}. {m.full_name} — {m.get_kind_display().lower()}, {m.get_status_display().lower()}"


class ApplicationMultipleChoice(forms.ModelMultipleChoiceField):
    def label_from_instance(self, a):
        return f"{a.user.full_name} ({a.user.email}) — złożona {timezone.localtime(a.submitted_at):%d.%m.%Y}, termin {a.deadline:%d.%m.%Y}"


class ResolutionForm(forms.Form):
    organ = forms.ChoiceField(label="Organ", choices=[c for c in Resolution.Organ.choices if c[0] != Resolution.Organ.FOUNDING])
    title = forms.CharField(label="W sprawie", max_length=255)
    content = forms.CharField(label="Treść uchwały", widget=forms.Textarea(attrs={"rows": 10}))
    action = forms.ChoiceField(label="Skutek po przyjęciu", choices=Resolution.Action.choices, required=False)
    applications = ApplicationMultipleChoice(
        label="Deklaracje (przyjęcie / odmowa)", queryset=MembershipApplication.objects.none(), required=False, widget=forms.CheckboxSelectMultiple,
    )
    members = MemberMultipleChoice(label="Członkowie (zawieszenie, przywrócenie, wykreślenie, zmiana rodzaju, umorzenie)",
                                   queryset=Member.objects.none(), required=False, widget=forms.CheckboxSelectMultiple)
    new_kind = forms.ChoiceField(label="Nowy rodzaj członkostwa", choices=[("", "—")] + list(Member.Kind.choices), required=False)
    annual_fee = PLNField(label="Składka roczna [zł]", required=False)
    entry_fee = PLNField(label="Wpisowe [zł]", required=False)
    meeting = forms.ModelChoiceField(label="Zebranie (dla uchwał WZC / nadzwyczajnych)", queryset=GeneralMeeting.objects.none(), required=False)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["applications"].queryset = MembershipApplication.objects.filter(status=MembershipApplication.Status.PENDING).select_related("user")
        self.fields["members"].queryset = Member.objects.all()
        self.fields["meeting"].queryset = GeneralMeeting.objects.filter(closed=False)

    def clean(self):
        data = super().clean()
        params = {}
        action = data.get("action") or ""
        if action == Resolution.Action.SET_FEES:
            if data.get("annual_fee") is None or data.get("entry_fee") is None:
                raise forms.ValidationError("Podaj wysokość składki rocznej i wpisowego.")
            params = {"annual_fee": self.fields["annual_fee"].to_grosze(data["annual_fee"]),
                      "entry_fee": self.fields["entry_fee"].to_grosze(data["entry_fee"])}
        if action == Resolution.Action.CHANGE_KIND:
            params = {"kind": data.get("new_kind")}
        if action in (Resolution.Action.ADMIT, Resolution.Action.REJECT_APPLICATIONS) and not data.get("applications"):
            raise forms.ValidationError("Zaznacz deklaracje, których dotyczy uchwała.")
        if action in (Resolution.Action.SUSPEND, Resolution.Action.REINSTATE, Resolution.Action.REMOVE,
                      Resolution.Action.CHANGE_KIND, Resolution.Action.WAIVE_FEES) and not data.get("members"):
            raise forms.ValidationError("Zaznacz członków, których dotyczy uchwała.")
        data["params"] = params
        return data


class HistoricalResolutionForm(forms.Form):
    organ = forms.ChoiceField(label="Organ", choices=Resolution.Organ.choices)
    number = forms.IntegerField(label="Numer", min_value=1)
    year = forms.IntegerField(label="Rok", min_value=2000, max_value=2100)
    adopted_on = forms.DateField(label="Data podjęcia", widget=DateInput())
    title = forms.CharField(label="W sprawie", max_length=255)
    content = forms.CharField(label="Treść (bez danych takich jak PESEL)", widget=forms.Textarea(attrs={"rows": 6}))
    attachment = forms.FileField(label="Skan (PDF)", required=False)

    def clean_attachment(self):
        f = self.cleaned_data.get("attachment")
        if f:
            if f.size > 15 * 1024 * 1024:
                raise forms.ValidationError("Plik jest za duży (maks. 15 MB).")
            head = f.read(5)
            f.seek(0)
            if head != b"%PDF-":
                raise forms.ValidationError("Dozwolony jest tylko plik PDF.")
        return f


class VoteForm(forms.Form):
    choice = forms.ChoiceField(choices=Vote.Choice.choices, widget=forms.RadioSelect)


class MeetingForm(forms.ModelForm):
    class Meta:
        model = GeneralMeeting
        fields = ["kind", "held_on", "place", "chair", "secretary", "announced_on", "notes"]
        widgets = {"held_on": DateInput(), "announced_on": DateInput(), "notes": forms.Textarea(attrs={"rows": 4})}

    def clean(self):
        data = super().clean()
        held, announced = data.get("held_on"), data.get("announced_on")
        if held and announced and (held - announced).days < 14:
            self.add_error("announced_on", "Zebranie ogłasza się co najmniej 14 dni wcześniej (§ 15 Regulaminu).")
        return data


class ClubForm(forms.ModelForm):
    class Meta:
        model = Club
        fields = ["name", "nip", "contact_name", "contact_email", "contact_phone", "agreement", "is_active"]
        widgets = {"agreement": forms.Textarea(attrs={"rows": 4})}


class AccessCodeForm(forms.ModelForm):
    contract_hourly_pln = PLNField(label="Kwota umowna / h [zł] (informacyjnie)", required=False)

    class Meta:
        model = AccessCode
        fields = ["valid_from", "valid_until", "lanes", "hours_limit", "is_active", "note"]
        widgets = {"valid_from": DateInput(), "valid_until": DateInput(), "lanes": forms.CheckboxSelectMultiple}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["lanes"].queryset = Lane.objects.all()
        if self.instance.pk and self.instance.contract_hourly is not None:
            self.initial["contract_hourly_pln"] = self.instance.contract_hourly / 100

    def clean(self):
        data = super().clean()
        if data.get("valid_from") and data.get("valid_until") and data["valid_until"] < data["valid_from"]:
            self.add_error("valid_until", "Data końcowa nie może być wcześniejsza.")
        return data

    def save(self, commit=True):
        obj = super().save(commit=False)
        value = self.cleaned_data.get("contract_hourly_pln")
        obj.contract_hourly = self.fields["contract_hourly_pln"].to_grosze(value) if value is not None else None
        if commit:
            obj.save()
            self.save_m2m()
        return obj
