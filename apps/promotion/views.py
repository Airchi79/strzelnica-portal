from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.db import transaction

from apps.audit.services import log_event
from apps.core.permissions import panel_required

from . import services
from .forms import BriefForm, PhotoForm, PostForm, PromotionSettingsForm
from .models import Post, PromotionSettings

PERM = "promotion.manage_promotion"


@panel_required(PERM)
@transaction.non_atomic_requests  # generowanie trwa do kilkudziesięciu sekund — bez transakcji trzymanej w tym czasie
def panel_list(request):
    form = BriefForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            post = services.create_draft(form.cleaned_data["brief"], by=request.user, event=form.cleaned_data["event"],
                                         kind=Post.Kind.EVENT if form.cleaned_data["event"] else Post.Kind.MANUAL)
        except ValidationError as e:
            form.add_error(None, e.messages[0])
        else:
            messages.success(request, "Szkic publikacji gotowy — sprawdź i popraw teksty przed zatwierdzeniem.")
            return redirect("panel:promotion_post", pk=post.pk)
    status = request.GET.get("status", "")
    posts = Post.objects.select_related("created_by")
    if status in Post.Status.values:
        posts = posts.filter(status=status)
    return render(request, "panel/promotion_list.html", {
        "form": form, "posts": posts[:100], "status": status, "statuses": [(v, l.split(" —")[0]) for v, l in Post.Status.choices],
        "api_ready": bool(getattr(settings, "ANTHROPIC_API_KEY", "")), "ps": PromotionSettings.load()})


@panel_required(PERM)
def panel_post(request, pk):
    post = get_object_or_404(Post, pk=pk)
    editable = post.status in (Post.Status.DRAFT, Post.Status.APPROVED)
    form = PostForm(request.POST or None, instance=post) if editable else None
    if request.method == "POST":
        action = request.POST.get("action", "save")
        try:
            if action in ("photo", "photo_remove"):
                if action == "photo":
                    photo_form = PhotoForm(request.POST, request.FILES)
                    if not photo_form.is_valid():
                        raise ValidationError("Wybierz zdjęcie (JPG lub PNG).")
                    services.set_photo(post, photo_form.cleaned_data["photo"], by=request.user)
                    messages.success(request, "Dodano zdjęcie — grafiki zostały odświeżone.")
                else:
                    services.set_photo(post, None, by=request.user)
                    messages.success(request, "Usunięto zdjęcie — grafika ma tło w barwach strzelnicy.")
                return redirect("panel:promotion_post", pk=post.pk)
            if action == "save":
                if form is None or not form.is_valid():
                    raise ValidationError("Popraw formularz.")
                services.save_texts(post, form.cleaned_data, by=request.user)
                messages.success(request, "Zapisano zmiany.")
            else:
                if form is not None and form.is_valid() and form.has_changed():
                    services.save_texts(post, form.cleaned_data, by=request.user)
                services.decide(post, action, by=request.user, note=request.POST.get("note", ""))
                messages.success(request, {"approve": "Zatwierdzono publikację.", "reject": "Odrzucono publikację.",
                                           "published": "Oznaczono jako opublikowaną.", "reopen": "Publikacja wróciła do poprawy."}[action])
        except ValidationError as e:
            messages.error(request, e.messages[0])
        return redirect("panel:promotion_post", pk=post.pk)
    ps = PromotionSettings.load()
    return render(request, "panel/promotion_post.html", {"post": post, "form": form, "photo_form": PhotoForm() if editable else None,
                                                         "booking_url": ps.booking_url(), "version": int(post.updated_at.timestamp())})


@panel_required(PERM)
def panel_graphic(request, pk, fmt):
    from apps.core.branding import icon_png_for_pdf

    from . import graphics

    post = get_object_or_404(Post, pk=pk)
    if fmt not in graphics.FORMATS:
        raise Http404
    photo = None
    if post.photo:
        with post.photo.open("rb") as f:
            photo = f.read()
    png = graphics.render(post, fmt, photo_bytes=photo, icon_bytes=icon_png_for_pdf())
    response = HttpResponse(png, content_type="image/png")
    response["Cache-Control"] = "private, max-age=3600"
    if request.GET.get("pobierz"):
        response["Content-Disposition"] = f'attachment; filename="promocja-{post.pk}-{fmt}.png"'
    return response


@panel_required(PERM)
def panel_settings(request):
    obj = PromotionSettings.load()
    form = PromotionSettingsForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        log_event("promocja.ustawienia", obj, f"Rezerwacje w publikacjach: {obj.get_booking_target_display()}", actor=request.user)
        messages.success(request, "Zapisano ustawienia promocji.")
        return redirect("panel:promotion_settings")
    return render(request, "panel/promotion_settings.html", {"form": form, "obj": obj})
