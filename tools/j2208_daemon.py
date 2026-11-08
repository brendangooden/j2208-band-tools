"""Long-running desktop collector for a J2208-family band.

Holds one persistent BLE connection and writes to SQLite. Reconnects on drop.
Runs entirely on the PC - the phone and the companion app are not involved.

Two collection modes, which differ enormously in battery cost:

  history  Poll the band's own stored records. The band logs HR every 10 min
           and skin temperature every 5 min regardless of who is connected,
           so this adds essentially nothing beyond the radio link. Resolution
           is fixed by the firmware, not by --interval.

  live     Run an on-demand measurement burst each interval. This powers the
           optical front-end (green LEDs + photodiode), which dominates the
           band's power draw. Resolution is per-interval, cost is roughly
           (burst / interval) x continuous-measurement drain.

Battery percentage is recorded on every cycle so actual drain can be measured
rather than guessed - see the `battery` table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bleak import BleakClient, BleakError

sys.path.insert(0, str(Path(__file__).parent))
from j2208 import (  # noqa: E402
    CHAR_NOTIFY,
    CHAR_WRITE,
    HISTORY,
    MODE_CONTINUE,
    MODE_START,
    OP_GET_BATTERY,
    OP_MEASUREMENT_WITH_TYPE,
    SERVICE,
    decode,
    frame,
    parse_records,
    parse_sleep_packet,
    totals_stride,
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS live (
    ts TEXT NOT NULL, hr INTEGER, spo2 INTEGER, hrv INTEGER, stress INTEGER,
    bp_sys INTEGER, bp_dia INTEGER, temp_c REAL,
    PRIMARY KEY (ts)
);
CREATE TABLE IF NOT EXISTS battery (
    ts TEXT PRIMARY KEY, pct INTEGER NOT NULL, mode TEXT
);
CREATE TABLE IF NOT EXISTS history (
    type TEXT NOT NULL, device_time TEXT NOT NULL, payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (type, device_time)
);
CREATE TABLE IF NOT EXISTS events (
    ts TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT
);
"""

# Which history types are worth polling on a loop. Excludes the ones that are
# empty on V2 firmware (0x54 hr_dynamic, 0x60 spo2, 0x62 temp).
POLL_TYPES = ["hr", "temp_axil", "spo2_auto", "hrv", "steps", "total", "sleep"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        self.db.commit()

    def event(self, kind: str, detail: str = ""):
        self.db.execute("INSERT INTO events VALUES (?,?,?)", (now_iso(), kind, detail))
        self.db.commit()

    def battery(self, pct: int, mode: str):
        self.db.execute(
            "INSERT OR REPLACE INTO battery VALUES (?,?,?)", (now_iso(), pct, mode)
        )
        self.db.commit()

    def live(self, rec: dict) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO live VALUES (?,?,?,?,?,?,?,?)",
            (rec["ts"], rec.get("hr"), rec.get("spo2"), rec.get("hrv"),
             rec.get("stress"), rec.get("bp_sys"), rec.get("bp_dia"),
             rec.get("temp_c")),
        )
        self.db.commit()
        return cur.rowcount > 0

    def history(self, htype: str, rows: list[dict]) -> int:
        new = 0
        stamp = now_iso()
        for r in rows:
            dt = r.get("device_time")
            if not dt:
                continue
            cur = self.db.execute(
                "INSERT OR IGNORE INTO history VALUES (?,?,?,?)",
                (htype, dt, json.dumps(r), stamp),
            )
            new += cur.rowcount
        self.db.commit()
        return new

    def drain_summary(self) -> str:
        rows = self.db.execute(
            "SELECT ts, pct FROM battery ORDER BY ts"
        ).fetchall()
        if len(rows) < 2:
            return "battery: not enough samples yet"
        t0 = datetime.fromisoformat(rows[0][0])
        t1 = datetime.fromisoformat(rows[-1][0])
        hours = (t1 - t0).total_seconds() / 3600
        drop = rows[0][1] - rows[-1][1]
        if hours < 0.5:
            return f"battery: {rows[-1][1]}% (need ~1h for a drain estimate)"
        rate = drop / hours
        life = f"{100 / rate:.0f}h to flat" if rate > 0 else "no measurable drop yet"
        return (f"battery: {rows[0][1]}% -> {rows[-1][1]}% over {hours:.1f}h "
                f"({rate:+.2f} %/h, {life})")


