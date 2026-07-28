# J2208-family band — BLE protocol

Recovered from the companion app, then verified live against a physical band of
this family — a **Hume Band V2**, advertised name
`Hume Band V2 <last 4 hex of MAC>`, firmware v0.0.3.9.

Source: `com.elink.fittrackhealth.pro` ("FitTrack Health") v9.22.3
(versionCode 5824), Play Store. Relevant code is **unobfuscated Kotlin/Java**
in `com.example.watch_flutter_plugin.humeBandJ2208` — `J2208` being the band's
ODM model code, and the most brand-neutral name this protocol has.

> **Sample values in this document are illustrative.** Frame layouts, opcodes,
> strides and firmware behaviour are real and verified; the physiological
> numbers shown alongside them are representative placeholders, not
> measurements from a real person. Device addresses are placeholders too.

## Summary

| Question | Answer |
| --- | --- |
| Encrypted? | **No.** Plaintext ATT, no LE Secure Connections requirement. |
| Auth handshake? | **None.** Discover services → enable notify → send commands. |
| Pairing/bonding? | **Not required.** The band never appears in Android's bonded list. |
| Raw PPG available? | **No on V2 firmware** — `0x3A` returns a well-formed frame of 75 zeros in every condition. See below. |
| Raw ECG available? | No. Mode opens (`0x99`), but no waveform is ever pushed. |
| Live metrics? | **Yes** — `0x28` streams HR/SpO2/HRV/temp at ~2 Hz. |

## Provenance — this is an ODM design, not a brand's

Worth establishing first, because it explains most of the protocol's oddities
and tells you which other hardware this document applies to.

The app is a Flutter shell whose BLE plugin is left at the default template
package name `com.example.watch_flutter_plugin` — nobody renamed it. It is a
**multi-brand binary**: the string pool carries `Hume`, `FitTrack` and `Dara`
side by side, all served by the same code paths.

Inside, the device support splits cleanly by ODM:

| Product line | Stack in the APK |
| --- | --- |
| Body-composition scales | `com.qingniu.qnble`, Lefu (`LEFU-CF818` OTA blobs), `libyolanda_calc.so` — Guangdong/Shenzhen scale ODMs |
| **The band** | `humeBandJ2208`, with Jieli (`com.jieli.jl_bt_ota`), Realtek/Realsil and Telink OTA stacks |

The band path is essentially free of the scale vendors' code — it is a separate
ODM product, model **J2208**, on a stock Chinese SoC stack using the generic
`fff0/fff6/fff7` profile common to low-cost Shenzhen fitness bands.

The clearest evidence is the app's own device resolver, which classifies
hardware **by advertised name**:

```
startsWith "hume band v2" | "2301b" | "x3b"  ->  V2
otherwise                                    ->  V1
```

`2301B` and `X3B` are bare ODM model codes, not brand names. A vendor's own app
treating them as first-class only makes sense if the same reference design
ships under those names to other buyers. Both generations share this data
protocol; only firmware update differs (V1 custom, V2 Nordic DFU via `FE59`).

**Practical upshot:** nothing here is specific to one brand. Any band in the
J2208 family should answer the same commands. Only a Hume-badged V2 has
actually been tested — the rest is inference from the model list above.

## GATT profile

| Role | UUID |
| --- | --- |
| Service | `0000fff0-0000-1000-8000-00805f9b34fb` |
| Write | `0000fff6-...` (`read,write,write-without-response`) |
| Notify | `0000fff7-...` (`notify`, CCCD `2902`) |

A second vendor service `0000190e-...` exposes `0004`
(read/write/write-without-response) and `0003` (notify). **It is unused.**
Probed on the live band: `0004` reads back a constant single byte `0x37`,
`0003` never notifies, and writes using the `fff0` framing draw no response.
Nothing in the entire decompiled app references the service — it is leftover
from the SoC vendor's reference firmware, not a data path.

The app negotiates MTU 153, which caps notification payloads at ~156 bytes.

## Framing

Commands are **always exactly 16 bytes**:

```
byte[0]      opcode
byte[1..14]  arguments (zero padded)
byte[15]     checksum = sum(byte[0..14]) & 0xFF
```

