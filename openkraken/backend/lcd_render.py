"""Pillow rendering of 640x640 sensor screens for the Kraken's round LCD.

This module is pure :mod:`PIL` (Pillow) with **no Qt dependency** so it can run
inside the engine thread without touching the GUI toolkit.  It produces 640x640
RGB images designed for a *round* display: only the circle inscribed in the
square is physically visible, so every piece of content is kept inside a circle
of radius 320 (centred on the canvas) with an additional safe margin.

Three styles are offered (see :data:`STYLES`):

``liquid_ring``
    CAM-classic look: a thin gray full ring with a thick purple arc whose sweep
    is proportional to the liquid temperature, a huge centred temperature
    number, and pump/fan RPM in the footer.

``cpu_gpu``
    A horizontally split screen: CPU (cyan accent) on top, GPU (green accent) on
    the bottom, each showing a big temperature plus a horizontal load bar.

``triple``
    Everything at a glance: the liquid temperature large in the centre, CPU on
    the left and GPU on the right at medium size, with pump and fan RPM along
    the bottom.

The rendered frame is written as a PNG (typically to ``/dev/shm`` so the engine
can push it to the device via ``set_lcd_static`` without SSD wear at ~0.5 Hz).
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_LOGGER = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Public style registry
# --------------------------------------------------------------------------- #

STYLES: dict[str, str] = {
    "liquid_ring": "Liquid ring",      # huge liquid temp, purple arc gauge around rim
    "cpu_gpu": "CPU / GPU split",      # two half-dials: CPU temp+load top, GPU temp+load bottom
    "triple": "All sensors",           # liquid center-big, cpu left, gpu right, pump/fan footer
}

# --------------------------------------------------------------------------- #
# Geometry and palette
# --------------------------------------------------------------------------- #

CANVAS = 640
CENTER = (CANVAS / 2.0, CANVAS / 2.0)
RADIUS = 320.0          # inscribed-circle radius (corners are not visible)
SAFE_MARGIN = 24.0      # keep content this far inside the visible circle
SAFE_RADIUS = RADIUS - SAFE_MARGIN

# Design language (mirrors gui/theme.py COLORS where relevant).
_BG = (13, 14, 18)            # #0d0e12 near-black background
_ACCENT = (124, 58, 237)      # #7c3aed NZXT purple
_TEXT = (236, 237, 241)       # #ecedf1 primary numbers
_TEXT_DIM = (139, 142, 152)   # #8b8e98 labels
_RING_BG = (42, 45, 57)       # #2a2d39 unfilled ring / track
_OK = (52, 211, 153)          # #34d399 green
_WARN = (251, 191, 36)        # #fbbf24 amber
_CRIT = (239, 68, 68)         # #ef4444 red
_CPU = (56, 189, 248)         # #38bdf8 cyan
_GPU = (52, 211, 153)         # #34d399 green

# Vendor badges: wordmark text + brand colour (stylised, not the trademarked
# logos). Drawn next to the CPU/GPU readouts; vendor is auto-detected.
_VENDOR_BADGES: dict[str, tuple[str, tuple[int, int, int]]] = {
    "amd": ("AMD", (237, 28, 36)),       # AMD red
    "intel": ("INTEL", (0, 113, 197)),   # Intel blue
    "nvidia": ("NVIDIA", (118, 185, 0)), # NVIDIA green
}

# Per-metric warning / critical thresholds (°C).
_THRESHOLDS: dict[str, tuple[float, float]] = {
    "cpu": (75.0, 88.0),
    "gpu": (85.0, 100.0),
    "liquid": (42.0, 50.0),
}

# Gauge sweep: 270° arc, the classic open-bottom dial (135° .. 405°).
_ARC_START = 135.0
_ARC_SWEEP = 270.0

# Liquid temperature range mapped onto the ring sweep.
_LIQUID_MIN = 20.0
_LIQUID_MAX = 60.0

_PLACEHOLDER = "--"

# Font candidate chain. DejaVu Sans Bold is the design font, but it is not
# universally installed: Debian/Ubuntu ship it in fonts-dejavu-core (hence the
# truetype/dejavu path), while on Arch it is the optional ttf-dejavu package and
# lives in a differently named directory. So the chain widens to the other
# metric-generous bold sans families that a desktop is likely to already have
# (ttf-liberation, noto-fonts — both satisfy Arch's virtual "ttf-font"). Bare
# file names are resolved by Pillow, which walks the standard font directories;
# the absolute paths are there to make the common cases deterministic.
#
# Anything here beats the fallback: PIL's built-in font is a bitmap face that
# looks badly aliased scaled up to the 210 px temperature readout.
_FONT_CANDIDATES = (
    # DejaVu Sans Bold — the intended face.
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Debian/Ubuntu
    "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",              # Arch
    "DejaVuSans-Bold.ttf",
    # Liberation Sans Bold — metric-compatible with Arial, on most desktops.
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",   # Arch
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",  # Debian
    "LiberationSans-Bold.ttf",
    # Noto Sans Bold — the last broad fallback.
    "/usr/share/fonts/noto/NotoSans-Bold.ttf",               # Arch
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",      # Debian
    "NotoSans-Bold.ttf",
)

# --------------------------------------------------------------------------- #
# Data container
# --------------------------------------------------------------------------- #


@dataclass
class LcdData:
    """Snapshot of the values a sensor screen can display.

    Any field may be ``None`` (sensor missing / device disconnected); those are
    rendered as ``"--"``.
    """

    liquid_temp: float | None
    cpu_temp: float | None
    cpu_load: float | None
    gpu_temp: float | None
    gpu_load: float | None
    pump_rpm: int | None
    fan_rpm: int | None
    #: Hardware vendor tags ("amd"/"intel"/"nvidia"/None) for the CPU/GPU badges.
    cpu_vendor: str | None = None
    gpu_vendor: str | None = None
    #: Colour of the liquid arc when below the warn threshold (RGB 0-255).
    ring_color: tuple[int, int, int] = _ACCENT


# --------------------------------------------------------------------------- #
# Font caching
# --------------------------------------------------------------------------- #

_FONT_CACHE: dict[int, ImageFont.FreeTypeFont | ImageFont.ImageFont] = {}


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Return a bold font of *size* px, cached at module level by size.

    Tries the DejaVuSans-Bold candidate chain, then falls back to the bundled
    PIL default font (which still honours ``size`` on Pillow >= 10).
    """
    cached = _FONT_CACHE.get(size)
    if cached is not None:
        return cached

    font: ImageFont.FreeTypeFont | ImageFont.ImageFont | None = None
    for candidate in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(candidate, size)
            break
        except OSError:
            continue

    if font is None:
        _LOGGER.warning(
            "no bold TrueType face found (tried DejaVu/Liberation/Noto); "
            "falling back to PIL's bitmap font at %d px. Install a font "
            "package (Arch: ttf-dejavu; Debian: fonts-dejavu-core) for "
            "legible LCD sensor screens.",
            size,
        )
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:
            # Very old Pillow without the size kwarg.
            font = ImageFont.load_default()

    _FONT_CACHE[size] = font
    return font


