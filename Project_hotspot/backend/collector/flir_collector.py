#!/usr/bin/env python3
"""Retired FLIR collector entry point.

This script used to run an independent camera loop. It is deliberately kept
as a safe stub so an old systemd unit cannot create duplicate measurements or
alarms. Use the Product Dashboard instead.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "flir_collector.py is retired. Run `python dashboard.py` from the "
        "ThermoGuard project root; keep hotspot-flir-collector.service disabled.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
