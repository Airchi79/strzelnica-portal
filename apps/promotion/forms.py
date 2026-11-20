from django import forms

from .models import Post, PromotionSettings


class BriefForm(forms.Form):
    brief = forms.CharField(
        label="Polecenie", max_length=2000, widget=forms.Textarea(attrs={"rows": 3}),
        help_text="Napisz zwyczajnie, np. „Zaproś na trening w sobotę, są wolne godziny na 100 m” albo „Podziękuj uczestnikom "
                  "zawodów GPA i zaproś na kolejne”. System doda aktualne wolne terminy, cennik i wydarzenia.")
    event = forms.ModelChoiceField(label="Wydarzenie", required=False, queryset=None,
                                   help_text="Dane wydarzenia (termin, opłata, wolne miejsca, link do zapisów) trafią do tekstu.")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from django.utils import timezone

        from apps.events.models import Event

        self.fields["event"].queryset = Event.objects.filter(status=Event.Status.PUBLISHED, ends_at__gte=timezone.now()).order_by("starts_at")


class PostForm(forms.ModelForm):
    class Meta:
        model = Post
        fields = ["title", "facebook", "instagram", "tiktok", "website_title", "website_body", "hashtags"]
        widgets = {f: forms.Textarea(attrs={"rows": 8}) for f in ["facebook", "instagram", "tiktok", "website_body"]}


class PromotionSettingsForm(forms.ModelForm):
    class Meta:
        model = PromotionSettings
        fields = ["booking_target", "guntracker_url", "tone", "hashtags", "facts"]
        widgets = {"tone": forms.Textarea(attrs={"rows": 3}), "facts": forms.Textarea(attrs={"rows": 5})}