# --------------------------------------------------------------------------- #
# Small drawing helpers
# --------------------------------------------------------------------------- #


def _temp_color(value: float | None, kind: str) -> tuple[int, int, int]:
    """Pick text color for a temperature *value* given its metric *kind*."""
    if value is None:
        return _TEXT_DIM
    warn, crit = _THRESHOLDS.get(kind, (float("inf"), float("inf")))
    if value >= crit:
        return _CRIT
    if value >= warn:
        return _WARN
    return _TEXT


def _measure(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
) -> tuple[float, float]:
    """Return (width, height) of *text* using the font's tight bounding box."""
    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    return (right - left, bottom - top)


def _draw_text_anchored(
    draw: ImageDraw.ImageDraw,
    xy: tuple[float, float],
    text: str,
    font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    fill: tuple[int, int, int],
    anchor: str = "mm",
) -> None:
    """Draw *text* with a PIL anchor, tolerating fonts that lack anchor support."""
    try:
        draw.text(xy, text, font=font, fill=fill, anchor=anchor)
    except (ValueError, AttributeError):
        # Bitmap default font: emulate centre/middle anchoring manually.
        w, h = _measure(draw, text, font)
        x, y = xy
        if anchor and anchor[0] == "m":
            x -= w / 2.0
        elif anchor and anchor[0] == "r":
            x -= w
        if len(anchor) > 1 and anchor[1] == "m":
            y -= h / 2.0
        elif len(anchor) > 1 and anchor[1] in ("s", "b"):
            y -= h
        draw.text((x, y), text, font=font, fill=fill)


