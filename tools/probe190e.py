"""Probe the second vendor service 0x190e on a J2208-family band.

Reads every readable characteristic, enables notify on 0003, listens for
unsolicited traffic, then tries a few speculative writes on 0004 using the
same 16-byte framing the fff0 service uses.
"""
import asyncio
import sys

from bleak import BleakClient

if len(sys.argv) < 2:
    sys.exit("usage: probe190e.py <band-mac>   (find it with: j2208.py scan)")
ADDR = sys.argv[1]
SVC = "0000190e-0000-1000-8000-00805f9b34fb"
WRITE = "00000004-0000-1000-8000-00805f9b34fb"
NOTIFY = "00000003-0000-1000-8000-00805f9b34fb"


def frame(op, *args):
    p = bytearray(16)
    p[0] = op & 0xFF
    for i, v in enumerate(args, 1):
        p[i] = v & 0xFF
    p[15] = sum(p[:15]) & 0xFF
    return bytes(p)


async def main():
    got = []
    async with BleakClient(ADDR, timeout=20) as c:
        svc = c.services.get_service(SVC)
        print(f"service {svc.uuid}")
        for ch in svc.characteristics:
            print(f"  char {ch.uuid}  {ch.properties}")
            if "read" in ch.properties:
                try:
                    val = await c.read_gatt_char(ch)
                    print(f"    read -> {val.hex() or '(empty)'}  len={len(val)}")
                except Exception as e:
                    print(f"    read failed: {type(e).__name__}: {e}")

        def cb(_ch, data):
            got.append(bytes(data))
            print(f"    <- {bytes(data).hex()}")

        try:
            await c.start_notify(NOTIFY, cb)
            print("notify enabled on 0003")
        except Exception as e:
            print(f"notify failed: {type(e).__name__}: {e}")

        print("\nlistening 8s for unsolicited traffic...")
        await asyncio.sleep(8)
        print(f"  {len(got)} unsolicited notifications")

        # Speculative pokes using the fff0 framing conventions.
        print("\nspeculative writes on 0004:")
        for label, pkt in [
            ("zero16", frame(0x00)),
            ("getver 0x27", frame(0x27)),
            ("battery 0x13", frame(0x13)),
            ("single 0x01", bytes([0x01])),
        ]:
            before = len(got)
            try:
                await c.write_gatt_char(WRITE, pkt, response=False)
                await asyncio.sleep(1.5)
                print(f"  {label:<14} {pkt.hex()[:34]:<34} -> {len(got)-before} replies")
            except Exception as e:
                print(f"  {label:<14} write failed: {type(e).__name__}: {e}")

        await c.stop_notify(NOTIFY)
    print(f"\ntotal notifications: {len(got)}")


asyncio.run(main())
