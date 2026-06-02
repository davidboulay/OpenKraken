"""System sensor sampling for OpenKraken.

Reads CPU/GPU/RAM telemetry from the Linux ``sysfs``/``procfs`` interfaces with
no external dependencies (no ``psutil``).  Everything is best-effort: every file
read is individually guarded so :meth:`SystemSensors.read` never raises -- a
missing or unreadable sensor simply yields ``None`` for that field.

Discovery happens once in :meth:`SystemSensors.rescan` (called from
``__init__``) by walking ``/sys/class/hwmon/hwmon*`` and matching the ``name``
file:

* CPU temperature: ``k10temp`` (AMD), preferring the ``Tctl`` label, else the
  first ``tempN_input``.  ``coretemp`` (Intel, label ``Package id 0``) and
  ``zenpower`` are also accepted for portability.
* GPU: ``amdgpu`` -- temperature label ``edge`` preferred, then ``junction``.
  ``gpu_busy_percent``, ``mem_info_vram_used`` and ``mem_info_vram_total`` live
  in the hwmon's ``device/`` subdirectory; power comes from ``power1_average``
  (falling back to ``power1_input``), in microwatts.

CPU load is computed from the aggregate ``cpu`` line of ``/proc/stat`` as the
busy fraction between successive :meth:`read` calls -- the first call therefore
returns ``cpu_load=None`` because no delta is available yet.  CPU frequency is
the mean of ``scaling_cur_freq`` across all ``cpufreq`` policies (kHz).  RAM is
derived from ``/proc/meminfo`` ``MemTotal``/``MemAvailable``.

Cached values are file *paths*, never open handles, so a device that disappears
and reappears (or a stale path) does not wedge the sampler -- a failed read just
produces ``None`` and a :meth:`rescan` re-discovers everything.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path

__all__ = ["SystemSnapshot", "SystemSensors"]

_LOGGER = logging.getLogger(__name__)

_HWMON_ROOT = Path("/sys/class/hwmon")
_PROC_STAT = Path("/proc/stat")
_PROC_MEMINFO = Path("/proc/meminfo")
_CPUFREQ_ROOT = Path("/sys/devices/system/cpu/cpufreq")

# hwmon "name" values we treat as a CPU temperature source, in preference order.
_CPU_HWMON_NAMES = ("k10temp", "zenpower", "coretemp")
# hwmon "name" value for the AMD GPU.
_GPU_HWMON_NAME = "amdgpu"

# Preferred temperature labels per source (first match wins; otherwise the
# first available ``tempN_input`` is used).
_CPU_TEMP_LABELS = ("Tctl", "Tdie", "Package id 0")
_GPU_TEMP_LABELS = ("edge", "junction")

_MB = 1024 * 1024
_KB_PER_GB = 1024 * 1024


@dataclass
class SystemSnapshot:
    """A point-in-time reading of system telemetry.

    Every field is optional: a value of ``None`` means that sensor was missing
    or could not be read on this sample.  Temperatures are in degrees Celsius,
    loads and ``cpu``/``gpu`` utilisation are percentages (0-100), frequency is
    in MHz, VRAM/RAM in their respective units, and power in watts.
    """

    cpu_temp: float | None
    cpu_load: float | None
    cpu_freq_mhz: float | None
    gpu_temp: float | None
    gpu_load: float | None
    gpu_vram_used_mb: float | None
    gpu_vram_total_mb: float | None
    gpu_power_w: float | None
    ram_used_gb: float | None
    ram_total_gb: float | None
    timestamp: float


def _read_text(path: Path) -> str | None:
    """Read and strip a sysfs/procfs file; return ``None`` on any error.

    Empty files (some sysfs attributes return an empty string when the value is
    unavailable, e.g. ``power1_average`` on idle GPUs) are treated as missing.
    """
    try:
        text = path.read_text(encoding="ascii", errors="replace").strip()
    except (OSError, ValueError) as exc:  # FileNotFound, PermissionError, etc.
        _LOGGER.debug("sensor read failed for %s: %s", path, exc)
        return None
    return text or None


def _read_int(path: Path) -> int | None:
    """Read a sysfs file as an integer; return ``None`` on error/empty."""
    text = _read_text(path)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        _LOGGER.debug("non-integer value %r in %s", text, path)
        return None


class SystemSensors:
    """Discovers and samples CPU/GPU/RAM sensors from sysfs/procfs.

    Discovery is performed once at construction (and again on :meth:`rescan`);
    :meth:`read` then returns a fresh :class:`SystemSnapshot` and never raises.
    """

    def __init__(self) -> None:
        # Cached file paths (not handles), populated by rescan().
        self._cpu_temp_path: Path | None = None
        self._gpu_temp_path: Path | None = None
        self._gpu_power_path: Path | None = None
        self._gpu_busy_path: Path | None = None
        self._gpu_vram_used_path: Path | None = None
        self._gpu_vram_total_path: Path | None = None

        # State for the CPU-load delta across read() calls.
        self._prev_cpu_total: int | None = None
        self._prev_cpu_idle: int | None = None

        self.rescan()

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #
    def rescan(self) -> None:
        """Re-walk ``/sys/class/hwmon`` and re-cache all sensor file paths.

        Safe to call at any time; clears any previously discovered paths first
        so a removed device does not leave a stale path cached.
        """
        self._cpu_temp_path = None
        self._gpu_temp_path = None
        self._gpu_power_path = None
        self._gpu_busy_path = None
        self._gpu_vram_used_path = None
        self._gpu_vram_total_path = None

        try:
            hwmon_dirs = sorted(_HWMON_ROOT.glob("hwmon*"))
        except OSError as exc:
            _LOGGER.warning("cannot enumerate %s: %s", _HWMON_ROOT, exc)
            hwmon_dirs = []

        # Collect candidate CPU hwmons keyed by name so we can honour the
        # preference order in _CPU_HWMON_NAMES regardless of hwmon numbering.
        cpu_candidates: dict[str, Path] = {}
        gpu_dir: Path | None = None

        for hwmon in hwmon_dirs:
            name = _read_text(hwmon / "name")
            if name is None:
                continue
            if name in _CPU_HWMON_NAMES and name not in cpu_candidates:
                cpu_candidates[name] = hwmon
            elif name == _GPU_HWMON_NAME and gpu_dir is None:
                gpu_dir = hwmon

        for preferred in _CPU_HWMON_NAMES:
            hwmon = cpu_candidates.get(preferred)
            if hwmon is not None:
                self._cpu_temp_path = self._discover_temp_input(hwmon, _CPU_TEMP_LABELS)
                if self._cpu_temp_path is not None:
                    _LOGGER.info(
                        "CPU temperature from %s (%s) at %s",
                        preferred,
                        hwmon.name,
                        self._cpu_temp_path,
                    )
                    break

        if gpu_dir is not None:
            self._discover_gpu(gpu_dir)

        if self._cpu_temp_path is None:
            _LOGGER.warning("no CPU temperature sensor found")
        if self._gpu_temp_path is None:
            _LOGGER.info("no amdgpu temperature sensor found")

    @staticmethod
    def _discover_temp_input(hwmon: Path, preferred_labels: tuple[str, ...]) -> Path | None:
        """Return the best ``tempN_input`` path inside ``hwmon``.

        Prefers an input whose sibling ``tempN_label`` matches one of
        ``preferred_labels`` (case-insensitive, in order); otherwise falls back
        to the numerically first ``tempN_input``.
        """
        try:
            inputs = sorted(hwmon.glob("temp*_input"))
        except OSError as exc:
            _LOGGER.debug("cannot list temp inputs in %s: %s", hwmon, exc)
            return None
        if not inputs:
            return None

        # Map label text -> input path for labelled inputs.
        labelled: dict[str, Path] = {}
        for inp in inputs:
            label_path = Path(str(inp).replace("_input", "_label"))
            label = _read_text(label_path)
            if label is not None:
                labelled[label.lower()] = inp

        for want in preferred_labels:
            match = labelled.get(want.lower())
            if match is not None:
                return match

        # No preferred label matched: use the first temp input.
        return inputs[0]

    def _discover_gpu(self, hwmon: Path) -> None:
        """Cache amdgpu temperature, power, busy and VRAM paths."""
        self._gpu_temp_path = self._discover_temp_input(hwmon, _GPU_TEMP_LABELS)

        # Power: prefer the averaged reading, fall back to the instantaneous one.
        avg = hwmon / "power1_average"
        inst = hwmon / "power1_input"
        if avg.exists():
            self._gpu_power_path = avg
        elif inst.exists():
            self._gpu_power_path = inst
        else:
            self._gpu_power_path = None

        # gpu_busy_percent / VRAM live under the hwmon's device/ symlink dir.
        # Fall back to the hwmon dir itself in case a future layout exposes them
        # directly.
        device_dir = hwmon / "device"
        search_dirs = [device_dir, hwmon]
        self._gpu_busy_path = self._first_existing(search_dirs, "gpu_busy_percent")
        self._gpu_vram_used_path = self._first_existing(search_dirs, "mem_info_vram_used")
        self._gpu_vram_total_path = self._first_existing(search_dirs, "mem_info_vram_total")

        _LOGGER.info(
            "amdgpu sensors: temp=%s power=%s busy=%s vram=%s",
            self._gpu_temp_path,
            self._gpu_power_path,
            self._gpu_busy_path,
            self._gpu_vram_total_path,
        )

    @staticmethod
    def _first_existing(dirs: list[Path], filename: str) -> Path | None:
        """Return ``dir/filename`` for the first dir where that file exists."""
        for d in dirs:
            candidate = d / filename
            try:
                if candidate.exists():
                    return candidate
            except OSError:
                continue
        return None

    # ------------------------------------------------------------------ #
    # Sampling
    # ------------------------------------------------------------------ #
    def read(self) -> SystemSnapshot:
        """Sample all sensors and return a :class:`SystemSnapshot`.

        Never raises.  The first call returns ``cpu_load=None`` (a CPU-load
        delta needs two ``/proc/stat`` samples).  Any individual sensor that is
        missing or fails to read yields ``None`` for its field.
        """
        cpu_temp = self._read_cpu_temp()
        cpu_load = self._read_cpu_load()
        cpu_freq = self._read_cpu_freq_mhz()
        gpu_temp = self._read_gpu_temp()
        gpu_load = self._read_gpu_load()
        vram_used, vram_total = self._read_gpu_vram_mb()
        gpu_power = self._read_gpu_power_w()
        ram_used, ram_total = self._read_ram_gb()

        return SystemSnapshot(
            cpu_temp=cpu_temp,
            cpu_load=cpu_load,
            cpu_freq_mhz=cpu_freq,
            gpu_temp=gpu_temp,
            gpu_load=gpu_load,
            gpu_vram_used_mb=vram_used,
            gpu_vram_total_mb=vram_total,
            gpu_power_w=gpu_power,
            ram_used_gb=ram_used,
            ram_total_gb=ram_total,
            timestamp=time.monotonic(),
        )

    def _read_cpu_temp(self) -> float | None:
        if self._cpu_temp_path is None:
            return None
        millidegrees = _read_int(self._cpu_temp_path)
        if millidegrees is None:
            return None
        return millidegrees / 1000.0

    def _read_gpu_temp(self) -> float | None:
        if self._gpu_temp_path is None:
            return None
        millidegrees = _read_int(self._gpu_temp_path)
        if millidegrees is None:
            return None
        return millidegrees / 1000.0

    def _read_gpu_load(self) -> float | None:
        if self._gpu_busy_path is None:
            return None
        value = _read_int(self._gpu_busy_path)
        if value is None:
            return None
        return float(min(max(value, 0), 100))

    def _read_gpu_power_w(self) -> float | None:
        if self._gpu_power_path is None:
            return None
        microwatts = _read_int(self._gpu_power_path)
        if microwatts is None:
            return None
        return microwatts / 1_000_000.0

    def _read_gpu_vram_mb(self) -> tuple[float | None, float | None]:
        used: float | None = None
        total: float | None = None
        if self._gpu_vram_used_path is not None:
            raw = _read_int(self._gpu_vram_used_path)
            if raw is not None:
                used = raw / _MB
        if self._gpu_vram_total_path is not None:
            raw = _read_int(self._gpu_vram_total_path)
            if raw is not None:
                total = raw / _MB
        return used, total

    def _read_cpu_load(self) -> float | None:
        """Busy fraction of the aggregate ``cpu`` line since the last call.

        Returns ``None`` on the first call (no previous sample to diff against)
        or on any read/parse failure.
        """
        text = _read_text(_PROC_STAT)
        if text is None:
            return None

        first_line = text.splitlines()[0] if text else ""
        parts = first_line.split()
        # Expected: "cpu" user nice system idle iowait irq softirq steal ...
        if len(parts) < 5 or parts[0] != "cpu":
            _LOGGER.debug("unexpected /proc/stat first line: %r", first_line)
            return None

        try:
            fields = [int(v) for v in parts[1:]]
        except ValueError:
            _LOGGER.debug("non-integer field in /proc/stat: %r", first_line)
            return None

        # The kernel already folds guest (index 8) and guest_nice (index 9) into
        # user (0) and nice (1).  Summing every field would therefore count guest
        # time twice in the denominator and over-report load when VMs run, so sum
        # only the canonical 8 fields (user, nice, system, idle, iowait, irq,
        # softirq, steal) -- matching the standard procps formula.
        fields = fields[:8]

        total = sum(fields)
        # idle = idle (index 3) + iowait (index 4, if present).
        idle = fields[3]
        if len(fields) > 4:
            idle += fields[4]

        prev_total = self._prev_cpu_total
        prev_idle = self._prev_cpu_idle
        self._prev_cpu_total = total
        self._prev_cpu_idle = idle

        if prev_total is None or prev_idle is None:
            return None  # first sample: need a delta

        total_delta = total - prev_total
        idle_delta = idle - prev_idle
        if total_delta <= 0:
            return None  # counter wrap or no elapsed time

        busy_fraction = (total_delta - idle_delta) / total_delta
        load = busy_fraction * 100.0
        return float(min(max(load, 0.0), 100.0))

    @staticmethod
    def _read_cpu_freq_mhz() -> float | None:
        """Mean of ``scaling_cur_freq`` (kHz) across all cpufreq policies, in MHz."""
        try:
            policies = sorted(_CPUFREQ_ROOT.glob("policy*"))
        except OSError as exc:
            _LOGGER.debug("cannot enumerate cpufreq policies: %s", exc)
            return None
        if not policies:
            return None

        freqs_khz: list[int] = []
        for policy in policies:
            khz = _read_int(policy / "scaling_cur_freq")
            if khz is not None:
                freqs_khz.append(khz)

        if not freqs_khz:
            return None
        mean_khz = sum(freqs_khz) / len(freqs_khz)
        return mean_khz / 1000.0  # kHz -> MHz

    @staticmethod
    def _read_ram_gb() -> tuple[float | None, float | None]:
        """Return ``(used_gb, total_gb)`` from ``/proc/meminfo``.

        ``used`` is ``MemTotal - MemAvailable`` (the kernel's own estimate of
        memory that cannot be reclaimed for new allocations).
        """
        text = _read_text(_PROC_MEMINFO)
        if text is None:
            return None, None

        mem_total_kb: int | None = None
        mem_available_kb: int | None = None
        for line in text.splitlines():
            if line.startswith("MemTotal:"):
                mem_total_kb = SystemSensors._parse_meminfo_kb(line)
            elif line.startswith("MemAvailable:"):
                mem_available_kb = SystemSensors._parse_meminfo_kb(line)
            if mem_total_kb is not None and mem_available_kb is not None:
                break

        if mem_total_kb is None:
            return None, None

        total_gb = mem_total_kb / _KB_PER_GB
        if mem_available_kb is None:
            return None, total_gb
        used_kb = max(mem_total_kb - mem_available_kb, 0)
        used_gb = used_kb / _KB_PER_GB
        return used_gb, total_gb

    @staticmethod
    def _parse_meminfo_kb(line: str) -> int | None:
        """Parse ``"Label:   12345 kB"`` -> ``12345`` (kB int), or ``None``."""
        parts = line.split()
        if len(parts) < 2:
            return None
        try:
            return int(parts[1])
        except ValueError:
            return None
