# OpenKraken — Interface Specification

A Linux clone of NZXT CAM for the **NZXT Kraken 2024 Elite RGB** (USB `1e71:3012`),
built on PyQt6 + liquidctl. This document is the **single source of truth** for all
module interfaces. Implementers: follow it exactly — other modules are being written
in parallel against these signatures.

## Hard facts about the target system (verified on real hardware)

- Device: `NZXT Kraken 2024 Elite RGB`, matched by liquidctl driver class `KrakenZ3`
  (`liquidctl.driver.kraken3`), liquidctl >= 1.15 required (dev machine has 1.16.0).
  Reference driver source (read it for exact semantics, do NOT modify):
  `/home/davidboulay/.local/share/pipx/venvs/liquidctl/lib/python3.12/site-packages/liquidctl/driver/kraken3.py`
- Speed channels: `pump` (duty clamp 20–100 %), `fan` (0–100 %).
  `dev.set_speed_profile(channel, [(temp, duty), ...])` — temps are **liquid** temps;
  firmware stores a 40-point curve for 20–59 °C and runs it autonomously.
  `dev.set_fixed_speed(channel, duty)` sets a flat duty.
  CRITICAL_TEMPERATURE = 59 (profiles are normalized so duty = 100 at 59/60 °C).
- `dev.get_status()` returns list of tuples:
  `[("Liquid temperature", 40.0, "°C"), ("Pump speed", 1860, "rpm"), ("Pump duty", 40, "%"), ("Fan speed", 983, "rpm"), ("Fan duty", 40, "%")]`
- `dev.initialize()` returns `[("Firmware version", "x.y.z", ""), ("LCD Brightness", 50, "%"), ("LCD Orientation", 0, "°")]` (order may vary — parse by label).
- LCD: 640×640 **round** display. `dev.set_screen("lcd", mode, value)`:
  - `("lcd", "liquid", None)` — firmware liquid-temp screen
  - `("lcd", "brightness", "0".."100")`
  - `("lcd", "orientation", "0"|"90"|"180"|"270")`
  - `("lcd", "static", "/path/to/image")` — any PIL-openable image; driver resizes to 640×640, converts RGB565, bulk-uploads (~819 KB)
  - `("lcd", "gif", "/path/to.gif")` — encoded size must be < ~24 MB
  - There is **no blank mode** — emulate "off" with brightness 0 (remember/restore previous brightness).
- RGB lighting: NOT supported by liquidctl for this device (empty color channel map). Do not implement lighting.
- No NZXT kernel hwmon driver is bound on this system — liquidctl uses direct hidraw access. The driver may log hwmon warnings; harmless.
- Sensors on this machine: hwmon `k10temp` (AMD CPU; labels like `Tctl`, `Tccd1`),
  hwmon `amdgpu` (labels `edge`, `junction`, `mem`; also sysfs `gpu_busy_percent`,
  `mem_info_vram_used`, `mem_info_vram_total`, `power1_average`/`power1_input` in µW under the
  hwmon device dir or its parent `device/` dir), `/proc/stat` for CPU load, `/proc/meminfo` for RAM.
- Desktop: Pop!_OS, COSMIC, Wayland. Python 3.12, PyQt6 system-installed, Pillow 10.2.

## Project layout

```
openkraken/
├── INTERFACES.md            (this file)
├── pyproject.toml
├── README.md
├── setup.sh                 venv bootstrap + launcher + .desktop install
├── openkraken.desktop
└── openkraken/
    ├── __init__.py          __version__ = "0.1.0"
    ├── __main__.py
    ├── app.py
    ├── config.py
    ├── backend/
    │   ├── device.py
    │   ├── sensors.py
    │   ├── curves.py
    │   ├── engine.py
    │   └── lcd_render.py
    └── gui/
        ├── theme.py
        ├── main_window.py
        ├── widgets/   gauge.py, graph.py, curve_editor.py
        └── pages/     dashboard.py, cooling.py, lcd.py, settings.py
```