def _fmt_temp(value: float | None) -> str:
    """Integer-rounded temperature string, or the placeholder."""
    if value is None:
        return _PLACEHOLDER
    return f"{round(value):d}"


def _fmt_load(value: float | None) -> str:
    """Integer load-percentage string with ``%``, or the placeholder."""
    if value is None:
        return _PLACEHOLDER
    return f"{round(value):d}%"


def _fmt_rpm(value: int | None) -> str:
    """RPM string, or the placeholder."""
    if value is None:
        return _PLACEHOLDER
    return f"{int(value)}"


def _new_canvas() -> tuple[Image.Image, ImageDraw.ImageDraw]:
    """Create a fresh background image and an anti-aliased draw context."""
    img = Image.new("RGB", (CANVAS, CANVAS), _BG)
    draw = ImageDraw.Draw(img)
    return img, draw


def _arc_bbox(radius: float) -> tuple[float, float, float, float]:
    """Bounding box (x0, y0, x1, y1) for a circle of *radius* about CENTER."""
    cx, cy = CENTER
    return (cx - radius, cy - radius, cx + radius, cy + radius)


def _draw_load_bar(
    draw: ImageDraw.ImageDraw,
    x0: float,
    y: float,
    x1: float,
    height: float,
    fraction: float | None,
    color: tuple[int, int, int],
) -> None:
    """Draw a rounded horizontal load bar from *x0* to *x1* centred on *y*.

    *fraction* is 0..1; ``None`` leaves the track empty (dimmed).
    """
    radius = height / 2.0
    top = y - radius
    bottom = y + radius
    # Track.
    draw.rounded_rectangle((x0, top, x1, bottom), radius=radius, fill=_RING_BG)
    if not fraction:
        return
    frac = max(0.0, min(1.0, fraction))
    fill_w = (x1 - x0) * frac
    if fill_w < height:
        fill_w = height  # keep the rounded cap visible for tiny values
    draw.rounded_rectangle(
        (x0, top, x0 + fill_w, bottom), radius=radius, fill=color
    )


#: Where users may drop their own official vendor logo PNGs (RGBA, any size);
#: e.g. ``~/.config/openkraken/logos/nvidia.png``. Used in preference to the
#: built-in stylised wordmark so the project ships no trademarked artwork.
LOGO_DIR = Path.home() / ".config" / "openkraken" / "logos"

#: Cache of loaded+scaled logo images, keyed by (vendor, target_height).
_LOGO_CACHE: dict[tuple[str, int], Image.Image | None] = {}

#: Bundled OpenKraken droplet mark (used as the "Liquid" logo on sensor screens).
_APP_MARK_PATH = Path(__file__).resolve().parent.parent / "resources" / "openkraken-mark.png"
_APP_MARK_CACHE: dict[int, Image.Image | None] = {}


def _load_app_mark(target_h: int) -> Image.Image | None:
    """Load and scale the bundled OpenKraken droplet mark to *target_h* px."""
    if target_h in _APP_MARK_CACHE:
        return _APP_MARK_CACHE[target_h]
    mark: Image.Image | None = None
    try:
        if _APP_MARK_PATH.is_file():
            src = Image.open(_APP_MARK_PATH).convert("RGBA")
            scale = target_h / max(1, src.height)
            mark = src.resize((max(1, round(src.width * scale)), target_h))
    except (OSError, ValueError) as exc:
        _LOGGER.warning("could not load app mark %s: %s", _APP_MARK_PATH, exc)
    _APP_MARK_CACHE[target_h] = mark
    return mark


