import pytest

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("kind", ["zajetosc", "teraz", "wydarzenia"])
def test_widget_pages_embeddable_and_public(client, kind):
    r = client.get(f"/widget/{kind}/?motyw=ciemny")
    assert r.status_code == 200
    assert "X-Frame-Options" not in r.headers
    assert "frame-ancestors *" in r.headers["Content-Security-Policy"]
    html = r.content.decode()
    assert 'data-theme="dark"' in html and "<form" not in html  # bez formularzy — tylko dane publiczne


def test_other_pages_still_not_embeddable(client):
    r = client.get("/")
    assert r.headers.get("X-Frame-Options") == "DENY" and "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]


def test_widget_unknown_kind_and_embed_js(client):
    assert client.get("/widget/nieznany/").status_code == 404
    js = client.get("/widget.js")
    assert js["Content-Type"].startswith("application/javascript") and b"data-satok-widget" in js.content


def test_panel_widget_builder(owner_client):
    page = owner_client.get("/panel/widzet/").content.decode()
    assert "Kod do wklejenia" in page and "widget-builder.js" in page
