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

# Font candidate chain (per spec).
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "DejaVuSans-Bold.ttf",
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
            "DejaVuSans-Bold not found; falling back to PIL default font at %d px",
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


# --------------------------------------------------------------------------- #
# Style: liquid_ring
# --------------------------------------------------------------------------- #


def _render_liquid_ring(data: LcdData) -> Image.Image:
    """Render the CAM-classic liquid-temperature ring style."""
    img, draw = _new_canvas()
    cx, cy = CENTER

    ring_radius = SAFE_RADIUS - 14.0
    track_w = 14
    arc_w = 26
    arc_box = _arc_bbox(ring_radius)

    # Thin full track ring (270° to match the dial opening at the bottom).
    draw.arc(
        arc_box,
        start=_ARC_START,
        end=_ARC_START + _ARC_SWEEP,
        fill=_RING_BG,
        width=track_w,
    )

    # Thick value arc proportional to liquid temp over the 270° sweep.
    if data.liquid_temp is not None:
        frac = (data.liquid_temp - _LIQUID_MIN) / (_LIQUID_MAX - _LIQUID_MIN)
        frac = max(0.0, min(1.0, frac))
        sweep = _ARC_SWEEP * frac
        arc_color = _temp_color(data.liquid_temp, "liquid")
        if arc_color == _TEXT:  # value below warn -> use the brand purple
            arc_color = _ACCENT
        if sweep > 0.5:
            draw.arc(
                arc_box,
                start=_ARC_START,
                end=_ARC_START + sweep,
                fill=arc_color,
                width=arc_w,
            )
        value_color = _temp_color(data.liquid_temp, "liquid")
    else:
        value_color = _TEXT_DIM

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
    draw: ImageDraw.ImageDraw,
    band_center_y: float,
    label: str,
    accent: tuple[int, int, int],
    kind: str,
    temp: float | None,
    load: float | None,
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
    # Accent label (e.g. "CPU") above-left of the number.
    _draw_text_anchored(
        draw, (cx, band_center_y - 116), label, label_font, accent, anchor="mm"
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
        draw,
        band_center_y=cy - 156,
        label="CPU",
        accent=_CPU,
        kind="cpu",
        temp=data.cpu_temp,
        load=data.cpu_load,
    )
    _render_half(
        draw,
        band_center_y=cy + 156,
        label="GPU",
        accent=_GPU,
        kind="gpu",
        temp=data.gpu_temp,
        load=data.gpu_load,
    )

    return img


# --------------------------------------------------------------------------- #
# Style: triple
# --------------------------------------------------------------------------- #


def _render_triple(data: LcdData) -> Image.Image:
    """Render liquid-big-center, CPU-left / GPU-right, pump/fan footer."""
    img, draw = _new_canvas()
    cx, cy = CENTER

    # --- Liquid, large, centred (shifted slightly up to leave footer room). ---
    liquid_y = cy - 36
    liquid_label_font = _font(34)
    liquid_font = _font(150)
    liquid_unit_font = _font(44)

    _draw_text_anchored(
        draw, (cx, liquid_y - 108), "LIQUID", liquid_label_font, _TEXT_DIM, anchor="mm"
    )
    liquid_text = _fmt_temp(data.liquid_temp)
    liquid_color = _temp_color(data.liquid_temp, "liquid")
    lw, lh = _measure(draw, liquid_text, liquid_font)
    unit = "°C"
    uw, _uh = _measure(draw, unit, liquid_unit_font)
    gap = 10.0
    group_w = lw + gap + uw
    num_cx = cx - group_w / 2.0 + lw / 2.0
    _draw_text_anchored(
        draw, (num_cx, liquid_y), liquid_text, liquid_font, liquid_color, anchor="mm"
    )
    _draw_text_anchored(
        draw,
        (num_cx + lw / 2.0 + gap + uw / 2.0, liquid_y - lh / 2.0 + 30),
        unit,
        liquid_unit_font,
        _TEXT_DIM,
        anchor="mm",
    )

    # --- CPU (left) and GPU (right), medium. ---
    side_label_font = _font(30)
    side_temp_font = _font(64)
    side_load_font = _font(30)
    side_y = cy + 118
    cpu_x = cx - 150
    gpu_x = cx + 150

    def _side(x: float, label: str, accent: tuple[int, int, int], kind: str,
              temp: float | None, load: float | None) -> None:
        _draw_text_anchored(draw, (x, side_y - 52), label, side_label_font, accent, anchor="mm")
        temp_text = _fmt_temp(temp)
        if temp is not None:
            temp_text += "°"
        _draw_text_anchored(
            draw, (x, side_y), temp_text, side_temp_font, _temp_color(temp, kind), anchor="mm"
        )
        _draw_text_anchored(
            draw,
            (x, side_y + 48),
            _fmt_load(load),
            side_load_font,
            _TEXT_DIM if load is None else _TEXT,
            anchor="mm",
        )

    _side(cpu_x, "CPU", _CPU, "cpu", data.cpu_temp, data.cpu_load)
    _side(gpu_x, "GPU", _GPU, "gpu", data.gpu_temp, data.gpu_load)

    # Thin vertical divider between the two side columns.
    draw.line((cx, side_y - 40, cx, side_y + 56), fill=_RING_BG, width=2)

    # --- Pump + fan RPM footer row, inside the lower circle. ---
    foot_label_font = _font(24)
    foot_font = _font(30)
    foot_y = cy + 198
    pump_x = cx - 108
    fan_x = cx + 108
    _draw_text_anchored(draw, (pump_x, foot_y), "PUMP", foot_label_font, _TEXT_DIM, anchor="mm")
    _draw_text_anchored(
        draw,
        (pump_x, foot_y + 30),
        _fmt_rpm(data.pump_rpm),
        foot_font,
        _TEXT if data.pump_rpm is not None else _TEXT_DIM,
        anchor="mm",
    )
    _draw_text_anchored(draw, (fan_x, foot_y), "FAN", foot_label_font, _TEXT_DIM, anchor="mm")
    _draw_text_anchored(
        draw,
        (fan_x, foot_y + 30),
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
    path: str = "/dev/shm/krakencam_lcd.png",
) -> str:
    """Render *style*/*data* and write it as a PNG to *path*; return *path*.

    Defaults to ``/dev/shm`` (tmpfs) so the engine can write a fresh frame at
    ~0.5 Hz without wearing the SSD.
    """
    img = render(style, data)
    img.save(path, format="PNG")
    _LOGGER.debug("rendered LCD style %r to %s", style, path)
    return path
