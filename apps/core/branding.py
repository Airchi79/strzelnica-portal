"""Logo stowarzyszenia: sprawdzanie wgranego pliku, generowanie ikon aplikacji, adresy do szablonów.

SVG jest sprawdzany listą dozwolonych elementów i atrybutów (bez skryptów, zdarzeń, odnośników zewnętrznych), a przy
wysyłce dostaje nagłówek CSP „sandbox” — nawet otwarty bezpośrednio nie uruchomi żadnego kodu. Pliki rastrowe są
przekodowywane przez Pillow (usuwa metadane). Ikony z SVG generuje przeglądarka (canvas) w panelu; serwer sprawdza,
czy to poprawne PNG o właściwych wymiarach, i zapisuje je od nowa.
"""

import base64
import io
import re
import xml.etree.ElementTree as ET

from django.core.exceptions import ValidationError
from django.templatetags.static import static
from django.urls import reverse

MAX_LOGO_BYTES = 1024 * 1024
ICON_SIZES = {"icon_192": 192, "icon_512": 512, "icon_maskable": 512}
MASKABLE_SAFE = 0.80  # strefa bezpieczna ikony maskowalnej (Android przycina do koła/kształtu)

_SVG_TAGS = {
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon", "text", "tspan", "textPath",
    "defs", "lineargradient", "radialgradient", "stop", "clippath", "mask", "use", "symbol", "title", "desc",
    "pattern", "filter", "fegaussianblur", "feoffset", "feblend", "fecolormatrix", "feflood", "fecomposite",
    "femerge", "femergenode", "fedropshadow", "metadata", "style", "image",
}
_SVG_NS = "{http://www.w3.org/2000/svg}"
_SAFE_HREF = re.compile(r"^(#|data:image/(png|jpeg|gif|webp);base64,)", re.I)