Style: Python 3.12, type hints everywhere, no external deps beyond PyQt6 / Pillow /
liquidctl (psutil is available but prefer raw /proc and /sys reads — fewer deps).
Every module: module docstring, logging via `logging.getLogger(__name__)`.
**Never** call `print()`. **Never** access `/dev/hidraw*` or run `liquidctl` CLI during
development — the real device is attached to this machine; integration testing happens later.

---

## openkraken/config.py

```python
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "openkraken"   # media cache: DEFAULT_CONFIG_DIR / "media"

@dataclass
class ChannelConfig:
    mode: str = "curve"            # "curve" | "fixed"
    source: str = "liquid"         # "liquid" | "cpu" | "gpu"   (x-axis input for curve mode)
    fixed_duty: int = 50
    points: list[tuple[float, int]] = field(...)   # [(temp_c, duty_pct)] sorted by temp
    profile: str = "balanced"      # "silent"|"balanced"|"performance"|"fixed"|"custom"

@dataclass
class LcdConfig:
    mode: str = "liquid"           # "liquid" | "sensors" | "static" | "gif" | "off"
    brightness: int = 50           # 0-100
    orientation: int = 0           # 0|90|180|270
    image_path: str = ""           # last chosen static image (absolute path)
    gif_path: str = ""             # last chosen gif
    sensor_style: str = "liquid_ring"   # see lcd_render.STYLES
    sensor_interval: float = 2.0   # seconds between sensor-screen pushes

@dataclass
class AppConfig:
    poll_interval: float = 1.0
    history_seconds: int = 600
    start_minimized: bool = False
    close_to_tray: bool = True
    apply_on_start: bool = True
    pump: ChannelConfig            # default profile "balanced"
    fan: ChannelConfig
    lcd: LcdConfig

    @classmethod
    def load(cls, path: Path | None = None) -> "AppConfig":   # missing/corrupt file -> defaults (log warning)
    def save(self, path: Path | None = None) -> None          # atomic write (tmp+rename), mkdir -p
    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, d: dict) -> "AppConfig"                # tolerant: unknown keys ignored, missing keys -> defaults
```

JSON file at `DEFAULT_CONFIG_DIR / "config.json"`. Tuples may round-trip as lists — normalize in `from_dict`.

---

## openkraken/backend/device.py

Thread-safe wrapper around the liquidctl driver. **All device I/O in the whole app goes
through this class.** Uses an internal `threading.RLock` around every driver call.

```python
@dataclass
class DeviceStatus:
    liquid_temp: float | None
    pump_rpm: int | None
    pump_duty: int | None
    fan_rpm: int | None
    fan_duty: int | None
    connected: bool
    timestamp: float        # time.monotonic()

class KrakenDevice:
    PUMP_DUTY_MIN, PUMP_DUTY_MAX = 20, 100
    FAN_DUTY_MIN, FAN_DUTY_MAX = 0, 100
    CRITICAL_TEMP = 59
    LCD_RESOLUTION = (640, 640)

    def __init__(self) -> None        # does NOT touch hardware
    def connect(self) -> bool         # find_liquidctl_devices(), match vendor 0x1e71 + KrakenZ3 instance,
                                      # dev.connect(), dev.initialize(); stores firmware_version,
                                      # lcd_brightness, lcd_orientation parsed from initialize() output.
                                      # Returns False (and logs) on any failure. Idempotent.
    def disconnect(self) -> None
    @property
    def is_connected(self) -> bool
    @property
    def description(self) -> str      # e.g. "NZXT Kraken 2024 Elite RGB" or "" when disconnected
    firmware_version: str             # "" until known
    lcd_brightness: int               # last known (50 default)
    lcd_orientation: int              # degrees 0/90/180/270

    def get_status(self) -> DeviceStatus      # on driver exception: log, mark disconnected,
                                              # return DeviceStatus(None*..., connected=False)
    def set_speed_profile(self, channel: str, points: list[tuple[float, int]]) -> bool
    def set_fixed_speed(self, channel: str, duty: int) -> bool   # clamps to channel limits
    def set_lcd_brightness(self, value: int) -> bool             # updates self.lcd_brightness on success
    def set_lcd_orientation(self, degrees: int) -> bool
    def set_lcd_liquid_mode(self) -> bool
    def set_lcd_static(self, image_path: str) -> bool            # blocking; can take ~0.1-1 s
    def set_lcd_gif(self, gif_path: str) -> bool
```

