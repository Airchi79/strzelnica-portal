"""Grafiki publikacji z szablonu (Pillow): kwadrat 1080×1080 (Facebook, Instagram) i pion 1080×1920 (TikTok, Stories).

Tło: zdjęcie publikacji (kadrowane na środek, przyciemnione u dołu) albo zieleń strzelnicy. Na wierzchu: logo, nagłówek,
podtytuł, do 4 wierszy szczegółów (np. data, godziny, cena) i stopka z adresem strony. Czcionki DejaVu (polskie znaki).
Grafika powstaje na żądanie z pól publikacji — zmiana tekstu = nowa grafika, bez zapisywania plików."""

import io
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

FORMATS = {"kwadrat": (1080, 1080), "pion": (1080, 1920)}
BRAND_DARK = (24, 52, 32)
BRAND = (47, 93, 58)
ACCENT = (226, 184, 76)
WHITE = (255, 255, 255)
FONTS = Path(__file__).resolve().parent.parent / "reports" / "fonts"
FOOTER = "strzelnicasatok.pl"


@lru_cache(maxsize=32)
def _font(bold, size):
    return ImageFont.truetype(str(FONTS / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size)


def _wrap(draw, text, font, width, max_lines):
    words, lines, line = (text or "").split(), [], ""
    for w in words:
        test = f"{line} {w}".strip()
        if draw.textlength(test, font=font) <= width:
            line = test
            continue
        if line:
            lines.append(line)
        line = w
        while draw.textlength(line, font=font) > width and len(line) > 1:  # bardzo długie słowo — tnij
            lines.append(line[:-1])
            line = line[-1]
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=font) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def _fit_lines(draw, text, width, max_lines, start, minimum, bold=True):
    """Największa czcionka (od `start` w dół), przy której tekst mieści się w `max_lines` wierszach."""
    size = start
    while size > minimum:
        font = _font(bold, size)
        lines = _wrap(draw, text, font, width, 99)
        if len(lines) <= max_lines:
            return font, lines
        size -= 4
    font = _font(bold, minimum)
    return font, _wrap(draw, text, font, width, max_lines)


def _background(size, photo_bytes):
    w, h = size
    if photo_bytes:
        try:
            img = Image.open(io.BytesIO(photo_bytes))
            img = ImageOps.exif_transpose(img).convert("RGB")
            img = ImageOps.fit(img, size, method=Image.LANCZOS)
            # przyciemnienie: równomierne + mocniejsze u dołu (czytelność tekstu)
            shade = Image.new("L", (1, h))
            for y in range(h):
                shade.putpixel((0, y), int(90 + 140 * (y / h) ** 1.4))
            overlay = Image.new("RGB", size, (8, 16, 10))
            return Image.composite(overlay, img, shade.resize(size))
        except (OSError, ValueError, Image.DecompressionBombError):
            pass
    img = Image.new("RGB", size, BRAND_DARK)
    grad = Image.linear_gradient("L").resize(size)
    img = Image.composite(Image.new("RGB", size, BRAND), img, grad.rotate(180))
    draw = ImageDraw.Draw(img)
    for i in range(-h, w, 120):  # delikatne ukośne linie
        draw.line([(i, h), (i + h, 0)], fill=(58, 104, 69), width=2)
    return img


def _logo(icon_bytes, px):
    try:
        icon = Image.open(io.BytesIO(icon_bytes)).convert("RGBA").resize((px, px), Image.LANCZOS)
    except (OSError, ValueError):
        return None
    disc = Image.new("RGBA", (px + 24, px + 24), (0, 0, 0, 0))
    ImageDraw.Draw(disc).ellipse([0, 0, px + 23, px + 23], fill=WHITE + (255,))
    disc.alpha_composite(icon, (12, 12))
    return disc


def render(post, fmt="kwadrat", photo_bytes=None, icon_bytes=None):
    """PNG (bytes) grafiki publikacji w danym formacie."""
    size = FORMATS[fmt]
    w, h = size
    img = _background(size, photo_bytes).convert("RGBA")
    draw = ImageDraw.Draw(img)
    margin = 80
    width = w - 2 * margin
    if icon_bytes:
        logo = _logo(icon_bytes, 120 if fmt == "kwadrat" else 150)
        if logo is not None:
            img.alpha_composite(logo, (margin, margin))
    draw.text((margin + (170 if fmt == "kwadrat" else 200), margin + 40), "STRZELNICA SĄTOK", font=_font(True, 40), fill=WHITE)

    headline = post.graphic_headline or post.website_title or post.title or "Strzelnica Sątok"
    details = [d.strip() for d in (post.graphic_details or "").splitlines() if d.strip()][:4]
    header_bottom = margin + (150 if fmt == "kwadrat" else 180)
    base = {"kwadrat": (96, 46, 42, 3), "pion": (130, 56, 60, 4)}[fmt]  # nagłówek, podtytuł, szczegóły, wiersze nagłówka

    def layout(scale):
        h_font, h_lines = _fit_lines(draw, headline, width, base[3], int(base[0] * scale), int(52 * scale))
        s_font, s_lines = (_fit_lines(draw, post.graphic_subtitle, width, 2, int(base[1] * scale), int(28 * scale), bold=False)
                           if post.graphic_subtitle else (None, []))
        d_font = _font(True, int(base[2] * scale))
        height = (len(h_lines) * (h_font.size + 12) + 34 + (len(s_lines) * (s_font.size + 14) + 24 if s_lines else 0)
                  + (len(details) * (d_font.size + 22) + 30 if details else 0) + 80)
        return h_font, h_lines, s_font, s_lines, d_font, height

    scale = 1.0
    while True:  # zmniejszanie, aż blok tekstu zmieści się pod logo
        h_font, h_lines, s_font, s_lines, d_font, height = layout(scale)
        if h - margin - height >= header_bottom or scale <= 0.55:
            break
        scale -= 0.05

    # układ od dołu: stopka → szczegóły → podtytuł → nagłówek
    y = h - margin - 40
    draw.text((margin, y), FOOTER, font=_font(False, 36 if fmt == "kwadrat" else 44), fill=(220, 230, 222))
    y -= 40
    line_h = d_font.size + 22
    for text in reversed(details):
        y -= line_h
        draw.rectangle([margin, y + 6, margin + 10, y + d_font.size + 2], fill=ACCENT)
        draw.text((margin + 32, y), text, font=d_font, fill=WHITE)
    if details:
        y -= 30
    if s_lines:
        for text in reversed(s_lines):
            y -= s_font.size + 14
            draw.text((margin, y), text, font=s_font, fill=(232, 240, 234))
        y -= 24
    for text in reversed(h_lines):
        y -= h_font.size + 12
        draw.text((margin, y), text, font=h_font, fill=WHITE)
    draw.rectangle([margin, y - 34, margin + 120, y - 24], fill=ACCENT)

    out = io.BytesIO()
    img.convert("RGB").save(out, "PNG", optimize=True)
    return out.getvalue()
