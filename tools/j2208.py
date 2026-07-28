"""J2208-family fitness band raw BLE client.

Protocol recovered from com.elink.fittrackhealth.pro v9.22.3, package
com.example.watch_flutter_plugin.humeBandJ2208 (unobfuscated Kotlin).

GATT profile
    service       0000fff0-0000-1000-8000-00805f9b34fb
    write   fff6  0000fff6-0000-1000-8000-00805f9b34fb
    notify  fff7  0000fff7-0000-1000-8000-00805f9b34fb

Framing
    Fixed 16-byte commands. byte[0] = opcode, bytes[1..14] = args,
    byte[15] = (sum of bytes 0..14) & 0xFF. No encryption, no auth handshake.

Notifications are variable length; multi-sample payloads pack uint16
little-endian values from offset 3.

Only one BLE central can hold the band at a time - close the phone app (or
turn the phone's Bluetooth off) before running this.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakClient, BleakScanner

SERVICE = "0000fff0-0000-1000-8000-00805f9b34fb"
CHAR_WRITE = "0000fff6-0000-1000-8000-00805f9b34fb"
CHAR_NOTIFY = "0000fff7-0000-1000-8000-00805f9b34fb"

# DeviceConst opcodes (signed Java bytes converted to unsigned).
OP_SET_TIME = 0x01
OP_GET_BATTERY = 0x13
OP_GET_MAC = 0x22
OP_GET_VERSION = 0x27
OP_MEASUREMENT_WITH_TYPE = 0x28  # StartDeviceMeasurementWithHrv
OP_PPG = 0x39  # getPPG notification
OP_OPEN_PPG = 0x3A  # realtime PPG/PPI sample block
OP_PPG_SENSOR = 0x3B  # GetPpgPPGSensor on/off
OP_GET_TIME = 0x41
OP_OPEN_ECG = 0x99
OP_CLOSE_ECG = 0x98
OP_OPEN_RR_INTERVAL = 0x9B  # also carries realtime PPI notifications
OP_ECG_DATA = 0xAA
OP_PPG_DATA = 0xAB

OPCODE_NAMES = {
    OP_GET_BATTERY: "battery",
    OP_GET_MAC: "mac",
    OP_GET_VERSION: "version",
    OP_MEASUREMENT_WITH_TYPE: "measurement",
    OP_PPG: "ppg_block",
    OP_OPEN_PPG: "ppg_realtime",
    OP_PPG_SENSOR: "ppg_sensor",
    OP_GET_TIME: "time",
    OP_OPEN_ECG: "ecg",
    OP_CLOSE_ECG: "ecg_closed",
    OP_OPEN_RR_INTERVAL: "rr_interval",
    OP_ECG_DATA: "ecg_raw",
    OP_PPG_DATA: "ppg_raw",
}


# History download: opcode, record stride, and how to read fields from +9.
# All record types share a BCD datetime at offset +3..+8 within the record.
# Strides come from the per-type parsers in ResolveUtil.java.
HISTORY = {
    # Daily totals: date-only, no time, so the BCD date sits at +2..+4.
    "total":     {"op": 0x51, "stride": 26, "fields": "totals", "date_only": True},
    "steps":     {"op": 0x52, "stride": 25, "fields": "steps"},
    "sleep":     {"op": 0x53, "stride": 34, "fields": "sleep"},
    # 0x55 is the populated HR history on V2 firmware (one sample / 10 min).
    # 0x54 "dynamic HR" answers 54 ff - empty - on this unit.
    "hr":        {"op": 0x55, "stride": 10, "fields": "u8"},
    "hr_dynamic": {"op": 0x54, "stride": 24, "fields": "hr15"},
    "hrv":       {"op": 0x56, "stride": 15, "fields": "hrv"},
    "sport":     {"op": 0x5C, "stride": 25, "fields": None},
    "spo2":      {"op": 0x60, "stride": 10, "fields": "u8"},
    "temp":      {"op": 0x62, "stride": 11, "fields": "u16x0.1"},
    "temp_axil": {"op": 0x65, "stride": 11, "fields": "u16x0.1"},
    "spo2_auto": {"op": 0x66, "stride": 10, "fields": "u8"},
}

MODE_START = 0
MODE_CONTINUE = 2
CONTINUE_EVERY = 50  # HumeBandAdapter re-requests after 50 packets


def bcd(value: int) -> int:
    """ResolveUtil.getTimeValue: parses the decimal digits as hex, i.e. BCD."""
    return int(str(value), 16)


def unbcd(byte: int) -> int:
    return ((byte >> 4) & 0x0F) * 10 + (byte & 0x0F)


def record_datetime(rec: bytes, date_only: bool = False) -> str | None:
    """BCD timestamp inside a history record.

    Normal records carry yy.mm.dd HH:MM:SS at +3..+8. Daily-total records
    (0x51) are date-only and sit at +2..+4.
    """
    if date_only:
        if len(rec) < 5:
            return None
        yy, mm, dd = (unbcd(b) for b in rec[2:5])
        hh = mi = ss = 0
    else:
        if len(rec) < 9:
            return None
        yy, mm, dd, hh, mi, ss = (unbcd(b) for b in rec[3:9])
    if not (1 <= mm <= 12 and 1 <= dd <= 31 and hh < 24 and mi < 60 and ss < 60):
        return None
    if date_only:
        return f"20{yy:02d}-{mm:02d}-{dd:02d}"
    return f"20{yy:02d}-{mm:02d}-{dd:02d}T{hh:02d}:{mi:02d}:{ss:02d}"


def frame(opcode: int, *args: int) -> bytes:
    """Build a 16-byte command packet with the trailing sum checksum."""
    pkt = bytearray(16)
    pkt[0] = opcode & 0xFF
    for i, value in enumerate(args, start=1):
        pkt[i] = value & 0xFF
    pkt[15] = sum(pkt[:15]) & 0xFF
    return bytes(pkt)


def u16le(data: bytes, offset: int) -> int:
    """Mirror of ResolveUtil.getValue(b,0) + getValue(b,1)."""
    return data[offset] | (data[offset + 1] << 8)


def samples_from(data: bytes, offset: int = 3) -> list[int]:
    """Unpack uint16 LE samples, as ResolveUtil.getDoublePPGData does."""
    body = data[offset:]
    return [u16le(body, i * 2) for i in range(len(body) // 2)]


def decode(data: bytes) -> dict:
    """Best-effort decode of a notification. Raw hex is always retained."""
    out = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "hex": data.hex(),
        "len": len(data),
    }
    if not data:
        return out
    op = data[0]
    out["opcode"] = f"0x{op:02x}"
    out["kind"] = OPCODE_NAMES.get(op, "unknown")

    if op == OP_OPEN_RR_INTERVAL and len(data) >= 3:
        # BleSDK.DataParsingWithData: b2 == -101 -> realtimePPGData, PPI value
        out["ppi_ms"] = u16le(data, 1)
    elif op == OP_OPEN_PPG and len(data) > 3:
        # case 58 -> realtimePPIData, getDoublePPGData(bArr[3:])
        out["samples"] = samples_from(data, 3)
    elif op == OP_PPG and len(data) > 3:
        # case 57 -> ResolveUtil.getPPG
        out["channel"] = data[1]
        out["samples"] = samples_from(data, 3)
    elif op in (OP_ECG_DATA, OP_PPG_DATA):
        # Passed through to the listener untouched by the app.
        out["samples"] = samples_from(data, 3)
    elif op == OP_GET_BATTERY and len(data) >= 2:
        out["battery_pct"] = data[1]
    elif op == OP_MEASUREMENT_WITH_TYPE and len(data) >= 10:
        # BleSDK case 40: [1]=type, [2]=HR, [3]=SpO2, [4]=HRV, [5]=stress,
        # [6]=systolic, [7]=diastolic, [8..9]=temp*0.1 (uint16 LE)
        out["measure_type"] = {1: "hrv", 2: "heart", 3: "spo2", 4: "temp"}.get(
            data[1], f"0x{data[1]:02x}"
        )
        out["hr"] = data[2]
        out["spo2"] = data[3]
        out["hrv"] = data[4]
        out["stress"] = data[5]
        out["bp_sys"] = data[6]
        out["bp_dia"] = data[7]
        out["temp_c"] = round(u16le(data, 8) * 0.1, 1)
    elif op == OP_GET_VERSION and len(data) >= 3:
        out["version_bytes"] = list(data[1:11])
    if len(data) >= 2 and data[1] == 0xFF:
        out["rejected"] = True
    return out


async def cmd_scan(args) -> int:
    print(f"scanning {args.seconds}s ...")
    devices = await BleakScanner.discover(timeout=args.seconds, return_adv=True)
    rows = []
    for dev, adv in devices.values():
        uuids = [u.lower() for u in (adv.service_uuids or [])]
        match = SERVICE in uuids
        rows.append((adv.rssi, dev.address, adv.local_name or dev.name or "?", match, uuids))
    rows.sort(key=lambda r: (not r[3], -(r[0] or -999)))
    print(f"{'RSSI':>5}  {'ADDRESS':<18} {'NAME':<24} MATCH")
    for rssi, addr, name, match, uuids in rows:
        flag = "<-- fff0 service" if match else ""
        print(f"{rssi:>5}  {addr:<18} {name[:24]:<24} {flag}")
    if not any(r[3] for r in rows):
        print(
            "\nNo device advertised the fff0 service. Many bands only expose it "
            "after connecting - try 'probe' against the most likely address."
        )
    return 0


async def cmd_probe(args) -> int:
    """Connect and dump the full GATT tree."""
    async with BleakClient(args.address, timeout=args.timeout) as client:
        print(f"connected: {client.address}\n")
        for service in client.services:
            print(f"service {service.uuid}  ({service.description})")
            for ch in service.characteristics:
                props = ",".join(ch.properties)
                print(f"  char {ch.uuid}  [{props}]")
                for d in ch.descriptors:
                    print(f"    desc {d.uuid}")
    return 0


class Session:
    def __init__(self, client: BleakClient, jsonl: Path | None):
        self.client = client
        self.jsonl = jsonl.open("w", encoding="utf-8") if jsonl else None
        self.records: list[dict] = []
        self._write_char = None
        self.quiet = False
        self.on_packet = None

    async def setup(self):
        svc = self.client.services.get_service(SERVICE)
        if svc is None:
            raise RuntimeError(
                f"service {SERVICE} not found on this device - is it the right band?"
            )
        self._write_char = svc.get_characteristic(CHAR_WRITE)
        if self._write_char is None:
            raise RuntimeError(f"write characteristic {CHAR_WRITE} not found")
        await self.client.start_notify(CHAR_NOTIFY, self._on_notify)

    def _on_notify(self, _char, data: bytearray):
        raw = bytes(data)
        rec = decode(raw)
        self.records.append(rec)
        if self.jsonl:
            self.jsonl.write(json.dumps(rec) + "\n")
            self.jsonl.flush()
        if self.on_packet:
            self.on_packet(raw)
        if self.quiet:
            return
        skip = {"ts", "hex", "len", "opcode", "kind", "samples"}
        bits = [f"{k}={v}" for k, v in rec.items() if k not in skip]
        n = len(rec.get("samples", []))
        if n:
            bits.insert(0, f"samples={n}:{rec['samples'][:6]}")
        extra = ("  " + " ".join(bits)) if bits else ""
        print(f"  <- {rec['kind']:<14} {rec['hex']}{extra}")

    async def send(self, name: str, packet: bytes, wait: float = 0.4):
        if not self.quiet:
            print(f"  -> {name:<14} {packet.hex()}")
        # The app writes without response on fff6; fall back if unsupported.
        no_resp = "write-without-response" in self._write_char.properties
        await self.client.write_gatt_char(self._write_char, packet, response=not no_resp)
        await asyncio.sleep(wait)

    def close(self):
        if self.jsonl:
            self.jsonl.close()


async def cmd_info(args) -> int:
    async with BleakClient(args.address, timeout=args.timeout) as client:
        print(f"connected: {client.address}")
        s = Session(client, None)
        await s.setup()
        await s.send("get_version", frame(OP_GET_VERSION))
        await s.send("get_battery", frame(OP_GET_BATTERY))
        await s.send("get_mac", frame(OP_GET_MAC))
        await s.send("get_time", frame(OP_GET_TIME))
        await asyncio.sleep(1.5)
        await client.stop_notify(CHAR_NOTIFY)
        s.close()
    return 0


async def cmd_ppg(args) -> int:
    out_dir = Path(args.out).parent
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl = Path(args.out).with_suffix(".jsonl")

    async with BleakClient(args.address, timeout=args.timeout) as client:
        print(f"connected: {client.address}")
        s = Session(client, jsonl)
        await s.setup()

        print("\nenabling sensor:")
        # StartDeviceMeasurementWithHrv(start=1, openPPG=1, openPPI=1)
        await s.send("measure+ppg", frame(OP_MEASUREMENT_WITH_TYPE, 1, 1, 1, 1), wait=1.0)
        if args.ecg:
            await s.send("open_ecg_ppg", frame(OP_OPEN_ECG, 1), wait=1.0)

        # 0x3a is request/response, not a push stream: each poll returns one
        # block of ~76 uint16 samples, so poll continuously for the duration.
        print(f"\npolling 0x3a every {args.interval}s for {args.seconds}s")
        print("(wear the band on the wrist, snug, and keep still)\n")
        deadline = time.monotonic() + args.seconds
        polls = 0
        while time.monotonic() < deadline:
            await s.send(f"poll#{polls}", frame(OP_OPEN_PPG, 1), wait=args.interval)
            polls += 1

        print("\nstopping:")
        await s.send("measure_stop", frame(OP_MEASUREMENT_WITH_TYPE, 1, 0, 0, 0))
        if args.ecg:
            await s.send("close_ecg_ppg", frame(OP_CLOSE_ECG))
        await client.stop_notify(CHAR_NOTIFY)
        s.close()

    _write_csv(s.records, Path(args.out))
    return 0


def _write_csv(records: list[dict], path: Path):
    total = 0
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "kind", "opcode", "index", "value"])
        for rec in records:
            for i, v in enumerate(rec.get("samples", [])):
                w.writerow([rec["ts"], rec["kind"], rec["opcode"], i, v])
                total += 1
            if "ppi_ms" in rec:
                w.writerow([rec["ts"], "ppi", rec["opcode"], 0, rec["ppi_ms"]])
                total += 1
    kinds = {}
    for rec in records:
        kinds[rec.get("kind", "?")] = kinds.get(rec.get("kind", "?"), 0) + 1
    print(f"\n{len(records)} notifications, {total} data points -> {path}")
    print(f"raw log -> {path.with_suffix('.jsonl')}")
    for k, n in sorted(kinds.items(), key=lambda x: -x[1]):
        print(f"  {k:<16} {n}")


def u32le(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset : offset + 4], "little")


def totals_stride(n: int) -> int:
    """ResolveUtil.getStepCount: daily-total records are 26 or 27 bytes,
    inferred from the packet length."""
    if n != 2:
        if n % 26 == 0:
            return 26
        if n % 27 == 0:
            return 27
        if (n - 2) % 26 == 0:
            return 26
    return 27


# Derived from sleep architecture across 4 nights (onset stage, deep-sleep
# latency ~20 min, REM latency >80 min). See PROTOCOL.md.
SLEEP_STAGES = {1: "deep", 2: "light", 3: "rem", 5: "awake"}


def parse_sleep_packet(data: bytes) -> list[dict] | None:
    """ResolveUtil.getSleepData single-block form (130 bytes after the
    terminator is stripped): [9] = count, then one stage byte per minute.
    Returns None if this packet is the stride-34 form instead.
    """
    if len(data) != 130 or len(data) < 10:
        return None
    count = data[9]
    stages = list(data[10 : 10 + count])
    mins = {name: 0 for name in ("deep", "light", "rem", "awake")}
    for v in stages:
        name = SLEEP_STAGES.get(v)
        if name:
            mins[name] += 1
    return [{
        "device_time": record_datetime(data),
        "raw": data.hex(),
        "unit_minutes": 1,
        "count": count,
        **{f"{k}_min": v for k, v in mins.items()},
        "stages": stages,
    }]


def parse_records(
    data: bytes, stride: int, fields: str | None, date_only: bool = False
) -> list[dict]:
    """Split a history notification into records, mirroring ResolveUtil."""
    if stride <= 0:
        return []
    out = []
    for i in range(len(data) // stride):
        rec = data[i * stride : (i + 1) * stride]
        row: dict = {"device_time": record_datetime(rec, date_only), "raw": rec.hex()}
        if fields == "totals" and len(rec) >= 26:
            # ResolveUtil.getTotalStepData. Goal is u8 at stride 26, u16 at 27.
            row["steps"] = u32le(rec, 5)
            row["exercise"] = u32le(rec, 9)  # app calls this minutes; looks like seconds
            row["distance_km"] = round(u32le(rec, 13) / 100.0, 2)
            row["calories_kcal"] = round(u32le(rec, 17) / 100.0, 2)
            row["goal"] = rec[21] if stride == 26 else u16le(rec, 21)
            row["active_minutes"] = u32le(rec, stride - 4)
            out.append(row)
            continue
        if fields == "steps" and len(rec) >= 25:
            # ResolveUtil.getDetailData, stride 25.
            row["steps"] = u16le(rec, 9)
            row["calories_kcal"] = round(u16le(rec, 11) / 100.0, 2)
            row["distance_km"] = round(u16le(rec, 13) / 100.0, 2)
            row["step_series"] = list(rec[15:25])  # 10 sub-interval step counts
            out.append(row)
            continue
        if len(rec) < 10:
            out.append(row)
            continue
        if fields == "u8":
            row["value"] = rec[9]
        elif fields == "u16x0.1":
            if len(rec) >= 11:
                row["value"] = round(u16le(rec, 9) * 0.1, 1)
        elif fields == "hr15":
            # +9..+23: 15 consecutive heart-rate bytes. Sampling interval
            # is not stated anywhere in the app - do not assume 1/minute.
            row["hr_series"] = list(rec[9:24])
        elif fields == "hrv":
            if len(rec) >= 15:
                row["hrv"] = rec[9]
                row["vascular_aging"] = rec[10]
                row["hr"] = rec[11]
                row["stress"] = rec[12]
                row["bp_sys"] = rec[13]
                row["bp_dia"] = rec[14]
        out.append(row)
    return out


async def cmd_history(args) -> int:
    spec = HISTORY[args.type]
    op, stride, fields = spec["op"], spec["stride"], spec.get("fields")

    # An all-zero date means "everything you have"; a date restricts the range.
    if args.since is None:
        when, date_args = None, []
    else:
        when = datetime.fromisoformat(args.since)
        date_args = [bcd(when.year % 100), bcd(when.month), bcd(when.day)]
        if not spec.get("date_only"):
            date_args += [bcd(when.hour), bcd(when.minute), bcd(when.second)]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    async with BleakClient(args.address, timeout=args.timeout) as client:
        print(f"connected: {client.address}")
        s = Session(client, out_path.with_suffix(".jsonl"))
        s.quiet = True
        await s.setup()

        done = asyncio.Event()
        packets: list[bytes] = []
        state = {"count": 0, "last_rx": time.monotonic(), "continues": 0}

        def on_packet(data: bytes):
            if not data or data[0] != op:
                return
            state["last_rx"] = time.monotonic()
            # Terminator is the byte pair <opcode> FF. Usually a standalone
            # 2-byte packet, but 0x51 appends it to the final data packet, so
            # match the pair rather than the packet length.
            if data[-2:] == bytes([op, 0xFF]):
                done.set()
                if len(data) == 2:
                    return
                data = data[:-2]
            packets.append(data)
            state["count"] += 1
            if state["count"] % CONTINUE_EVERY == 0:
                asyncio.get_running_loop().create_task(ask_continue())

        async def ask_continue():
            state["continues"] += 1
            state["last_rx"] = time.monotonic()
            await s.send("continue", frame(op, MODE_CONTINUE), wait=0)

        s.on_packet = on_packet

        scope = "all stored data" if when is None else f"from {when:%Y-%m-%d %H:%M:%S}"
        print(f"requesting {args.type} (0x{op:02x}), {scope}")
        # [1]=mode, [2..3]=0, [4..9]=BCD date/time
        await s.send("start", frame(op, MODE_START, 0, 0, *date_args), wait=0)

        # The band streams a burst then waits to be prompted. The app prompts
        # every 50 packets; that alone stalls on smaller bursts, so also prompt
        # whenever the stream goes quiet for --idle seconds.
        deadline = time.monotonic() + args.seconds
        finished = False
        while time.monotonic() < deadline:
            try:
                await asyncio.wait_for(done.wait(), timeout=0.25)
                finished = True
                break
            except asyncio.TimeoutError:
                pass
            if time.monotonic() - state["last_rx"] > args.idle:
                await ask_continue()

        if finished:
            print(f"end-of-data terminator received "
                  f"({state['continues']} continue prompts)")
        else:
            print(f"TIMED OUT after {args.seconds}s with no terminator - "
                  f"data may be incomplete")

        await client.stop_notify(CHAR_NOTIFY)
        s.close()

    rows = []
    for pkt in packets:
        if fields == "sleep":
            block = parse_sleep_packet(pkt)
            if block is not None:
                rows.extend(block)
                continue
        st = totals_stride(len(pkt)) if fields == "totals" else stride
        rows.extend(parse_records(pkt, st, fields, spec.get("date_only", False)))
    dated = [r for r in rows if r.get("device_time")]

    keys, seen = [], set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow({k: (json.dumps(v) if isinstance(v, list) else v)
                        for k, v in r.items()})

    print(f"\n{len(packets)} packets -> {len(rows)} records "
          f"({len(dated)} with a valid timestamp)")
    if dated:
        print(f"  range: {min(r['device_time'] for r in dated)} .. "
              f"{max(r['device_time'] for r in dated)}")
    print(f"  csv  -> {out_path}")
    print(f"  raw  -> {out_path.with_suffix('.jsonl')}")
    if stride == 0:
        print("  (no stride known for this type - inspect the raw hex)")
    return 0


MEASURE_TYPES = {"hrv": 1, "heart": 2, "spo2": 3, "temp": 4}

# Automatic background monitoring. This is what sets the interval at which the
# band logs to its own history, independently of any BLE connection.
OP_SET_AUTO = 0x2A
OP_GET_AUTO = 0x2B
AUTO_METRICS = {"hr": 1, "spo2": 2, "temp": 3, "hrv": 4}
AUTO_METRIC_NAMES = {v: k for k, v in AUTO_METRICS.items()}


def decode_auto(data: bytes) -> dict | None:
    """Parse a 0x2B automatic-monitoring settings reply.

    2b 02 0000 2359 ff 0a00 01
       |  |    |    |  |    metric
       |  |    |    |  interval, u16 LE minutes
       |  |    |    weekday bitmask
       |  |    window end, BCD HH MM
       |  window start, BCD HH MM
       enabled (2 = on, 0 = off)
    """
    if len(data) < 10 or data[0] != OP_GET_AUTO:
        return None
    return {
        "metric": AUTO_METRIC_NAMES.get(data[9], f"0x{data[9]:02x}"),
        "enabled": data[1] == 2,
        "window": f"{unbcd(data[2]):02d}:{unbcd(data[3]):02d}"
                  f"-{unbcd(data[4]):02d}:{unbcd(data[5]):02d}",
        "days_mask": f"0x{data[6]:02x}",
        "interval_min": u16le(data, 7),
    }


def frame_set_auto(metric: int, interval: int, enabled: bool = True) -> bytes:
    """BleSDK.SetAutomatic - full-day window, all days."""
    return frame(
        OP_SET_AUTO,
        2 if enabled else 0,
        0x00, 0x00,        # window start 00:00 (BCD)
        0x23, 0x59,        # window end 23:59 (BCD)
        0xFF,              # every day
        interval & 0xFF, (interval >> 8) & 0xFF,
        metric,
    )


async def cmd_auto(args) -> int:
    async with BleakClient(args.address, timeout=args.timeout) as client:
        print(f"connected: {client.address}")
        s = Session(client, None)
        s.quiet = True
        await s.setup()
        seen: dict[str, dict] = {}

        def on_packet(data: bytes):
            info = decode_auto(data)
            if info:
                seen[info["metric"]] = info

        s.on_packet = on_packet

        if args.set:
            metric = AUTO_METRICS[args.set]
            print(f"setting {args.set} interval to {args.interval} min")
            await s.send("set", frame_set_auto(metric, args.interval), wait=1.0)

        for name, code in AUTO_METRICS.items():
            await s.send(f"get {name}", frame(OP_GET_AUTO, code), wait=0.6)
        await asyncio.sleep(1.0)
        await client.stop_notify(CHAR_NOTIFY)
        s.close()

    print(f"\n{'metric':<8}{'enabled':<9}{'interval':<10}{'window':<14}days")
    for name in AUTO_METRICS:
        i = seen.get(name)
        if not i:
            print(f"{name:<8}(no reply)")
            continue
        print(f"{name:<8}{str(i['enabled']):<9}{str(i['interval_min'])+' min':<10}"
              f"{i['window']:<14}{i['days_mask']}")
    return 0


async def cmd_live(args) -> int:
    """Stream live measurements. StartDeviceMeasurementWithType(type, on).

    type 2 (heart) pushes a fresh reading about twice a second.
    """
    mtype = MEASURE_TYPES[args.type]
    out_path = Path(args.out) if args.out else None
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)

    async with BleakClient(args.address, timeout=args.timeout) as client:
        print(f"connected: {client.address}")
        s = Session(client, out_path.with_suffix(".jsonl") if out_path else None)
        s.quiet = True
        await s.setup()

        rows: list[dict] = []

        def on_packet(data: bytes):
            if not data or data[0] != OP_MEASUREMENT_WITH_TYPE or len(data) < 10:
                return
            rec = decode(data)
            rows.append(rec)
            live = {k: rec[k] for k in ("hr", "spo2", "hrv", "stress",
                                        "bp_sys", "bp_dia", "temp_c")
                    if rec.get(k)}
            stamp = rec["ts"][11:19]
            print(f"  {stamp}  " + "  ".join(f"{k}={v}" for k, v in live.items()))

        s.on_packet = on_packet

        print(f"starting {args.type} measurement for {args.seconds}s\n")
        await s.send("start", frame(OP_MEASUREMENT_WITH_TYPE, mtype, 1), wait=0)
        await asyncio.sleep(args.seconds)
        await s.send("stop", frame(OP_MEASUREMENT_WITH_TYPE, mtype, 0), wait=0.5)
        await client.stop_notify(CHAR_NOTIFY)
        s.close()

    if out_path and rows:
        keys = ["ts", "hr", "spo2", "hrv", "stress", "bp_sys", "bp_dia", "temp_c"]
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=keys, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print(f"\n{len(rows)} readings -> {out_path}")
    else:
        print(f"\n{len(rows)} readings")
    return 0


async def cmd_send(args) -> int:
    """Send arbitrary 16-byte commands and log whatever comes back.

    Each --cmd is 'opcode[:arg1,arg2,...]' in hex or decimal, e.g.
        --cmd 0x99:1,0,64,0   --cmd 0x71   --cmd 0x39:1
    """

    def parse(spec: str) -> tuple[str, bytes]:
        head, _, tail = spec.partition(":")
        op = int(head, 0)
        vals = [int(v, 0) for v in tail.split(",") if v.strip()] if tail else []
        return spec, frame(op, *vals)

    async with BleakClient(args.address, timeout=args.timeout) as client:
        print(f"connected: {client.address}")
        s = Session(client, Path(args.out) if args.out else None)
        await s.setup()
        for spec in args.cmd:
            label, pkt = parse(spec)
            await s.send(label, pkt, wait=args.gap)
        print(f"\nlistening a further {args.seconds}s ...")
        await asyncio.sleep(args.seconds)
        await client.stop_notify(CHAR_NOTIFY)
        s.close()
        kinds = {}
        for rec in s.records:
            kinds[rec.get("kind", "?")] = kinds.get(rec.get("kind", "?"), 0) + 1
        print(f"\n{len(s.records)} notifications")
        for k, n in sorted(kinds.items(), key=lambda x: -x[1]):
            print(f"  {k:<16} {n}")
    return 0


async def cmd_listen(args) -> int:
    """Passive: enable notify, send nothing. Shows unsolicited traffic."""
    jsonl = Path(args.out) if args.out else None
    async with BleakClient(args.address, timeout=args.timeout) as client:
        print(f"connected: {client.address}\nlistening {args.seconds}s (no commands sent)")
        s = Session(client, jsonl)
        await s.setup()
        await asyncio.sleep(args.seconds)
        await client.stop_notify(CHAR_NOTIFY)
        s.close()
        print(f"\n{len(s.records)} notifications")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    sc = sub.add_parser("scan", help="scan for BLE devices")
    sc.add_argument("--seconds", type=float, default=10.0)
    sc.set_defaults(func=cmd_scan)

    common = dict(type=str)
    for name, help_text, fn in [
        ("probe", "connect and dump the GATT tree", cmd_probe),
        ("info", "read version, battery, mac, time", cmd_info),
    ]:
        q = sub.add_parser(name, help=help_text)
        q.add_argument("address", **common)
        q.add_argument("--timeout", type=float, default=20.0)
        q.set_defaults(func=fn)

    pg = sub.add_parser("ppg", help="stream raw PPG / PPI samples")
    pg.add_argument("address", type=str)
    pg.add_argument("--seconds", type=float, default=30.0)
    pg.add_argument("--out", default="captures/ppg.csv")
    pg.add_argument("--timeout", type=float, default=20.0)
    pg.add_argument("--interval", type=float, default=0.5, help="seconds between 0x3a polls")
    pg.add_argument("--ecg", action="store_true", help="also open ECG/PPG mode (0x99)")
    pg.set_defaults(func=cmd_ppg)

    au = sub.add_parser("auto", help="read/set the band's own logging intervals")
    au.add_argument("address", type=str)
    au.add_argument("--set", choices=sorted(AUTO_METRICS), default=None)
    au.add_argument("--interval", type=int, default=5, help="minutes")
    au.add_argument("--timeout", type=float, default=20.0)
    au.set_defaults(func=cmd_auto)

    lv = sub.add_parser("live", help="stream live measurements (~0.7 Hz)")
    lv.add_argument("address", type=str)
    lv.add_argument("--type", choices=sorted(MEASURE_TYPES), default="heart")
    lv.add_argument("--seconds", type=float, default=30.0)
    lv.add_argument("--out", default=None)
    lv.add_argument("--timeout", type=float, default=20.0)
    lv.set_defaults(func=cmd_live)

    hs = sub.add_parser("history", help="download stored history off the band")
    hs.add_argument("address", type=str)
    hs.add_argument("--type", choices=sorted(HISTORY), default="hr")
    hs.add_argument("--since", default=None, help="ISO datetime, default now")
    hs.add_argument("--seconds", type=float, default=120.0, help="overall timeout")
    hs.add_argument("--idle", type=float, default=1.5,
                    help="seconds of silence before re-prompting the band")
    hs.add_argument("--out", default=None)
    hs.add_argument("--timeout", type=float, default=20.0)
    hs.set_defaults(func=cmd_history)

    sd = sub.add_parser("send", help="send arbitrary commands, e.g. --cmd 0x99:1,0,64,0")
    sd.add_argument("address", type=str)
    sd.add_argument("--cmd", action="append", default=[], required=True)
    sd.add_argument("--seconds", type=float, default=15.0)
    sd.add_argument("--gap", type=float, default=1.0)
    sd.add_argument("--out", default=None)
    sd.add_argument("--timeout", type=float, default=20.0)
    sd.set_defaults(func=cmd_send)

    ls = sub.add_parser("listen", help="passively log notifications")
    ls.add_argument("address", type=str)
    ls.add_argument("--seconds", type=float, default=30.0)
    ls.add_argument("--out", default=None)
    ls.add_argument("--timeout", type=float, default=20.0)
    ls.set_defaults(func=cmd_listen)

    args = p.parse_args()
    if getattr(args, "func", None) is cmd_history and args.out is None:
        args.out = f"captures/history_{args.type}.csv"
    try:
        return asyncio.run(args.func(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