def _localname(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def check_svg(data: bytes) -> bytes:
    """Zwraca SVG bez zmian, jeśli jest bezpieczny; w przeciwnym razie ValidationError."""
    text = data.decode("utf-8-sig", errors="strict")
    if re.search(r"<!ENTITY", text, re.I):
        raise ValidationError("Plik SVG zawiera definicje encji — zapisz go ponownie jako zwykły SVG.")
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValidationError("To nie jest poprawny plik SVG.") from e
    if _localname(root.tag) != "svg":
        raise ValidationError("To nie jest plik SVG.")
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue  # komentarze, instrukcje przetwarzania
        name = _localname(el.tag)
        ns = el.tag[1:].split("}", 1)[0] if el.tag.startswith("{") else ""
        if ns in ("", _SVG_NS[1:-1]):
            if name not in _SVG_TAGS:
                raise ValidationError(f"Plik SVG zawiera niedozwolony element <{name}> (np. skrypt lub osadzoną stronę).")
        elif "xhtml" in ns or name in ("script", "foreignobject", "iframe"):
            raise ValidationError(f"Plik SVG zawiera niedozwolony element <{name}>.")
        # inne przestrzenie nazw (metadane Inkscape, RDF, Dublin Core) — dozwolone, są ignorowane przez przeglądarki
        for attr, value in el.attrib.items():
            a = _localname(attr)
            if a.startswith("on"):
                raise ValidationError("Plik SVG zawiera skrypty (atrybuty zdarzeń) — usuń je w programie graficznym.")
            if a == "href" and not _SAFE_HREF.match(value.strip()):
                raise ValidationError("Plik SVG odwołuje się do zewnętrznych plików — osadź grafikę w pliku.")
            if re.search(r"javascript:|url\(\s*['\"]?\s*(https?:|//)", value, re.I):
                raise ValidationError("Plik SVG odwołuje się do zewnętrznych zasobów.")
        if name == "style" and re.search(r"@import|url\(\s*['\"]?\s*(https?:|//)|javascript:", el.text or "", re.I):
            raise ValidationError("Styl w pliku SVG odwołuje się do zewnętrznych zasobów.")
    return data


def detect_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith(b"<?xml") or head.startswith(b"<svg") or head.startswith(b"<!doctype svg"):
        return "image/svg+xml"
    return ""


def _open_raster(data: bytes):
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as e:  # noqa: BLE001 — każdy błąd dekodowania to „zły plik”
        raise ValidationError("Nie udało się odczytać obrazu.") from e
    if max(img.size) > 6000:
        raise ValidationError("Obraz jest za duży (najwyżej 6000 px).")
    return img.convert("RGBA")


def _content_box(img):
    """Prostokąt treści: bez przezroczystego (albo jednolicie białego) marginesu."""
    from PIL import ImageChops

    alpha_box = img.getchannel("A").getbbox()
    if alpha_box and alpha_box != (0, 0, *img.size):
        return alpha_box
    rgb = img.convert("RGB")
    bg = rgb.getpixel((0, 0))
    diff = ImageChops.difference(rgb, rgb.__class__.new("RGB", rgb.size, bg)).convert("L").point(lambda v: 255 if v > 16 else 0)
    return diff.getbbox() or (0, 0, *img.size)


def make_icons(img, background="#ffffff"):
    """Z obrazu (PIL RGBA) robi ikony: 192, 512 (przezroczyste tło) i maskowalną 512 (z tłem, logo w strefie bezpiecznej)."""
    from PIL import Image

    box = _content_box(img)
    crop = img.crop(box)
    side = max(crop.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(crop, ((side - crop.width) // 2, (side - crop.height) // 2))

    def render(size, fill=None, scale=1.0):
        canvas = Image.new("RGBA", (size, size), fill or (0, 0, 0, 0))
        inner = max(1, round(size * scale))
        logo = square.resize((inner, inner), Image.LANCZOS)
        canvas.alpha_composite(logo, ((size - inner) // 2, (size - inner) // 2))
        out = io.BytesIO()
        canvas.save(out, "PNG", optimize=True)
        return out.getvalue()

    return {
        "icon_192": render(192, scale=0.96),
        "icon_512": render(512, scale=0.96),
        "icon_maskable": render(512, fill=background, scale=MASKABLE_SAFE),
    }


def raster_to_png(img) -> bytes:
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue()


def check_icon_data_url(value: str, size: int) -> bytes:
    """Ikona PNG wygenerowana w przeglądarce (data URL) — sprawdzona i zapisana od nowa."""
    m = re.match(r"^data:image/png;base64,([A-Za-z0-9+/=\s]+)$", value or "")
    if not m:
        raise ValidationError("Brak wygenerowanej ikony — wybierz plik ponownie.")
    raw = base64.b64decode(m.group(1))
    if len(raw) > 2 * 1024 * 1024:
        raise ValidationError("Wygenerowana ikona jest za duża.")
    img = _open_raster(raw)
    if img.size != (size, size):
        raise ValidationError("Wygenerowana ikona ma złe wymiary.")
    return raster_to_png(img)


def brand_urls(version=None):
    """Adresy logo i ikon do szablonów. `version` — znacznik zmiany (pomija pamięć podręczną po wgraniu nowego logo)."""
    if version is None:
        return {
            "logo": static("img/logo.svg"),
            "icon_192": static("icons/icon-192.png"),
            "icon_512": static("icons/icon-512.png"),
            "icon_maskable": static("icons/icon-512-maskable.png"),
            "version": "domyslne",
        }
    v = f"?v={version}"
    return {
        "logo": reverse("brand_logo") + v,
        "icon_192": reverse("brand_icon", args=["192"]) + v,
        "icon_512": reverse("brand_icon", args=["512"]) + v,
        "icon_maskable": reverse("brand_icon", args=["maskable"]) + v,
        "version": str(version),
    }


def current_version():
    from .models import Branding

    ts = Branding.objects.filter(pk=1).values_list("updated_at", flat=True).first()
    return int(ts.timestamp()) if ts else None


def current_urls():
    return brand_urls(current_version())


def icon_png_for_pdf() -> bytes:
    """Ikona 512 do dokumentów PDF (wgrane logo albo domyślne)."""
    from django.contrib.staticfiles import finders

    from .models import Branding

    row = Branding.objects.filter(pk=1).values_list("icon_512", flat=True).first()
    if row:
        return bytes(row)
    path = finders.find("icons/icon-512.png")
    with open(path, "rb") as f:
        return f.read()
