import base64
from io import BytesIO

import qrcode
import qrcode.image.svg


def qr_svg_data_uri(text, box_size=8):
    """Kod QR jako obraz SVG w adresie data: (do osadzenia w <img>)."""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=box_size)
    buf = BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()