The "CRC" is a truncated additive checksum, not a real CRC:

```
checksum(frame):
    s = 0
    for each byte b in frame[0 .. len-2]:
        s = (s + b) mod 256
    frame[len-1] = s
```

Notifications reuse the request opcode in `byte[0]`. **`byte[1] == 0xFF` means
the firmware rejected the command.** Multi-sample payloads pack **uint16
little-endian** values starting at **offset 3**.

## Opcodes

From `humeBandJ2208/constant/DeviceConst.java`. Status column reflects what
the V2 unit actually did.

| Opcode | Name | Status |
| --- | --- | --- |
| `0x01` | SetDeviceTime | not tested (writes to the band) |
| `0x04` | GetDeviceInfo | works — units, night mode, base HR at `[9]`, brightness |
| `0x13` | GetDeviceBatteryLevel | works — `byte[1]` = percent |
| `0x3E` | GetDeviceName | works — ASCII from `[1]`, the 4 hex digits ending the MAC |
| `0x4B` | GetStepGoal | works — u16 LE at `[1]`, observed 10000 |
| `0x5A` | GPS history | no response at all |
| `0x5C` | sport sessions | `5c ff` — empty, no sessions recorded |
| `0x22` | GetDeviceMacAddress | works — `byte[1..6]` = MAC |
| `0x27` | GetDeviceVersion | works — see below |
| `0x2A` | SetAutomatic | works — sets the band's own logging interval |
| `0x2B` | GetAutomatic | works — see below |
| `0x28` | StartDeviceMeasurement | works — see below |
| `0x39` | PPG block | **rejected** (`39 ff`) |
| `0x3A` | openPPG / raw PPG block | **works — the raw waveform channel** |
| `0x3B` | GetPpgPPGSensor | **rejected** (`3b ff`) |
| `0x41` | GetDeviceTime | works — BCD, see below |
| `0x71` | GetECGwaveform | **rejected** (`71 ff`) |
| `0x98` | stopEcgPPg | works |
| `0x99` | OpenECGPPG | works — replies `99 0a` |
| `0x9B` | openRRIntervalTime | **rejected** (`9b ff`) |
| `0x9C` | GetEcgPpgStatus | **rejected** (`9c ff`) |
| `0xAA` | ECG data (notify) | not observed |
| `0xAB` | PPG data (notify) | not observed |

## History download

This is the useful part: the band stores several days of data and hands it
over with no authentication.

### Request

```
[0]=opcode  [1]=mode  [2..3]=0  [4..9]=BCD yy mm dd HH MM SS
```

`mode`: `0` = start, `2` = continue, `0x99` = delete. **An all-zero date means
"everything you have"**; supplying a date restricts the range. Daily totals
(`0x51`) take a date only, no time.

### Response and paging

The band streams notifications, then waits to be prompted again. The app
re-sends `mode=2` every 50 packets (`HumeBandAdapter.dataCount`), but bursts
are often shorter than that, so prompting on idle is more reliable.

**End of data is the byte pair `<opcode> FF`.** Usually a standalone 2-byte
packet; `0x51` appends it to the final data packet instead. Matching only
"last byte is 0xFF" gives false positives and truncates the download.

### Record layouts

Every record carries a BCD timestamp at `+3..+8` (`yy mm dd HH MM SS`),
except daily totals which are date-only at `+2..+4`. Verified against a live
V2 band:

| Type | Opcode | Stride | Payload from +9 | Result |
| --- | --- | --- | --- | --- |
| totals | `0x51` | 26 or 27 | see below | 6 days |
| steps | `0x52` | 25 | u16 steps, u16 kcal/100, u16 km/100, 10 sub-counts | 226 recs |
| sleep | `0x53` | 130-byte block | `[9]`=count, then 1 stage byte/min | 17 blocks |
| HR | `0x55` | 10 | u8 bpm | 480 recs, 1/10 min |
| HRV | `0x56` | 15 | hrv, vascular age, hr, stress, sys, dia | 96 recs |
| SpO2 | `0x66` | 10 | u8 percent | 192 recs |
| temperature | `0x65` | 11 | u16 LE x 0.1 degC | 1148 recs, 1/5 min |

