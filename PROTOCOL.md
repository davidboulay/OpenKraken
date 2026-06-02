# Kraken CAM — Native LED Control Protocol: NZXT Kraken 2024 Elite RGB

Authoritative wire protocol for adding native LED control of the **NZXT Kraken 2024 Elite RGB**
to the Kraken CAM app. Every claim is tagged with its source:

- **[PR882]** = liquidctl PR #882 (`feat/kraken-2024-elite-rgb` @ 708a375), tested on real hardware
  (same device + firmware as ours). Open/unmerged.
- **[OpenRGB]** = OpenRGB master `Controllers/NZXTHue2Controller/*`, plus device issues #4828 / #4985.
- **[smart_device.py]** = local liquidctl `SmartDevice2` reference (HUE2 set_color), v1.16.0.
- **[kraken3.py]** = local liquidctl `KrakenX3`/`KrakenZ3` reference, v1.16.0.
- **[INFERENCE]** = derived/reasoned, NOT directly observed on this device. Marked clearly.

**Conservatism rule applied:** anything not confirmed for THIS device (1e71:3012, fw 1.2.0) is in the
"Untested / unconfirmed" section, not the main spec.

---

## 1. Device summary

| Property | Value | Source |
| --- | --- | --- |
| Vendor / Product ID | `1e71:3012` | [PR882][OpenRGB] |
| Firmware tested | `1.2.0` | [PR882] (tester) |
| HID report size | 64-byte IN and OUT reports, right-zero-padded | [PR882][OpenRGB][kraken3.py L42-43] |
| Color byte order | **GRB** (not RGB) everywhere on the wire | [PR882][OpenRGB][smart_device.py L161] |
| LCD | 640x640 (driven separately; not in scope of LED control) | [kraken3.py L607] |
| Lighting protocol family | NZXT **HUE2** "direct" path (`0x22` opcode family) | [PR882][OpenRGB] |
| Brightness command | **None.** Scale RGB host-side before sending. | [OpenRGB] (#4985 slider greyed out) |

**State of liquidctl on this machine (important):** the *installed* liquidctl is **1.16.0**, which maps
`0x3012` in `KrakenZ3` with `color_channels = _COLOR_CHANNELS_KRAKEN2023 = {}` (empty) — so the installed
liquidctl provides **no RGB API** for this device; `set_color` raises `NotSupportedByDevice`.
[kraken3.py L597-608, L85, L331-332]. PR #882 (which would add RGB) is **not merged** into the installed
version. Kraken CAM must therefore implement the wire protocol itself (or vendor the PR #882 logic) — we
cannot rely on `liquidctl set-color` for this PID today. [OpenRGB][kraken3.py][PR882]

**Why we follow the OpenRGB Direct path as our primary spec:** PR #882's hardware tester and the OpenRGB
issue reporters independently converge: the device only honors the HUE2 **Direct** path
(`0x22 0x10` / `0x22 0x11` / `0x22 0xA0`). PR #882 reaches that path by rewriting `mode="fixed"` to
`"super-fixed"` (see §4). OpenRGB drives it directly. Both emit the same `0x22`-family bytes. Hardware
animation modes (`0x28 0x03`) are rejected by this device's firmware (see §7). [PR882][OpenRGB #4828/#4985]

---

## 2. Channel map

The firmware exposes **two** HUE2 RGB (lighting) channels and two fan channels. OpenRGB calls
`spawn_hue(info, name, 2, 2)` → 2 RGB channels, 2 fan channels. [OpenRGB ControllerDetect, confirmed]

Channels are addressed by a **bitmask** in HID byte index `0x02`: `mask = (1 << channel_index)`.
[OpenRGB] [smart_device.py L503 `1 << i`]

| App channel | channel_index | wire mask (byte `0x02`) | Contents | LED count | Source |
| --- | --- | --- | --- | --- | --- |
| `ring` (pump ring) | 0 | `0x01` | Kraken 2024 Elite pump ring | **24** | [PR882][OpenRGB] |
| `fans` (RGB Core connector) | 1 | `0x02` | bundled RGB Core fan daisy-chain | 8 × N fans (see below) | [OpenRGB][INFERENCE] |

- **Ring = 24 LEDs.** `_KRAKEN2024_ELITE_RING_LEDS = 24` [PR882]; OpenRGB accessory ID `0x1E` "Kraken Elite
  Ring" → 24 LEDs [OpenRGB]. This is the only LED count PR #882 defines.
- **Fan channel LED count is computed, not constant.** Each bundled **F-series RGB Core** fan = **8 LEDs**
  (accessory IDs `0x17` F140 RGB Core = 8, `0x18` F120 RGB Core = 8). A 240 kit = 2 fans = 16 LEDs; a 360
  kit (3 fans) = 24 LEDs. Compute the real total from the `0x20 0x03` reply at runtime (see §3 + §6).
  [OpenRGB led-count table] [INFERENCE for "which kit is attached"]

**liquidctl id note (do not confuse):** in PR #882, the *ring* channel's liquidctl dict id is `0b001`
(`_COLOR_CHANNELS_KRAKEN2024_ELITE = {"ring": 0b001}`) [PR882]. That `0b001` is the same numeric value as the
wire mask `0x01`, but PR #882 only ever models channel 0 (one key). OpenRGB additionally addresses
channel 1 via mask `0x02`. **There is no `logo` and no `sync` channel on this device.** A `set_color("logo", …)`
would raise `KeyError`; `sync` does not exist. [PR882]

**Accessory IDs differ between code bases (cross-check resolved):**

- Installed liquidctl `util.py` does **not** contain `0x1E` (Kraken 2024 Elite Ring) or `0x18` — its enum
  stops at `F360_RGB_CORE = 0x1d`; `0x17 = F140 RGB Core`. [util.py L43-59]
- PR #882 *adds* `KRAKEN_2024_ELITE_RING = (0x1e, 'Kraken 2024 Elite Pump Ring')`. [PR882, confirmed in diff]
- OpenRGB: `0x17` and `0x18` are both 8-LED RGB Core fans; `0x1E` = Kraken Elite Ring = 24. [OpenRGB]
- **Resolution:** for LED-count purposes use the OpenRGB table (the authoritative count source). Do not
  rely on the installed liquidctl enum to *name* `0x1E`/`0x18` — it will fall back to `UNKNOWN_30` /
  `UNKNOWN_24`. This is cosmetic only; it does not affect the wire bytes. [util.py L67-73][OpenRGB]

`HUE2_MAX_ACCESSORIES_IN_CHANNEL = 6`, max 40 LEDs per channel. [util.py L21] (resolves a PR882 open question.)

---

## 3. Initialization / handshake

Run once at app start / resume / reconnect, before any lighting write. All writes are 64 bytes
(right-zero-padded). [kraken3.py][OpenRGB]

1. **Firmware request** — `[0x10, 0x01]`. Read 64-byte reports until prefix `0x11 0x01`; firmware =
   `reply[0x11].reply[0x12].reply[0x13]` (expect `1.2.0`). [PR882][OpenRGB][smart_device.py L539,L543-545]
2. **Lighting / accessory info** — `[0x20, 0x03]`. Read until prefix `0x21 0x03`. This is **required** to
   learn per-channel LED counts. Parse layout in §6. [PR882][OpenRGB][smart_device.py L540,L547-557]

Optional (status-interval) reports, inherited & unchanged by PR #882. Whether they are *required* before
lighting writes are accepted on this unit is **unconfirmed** (see §8):

3. `[0x70, 0x02, 0x01, 0xB8, 0x01]` — status report interval. The `0x01` =
   `1 + round((0.5-0.5)/0.25) = 1`. [kraken3.py L709-710]
4. `[0x70, 0x01]` — start/apply. [kraken3.py L711]

OpenRGB disables the fan-status read for this device (`if(false)`); fan/pump/LCD telemetry is better driven
through liquidctl `KrakenZ3` (which *does* work for this PID — only its RGB is empty). [OpenRGB][kraken3.py]

**No separate "unlock"/software-control command is known.** OpenRGB and liquidctl send nothing beyond the
above. The #4985 "cannot switch into software control mode" symptom *may* hint at a missing handshake, but
none has been reverse-engineered — Direct still works without it. [OpenRGB #4985][INFERENCE]

---

## 4. Direct / fixed color command (PRIMARY — use this for solid colors)

This is the **only** reliable way to set color on this device. A solid "fixed" color is implemented as a
full per-LED Direct frame: write the same GRB triplet into all N LEDs. PR #882 achieves "fixed" by
rewriting it to `super-fixed` and expanding the single color to `colors * 24`. [PR882][OpenRGB]

Send the following three reports **in order**, every time you change the color:

### Packet 1 — per-LED buffer, LEDs 0–19

```
byte[0x00] = 0x22
byte[0x01] = 0x10            ; 0x10 | group, group = 0
byte[0x02] = mask            ; ring = 0x01, fans = 0x02
byte[0x03] = 0x00
byte[0x04..0x3F] = up to 20 LEDs × (G,R,B) = up to 60 color bytes, then zero-pad to 64
```
[OpenRGB SendDirect][smart_device.py L638]

### Packet 2 — per-LED buffer continuation, LEDs 20–39 (REQUIRED for ring, 24 > 20)

```
byte[0x00] = 0x22
byte[0x01] = 0x11            ; 0x10 | group, group = 1
byte[0x02] = mask
byte[0x03] = 0x00
byte[0x04..] = LEDs 20..39 × (G,R,B) = the bytes after the first 60 (i.e. color_data[60:]),
               then zero-pad to 64
```
**This packet MUST carry the remaining color bytes** (`leds[60:]`). Sending it *empty* is the root cause
of the last-LED bug — see §9, bug (a). [OpenRGB SendDirect(channel,1,20,&color_data[60])]
[smart_device.py L639] — and contrast with [kraken3.py L487] which sends it empty.

For ≤20 LEDs (e.g. a 16-LED 240 fan chain) packet 2 is **not sent**. The 24-LED ring **always** needs it.
[OpenRGB SetChannelLEDs]

### Packet 3 — apply / commit

Send after the buffer packet(s). Use the **OpenRGB known-good** Direct-apply byte set:

```
byte[0x00] = 0x22
byte[0x01] = 0xA0
byte[0x02] = mask
byte[0x04] = 0x01
byte[0x07] = 0x28
byte[0x0A] = 0x80
byte[0x0C] = 0x32
byte[0x0F] = 0x01
; all other bytes 0, padded to 64
```
i.e. `[0x22, 0xA0, mask, 0x00, 0x01, 0x00, 0x00, 0x28, 0x00, 0x00, 0x80, 0x00, 0x32, 0x00, 0x00, 0x01]` + pad.
[OpenRGB SendApply, verbatim]

**Apply-packet payload discrepancy (cross-check):** liquidctl's super-fixed apply differs in the middle bytes:
`[0x22, 0xA0, cid, 0x00, mval, speedL, speedH, 0x08, 0x00, 0x00, 0x80, 0x00, 0x32, 0x00, 0x00, 0x01]` where for
super-fixed `mval=0x01` and speed = `[0x00,0x00]` → `[0x22,0xA0,cid,0x00,0x01,0x00,0x00,0x08,0x00,0x00,0x80,0x00,0x32,0x00,0x00,0x01]`.
[kraken3.py L488-492]. The difference is byte `0x07` (`0x28` in OpenRGB vs `0x08` in liquidctl) and byte
`0x04` (`0x01` both). Both code-base comments admit these middle bytes are not fully reverse-engineered.
**Use OpenRGB's set** (byte 0x07 = 0x28) as primary — it is the byte stream OpenRGB ships and #4985 confirms
Direct works; flag the `0x07` value for hardware verification. [OpenRGB][kraken3.py][INFERENCE on which to prefer]

**Color order:** within every LED triplet the bytes are **G, R, B**. Convert user `[r,g,b]` → `[g,r,b]`
before writing. [PR882][OpenRGB][smart_device.py L161]

**Brightness:** scale R, G, B host-side before building the triplets. No device brightness command exists.
[OpenRGB #4985]

**Off:** send a Direct frame with all triplets = `(0,0,0)`. (Do not use a hardware "off" mode — see §7.)
[INFERENCE from §7]

---

## 5. Host-side animation (how to animate on this device)

Because hardware effect modes are rejected (§7), **animate by streaming Direct frames** (§4) from the host,
one full frame per tick. [OpenRGB #4828/#4985 — "Direct works"; effects need host streaming]

- **Frame rate is limited.** OpenRGB #4828 reports the device only reliably accepts updates at ~**1 FPS**;
  higher rates fail to animate, and *even at 1 FPS some frames are occasionally skipped*. Treat ~1 update/sec
  as the safe default and make the rate configurable; do not assume smooth high-FPS streaming works.
  [OpenRGB #4828]
- Each frame = packet 1 (+ packet 2 for >20 LEDs) + packet 3 apply (§4). [OpenRGB][INFERENCE]
- Implement breathing/fading/rainbow/etc. as host-computed color sequences, not device modes. [INFERENCE]

---

## 6. `0x20 0x03` reply parsing (LED-count discovery)

Reply prefix `0x21 0x03`. [PR882][OpenRGB][smart_device.py]

```
reply[14]                              = channel_count   (THIS DEVICE REPORTS 2 — see §9 bug (b))
accessories start at offset 15, stride HUE2_MAX_ACCESSORIES_IN_CHANNEL = 6 slots/channel:
    accessory_id = reply[15 + channel_index*6 + slot]   (slot 0..5; 0 = empty)
```
[smart_device.py L549-557][util.py L21][OpenRGB UpdateDeviceList: start = 0x0F + 6*chan]

Map each non-zero accessory_id to its LED count via the OpenRGB table and **sum per channel** to get that
channel's total LED count (then apply the §4 packet-split rule). Relevant IDs:

| accessory_id | name | LEDs | source |
| --- | --- | --- | --- |
| `0x1E` | Kraken 2024 Elite Pump Ring | 24 | [OpenRGB][PR882] |
| `0x17` | F140 RGB Core | 8 | [OpenRGB][util.py] |
| `0x18` | F120 RGB Core | 8 | [OpenRGB] |
| `0x1B` | F240 RGB Core | (24 — verify) | [OpenRGB/util.py — see note] |
| `0x1D` | F360 RGB Core | 24 | [OpenRGB][util.py] |

> Note on `0x1B`/`0x1D`: these are *radiator-kit* aggregate IDs. The per-fan Core count is 8 (`0x17`/`0x18`).
> Whether a given chain enumerates as N×(8-LED fan) or as one aggregate ID is **device-dependent — read it
> at runtime** rather than hard-coding. [INFERENCE]

**Expected on our unit (UNCONFIRMED until read):** channel 0 → `0x1E` (24); channel 1 → the fan chain.
No raw `0x21 0x03` dump from a real 3012 exists in either PR thread. [PR882 open Q][OpenRGB open Q]

---

## 7. DO-NOT-USE list (commands known to FAIL on this device)

| Command | Why it fails | Evidence |
| --- | --- | --- |
| **Hardware effect / animation packet `0x28 0x03 …`** (FIXED/FADING/SPECTRUM/MARQUEE/PULSE/BREATHING/CANDLE/STARRY/RAINBOW etc. as *device* modes) | Firmware rejects it; built-in Modes "fail to operate." | [OpenRGB #4828, confirmed] [smart_device.py L663 uses this header] |
| **Effect-plugin animation at >1 FPS** | Does not animate above ~1 FPS; frames skipped even at 1 FPS. | [OpenRGB #4828] |
| **Any "Saving"/hardware-store / software-control-mode switch** | Device shows "Saving Not Supported"; "cannot switch into software control mode." | [OpenRGB #4985] |
| **Brightness command / Speed slider** | No such command; sliders are greyed out. Scale RGB host-side instead. | [OpenRGB #4985] |
| **liquidctl `set-color` on installed v1.16.0** | `color_channels={}` for 0x3012 → `NotSupportedByDevice`. | [kraken3.py L597-608, L331-332] |
| **kraken3.py `0x2A 0x04 …` animation path** (the `KrakenX3` non-super-fixed branch) | This is the same class of hardware effect the device rejects; it is also unused by PR #882. | [kraken3.py L519-550][OpenRGB #4828][INFERENCE] |
| **liquidctl `KrakenX3` super-fixed as-is (empty `0x22 0x11`)** | Drops trailing LEDs on this device — see §9 bug (a). Split the buffer instead. | [kraken3.py L486-487][OpenRGB][PR882] |
| **`Nzxt2023RgbController` framing** (`0x2a 0x04` + 16-color footer, `0x22` individual) | Wrong driver/PID (`0x2012`/`0x2021`); not used by this device. | [smart_device.py L705-865][PR882] |

PR #882's own description table claims "Ring: fixed Working," which **contradicts** the external tester's
last-LED report; treat the tester report as authoritative for the bug, the author's "fixed reaches the
device" as authoritative for the opcode. [PR882]

---

## 8. Known device quirks (with root cause + workaround)

### Quirk A — fixed-mode last-LED bug (last LED clockwise stays off)

- **Symptom:** in fixed/solid color, the last LED on the ring (tester: "the last LED clockwise") stays off.
  [PR882 tester]
- **Root cause:** PR #882 redirects `fixed` → `super-fixed`, which runs the `KrakenX3._write_colors`
  super-fixed branch. That branch appends the **entire** 120-byte color buffer to a single `0x22 0x10`
  report and sends the `0x22 0x11` continuation **empty**. Since `_write()` pads-but-does-not-extend and
  hidapi transmits only one 64-byte report, only the first 60 color bytes (LEDs 0–19) reach the device;
  LEDs 20–23 are never delivered. [kraken3.py L483-487 — `_write([0x22,0x10,cid,0x00] + color)` then
  `_write([0x22,0x11,cid,0x00])` empty]. The correct framing (`leds[0:60]` in packet 1, `leds[60:]` in
  packet 2) is what `SmartDevice2` [smart_device.py L638-639] and OpenRGB `SendDirect` do.
- **Workaround (implement this):** **split the per-LED payload across the two reports** exactly as §4:
  packet 1 carries LEDs 0–19 (`leds[0:60]`), packet 2 (`0x22 0x11`) carries LEDs 20–39 (`leds[60:]`), then
  the apply packet. This delivers all 24 LEDs. [OpenRGB][smart_device.py][PR882]
- **Residual uncertainty:** the packet math predicts LEDs 20–23 (4 LEDs) go dark, but the tester saw only
  *one* dark LED — the firmware's exact behavior with a short/over-length `0x22` frame is undocumented.
  The fix (proper split) makes the count moot, but verify on hardware after implementing. [PR882 open Q]

### Quirk B — initialize reports 2 color channels (`AssertionError: Unexpected number of color channels received: 2`)

- **Symptom:** `initialize()` asserts/crashes parsing the `0x20 0x03` reply.
- **Root cause:** `parse_led_info` asserts `channel_count (== reply[14]) == channels_without_sync`. The real
  firmware reports `channel_count = 2` (pump ring **and** the RGB Core fan connector), but PR #882's
  `_COLOR_CHANNELS_KRAKEN2024_ELITE = {"ring": 0b001}` yields `channels_without_sync = 1`, so `2 != 1` trips
  the assert. PR #882 only relaxed the assert for the `channels_without_sync == 0` early-return case (non-RGB
  2023 models); it did **not** handle the device reporting *more* channels than modeled, so **this bug is
  unresolved in PR #882 as of head 708a375.** [PR882 diff — `assert channel_count == channels_without_sync`;
  the equality is exact] [kraken3.py L258-262 shows the original strict equality]
- **Workaround (implement this):** model **both** RGB channels (ring on mask `0x01`, fan connector on mask
  `0x02`) so `channels_without_sync == 2 == channel_count` — this matches OpenRGB's `spawn_hue(…, 2, 2)`.
  Alternatively, relax our parser to `channel_count >= channels_without_sync` (do not hard-assert equality).
  Prefer modeling 2 channels — it both fixes the assert and unlocks fan-connector control. [OpenRGB][PR882][INFERENCE]

---

## 9. Reference: HUE2 mode/speed tables (FYI only — NOT used on this device)

These tables exist in `kraken3.py` and are inherited by the `KrakenX3` animation path. They are documented
here for completeness only; **per §7 the hardware effect path that consumes them is rejected by this device**,
so Kraken CAM should not emit them. [kraken3.py L97-169][OpenRGB #4828]

- Effect opcode header (`0x28 0x03` in HUE2 / `0x2A 0x04` in kraken3) — see §7, do not use.
- `_COLOR_MODES` mode ids: fixed/off=0x00, fading=0x01, super-fixed=(0x01,variant 0x01), spectrum-wave=0x02,
  marquee=0x03, covering-marquee=0x04, alternating=0x05, pulse=0x06, breathing=0x07, candle=0x08,
  starry-night=0x09, rainbow-flow=0x0B, super-rainbow=0x0C, rainbow-pulse=0x0D, tai-chi=0x0E, water-cooler=0x0F,
  loading=0x10. [kraken3.py L97-142]
- `_SPEED_VALUE[scale][speed]` 2-byte timing pairs; super-fixed (scale 9) = `[0x00,0x00]`. [kraken3.py L156-169]
- `_ANIMATION_SPEEDS`: slowest=0, slower=1, normal=2, faster=3, fastest=4. [kraken3.py L171-177]

The **only** mode value we actually rely on is the super-fixed apply byte `mval = 0x01` in the §4 apply
packet (and even that is superseded by OpenRGB's explicit apply bytes). [kraken3.py L101][OpenRGB]

---

## 10. Untested / unconfirmed — **RESOLVED on real hardware, see §11**

> Status update 2026-06-02: items 1, 3, 5, 6 and 7 below were verified on the
> target unit with `scripts/rgb_probe.py`; §11 records the results. Items 2, 4
> and 8 remain open but are moot for our implementation (we always send the
> apply packet and inherit the 0x70 init from KrakenZ3.initialize()).

1. **Apply-packet exact middle bytes** — OpenRGB byte `0x07 = 0x28` vs liquidctl `0x07 = 0x08`. Both
   code bases say these are not fully reverse-engineered. Verify which the 3012 honors. [OpenRGB][kraken3.py]
2. **Whether the apply packet (`0x22 0xA0`) is required at all**, or whether the device latches on the
   `0x22 0x10`/`0x11` writes. #4985 confirms Direct works but shows no packet trace. [OpenRGB open Q]
3. **Real `0x20 0x03` reply contents** for our unit: which accessory id sits on channel 0 vs channel 1, and
   the fan chain's actual LED count. No raw dump from a real 3012 exists. [PR882][OpenRGB open Q]
4. **Whether the `0x70 0x02` / `0x70 0x01` init reports are required** before lighting writes are accepted
   on this PID. Inherited code sends them; not independently verified for ring writes. [kraken3.py][PR882 open Q]
5. **Fan-connector control via the same `0x22` protocol on mask `0x02`** — strongly implied by
   `spawn_hue(…,2,…)` but not independently verified on a 3012. [OpenRGB][INFERENCE]
6. **Reliable streaming FPS on fw 1.2.0** — #4828 says ~1 FPS with occasional skips; the actual usable ceiling
   needs empirical measurement on our unit. [OpenRGB #4828]
7. **Exact dark-LED count for quirk A** before the split-buffer fix (1 vs 4). Moot after the fix but worth a
   sanity check. [PR882 open Q]
8. **Whether any undocumented unlock/software-control handshake exists** (hinted by #4985). None found in
   either code base. [OpenRGB #4985]

---

## 11. Hardware verification results (2026-06-02, our unit, fw 1.2.0)

Probe: `scripts/rgb_probe.py`, operator-confirmed visually. Final LED state: off.

**Raw `0x20 0x03` reply (first known capture from a real 3012):**

```
21 03 6d 63 94 82 0e 58 19 02 00 00 00 00 02 1e 00 00 00 00 00 1b 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
00 00 00 00 00 00 00 00 00 00 00 00
```
(bytes 2–9 are the unit serial; redact if sharing publicly.)

| §10 item | Verdict |
| --- | --- |
| 1. Apply byte `0x07` | **`0x28` (OpenRGB variant) works** — full ring AND fans lit first try; liquidctl `0x08` variant never needed. Default = variant 0, confirmed. |
| 3. Real `0x20 0x03` contents | `channel_count=2`; channel 0 = `0x1E` (Elite Ring, 24 LEDs); channel 1 = **`0x1B` aggregate** = F360 RGB Core kit, **24 LEDs total** — chains enumerate as ONE kit id, not N×8-LED fans. (Resolves the §6 note; `0x1B = 24` is now hardware-confirmed.) |
| 5. Fan connector via mask `0x02` | **WORKS** — all 24 fan LEDs solid purple with the same §4 framing. |
| 6. Streaming rate | Host-side write cost ≈ 0.4 ms/frame (no failures at full speed). Visual device limit per §5 still governs; keep ≥ 1 s between frames. |
| 7. Quirk A split-buffer fix | **Confirmed** — all 24 ring LEDs lit, no dark last LED. |
| Per-LED addressing | **Confirmed** — spectrum burst showed distinct hues around the ring. |

Still open (moot for us): §10.2 (apply packet necessity — we always send it),
§10.4 (0x70 init — inherited from initialize()), §10.8 (unlock handshake — not needed,
Direct works without one).