All setters return `True` on success, `False` on failure (exception logged, device
marked disconnected on I/O errors so the engine can attempt reconnection).
Import liquidctl lazily inside methods or at module top — module import must not
touch hardware. `set_speed_profile` passes `direct_access=True`? **No** — call
plainly; no hwmon is bound so the driver writes directly anyway.

---

## openkraken/backend/sensors.py

```python
@dataclass
class SystemSnapshot:
    cpu_temp: float | None      # °C, k10temp Tctl preferred, else first temp
    cpu_load: float | None      # 0-100 %, /proc/stat delta since previous call
    cpu_freq_mhz: float | None  # mean of scaling_cur_freq across policies
    gpu_temp: float | None      # amdgpu "edge" label preferred
    gpu_load: float | None      # gpu_busy_percent
    gpu_vram_used_mb: float | None
    gpu_vram_total_mb: float | None
    gpu_power_w: float | None
    ram_used_gb: float | None
    ram_total_gb: float | None
    timestamp: float

class SystemSensors:
    def __init__(self) -> None    # discovers hwmon paths once (rescan() to redo)
    def rescan(self) -> None      # walk /sys/class/hwmon, match names "k10temp"/"coretemp"/"zenpower", "amdgpu"
    def read(self) -> SystemSnapshot   # never raises; missing sensor -> None field
```

Implementation notes: all values from sysfs are millidegrees / µW etc. — convert.
First `read()` returns `cpu_load=None` (needs a delta). Cache file paths, not handles.
amdgpu extras (`gpu_busy_percent`, vram, power) live in the hwmon's `device/` symlink dir;
power via hwmon `power1_average` (fall back `power1_input`), µW → W.

---

## openkraken/backend/curves.py

```python
PROFILES: dict[str, dict[str, list[tuple[float, int]]]] = {
    # liquid-temp keyed presets; pump never below 20.  Calibrated to the real
    # loop: idles ~37-40 C liquid, so 100% is reached only at/after 50 C.
    "silent":      {"pump": [(20,40),(34,50),(40,60),(46,85),(50,100)], "fan": [(20,20),(34,30),(40,45),(46,70),(50,100)]},
    "balanced":    {"pump": [(20,50),(33,60),(40,75),(46,90),(50,100)], "fan": [(20,30),(33,40),(40,60),(45,80),(50,100)]},
    "performance": {"pump": [(20,70),(30,80),(38,95),(42,100)],          "fan": [(20,50),(30,65),(38,85),(44,100)]},
}

def interpolate(points: list[tuple[float, int]], x: float) -> float
    # piecewise-linear, clamped to end duties; empty list -> 50.0

def validate_points(points) -> list[tuple[float, int]]
    # sort by temp, dedupe temps, clamp duty 0-100, temp 0-99; ensure >= 2 points
    # (pad with (20, d) / (59, d) if needed)

def software_failsafe(points_or_duty, channel: str) -> list[tuple[float, int]]
    # When the curve source is cpu/gpu we write a FLAT liquid-temp profile at the
    # computed duty d — but with a firmware-level failsafe so a dead app can't cook
    # the loop:  [(20, d), (48, d), (54, 100)]
    # (firmware interpolates 48→54 °C up to 100 %; liquid stays < 45 °C in normal use)

class DutySmoother:
    """Hysteresis so we don't spam HID writes nor yo-yo the fan."""
    def __init__(self, deadband: float = 2.0, max_step_up: int = 100, max_step_down: int = 5) -> None
    def update(self, target: float) -> int | None
        # returns a duty to apply, or None if within deadband of last applied;
        # ramps DOWN slowly (max_step_down %/tick), jumps UP immediately (safety)
    def reset(self) -> None
```