Daily totals (`getStepCount` picks stride 26 or 27 from packet length):
date at `+2..+4`, then u32 LE steps `+5`, exercise `+9`, distance/100 `+13`,
calories/100 `+17`, goal `+21` (u8 at stride 26, u16 at 27), active minutes at
`stride-4`.

**Empty on V2 firmware:** `0x54` dynamic HR, `0x60` SpO2, `0x62` temperature.
Their populated equivalents are `0x55`, `0x66` and `0x65` respectively — the
SDK exposes both variants and only one is used.

### Validation

Detailed step records (`0x52`) summed per day match the daily totals (`0x51`)
exactly across all five stored days — independent confirmation that both
layouts are decoded correctly. HR, SpO2 and temperature all fall in
physiological ranges with no implausible values.

### Sleep stage codes

| Code | Stage |
| --- | --- |
| `1` | deep |
| `2` | light |
| `3` | REM |
| `5` | awake |

The mapping is **not** recoverable from the app: the Kotlin plugin passes the
raw byte array straight through, and the Dart `_mapSleepStage` is compiled
into `libapp.so` with a hash-ordered string pool, so the enum table cannot be
read off.

Derived instead from the shape of the recorded data across several nights:

- `2` is always the stage at sleep onset — that is light sleep by definition.
- `1` first appears in the first hour or so after onset, which is where deep
  sleep normally falls.
- `3` never appears until well over an hour in. REM latency in healthy adults
  is 70–110 min, and nothing else explains a stage systematically absent from
  the start of every night.
- `5` occurs in short scattered bouts of a few minutes — wake epochs.
- The resulting proportions land inside the normal adult ranges (light 50–60%,
  deep 13–23%, REM 20–25%), which is a further consistency check.

Confident, but derived rather than read from the source — cross-check against
the app's own sleep screen if it matters.

### `0x28` — measurement

Request, from `BleSDK.StartDeviceMeasurementWithHrv(start, openPPG, openPPI)`:

```
[0]=0x28  [1]=1  [2]=start  [3]=openPPG  [4]=openPPI
```

Response (repeats ~2 Hz while running):

```
[1] type   1=HRV 2=heart 3=SpO2 4=temperature
[2] heart rate (bpm)
[3] SpO2 (%)
[4] HRV
[5] stress
[6] systolic     [7] diastolic
[8..9] temperature x 0.1 degC, uint16 LE
```

Off-wrist the frame is well-formed with a zero heart rate — HR reads 0 with no
skin contact — e.g. `2801000000000000650100000000008f` → HR 0, temp 35.7 °C.

**Type 2 (heart) is the live-streaming path.** Worn, it pushes a fresh reading
roughly twice a second (illustrative: 78, 77, 76, 74, 74, 73 bpm). Types 3 and
4 warm up over ~30 s and then report a full panel — HR, SpO2, HRV, stress and
temperature together, all populated. Type 1 (HRV) returns the last cached
reading immediately, then zeros while it re-measures.

This is the closest thing to raw access the firmware offers: derived values,
but live and at 2 Hz rather than the 10-minute history interval.

### `0x3A` — raw PPG: present in the SDK, empty in the firmware

**Request/response, not a push stream.** Each `3a 01 …` request returns one
notification of exactly **153 bytes**: 3 header bytes then 150 bytes of
payload = **75 uint16 LE samples**.

**Every sample is zero on V2 firmware.** Tested off-wrist; on-wrist with a
confirmed pulse (the `0x28` live channel streaming plausible heart rates at
the same moment); before and after opening ECG/PPG mode via `0x99`; and with
`openPPG`/`openPPI` flags set on `0x28`. The frame is always well-formed and
always empty.

This is the same pattern as `0x54`/`0x60`/`0x62`: the shared ODM SDK exposes
the capability, this firmware build does not implement it. An earlier reading
of the decompiled constants suggested raw PPG was available — the constants
are there, the data is not.

