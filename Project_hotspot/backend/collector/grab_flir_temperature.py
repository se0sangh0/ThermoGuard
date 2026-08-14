#!/usr/bin/env python3
"""Retired one-shot Aravis collector entry point.

The dashboard owns FLIR capture, analysis, DB persistence, and alerts. This
stub prevents an old direct invocation from bypassing that single flow.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "grab_flir_temperature.py is retired. Run `python dashboard.py` from "
        "the ThermoGuard project root instead.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