def _load_vendor_logo(vendor: str, target_h: int) -> Image.Image | None:
    """Load and scale a user-supplied ``<vendor>.png`` to *target_h* px, or None."""
    key = (vendor, target_h)
    if key in _LOGO_CACHE:
        return _LOGO_CACHE[key]
    logo: Image.Image | None = None
    path = LOGO_DIR / f"{vendor}.png"
    try:
        if path.is_file():
            src = Image.open(path).convert("RGBA")
            scale = target_h / max(1, src.height)
            logo = src.resize((max(1, round(src.width * scale)), target_h))
    except (OSError, ValueError) as exc:  # unreadable / not an image
        _LOGGER.warning("could not load vendor logo %s: %s", path, exc)
        logo = None
    _LOGO_CACHE[key] = logo
    return logo


def _draw_vendor_badge(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    center: tuple[float, float],
    vendor: str | None,
    size: int = 22,
    anchor: str = "center",
    max_width: float | None = None,
) -> None:
    """Draw the vendor mark at *center* (no-op if vendor unknown).

    Prefers a user-supplied ``LOGO_DIR/<vendor>.png`` (official artwork the user
    is entitled to); otherwise falls back to a stylised wordmark badge so the
    project itself bundles no trademarked logos.  *anchor* is ``"center"`` or
    ``"left"`` (``center[0]`` is then the mark's left edge); *max_width* scales a
    wide logo down so it still fits its slot (logos vary a lot in aspect ratio).
    """
    key = (vendor or "").lower()
    entry = _VENDOR_BADGES.get(key)
    if entry is None:
        return
    cx, cy = center

    logo = _load_vendor_logo(key, target_h=int(size * 1.6))
    if logo is not None:
        if max_width is not None and logo.width > max_width:
            scale = max_width / logo.width
            logo = logo.resize((int(max_width), max(1, int(logo.height * scale))))
        x = int(cx) if anchor == "left" else int(cx - logo.width / 2)
        img.paste(logo, (x, int(cy - logo.height / 2)), logo)
        return

    text, color = entry
    font = _font(size)
    pad_x, pad_y = 9.0, 4.0
    w, h = _measure(draw, text, font)
    bx = cx + w / 2.0 + pad_x if anchor == "left" else cx
    x0, y0 = bx - w / 2.0 - pad_x, cy - h / 2.0 - pad_y
    x1, y1 = bx + w / 2.0 + pad_x, cy + h / 2.0 + pad_y
    draw.rounded_rectangle((x0, y0, x1, y1), radius=(y1 - y0) / 2.0, outline=color, width=2)
    _draw_text_anchored(draw, (bx, cy), text, font, color, anchor="mm")


def _draw_liquid_arc(
    draw: ImageDraw.ImageDraw,
    liquid_temp: float | None,
    ring_radius: float,
    track_w: int = 14,
    arc_w: int = 26,
    ring_color: tuple[int, int, int] = _ACCENT,
) -> None:
    """Draw the full track ring + the liquid-temperature value arc (shared).

    *ring_color* tints the arc while the temperature is below the warn
    threshold; warn/crit still override it amber/red so the safety cue survives.
    """
    arc_box = _arc_bbox(ring_radius)
    draw.arc(arc_box, start=_ARC_START, end=_ARC_START + _ARC_SWEEP, fill=_RING_BG, width=track_w)
    if liquid_temp is None:
        return
    frac = max(0.0, min(1.0, (liquid_temp - _LIQUID_MIN) / (_LIQUID_MAX - _LIQUID_MIN)))
    sweep = _ARC_SWEEP * frac
    if sweep <= 0.5:
        return
    arc_color = _temp_color(liquid_temp, "liquid")
    if arc_color == _TEXT:  # below warn -> user-chosen ring colour
        arc_color = ring_color
    draw.arc(arc_box, start=_ARC_START, end=_ARC_START + sweep, fill=arc_color, width=arc_w)


# --------------------------------------------------------------------------- #
# Style: liquid_ring
# --------------------------------------------------------------------------- #


