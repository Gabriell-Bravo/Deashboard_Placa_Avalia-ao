"""Gera a arte da plaquinha 10x10 cm com QR dinâmico embutido."""

from __future__ import annotations

import hashlib
import io
import math
import os
import threading
from pathlib import Path

import qrcode
from PIL import Image, ImageDraw, ImageFont
from qrcode.constants import ERROR_CORRECT_H

ASSETS = Path(__file__).resolve().parent / "assets"
GOOGLE_G_PATH = ASSETS / "google_g.png"
NFC_PATH = ASSETS / "nfc_icon.png"

# Placa física quadrada. 300 dpi para impressão nítida.
SIDE_CM = 10.0
DPI = 300
SIDE_PX = round(SIDE_CM / 2.54 * DPI)  # 1181
PDF_PAGE_SIZE = (SIDE_CM / 2.54 * 72, SIDE_CM / 2.54 * 72)
LAYOUT_VERSION = "square-10cm-v2"

BLUE = (0, 106, 245)
STAR = (254, 214, 40)
INK = (17, 24, 39)
WHITE = (255, 255, 255)

_lock = threading.Lock()
_logo_rgba: Image.Image | None = None
_nfc_rgba: Image.Image | None = None


def _cache_dir() -> Path:
    base = Path(os.environ.get("DATA_DIR", Path(__file__).resolve().parent / "data"))
    path = base / "cache"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _asset_mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _cache_key(public_url: str, code: str, kind: str, width: int | None = None) -> str:
    raw = (
        f"{public_url}|{code}|{kind}|{width or 0}|{LAYOUT_VERSION}|"
        f"{_asset_mtime(GOOGLE_G_PATH)}|{_asset_mtime(NFC_PATH)}"
    )
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _font(size: int, bold: bool = True, black: bool = False):
    if black:
        candidates = [
            "C:/Windows/Fonts/ariblk.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "arialbd.ttf",
        ]
    else:
        candidates = [
            "C:/Windows/Fonts/arialbd.ttf" if bold else "C:/Windows/Fonts/arial.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            if bold
            else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "arialbd.ttf" if bold else "arial.ttf",
        ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _get_logo() -> Image.Image | None:
    global _logo_rgba
    if not GOOGLE_G_PATH.exists():
        return None
    with _lock:
        if _logo_rgba is None:
            _logo_rgba = Image.open(GOOGLE_G_PATH).convert("RGBA")
        return _logo_rgba.copy()


def _get_nfc() -> Image.Image:
    global _nfc_rgba
    if not NFC_PATH.exists():
        raise FileNotFoundError(
            f"Ícone NFC não encontrado: {NFC_PATH}. "
            "Coloque nfc_icon.png em assets/."
        )
    with _lock:
        if _nfc_rgba is None:
            _nfc_rgba = Image.open(NFC_PATH).convert("RGBA")
        return _nfc_rgba.copy()


def _make_qr_with_logo(payload: str, size: int) -> Image.Image:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_H,
        box_size=10,
        border=1,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGBA")
    qr_img = qr_img.resize((size, size), Image.Resampling.NEAREST)

    logo = _get_logo()
    if logo is not None:
        logo_size = max(40, size // 4)
        logo = logo.resize((logo_size, logo_size), Image.Resampling.LANCZOS)
        pad = Image.new("RGBA", (logo_size + 10, logo_size + 10), (0, 0, 0, 0))
        mask = Image.new("L", pad.size, 0)
        ImageDraw.Draw(mask).ellipse((0, 0, pad.size[0] - 1, pad.size[1] - 1), fill=255)
        white = Image.new("RGBA", pad.size, (255, 255, 255, 255))
        pad.paste(white, (0, 0), mask)
        pad.paste(logo, (5, 5), logo)
        pos = ((size - pad.size[0]) // 2, (size - pad.size[1]) // 2)
        qr_img.paste(pad, pos, pad)

    return qr_img


def _star_points(cx: float, cy: float, r_out: float) -> list[tuple[float, float]]:
    r_in = r_out * 0.42
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        r = r_out if i % 2 == 0 else r_in
        pts.append((cx + r * math.cos(ang), cy + r * math.sin(ang)))
    return pts


def _text_width(draw: ImageDraw.ImageDraw, text: str, font, tracking: int) -> int:
    total = 0
    for i, ch in enumerate(text):
        box = draw.textbbox((0, 0), ch, font=font)
        total += box[2] - box[0]
        if i < len(text) - 1:
            total += tracking
    return total


def _draw_tracked(draw, text, font, y, fill, tracking, width):
    widths = []
    for ch in text:
        box = draw.textbbox((0, 0), ch, font=font)
        widths.append(box[2] - box[0])
    total = sum(widths) + tracking * (len(text) - 1)
    x = (width - total) / 2
    for ch, wch in zip(text, widths):
        draw.text((x, y), ch, font=font, fill=fill)
        x += wch + tracking
    box = draw.textbbox((0, 0), text, font=font)
    return y + (box[3] - box[1])


def _fit_font(draw, lines, max_w, start, tracking, black=False, bold=True):
    size = start
    while size > 12:
        font = _font(size, bold=bold, black=black)
        if all(_text_width(draw, line, font, tracking) <= max_w for line in lines):
            return font
        size -= 1
    return _font(12, bold=bold, black=black)


def _layout(side: int) -> dict:
    """Medidas da arte quadrada, em pixels do arquivo final."""
    margin = round(side * 0.045)
    star_r = round(side * 0.036)
    star_cy = round(side * 0.078)
    rainbow_h = max(22, round(side * 0.024))
    code_band = round(side * 0.064)
    g_d = round(side * 0.255)
    curve_drop = round(side * 0.082)
    return {
        "side": side,
        "margin": margin,
        "star_r": star_r,
        "star_cy": star_cy,
        "star_gap": round(star_r * 0.55),
        "rainbow_h": rainbow_h,
        "rainbow_top": side - rainbow_h,
        "code_band": code_band,
        "code_top": side - rainbow_h - code_band,
        "code_font": max(22, round(side * 0.026)),
        "g_d": g_d,
        "curve_drop": curve_drop,
        "text_max_w": side - margin * 2,
    }


def _curve_polygon(side: int, curve_side: int, curve_center: int) -> list[tuple[float, float]]:
    drop = curve_center - curve_side
    pts = [(0, 0), (side, 0), (side, curve_side)]
    steps = 140
    for i in range(steps + 1):
        x = side * (1 - i / steps)
        u = (x / side) * 2 - 1
        y = curve_side + drop * (1 - u * u)
        pts.append((x, y))
    pts.append((0, curve_side))
    return pts


def _paint_rainbow(img: Image.Image, top: int, height: int) -> None:
    colors = [
        (237, 47, 12),
        (234, 140, 18),
        (250, 204, 21),
        (132, 196, 18),
        (34, 160, 8),
        (20, 140, 90),
        (30, 110, 200),
    ]
    grad = Image.new("RGB", (len(colors), 1))
    grad.putdata(colors)
    grad = grad.resize((img.width, height), Image.Resampling.BILINEAR)
    img.paste(grad, (0, top))


def _render_base(side: int) -> tuple[Image.Image, dict]:
    """Desenha o fundo (sem QR e sem o código da placa) e devolve o encaixe do QR."""
    scale = 2
    hi = side * scale
    lay = _layout(side)
    canvas = Image.new("RGB", (hi, hi), WHITE)
    draw = ImageDraw.Draw(canvas)

    probe = ImageDraw.Draw(Image.new("RGB", (hi, hi), WHITE))
    headline = ("NÓS GOSTARÍAMOS DA", "SUA OPINIÃO NO GOOGLE")
    tracking = scale * 1
    font = _fit_font(
        probe,
        headline,
        lay["text_max_w"] * scale,
        round(side * 0.056) * scale,
        tracking,
        black=True,
    )
    line_h = font.getbbox("Hg")[3] - font.getbbox("Hg")[1]
    gap = round(side * 0.008) * scale
    block_h = line_h * 2 + gap

    star_bottom = (lay["star_cy"] + lay["star_r"]) * scale
    text_top = star_bottom + round(side * 0.02) * scale
    text_bottom = text_top + block_h

    g_d = lay["g_d"] * scale
    g_top = text_bottom + round(side * 0.012) * scale
    g_center = g_top + g_d / 2
    curve_center = g_center
    curve_side = curve_center - lay["curve_drop"] * scale
    min_curve = text_bottom + round(side * 0.018) * scale
    if curve_side < min_curve:
        shift = min_curve - curve_side
        curve_side += shift
        curve_center += shift
        g_top += shift
        g_center += shift

    draw.polygon(_curve_polygon(hi, int(curve_side), int(curve_center)), fill=BLUE)

    star_r = lay["star_r"] * scale
    step = star_r * 2 + lay["star_gap"] * scale
    start_x = hi / 2 - step * 2
    for i in range(5):
        cx = start_x + i * step
        draw.polygon(_star_points(cx, lay["star_cy"] * scale, star_r), fill=STAR)

    y = text_top
    for line in headline:
        y = _draw_tracked(draw, line, font, y, WHITE, tracking, hi) + gap

    logo = _get_logo()
    if logo is not None:
        logo = logo.resize((int(g_d), int(g_d)), Image.Resampling.LANCZOS)
        lx = (hi - logo.width) // 2
        ly = int(g_top)
        canvas.paste(logo, (lx, ly), logo)

    g_bottom = int(g_top + g_d)
    content_bottom = lay["code_top"] * scale
    qr_top = g_bottom + round(side * 0.02) * scale
    qr_bottom = content_bottom - round(side * 0.018) * scale
    qr_size = max(80, qr_bottom - qr_top)
    max_qr = round(side * 0.42) * scale
    qr_size = min(qr_size, max_qr)
    qr_right = hi - lay["margin"] * scale
    qr_left = qr_right - qr_size
    extra = (qr_bottom - qr_top) - qr_size
    qr_top = qr_top + max(0, extra // 2)
    qr_bottom = qr_top + qr_size

    # Texto e ícone NFC, alinhados à altura do QR.
    nfc_right = qr_left - round(side * 0.02) * scale
    nfc_left = lay["margin"] * scale
    nfc_w = nfc_right - nfc_left
    label = "APROXIME SEU CELULAR"
    label_font = _fit_font(
        probe,
        (label,),
        int(nfc_w),
        round(side * 0.028) * scale,
        0,
        bold=True,
    )
    label_box = probe.textbbox((0, 0), label, font=label_font)
    label_h = label_box[3] - label_box[1]
    label_w = label_box[2] - label_box[0]
    icon = _get_nfc()
    icon_max_h = qr_size - label_h - round(side * 0.012) * scale
    icon_scale = min(nfc_w / icon.width, icon_max_h / icon.height)
    icon = icon.resize(
        (max(1, int(icon.width * icon_scale)), max(1, int(icon.height * icon_scale))),
        Image.Resampling.LANCZOS,
    )
    group_h = label_h + round(side * 0.01) * scale + icon.height
    group_top = qr_top + max(0, (qr_size - group_h) // 2)
    label_x = nfc_left + (nfc_w - label_w) // 2
    draw.text(
        (label_x, group_top - label_box[1]),
        label,
        font=label_font,
        fill=INK,
    )
    icon_x = nfc_left + (nfc_w - icon.width) // 2
    icon_y = group_top + label_h + round(side * 0.01) * scale
    canvas.paste(icon, (int(icon_x), int(icon_y)), icon)

    _paint_rainbow(canvas, lay["rainbow_top"] * scale, lay["rainbow_h"] * scale)

    base = canvas.resize((side, side), Image.Resampling.LANCZOS)
    qr_box = {
        "left": int(round(qr_left / scale)),
        "top": int(round(qr_top / scale)),
        "size": int(round(qr_size / scale)),
        "radius": max(16, int(round(qr_size / scale) * 0.08)),
        "border": max(7, int(round(qr_size / scale) * 0.02)),
    }
    return base, qr_box


def _paste_qr_card(base: Image.Image, qr_img: Image.Image, box: dict) -> None:
    size = box["size"]
    radius = box["radius"]
    border = box["border"]
    inset = border + max(10, size // 28)
    qr_size = size - inset * 2
    qr_img = qr_img.resize((qr_size, qr_size), Image.Resampling.NEAREST)

    card = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=radius,
        fill=255,
    )
    plate = Image.new("RGBA", (size, size), (255, 255, 255, 255))
    plate.paste(qr_img, (inset, inset), qr_img)
    card.paste(plate, (0, 0), mask)
    stroke = ImageDraw.Draw(card)
    stroke.rounded_rectangle(
        (border // 2, border // 2, size - 1 - border // 2, size - 1 - border // 2),
        radius=radius,
        outline=INK + (255,),
        width=border,
    )
    base.paste(card, (box["left"], box["top"]), card)


def _draw_code(base: Image.Image, code: str, lay: dict) -> None:
    if not code:
        return
    draw = ImageDraw.Draw(base)
    font = _font(lay["code_font"], bold=True)
    bbox = draw.textbbox((0, 0), code, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    tx = (base.width - tw) // 2
    ty = lay["code_top"] + (lay["code_band"] - th) // 2 - bbox[1]
    pad_x, pad_y = 10, 4
    draw.rounded_rectangle(
        (tx - pad_x, ty + bbox[1] - pad_y, tx + tw + pad_x, ty + bbox[3] + pad_y),
        radius=8,
        fill=WHITE,
    )
    draw.text((tx, ty), code, fill=INK, font=font)


def build_placa_image(public_url: str, code: str = "") -> Image.Image:
    side = SIDE_PX
    base, qr_box = _render_base(side)
    base = base.convert("RGBA")
    qr_inner = qr_box["size"] - 2 * (qr_box["border"] + max(10, qr_box["size"] // 28))
    qr_img = _make_qr_with_logo(public_url, max(80, qr_inner))
    _paste_qr_card(base, qr_img, qr_box)
    _draw_code(base, code, _layout(side))
    return base.convert("RGB")


def placa_png_bytes(
    public_url: str,
    code: str = "",
    max_width: int | None = None,
    fmt: str = "PNG",
) -> bytes:
    key = _cache_key(public_url, code, f"img-{fmt.lower()}", max_width)
    ext = "jpg" if fmt.upper() == "JPEG" else "png"
    cache_path = _cache_dir() / f"{key}.{ext}"
    if cache_path.exists():
        return cache_path.read_bytes()

    img = build_placa_image(public_url, code=code)
    if max_width and img.width > max_width:
        ratio = max_width / img.width
        img = img.resize(
            (max_width, max(1, int(img.height * ratio))),
            Image.Resampling.LANCZOS,
        )

    buf = io.BytesIO()
    if fmt.upper() == "JPEG":
        img = img.convert("RGB")
        img.save(buf, format="JPEG", quality=90, optimize=True, dpi=(DPI, DPI))
    else:
        img.save(buf, format="PNG", optimize=True, dpi=(DPI, DPI))
    data = buf.getvalue()
    cache_path.write_bytes(data)
    return data


def placa_pdf_bytes(public_url: str, code: str = "") -> bytes:
    import pymupdf

    key = _cache_key(public_url, code, "pdf")
    cache_path = _cache_dir() / f"{key}.pdf"
    if cache_path.exists():
        return cache_path.read_bytes()

    png = placa_png_bytes(public_url, code=code, fmt="PNG")
    page_w, page_h = PDF_PAGE_SIZE
    doc = pymupdf.open()
    page = doc.new_page(width=page_w, height=page_h)
    page.insert_image(page.rect, stream=png)
    pdf_buf = io.BytesIO()
    doc.save(pdf_buf, deflate=True, garbage=4)
    doc.close()
    data = pdf_buf.getvalue()
    cache_path.write_bytes(data)
    return data
