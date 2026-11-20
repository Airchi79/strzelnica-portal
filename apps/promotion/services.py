"""Operacje na publikacjach: przygotowanie szkicu (generator), zapis poprawek, decyzje zarządu. Każda zmiana w dzienniku zdarzeń."""

from django.core.exceptions import ValidationError
from django.utils import timezone

from apps.audit.services import log_event

from . import generator
from .models import Post

TEXT_FIELDS = ["title", "facebook", "instagram", "tiktok", "website_title", "website_body", "hashtags"]


def create_draft(brief, *, by, event=None, kind=Post.Kind.MANUAL):
    brief = (brief or "").strip()
    if len(brief) < 5:
        raise ValidationError("Napisz polecenie — o czym ma być publikacja.")
    try:
        result, model, usage = generator.generate(brief, event=event)
    except generator.GenerationError as e:
        raise ValidationError(str(e)) from e
    post = Post.objects.create(
        kind=kind, brief=brief, event=event, created_by=by, model=model[:60], usage=usage,
        title=str(result.get("title", ""))[:160], facebook=result.get("facebook", ""), instagram=result.get("instagram", ""),
        tiktok=result.get("tiktok", ""), website_title=str(result.get("website_title", ""))[:200],
        website_body=result.get("website_body", ""), hashtags=" ".join(result.get("hashtags", []))[:400],
    )
    log_event("promocja.szkic", post, f"{post.title} ({post.get_kind_display()})", actor=by)
    return post


def save_texts(post, data, *, by):
    if post.status in (Post.Status.PUBLISHED, Post.Status.REJECTED):
        raise ValidationError("Opublikowanej ani odrzuconej publikacji nie można już zmieniać.")
    for f in TEXT_FIELDS:
        if f in data:
            setattr(post, f, data[f])
    post.save()
    log_event("promocja.edycja", post, post.title, actor=by)
    return post


def decide(post, action, *, by, note=""):
    transitions = {
        "approve": ({Post.Status.DRAFT}, Post.Status.APPROVED, "promocja.zatwierdzenie"),
        "reject": ({Post.Status.DRAFT, Post.Status.APPROVED}, Post.Status.REJECTED, "promocja.odrzucenie"),
        "published": ({Post.Status.APPROVED}, Post.Status.PUBLISHED, "promocja.opublikowanie"),
        "reopen": ({Post.Status.APPROVED, Post.Status.REJECTED}, Post.Status.DRAFT, "promocja.do_poprawy"),
    }
    if action not in transitions:
        raise ValidationError("Nieznana operacja.")
    allowed, target, event = transitions[action]
    if post.status not in allowed:
        raise ValidationError(f"Tej operacji nie można wykonać dla publikacji w stanie „{post.get_status_display()}”.")
    post.status, post.decided_by, post.decided_at = target, by, timezone.now()
    if note:
        post.note = note[:255]
    post.save(update_fields=["status", "decided_by", "decided_at", "note"])
    log_event(event, post, post.title, actor=by)
    return post
