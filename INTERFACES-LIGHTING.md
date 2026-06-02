# Kraken CAM — Lighting feature interface addendum

Extends `INTERFACES.md` (same rules apply). Adds native LED control for the
Kraken 2024 Elite RGB: the 24-LED pump ring and the RGB Core fan channel,
spoken directly over the device's HID interface per `PROTOCOL.md` (the wire
protocol document — the single source of truth for byte layouts; device.py
encapsulates ALL of it, nothing byte-level leaks above device.py).

## krakencam/config.py — additions

```python
@dataclass
class LightingChannelConfig:
    mode: str = "fixed"                  # key into lighting_fx.MODES
    colors: list[tuple[int, int, int]] = [(124, 58, 237)]   # RGB 0-255, count clamped to mode's min/max
    brightness: int = 100                # 0-100, applied HOST-SIDE (device has no brightness cmd)
    speed: str = "normal"                # animation step speed: "slow"|"normal"|"fast"

@dataclass
class LightingConfig:
    enabled: bool = False                # False = app never touches LEDs (default: don't surprise the user)
    sync: bool = True                    # True = ring config drives both channels
    ring: LightingChannelConfig
    fans: LightingChannelConfig

AppConfig.lighting: LightingConfig      # new field; from_dict tolerant as usual
```

## krakencam/backend/device.py — additions

ALL byte layouts come from PROTOCOL.md §3, §4, §6 (and ONLY the confirmed
sections — nothing from §7 do-not-use or §9 FYI tables). Native HID I/O goes
through the connected liquidctl driver's underlying HID handle, mirroring how
kraken3.py itself does `_write`/`_read` (64-byte reports, right-zero-padded,
`clear_enqueued_reports` before request/reply exchanges), guarded by the
existing RLock and disconnect-on-IO-error semantics.

```python
LIGHTING_CHANNELS: dict[str, int] = {"ring": 0x01, "fans": 0x02}   # wire masks (PROTOCOL.md §2)

@dataclass
class LightingInfo:                      # parsed 0x20 0x03 reply (PROTOCOL.md §6)
    channel_count: int
    accessories: dict[str, list[int]]    # channel name -> non-zero accessory ids
    led_counts: dict[str, int]           # channel name -> summed LED count (ring expected 24)

class KrakenDevice:
    lighting_info: LightingInfo | None   # populated by query_lighting_info(), None until then

    def query_lighting_info(self) -> LightingInfo | None
        # writes [0x20,0x03], reads until 0x21 0x03 prefix, parses per §6.
        # Unknown accessory ids -> log + assume 8 LEDs. Called from connect()
        # after initialize(); failure is non-fatal (lighting_info stays None,
        # fall back to ring=24, fans=16).

    def write_lighting_frame(self, channel: str, led_colors: list[tuple[int, int, int]]) -> bool
        # The ONE lighting write path (PROTOCOL.md §4): RGB->GRB, packet 0x22 0x10
        # with leds[0:60], packet 0x22 0x11 with leds[60:] ONLY when count > 20
        # (quirk A workaround), then the OpenRGB apply packet (0x22 0xA0, byte7=0x28).
        # Module constant _APPLY_VARIANTS holds both byte-7 variants (0x28 OpenRGB /
        # 0x08 liquidctl) so the hardware probe can A/B them; default index 0.
```

## krakencam/backend/lighting_fx.py — NEW module (pure functions, no Qt, no I/O)

Host-side effect engine (PROTOCOL.md §5: device hardware modes are rejected;
we stream Direct frames at ~1 FPS).

```python
@dataclass(frozen=True)
class ModeSpec:
    key: str; label: str; min_colors: int; max_colors: int; animated: bool

MODES: dict[str, ModeSpec] = {
    "off":      ModeSpec("off", "Off", 0, 0, False),
    "fixed":    ModeSpec("fixed", "Fixed", 1, 1, False),
    "breathing":ModeSpec("breathing", "Breathing", 1, 4, True),   # cycles colors, sine dim curve
    "cycle":    ModeSpec("cycle", "Color cycle", 2, 8, True),     # crossfade through color list
    "spectrum": ModeSpec("spectrum", "Spectrum wave", 0, 0, True),# hue rotation around the ring
}

SPEED_PERIODS = {"slow": 12.0, "normal": 6.0, "fast": 3.0}        # seconds per animation cycle

def frame(mode: str, colors: list[tuple[int,int,int]], brightness: int,
          led_count: int, t: float) -> list[tuple[int,int,int]]
    # t = seconds since the mode was applied (engine supplies monotonic elapsed).
    # Returns exactly led_count RGB triplets, brightness pre-scaled host-side.
    # "off" -> all (0,0,0); "fixed" -> solid; animated modes computed from t and
    # the mode's period. MUST look acceptable when sampled at 1 Hz (PROTOCOL.md
    # §5 FPS limit) — design transitions as slow drifts, not fast flashes.
```

## krakencam/backend/engine.py — additions

```python
def apply_lighting(self, cfg: LightingConfig) -> None   # queued like apply_lcd
```
- Applies ring then fans (ring config to both when sync) — only when cfg.enabled.
- Non-animated modes: write one frame per channel at apply time.
- Animated modes: each engine tick (poll_interval, min 1.0 s between lighting
  writes), compute `lighting_fx.frame(...)` per channel with elapsed time and the
  channel's `device.lighting_info` LED count, and `write_lighting_frame` it.
  Skip writes while disconnected; reset elapsed-time origin on (re)apply.
- Re-applied automatically after reconnect and on apply_on_start (like cooling/LCD).
- Emits applied("lighting", detail) / error(...) like other applies.
- LCD sensor pushes and lighting writes share the device lock — never block the
  status tick more than one frame.

## krakencam/gui/pages/lighting.py — new page `LightingPage`

`__init__(self, engine, config, parent=None)` like other pages.
- Top: "Control LEDs" enable checkbox + "Sync ring & fans" checkbox.
- Per-channel panel (Ring / Fans; Fans panel disabled+dimmed when sync on):
  mode combo (from lighting_fx.MODES labels), color swatch row — up to the
  mode's max_colors clickable swatches (QColorDialog), +/− buttons to add/remove
  swatches within min/max, brightness slider (0-100), speed combo (visible only
  for animated modes).
- Live preview: a 200 px round widget approximating the pump ring — 24 dots
  around a circle, driven by `lighting_fx.frame(...)` with a QTimer (1 s tick,
  only while page visible) so the preview matches what the device will show,
  including animations at their real (slow) cadence.
- Apply button → engine.apply_lighting + config.save(). A caption notes:
  "Protocol reverse-engineered by the community (liquidctl PR #882 / OpenRGB).
  Effects are streamed from this app at ~1 frame/s (device limit) and stop when
  the app closes; colors reset on AC power-cycle."
- A small info line shows detected channels from `engine` (via a getter that
  reads device.lighting_info): e.g. "Ring: 24 LEDs · Fans: 16 LEDs (detected)".

## krakencam/gui/main_window.py — change

Sidebar nav gains "Lighting" between Cooling and LCD; page wired into the stack.
Tray menu unchanged.

## Threading & safety

Same rules as INTERFACES.md. set_lighting calls are short (a few 64-byte
writes) — no special handling. The engine must never send lighting commands
when `lighting.enabled` is False.
