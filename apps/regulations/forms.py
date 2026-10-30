from django import forms
from django.utils import timezone

from .models import RegulationVersion


class VersionForm(forms.ModelForm):
    class Meta:
        model = RegulationVersion
        fields = ["content", "change_note", "effective_from"]
        widgets = {
            "content": forms.Textarea(attrs={"rows": 24}),
            "effective_from": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
        }
        help_texts = {"content": "Zwykły tekst. Puste linie oddzielają akapity, linie zaczynające się od „§” są wyróżniane."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if not self.instance.pk and not self.initial.get("effective_from"):
            self.initial["effective_from"] = timezone.localtime().replace(second=0, microsecond=0)


class AcceptForm(forms.Form):
    confirm = forms.BooleanField(label="Zapoznałem(-am) się z powyższymi dokumentami i akceptuję je.")