def _render_liquid_ring(data: LcdData) -> Image.Image:
    """Render the CAM-classic liquid-temperature ring style."""
    img, draw = _new_canvas()
    cx, cy = CENTER

    ring_radius = SAFE_RADIUS - 14.0
    _draw_liquid_arc(draw, data.liquid_temp, ring_radius, ring_color=data.ring_color)
    value_color = _temp_color(data.liquid_temp, "liquid") if data.liquid_temp is not None else _TEXT_DIM

    # "LIQUID" label above the big number.
    label_font = _font(46)
    _draw_text_anchored(
        draw, (cx, cy - 150), "LIQUID", label_font, _TEXT_DIM, anchor="mm"
    )

    # Huge centred temperature number (~200 px digits) with a small "°C" suffix.
    big_font = _font(210)
    temp_text = _fmt_temp(data.liquid_temp)
    tw, th = _measure(draw, temp_text, big_font)
    suffix_font = _font(58)
    suffix = "°C"
    sw, _sh = _measure(draw, suffix, suffix_font)
    gap = 14.0
    # Centre the (number + suffix) group as a unit on cx.
    group_w = tw + gap + sw
    num_cx = cx - group_w / 2.0 + tw / 2.0
    _draw_text_anchored(
        draw, (num_cx, cy + 2), temp_text, big_font, value_color, anchor="mm"
    )
    suffix_x = num_cx + tw / 2.0 + gap + sw / 2.0
    _draw_text_anchored(
        draw, (suffix_x, cy - th / 2.0 + 36), suffix, suffix_font, _TEXT_DIM, anchor="mm"
    )

    # Pump / fan RPM small at the bottom, inside the dial opening.
    foot_font = _font(34)
    foot_label_font = _font(26)
    foot_y = cy + 150
    pump_x = cx - 96
    fan_x = cx + 96
    _draw_text_anchored(
        draw, (pump_x, foot_y), "PUMP", foot_label_font, _TEXT_DIM, anchor="mm"
    )
    _draw_text_anchored(
        draw,
        (pump_x, foot_y + 34),
        _fmt_rpm(data.pump_rpm),
        foot_font,
        _TEXT if data.pump_rpm is not None else _TEXT_DIM,
        anchor="mm",
    )
    _draw_text_anchored(
        draw, (fan_x, foot_y), "FAN", foot_label_font, _TEXT_DIM, anchor="mm"
    )
    _draw_text_anchored(
        draw,
        (fan_x, foot_y + 34),
        _fmt_rpm(data.fan_rpm),
        foot_font,
        _TEXT if data.fan_rpm is not None else _TEXT_DIM,
        anchor="mm",
    )

    return img


# --------------------------------------------------------------------------- #
# Style: cpu_gpu
# --------------------------------------------------------------------------- #


def _render_half(
    img: Image.Image,
    draw: ImageDraw.ImageDraw,
    band_center_y: float,
    label: str,
    accent: tuple[int, int, int],
    kind: str,
    temp: float | None,
    load: float | None,
    vendor: str | None = None,
) -> None:
    """Render one half (CPU or GPU) of the split screen.

    The half is laid out around *band_center_y*: a small accent label and a big
    temperature on a line, with a horizontal load bar beneath.
    """
    cx, cy = CENTER

    label_font = _font(40)
    big_font = _font(132)
    unit_font = _font(40)
    load_font = _font(34)

    temp_y = band_center_y - 34
    bar_y = band_center_y + 70

    # Horizontal extent of the load bar, clipped to the inscribed safe circle at
    # the bar's own y-offset (the widest, outermost element of the band) and
    # pulled in a little so the rounded caps never kiss the rim.
    bar_dy = abs(bar_y - cy)
    half_w = math.sqrt(max(0.0, SAFE_RADIUS**2 - bar_dy**2)) - 16.0
    half_w = max(60.0, half_w)
    bar_x0 = cx - half_w
    bar_x1 = cx + half_w
    # Accent label (e.g. "CPU") above the number, with the vendor badge beside it.
    label_y = band_center_y - 116
    _draw_text_anchored(draw, (cx, label_y), label, label_font, accent, anchor="mm")
    if vendor:
        lw, _lh = _measure(draw, label, label_font)
        # Left-anchored just right of the label, width-capped so a wide wordmark
        # (e.g. AMD) clears the label and stays inside the round bezel.
        _draw_vendor_badge(
            img, draw, (cx + lw / 2.0 + 14, label_y), vendor,
            size=18, anchor="left", max_width=70,
        )

    temp_text = _fmt_temp(temp)
    color = _temp_color(temp, kind)
    tw, th = _measure(draw, temp_text, big_font)
    unit = "°"
    uw, _uh = _measure(draw, unit, unit_font)
    gap = 8.0
    group_w = tw + gap + uw
    num_cx = cx - group_w / 2.0 + tw / 2.0
    _draw_text_anchored(
        draw, (num_cx, temp_y), temp_text, big_font, color, anchor="mm"
    )
    _draw_text_anchored(
        draw,
        (num_cx + tw / 2.0 + gap + uw / 2.0, temp_y - th / 2.0 + 30),
        unit,
        unit_font,
        _TEXT_DIM,
        anchor="mm",
    )

    # Load bar beneath the temperature.
    frac = None if load is None else load / 100.0
    _draw_load_bar(draw, bar_x0, bar_y, bar_x1, 18.0, frac, accent)
    _draw_text_anchored(
        draw,
        (cx, bar_y + 34),
        _fmt_load(load),
        load_font,
        _TEXT if load is not None else _TEXT_DIM,
        anchor="mm",
    )


