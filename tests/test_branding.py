import base64
import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from apps.audit.models import AuditLog
from apps.core.models import Branding

pytestmark = pytest.mark.django_db

SVG = (b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
       b'<circle cx="50" cy="50" r="40" fill="#1c2a21"/></svg>')


def png(size, color=(200, 0, 0, 255), transparent_margin=0):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    m = transparent_margin
    img.paste(Image.new("RGBA", (size - 2 * m, size - 2 * m), color), (m, m))
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def data_url(b):
    return "data:image/png;base64," + base64.b64encode(b).decode()


def test_default_logo_and_icons_without_upload(client):
    html = client.get("/").content.decode()
    assert "/static/img/logo.svg" in html and "/static/icons/icon-192.png" in html
    assert client.get("/logo/").status_code == 302
    manifest = client.get("/manifest.webmanifest").content.decode()
    assert "icon-512-maskable.png" in manifest


def test_upload_svg_with_browser_icons(owner_client, client):
    response = owner_client.post("/panel/ustawienia/logo/", {
        "logo": SimpleUploadedFile("logo.svg", SVG, "image/svg+xml"), "icon_background": "#123456",
        "icon_192": data_url(png(192)), "icon_512": data_url(png(512)), "icon_maskable": data_url(png(512)),
    })
    assert response.status_code == 302
    b = Branding.objects.get()
    assert b.logo_type == "image/svg+xml" and bytes(b.logo) == SVG and b.icon_background == "#123456"
    assert AuditLog.objects.filter(action="ustawienia.logo").exists()
    html = client.get("/").content.decode()
    assert "/logo/?v=" in html and "/ikona/192.png?v=" in html
    r = client.get("/logo/?v=1")
    assert r["Content-Type"] == "image/svg+xml" and "sandbox" in r["Content-Security-Policy"] and "immutable" in r["Cache-Control"]
    icon = client.get("/ikona/maskable.png")
    assert icon["Content-Type"] == "image/png" and Image.open(io.BytesIO(icon.content)).size == (512, 512)
    assert "/ikona/maskable.png?v=" in client.get("/manifest.webmanifest").content.decode()
    assert client.get("/ikona/zle.png").status_code == 404
    # przywrócenie domyślnego
    owner_client.post("/panel/ustawienia/logo/", {"action": "reset"})
    assert not Branding.objects.exists()


def test_upload_png_server_generates_icons_and_crops_margin(owner_client):
    response = owner_client.post("/panel/ustawienia/logo/", {
        "logo": SimpleUploadedFile("logo.png", png(800, transparent_margin=200), "image/png"), "icon_background": "#ffffff",
    })
    assert response.status_code == 302
    b = Branding.objects.get()
    icon = Image.open(io.BytesIO(bytes(b.icon_512)))
    assert icon.size == (512, 512)
    # pusty margines przycięty — logo wypełnia prawie całą ikonę
    assert icon.getchannel("A").getbbox()[0] < 20
    mask = Image.open(io.BytesIO(bytes(b.icon_maskable))).convert("RGBA")
    assert mask.getpixel((2, 2)) == (255, 255, 255, 255)  # tło ikony maskowalnej


@pytest.mark.parametrize("bad", [
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg" xmlns:x="http://www.w3.org/1999/xlink"><image x:href="https://zly.example/a.png"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><foreignObject><p xmlns="http://www.w3.org/1999/xhtml">x</p></foreignObject></svg>',
    b"<html><body>to nie obraz</body></html>",
])
def test_dangerous_or_invalid_files_rejected(owner_client, bad):
    response = owner_client.post("/panel/ustawienia/logo/", {
        "logo": SimpleUploadedFile("logo.svg", bad, "image/svg+xml"), "icon_background": "#ffffff",
        "icon_192": data_url(png(192)), "icon_512": data_url(png(512)), "icon_maskable": data_url(png(512)),
    })
    assert response.status_code == 200 and "logo" in response.context["logo_form"].errors
    assert not Branding.objects.exists()


def test_svg_requires_valid_browser_icons(owner_client):
    response = owner_client.post("/panel/ustawienia/logo/", {
        "logo": SimpleUploadedFile("logo.svg", SVG, "image/svg+xml"), "icon_background": "#ffffff",
        "icon_192": data_url(png(100)), "icon_512": data_url(png(512)), "icon_maskable": data_url(png(512)),
    })
    assert response.status_code == 200 and not Branding.objects.exists()


def test_logo_upload_requires_settings_permission(client):
    from .conftest import grant, login_with_otp, make_user

    staff = grant(make_user("z@example.com"), "bookings.manage_reservations")
    login_with_otp(client, staff)
    assert client.post("/panel/ustawienia/logo/", {"action": "reset"}).status_code == 403


def test_repo_default_logo_passes_own_checks():
    from pathlib import Path

    from apps.core.branding import check_svg

    path = Path(__file__).resolve().parent.parent / "static" / "img" / "logo.svg"
    assert check_svg(path.read_bytes())