---

## openkraken/backend/lcd_render.py

Pillow rendering of 640×640 sensor screens for the round LCD. Pure functions, no Qt.

```python
STYLES: dict[str, str] = {            # key -> human label
    "liquid_ring": "Liquid ring",     # CAM-classic: huge liquid temp, purple arc gauge around rim
    "cpu_gpu":     "CPU / GPU split", # two half-dials: CPU temp+load top, GPU temp+load bottom
    "triple":      "All sensors",     # liquid center-big, cpu left-small, gpu right-small, pump/fan rpm footer
}

def render(style: str, data: "LcdData") -> PIL.Image.Image    # 640x640 RGB

@dataclass
class LcdData:
    liquid_temp: float | None
    cpu_temp: float | None
    cpu_load: float | None
    gpu_temp: float | None
    gpu_load: float | None
    pump_rpm: int | None
    fan_rpm: int | None

def render_to_file(style: str, data: LcdData, path: str = "/dev/shm/openkraken_lcd.png") -> str
    # returns path written (PNG). /dev/shm => tmpfs, no SSD wear at 0.5 Hz.
```

Design language: near-black `#0d0e12` background, NZXT purple `#7c3aed` accents,
white `#ecedf1` primary numbers, gray `#8b8e98` labels, green/amber/red value coloring
above thresholds (CPU/GPU/liquid: warn 75/85/42, crit 88/100/50). Everything must fit a
**circle inscribed in the square** (corners are not visible). Use default PIL fonts via
`ImageFont.truetype` trying `["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "DejaVuSans-Bold.ttf"]`,
fallback `ImageFont.load_default(size=…)`. Missing values render as `"--"`.

---

## openkraken/backend/engine.py

The control loop. A `QThread` subclass owning **all** periodic work; the GUI never
touches `KrakenDevice` / `SystemSensors` directly — it calls the engine's thread-safe
request methods and listens to signals.

```python
class HistoryBuffers:
    """Ring buffers: metric name -> deque[(wall_time, value)]."""
    METRICS = ["liquid_temp","cpu_temp","gpu_temp","pump_rpm","fan_rpm","pump_duty","fan_duty","cpu_load","gpu_load"]
    def __init__(self, seconds: int, interval: float) -> None
    def append(self, status: DeviceStatus, snap: SystemSnapshot) -> None
    def series(self, metric: str) -> list[tuple[float, float]]   # copy, thread-safe
    def resize(self, seconds: int, interval: float) -> None

class ControlEngine(QThread):
    # ---- signals (emitted from engine thread; auto-queued to GUI) ----
    sample_ready = pyqtSignal(object, object)     # (DeviceStatus, SystemSnapshot)
    connection_changed = pyqtSignal(bool, str)    # (connected, description)
    applied = pyqtSignal(str, str)                # (what, detail) e.g. ("cooling","pump: balanced curve")
    error = pyqtSignal(str)

    def __init__(self, device: KrakenDevice, sensors: SystemSensors, config: AppConfig) -> None
    history: HistoryBuffers

    # ---- thread-safe requests from GUI (queue the work onto the engine loop) ----
    def apply_channel(self, channel: str, cfg: ChannelConfig) -> None
        # liquid-source curve  -> device.set_speed_profile(channel, points)  [firmware-native]
        # cpu/gpu-source curve -> store; loop computes duty each tick via interpolate +
        #                         DutySmoother and writes software_failsafe profile when it changes
        # fixed                -> set_fixed_speed once (still through software_failsafe for pump? NO —
        #                         fixed mode uses device.set_fixed_speed directly; firmware clamps)
    def apply_lcd(self, cfg: LcdConfig) -> None
        # mode "liquid" -> set_lcd_liquid_mode; "static"/"gif" -> upload file;
        # "sensors" -> loop pushes rendered frames every cfg.sensor_interval;
        # "off" -> brightness 0 (restore configured brightness when leaving "off");
        # always applies brightness + orientation when they changed.
    def request_reconnect(self) -> None
    def update_config(self, config: AppConfig) -> None     # poll interval / history resize
    def stop(self) -> None                                  # graceful: join with timeout

    def run(self) -> None
        # loop every config.poll_interval:
        #   1. if disconnected: try reconnect every 5 s (connection_changed on transition)
        #   2. status = device.get_status(); snap = sensors.read(); history.append; sample_ready.emit
        #   3. software curve channels: duty = interpolate(points, source_temp); d = smoother.update(duty)
        #      if d is not None: device.set_speed_profile(channel, software_failsafe(d, channel))
        #      source temp None (sensor missing) -> failsafe: apply 80 % duty once, log error signal
        #   4. lcd "sensors" mode and interval elapsed: render_to_file + set_lcd_static
        #   5. drain request queue (applies queued apply_channel/apply_lcd work)
        # Use a queue.Queue for requests; never block the loop > poll_interval.
```