def _render_cpu_gpu(data: LcdData) -> Image.Image:
    """Render the CPU (top) / GPU (bottom) split style."""
    img, draw = _new_canvas()
    cx, cy = CENTER

    # Horizontal divider across the visible circle.
    div_half = math.sqrt(max(0.0, SAFE_RADIUS**2 - 0.0))  # widest chord, at center
    draw.line(
        (cx - div_half + 30, cy, cx + div_half - 30, cy),
        fill=_RING_BG,
        width=3,
    )

    _render_half(
        img,
        draw,
        band_center_y=cy - 156,
        label="CPU",
        accent=_CPU,
        kind="cpu",
        temp=data.cpu_temp,
        load=data.cpu_load,
        vendor=data.cpu_vendor,
    )
    _render_half(
        img,
        draw,
        band_center_y=cy + 156,
        label="GPU",
        accent=_GPU,
        kind="gpu",
        temp=data.gpu_temp,
        load=data.gpu_load,
        vendor=data.gpu_vendor,
    )

    return img


# --------------------------------------------------------------------------- #
# Style: triple
# --------------------------------------------------------------------------- #


def _render_triple(data: LcdData) -> Image.Image:
    """Render the all-sensors screen: rim ring + liquid centre, CPU/GPU sides,
    vendor badges, and a pump/fan footer (visuals lifted to clear the footer)."""
    img, draw = _new_canvas()
    cx, cy = CENTER

    # Liquid-temperature arc around the rim (shared with the liquid_ring style).
    _draw_liquid_arc(
        draw, data.liquid_temp, SAFE_RADIUS - 14.0, track_w=12, arc_w=20, ring_color=data.ring_color
    )

    # --- Liquid, large, centred and lifted up. ---
    liquid_y = cy - 92
    # The OpenKraken droplet mark stands in for the "LIQUID" label.
    mark = _load_app_mark(target_h=46)
    if mark is not None:
        img.paste(mark, (int(cx - mark.width / 2), int(liquid_y - 92 - mark.height / 2)), mark)
    else:
        _draw_text_anchored(
            draw, (cx, liquid_y - 92), "LIQUID", _font(32), _TEXT_DIM, anchor="mm"
        )
    liquid_text = _fmt_temp(data.liquid_temp)
    liquid_color = _temp_color(data.liquid_temp, "liquid")
    liquid_font = _font(138)
    liquid_unit_font = _font(42)
    lw, lh = _measure(draw, liquid_text, liquid_font)
    unit = "°C"
    uw, _uh = _measure(draw, unit, liquid_unit_font)
    gap = 10.0
    num_cx = cx - (lw + gap + uw) / 2.0 + lw / 2.0
    _draw_text_anchored(
        draw, (num_cx, liquid_y), liquid_text, liquid_font, liquid_color, anchor="mm"
    )
    _draw_text_anchored(
        draw,
        (num_cx + lw / 2.0 + gap + uw / 2.0, liquid_y - lh / 2.0 + 28),
        unit,
        liquid_unit_font,
        _TEXT_DIM,
        anchor="mm",
    )

    # --- CPU (left) and GPU (right), medium, with vendor badges. ---
    side_label_font = _font(28)
    side_temp_font = _font(60)
    side_load_font = _font(28)
    side_y = cy + 72
    cpu_x = cx - 118
    gpu_x = cx + 118

    def _side(x: float, label: str, accent: tuple[int, int, int], kind: str,
              temp: float | None, load: float | None, vendor: str | None) -> None:
        # Vendor logo replaces the CPU/GPU text label; fall back to the label
        # (or wordmark badge) when no logo image is installed for this vendor.
        logo = _load_vendor_logo((vendor or "").lower(), target_h=34) if vendor else None
        if logo is not None:
            max_w = 150.0
            if logo.width > max_w:
                s = max_w / logo.width
                logo = logo.resize((int(max_w), max(1, int(logo.height * s))))
            img.paste(logo, (int(x - logo.width / 2), int(side_y - 50 - logo.height / 2)), logo)
        elif vendor and (vendor or "").lower() in _VENDOR_BADGES:
            _draw_vendor_badge(img, draw, (x, side_y - 50), vendor, size=20)
        else:
            _draw_text_anchored(draw, (x, side_y - 48), label, side_label_font, accent, anchor="mm")
        temp_text = _fmt_temp(temp)
        if temp is not None:
            temp_text += "°"
        _draw_text_anchored(
            draw, (x, side_y), temp_text, side_temp_font, _temp_color(temp, kind), anchor="mm"
        )
        _draw_text_anchored(
            draw,
            (x, side_y + 44),
            _fmt_load(load),
            side_load_font,
            _TEXT_DIM if load is None else _TEXT,
            anchor="mm",
        )

    _side(cpu_x, "CPU", _CPU, "cpu", data.cpu_temp, data.cpu_load, data.cpu_vendor)
    _side(gpu_x, "GPU", _GPU, "gpu", data.gpu_temp, data.gpu_load, data.gpu_vendor)

    # Thin vertical divider between the two side columns.
    draw.line((cx, side_y - 36, cx, side_y + 52), fill=_RING_BG, width=2)

    # --- Pump + fan RPM footer row, well clear of the side block. ---
    foot_label_font = _font(22)
    foot_font = _font(28)
    foot_y = cy + 178
    pump_x = cx - 108
    fan_x = cx + 108
    _draw_text_anchored(draw, (pump_x, foot_y), "PUMP", foot_label_font, _TEXT_DIM, anchor="mm")
    _draw_text_anchored(
        draw,
        (pump_x, foot_y + 28),
        _fmt_rpm(data.pump_rpm),
        foot_font,
        _TEXT if data.pump_rpm is not None else _TEXT_DIM,
        anchor="mm",
    )
    _draw_text_anchored(draw, (fan_x, foot_y), "FAN", foot_label_font, _TEXT_DIM, anchor="mm")
    _draw_text_anchored(
        draw,
        (fan_x, foot_y + 28),
        _fmt_rpm(data.fan_rpm),
        foot_font,
        _TEXT if data.fan_rpm is not None else _TEXT_DIM,
        anchor="mm",
    )

    return img


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #

_RENDERERS = {
    "liquid_ring": _render_liquid_ring,
    "cpu_gpu": _render_cpu_gpu,
    "triple": _render_triple,
}


def render(style: str, data: LcdData) -> Image.Image:
    """Render a 640x640 RGB sensor screen for the given *style*.

    Unknown styles fall back to ``"liquid_ring"`` (logged), so a stale config
    value can never crash the engine's render loop.
    """
    renderer = _RENDERERS.get(style)
    if renderer is None:
        _LOGGER.warning("unknown LCD style %r; falling back to 'liquid_ring'", style)
        renderer = _render_liquid_ring
    return renderer(data)


def render_to_file(
    style: str,
    data: LcdData,
    path: str = "/dev/shm/openkraken_lcd.png",
) -> str:
    """Render *style*/*data* and write it as a PNG to *path*; return *path*.

    Defaults to ``/dev/shm`` (tmpfs) so the engine can write a fresh frame at
    ~0.5 Hz without wearing the SSD.
    """
    img = render(style, data)
    img.save(path, format="PNG")
    _LOGGER.debug("rendered LCD style %r to %s", style, path)
    return path
