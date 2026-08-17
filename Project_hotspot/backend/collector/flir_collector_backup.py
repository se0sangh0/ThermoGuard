#!/usr/bin/env python3
"""Retired backup collector entry point; intentionally non-operational."""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "flir_collector_backup.py is retired and cannot send measurements. "
        "Use `python dashboard.py` from the ThermoGuard project root.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