On startup (`run()` begin): connect device; if `config.apply_on_start`, apply pump,
fan and LCD configs. LCD static uploads take up to ~1 s — perform them in the engine
thread (they're already serialized by the device lock); skip a sensor tick if needed.

---

## openkraken/gui/theme.py

```python
COLORS = {
  "bg": "#0d0e12", "panel": "#16181f", "panel2": "#1d2029", "border": "#2a2d39",
  "text": "#ecedf1", "text_dim": "#8b8e98", "accent": "#7c3aed", "accent_hover": "#8f55f0",
  "ok": "#34d399", "warn": "#fbbf24", "crit": "#ef4444",
  "cpu": "#38bdf8", "gpu": "#34d399", "liquid": "#7c3aed", "pump": "#f472b6", "fan": "#fbbf24",
}
def apply_theme(app: QApplication) -> None      # sets Fusion style + dark QPalette + QSS string
def series_color(metric: str) -> QColor          # dashboard graph colors by metric name
def make_app_icon() -> QIcon                     # QPainter-drawn purple droplet, 64px
def make_tray_icon(temp: float | None) -> QIcon  # droplet + integer temp text, drawn at 64px
```

QSS must style: QPushButton (flat dark, accent on checked/hover), QComboBox + popup,
QSlider (accent groove), QTabBar, QGroupBox, QScrollArea, QToolTip, QLineEdit,
QSpinBox/QDoubleSpinBox, QCheckBox, QStatusBar. Sidebar buttons get
`setProperty("sidebar", True)` and QSS `QPushButton[sidebar="true"]{...}` — left-aligned,
tall (44 px), accent left-border when checked.

## openkraken/gui/widgets/gauge.py

```python
class GaugeTile(QWidget):
    """Circular-arc gauge tile: title on top, big value center, sub-line below."""
    def __init__(self, title: str, unit: str, vmin: float, vmax: float,
                 color: QColor, warn: float | None = None, crit: float | None = None, parent=None)
    def set_value(self, value: float | None, sub_text: str = "") -> None   # None -> "--", dimmed arc
```
270° arc gauge (135°→405°), 10 px pen, rounded caps; arc color switches to warn/crit
colors past thresholds. Fixed-ish size ~170×190, scales with layout. Pure QPainter.

## openkraken/gui/widgets/graph.py

```python
class TimeSeriesGraph(QWidget):
    def __init__(self, y_label: str = "", y_min: float | None = None, y_max: float | None = None, parent=None)
    def set_series(self, name: str, points: list[tuple[float, float]], color: QColor) -> None
    def remove_series(self, name: str) -> None
    def set_window_seconds(self, seconds: int) -> None     # x-axis = last N seconds, right edge = now
    def clear(self) -> None
```
QPainter: panel background, dotted horizontal gridlines + y tick labels, anti-aliased
2 px polylines, auto y-range when y_min/y_max None (pad 10 %), legend chips top-left,
"now" at right edge, x tick labels as `-5m`, `-2m`, `0`. Repaint on set_series.

## openkraken/gui/widgets/curve_editor.py

```python
class CurveEditor(QWidget):
    curveChanged = pyqtSignal(list)         # list[tuple[float,int]] after any user edit
    def __init__(self, x_min=20, x_max=60, y_min=0, y_max=100, parent=None)
    def set_curve(self, points: list[tuple[float, int]]) -> None    # no signal emitted
    def curve(self) -> list[tuple[float, int]]
    def set_y_floor(self, duty: int) -> None      # pump=20: points can't be dragged below
    def set_live_marker(self, x: float | None, y: float | None) -> None  # current op point, pulsing dot
    def set_enabled_visual(self, enabled: bool) -> None   # dim when channel is in fixed mode
```
Interactions: drag points (left button), double-click empty space = add point,
right-click point = remove (min 2 points remain), grid + axis labels (°C / %),
filled area under curve (translucent accent), points are 8 px accent circles with
hover halo. Margins ~36 px left/bottom for labels. Emit `curveChanged` on mouse release.

## openkraken/gui/pages — common pattern

Each page is a QWidget with `def __init__(self, engine: ControlEngine, config: AppConfig, parent=None)`.
Pages connect to `engine.sample_ready` themselves. Pages that mutate config call
`config.save()` after engine.apply_* (config object is shared).

### dashboard.py — `DashboardPage`
Top: 5 GaugeTiles — CPU °C (0-100, warn 75 crit 88), GPU °C (0-100, warn 85 crit 100),
Liquid °C (20-60, warn 42 crit 50), Pump RPM (0-3600), Fan RPM (0-2400). Sub-lines:
CPU `load% @ GHz`, GPU `load% · power W`, Liquid `pump duty %`, Pump `duty %`, Fan `duty %`.
Below: two TimeSeriesGraphs — temperatures (°C: liquid/cpu/gpu) and speeds (rpm: pump/fan)
with metric checkboxes and a time-window combo (1m/5m/10m) shared by both.
Data: gauges fed from `sample_ready`; graphs re-fed from `engine.history.series()` on each sample.

### cooling.py — `CoolingPage`
Two side-by-side channel panels (Pump / Fan), each:
profile combo [Silent, Balanced, Performance, Fixed, Custom], source combo
[Liquid temp, CPU temp, GPU temp] (disabled for Fixed), CurveEditor (hidden in Fixed mode),
fixed-duty slider+spinbox (shown in Fixed mode), live operating-point marker fed from
sample_ready, Apply button per panel + "auto-apply on change" checkbox (default off).
Selecting a preset loads its points into the editor; editing the curve flips profile to
Custom. Apply → `engine.apply_channel(...)` + `config.save()`. Pump editor `set_y_floor(20)`.
A footer note: "Liquid-temp curves run in cooler firmware and persist after this app closes."

### lcd.py — `LcdPage`
Left column: mode selector (radio list): Liquid temp (firmware), Sensor screen (rendered),
Static image, Animated GIF, Screen off. Sensor-screen sub-options: style combo (from
`lcd_render.STYLES`) + refresh interval spinbox (0.5–10 s). Static/GIF sub-options:
file picker button + path label.
Right column: **round preview** (circular-clipped 320 px label) showing: for sensors —
live `lcd_render.render()` of current data (refresh ~2 s via page timer); for static/gif —
the chosen file (first frame), circularly cropped; for liquid/off — a drawn placeholder.
Below: brightness slider (0-100), orientation combo (0/90/180/270°).
Apply button → `engine.apply_lcd(...)` + save. Show a "spec" caption: 640×640 · GIF ≤ 24 MB.

### settings.py — `SettingsPage`
Form: poll interval (0.5–5 s), history window (60–3600 s), start minimized, close to tray,
apply saved settings on start. Device box: model, firmware, connection state + Reconnect
button. About box: version, link to liquidctl, disclaimer. Save button persists + 
`engine.update_config`.

## openkraken/gui/main_window.py

```python
class MainWindow(QMainWindow):
    def __init__(self, engine: ControlEngine, config: AppConfig)
```
920×640 min. Left sidebar 200 px: app title "OPENKRAKEN", nav buttons
(Dashboard/Cooling/LCD/Settings — checkable, exclusive), connection pill at bottom
(green dot + "Kraken Elite" / red + "Disconnected"). QStackedWidget with the 4 pages.
Status bar: last applied action (from `engine.applied`), errors (from `engine.error`, 5 s).
Tray: QSystemTrayIcon when `QSystemTrayIcon.isSystemTrayAvailable()` — menu: Show/Hide,
profile quick-switch (Silent/Balanced/Performance → applies to BOTH channels), Quit.
Tray icon updated with liquid temp每 sample (only re-render when integer °C changes).
closeEvent: hide to tray if config.close_to_tray and tray active, else accept and
`engine.stop()`. Window title "OpenKraken". Set `make_app_icon()`.

## openkraken/app.py + __main__.py

```python
def main(argv: list[str] | None = None) -> int
    # argparse: --minimized, --config PATH, --version, --debug (DEBUG logging)
    # logging.basicConfig (INFO default, format with time+module)
    # QApplication, setApplicationName("openkraken") + setDesktopFileName("openkraken"),
    # apply_theme, AppConfig.load, KrakenDevice, SystemSensors, ControlEngine(start()),
    # MainWindow (show unless minimized&&tray), aboutToQuit -> engine.stop(),
    # SIGINT handler (QTimer trick) for clean Ctrl-C, return app.exec()
__main__.py: `from openkraken.app import main; raise SystemExit(main())`
```

## Packaging

- `pyproject.toml`: project name `openkraken`, version 0.1.0, requires-python >=3.10,
  deps: `liquidctl>=1.15`, `Pillow>=9` (PyQt6 intentionally NOT a dep — provided by
  system or venv extras `[gui]` with `PyQt6>=6.4`), `[project.scripts] openkraken = "openkraken.app:main"`.
- `setup.sh` (bash, set -euo pipefail):
  1. cd to script dir; `python3 -m venv --system-site-packages .venv` (system-site so the
     distro PyQt6 is visible)
  2. `.venv/bin/pip install -U pip` then `pip install -e .`
  3. verify: `.venv/bin/python -c "from liquidctl.driver.kraken3 import KrakenZ3"` and grep
     `0x3012` in the installed kraken3.py — if missing, `pip install -U git+https://github.com/liquidctl/liquidctl`
  4. verify PyQt6 importable from venv, else `pip install PyQt6`
  5. install `openkraken.desktop` to `~/.local/share/applications/` with Exec pointing at
     `<projdir>/.venv/bin/openkraken`, Icon=`<projdir>/openkraken/resources/openkraken.svg`
  6. print success + how to run. Idempotent.
- `openkraken.desktop`: Name=OpenKraken; Categories=System;Monitor; Terminal=false;
  StartupWMClass=openkraken. setup.sh rewrites Exec/Icon paths via sed into the installed copy.
- `openkraken/resources/openkraken.svg`: simple purple droplet on dark rounded square (hand-write the SVG).
- `README.md`: features, screenshot placeholder, install (`./setup.sh`), permissions note
  (plugdev/udev — this machine already OK), supported devices note, FAQ (RGB not supported
  upstream; LCD sensor mode refresh rate vs USB bandwidth), uninstall.

## Threading rules (all implementers)

- GUI thread: widgets only. Engine thread: all liquidctl + sysfs I/O.
- Signals carry plain Python objects (dataclasses); `pyqtSignal(object)` is fine.
- Engine request methods only enqueue; the loop dequeues (queue.Queue, non-blocking get).
- `KrakenDevice` methods may also be called during engine startup/shutdown — RLock makes them safe.
- No `time.sleep` in GUI thread. Engine sleeps in small slices (≤0.2 s) so `stop()` is responsive.