class BandSession:
    """One connected session. Owns the notify subscription."""

    def __init__(self, client: BleakClient, store: Store,
                 stop: asyncio.Event | None = None):
        self.client = client
        self.store = store
        self.stop = stop
        self.write_char = None
        self.inbox: asyncio.Queue[bytes] = asyncio.Queue()
        self.measuring = False
        self.interval_hint = 30.0

    async def setup(self):
        svc = self.client.services.get_service(SERVICE)
        if svc is None:
            raise BleakError(f"service {SERVICE} missing")
        self.write_char = svc.get_characteristic(CHAR_WRITE)
        await self.client.start_notify(CHAR_NOTIFY, self._on_notify)

    def _on_notify(self, _ch, data: bytearray):
        self.inbox.put_nowait(bytes(data))

    async def send(self, packet: bytes):
        no_resp = "write-without-response" in self.write_char.properties
        await self.client.write_gatt_char(
            self.write_char, packet, response=not no_resp
        )

    def drain_inbox(self):
        while not self.inbox.empty():
            self.inbox.get_nowait()

    async def collect(self, op: int, idle: float, overall: float) -> list[bytes]:
        """Gather packets for one opcode until the <op> FF terminator."""
        out: list[bytes] = []
        deadline = time.monotonic() + overall
        last = time.monotonic()
        while time.monotonic() < deadline:
            if self.stop is not None and self.stop.is_set():
                return out
            try:
                data = await asyncio.wait_for(self.inbox.get(), timeout=0.3)
            except asyncio.TimeoutError:
                if time.monotonic() - last > idle:
                    await self.send(frame(op, MODE_CONTINUE))
                    last = time.monotonic()
                continue
            last = time.monotonic()
            if not data or data[0] != op:
                continue
            if data[-2:] == bytes([op, 0xFF]):
                if len(data) > 2:
                    out.append(data[:-2])
                return out
            out.append(data)
        return out

    async def read_battery(self) -> int | None:
        self.drain_inbox()
        await self.send(frame(OP_GET_BATTERY))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                data = await asyncio.wait_for(self.inbox.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            if data and data[0] == OP_GET_BATTERY and len(data) >= 2:
                return data[1]
        return None

    async def sync_history(self, htype: str) -> int:
        spec = HISTORY[htype]
        op, stride, fields = spec["op"], spec["stride"], spec.get("fields")
        self.drain_inbox()
        await self.send(frame(op, MODE_START, 0, 0))
        packets = await self.collect(op, idle=1.5, overall=90)
        rows: list[dict] = []
        for pkt in packets:
            if fields == "sleep":
                blk = parse_sleep_packet(pkt)
                if blk is not None:
                    rows.extend(blk)
                    continue
            st = totals_stride(len(pkt)) if fields == "totals" else stride
            rows.extend(parse_records(pkt, st, fields, spec.get("date_only", False)))
        return self.store.history(htype, rows)

    async def live_burst(self, mtype: int, seconds: float) -> int:
        """Run the optical sensor for `seconds`, store every reading.

        The sensor needs ~9s to lock on before HR stops reading 0, so a burst
        shorter than about 15s yields nothing. seconds<=0 means leave the
        measurement running (continuous mode) - no warm-up after the first
        cycle, but the LEDs never switch off.
        """
        continuous = seconds <= 0
        if continuous and self.measuring:
            window = self.interval_hint
        else:
            self.drain_inbox()
            await self.send(frame(OP_MEASUREMENT_WITH_TYPE, mtype, 1))
            self.measuring = True
            window = self.interval_hint if continuous else seconds

        stored = 0
        deadline = time.monotonic() + window
        try:
            while time.monotonic() < deadline:
                if self.stop is not None and self.stop.is_set():
                    break
                try:
                    data = await asyncio.wait_for(self.inbox.get(), timeout=0.5)
                except asyncio.TimeoutError:
                    continue
                if not data or data[0] != OP_MEASUREMENT_WITH_TYPE or len(data) < 10:
                    continue
                rec = decode(data)
                if rec.get("hr"):  # skip the warm-up zeros
                    stored += self.store.live(rec)
        finally:
            if not continuous:
                await self.send(frame(OP_MEASUREMENT_WITH_TYPE, mtype, 0))
                self.measuring = False
        return stored


async def run(args) -> int:
    store = Store(Path(args.db))
    store.event("start", json.dumps(vars(args)))
    stop = asyncio.Event()

    def _sig(*_):
        print("\nshutting down...")
        stop.set()

    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, _sig)
        loop.add_signal_handler(signal.SIGTERM, _sig)
    except (NotImplementedError, RuntimeError, ValueError):
        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)

    cycle = 0
    backoff = 5
    print(f"collector: mode={args.mode} interval={args.interval}s db={args.db}")
    print("Ctrl-C to stop\n")

    while not stop.is_set():
        try:
            async with BleakClient(args.address, timeout=args.timeout) as client:
                s = BandSession(client, store, stop)
                s.interval_hint = max(5.0, args.interval - 3.0)
                await s.setup()
                backoff = 5
                store.event("connected")
                print(f"[{datetime.now():%H:%M:%S}] connected")

                while not stop.is_set():
                    cycle += 1
                    t0 = time.monotonic()
                    line = [f"[{datetime.now():%H:%M:%S}] #{cycle}"]

                    pct = await s.read_battery()
                    if pct is not None:
                        store.battery(pct, args.mode)
                        line.append(f"batt={pct}%")

                    if args.mode == "live":
                        n = await s.live_burst(args.measure_type, args.burst)
                        line.append(f"live+{n}")

                    # History sync is cheap but slow; do it every Nth cycle.
                    if cycle == 1 or cycle % args.history_every == 0:
                        for htype in args.types:
                            if stop.is_set():
                                break
                            try:
                                new = await s.sync_history(htype)
                                if new:
                                    line.append(f"{htype}+{new}")
                            except Exception as e:
                                line.append(f"{htype}!{type(e).__name__}")

                    print("  ".join(line))

                    spent = time.monotonic() - t0
                    try:
                        await asyncio.wait_for(
                            stop.wait(), timeout=max(1.0, args.interval - spent)
                        )
                    except asyncio.TimeoutError:
                        pass

        except asyncio.CancelledError:
            break
        except Exception as e:
            if stop.is_set():
                break
            store.event("disconnected", f"{type(e).__name__}: {e}")
            print(f"[{datetime.now():%H:%M:%S}] lost connection "
                  f"({type(e).__name__}), retry in {backoff}s")
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 120)

    store.event("stop")
    print("\n" + store.drain_summary())
    counts = store.db.execute(
        "SELECT type, COUNT(*) FROM history GROUP BY type ORDER BY type"
    ).fetchall()
    live_n = store.db.execute("SELECT COUNT(*) FROM live").fetchone()[0]
    print(f"live readings: {live_n}")
    for t, n in counts:
        print(f"  {t:<12} {n}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("address")
    p.add_argument("--mode", choices=["history", "live"], default="history",
                   help="history = near-zero extra drain; live = powers the LEDs")
    p.add_argument("--interval", type=float, default=60.0)
    p.add_argument("--burst", type=float, default=25.0,
                   help="live mode: seconds of measurement per interval. The "
                        "sensor needs ~9s to lock on, so <15s yields nothing. "
                        "Use 0 to leave it running continuously.")
    p.add_argument("--measure-type", type=int, default=2,
                   help="2=heart (fastest), 3=spo2, 4=temp, 1=hrv")
    p.add_argument("--history-every", type=int, default=10,
                   help="run a history sync every Nth cycle")
    p.add_argument("--types", nargs="+", default=POLL_TYPES, choices=POLL_TYPES,
                   help="which history types to sync. A full sweep takes ~90s; "
                        "use '--types hr' if heart rate is all you need.")
    p.add_argument("--db", default="captures/j2208.db")
    p.add_argument("--timeout", type=float, default=20.0)
    args = p.parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
