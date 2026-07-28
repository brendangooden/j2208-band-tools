"""Extract printable strings from DEX/APK files and mine them for BLE artefacts.

No Java required - this is a first-pass recon tool. Reads raw bytes and pulls
ASCII runs, then applies regexes for GATT UUIDs, brand names and SDK packages.
"""
import re
import sys
from pathlib import Path

MIN_LEN = 5
ASCII_RUN = re.compile(rb"[\x20-\x7e]{%d,}" % MIN_LEN)

UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)


def strings_of(path: Path):
    data = path.read_bytes()
    for m in ASCII_RUN.finditer(data):
        yield m.group().decode("ascii", "replace")


def main(paths, mode):
    seen = set()
    for p in paths:
        for s in strings_of(Path(p)):
            if s in seen:
                continue
            seen.add(s)

    if mode == "uuid":
        found = {}
        for s in seen:
            for u in UUID_RE.findall(s):
                found.setdefault(u.lower(), set()).add(s[:120])
        for u in sorted(found):
            print(u)
    elif mode == "all":
        for s in sorted(seen):
            print(s)
    else:  # keyword grep
        pat = re.compile(mode, re.I)
        for s in sorted(seen):
            if pat.search(s):
                print(s)


if __name__ == "__main__":
    mode = sys.argv[1]
    main(sys.argv[2:], mode)
