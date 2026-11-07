#!/usr/bin/env python3
"""Tray and mapping arithmetic for the H2C.

  ams_mapping.py encode -20 -51 -52      # ScadBuddy tray ids -> flat ams_mapping
  ams_mapping.py decode 259 770 771      # firmware `mapping` -> tray names
  ams_mapping.py check report.json       # a saved pushall: names every slot, fails on 65535

Tray id = -(ams_id*16 + slot_1based); HT units are ams ids 128+.
Flat entry = ams_id*4 + slot_0based. Firmware echo = ams_id*256 + slot_0based.
"""
import json
import sys


def unit(ams_id: int) -> str:
    return f"AMS-HT-{chr(65 + ams_id - 128)}" if ams_id >= 128 else f"AMS-{chr(65 + ams_id)}"


def from_tray(tray: int) -> tuple[int, int]:
    n = -tray
    return n // 16, (n % 16) - 1  # ams id, slot 0-based


def main() -> int:
    cmd, rest = sys.argv[1], sys.argv[2:]
    if cmd == "encode":
        flat, names = [], []
        for t in map(int, rest):
            a, s = from_tray(t)
            if a >= 128:
                print(f"tray {t} is {unit(a)} slot {s + 1}: the HT is not mappable over the LAN path (maps to 65535)", file=sys.stderr)
                return 1
            flat.append(a * 4 + s)
            names.append(f"{unit(a)} slot {s + 1}")
        print(json.dumps(flat), "=", ", ".join(names))
    elif cmd == "decode":
        for v in map(int, rest):
            print(v, "->", "UNMAPPED" if v == 65535 else f"{unit(v // 256)} slot {v % 256 + 1}")
    elif cmd == "check":
        d = json.load(open(rest[0]))
        m = d.get("mapping") or []
        for i, v in enumerate(m, 1):
            print(f"slot {i}: {v} ->", "UNMAPPED" if v == 65535 else f"{unit(v // 256)} slot {v % 256 + 1}")
        if 65535 in m:
            print("FAIL: an unmapped slot; the print will pause at that filament's first load")
            return 1
        print("ok: every slot mapped")
    else:
        print(__doc__)
        return 2
    return 0


sys.exit(main())