The app's parser (`ResolveUtil.getDoublePPGData`) does nothing more than
unpack the payload as consecutive unsigned 16-bit little-endian integers:

```
for i in 0 .. (len(payload) / 2) - 1:
    sample[i] = payload[2i] + payload[2i + 1] * 256
```

Sample rate is **not yet established** — it needs a capture with the band worn
so real values can be counted against wall-clock time.

### `0x27` — firmware version

`ResolveUtil.getDeviceVersion` joins bytes `[1..4]` with dots (the loop starts
at `i = 1`, so the opcode is excluded).

```
27 00 00 03 09 26 07 07 00 00 19 06 06 00 09 95
   \_______/  \______/
    version    build date (BCD)
```

Observed: **v0.0.3.9**, build `26 07 07` = 2026-07-07. The app only reads the
first four bytes; the build date is not surfaced in the UI.

That date also explains the stray all-zero `2026-07-07` row in the daily
totals — it is the firmware epoch, not a day you wore the band.

### `0x2A` / `0x2B` — automatic background monitoring

This is the setting that controls how often the band records to its own
history, with no BLE connection involved. Changing it is the cheapest way to
raise history resolution.

`0x2B` reads, `0x2A` writes. Both share a layout:

```
2b 02 0000 2359 ff 0a00 01
   |  |    |    |  |    metric: 1=HR 2=SpO2 3=temp 4=HRV
   |  |    |    |  interval, u16 LE minutes
   |  |    |    weekday bitmask (0xff = every day)
   |  |    window end, BCD HH MM
   |  window start, BCD HH MM
   enabled: 2 = on, 0 = off
```

To read, send `2b <metric>`. `BleSDK.SetAutomatic` always writes a full-day
00:00–23:59 / all-days window, so the window and mask fields appear fixed in
practice, but the firmware stores whatever you send.

Factory defaults on this unit:

| Metric | Interval |
| --- | --- |
| HR | 10 min |
| SpO2 | 30 min |
| temperature | 5 min |
| HRV | 60 min |

These match the observed spacing of every history record type exactly, which
confirms the field is the sampling interval and not something else.

**Verified end to end.** HR was changed 10 min -> 5 min and the history
re-read 13 minutes later. Records before the change sit on a 10-minute grid;
records after it sit on a 5-minute grid (`15:00`, `15:10`, `15:15`). The
firmware honours the setting, and it applies with nothing connected — the
band logs on its own schedule.

Two operational notes:

- Occasional off-grid records appear (e.g. `15:06:23`), presumably logged by
  an on-demand measurement rather than the automatic timer.
- Gaps appeared at 14:00 and 14:20, during a stretch of running live
  measurements and repeated history syncs. Plausibly the automatic sample is
  skipped while the sensor is busy with an on-demand measurement, though two
  missing samples is not enough to call it proven.

### `0x41` — device time

Response is **BCD**, not binary:

```
41 26 07 27 12 24 47 01 f4 ...
   yy mm dd HH MM SS
```

`0x26 0x07 0x27` read as BCD = 2026-07-27, matching the host clock.

## Naming caution

The app's constant names for the two realtime channels are swapped relative to
what they carry:

- opcode `0x9B` → `BleConst.realtimePPGData`, but stores a **PPI** value
- opcode `0x3A` → `BleConst.realtimePPIData`, but stores **PPG samples**

Trust the payload, not the constant name.

## Not yet done

- Remaining write commands (set time, personal info, alarms) are decoded in
  `BleSDK` but untested, since they modify device state. `0x2A`
  (SetAutomatic) is the only write exercised so far.
- `0x99` mode-byte variants — `OpenECGPPG(i, i2)` takes a second argument
  that may be a sample rate; only `i2 = 0` was tried.
- How far back history goes before it rolls over (~4–5 days observed, not
  bounded precisely).

Closed off by testing: raw PPG (`0x3A`) and ECG waveform are not implemented
on this firmware; `0x5C` sport and `0x5A` GPS return nothing; `0x190e` is
unused vendor leftover; `0x27` is version + build date; sleep stage codes are
deep/light/REM/awake = 1/2/3/5.
