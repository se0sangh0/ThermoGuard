#!/usr/bin/env python3
"""Verify an installed candidate venv against the factory constraints.

This script only reads package metadata and optionally runs ``pip check``.
It does not install, remove, or upgrade anything.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path


CONSTRAINTS_PATH = Path(__file__).with_name("factory-runtime.constraints.txt")
PIN_PATTERN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$")


def _normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _read_pins(path: Path) -> dict[str, tuple[str, str]]:
    pins: dict[str, tuple[str, str]] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN_PATTERN.fullmatch(line)
        if match is None:
            raise ValueError(f"unsupported constraint at {path}:{line_number}: {raw_line}")
        display_name, expected_version = match.groups()
        normalized_name = _normalize_name(display_name)
        if normalized_name in pins:
            raise ValueError(f"duplicate constraint for {display_name}")
        pins[normalized_name] = (display_name, expected_version)
    return pins


def _verify_versions(pins: dict[str, tuple[str, str]]) -> list[str]:
    problems: list[str] = []
    for normalized_name, (display_name, expected_version) in sorted(pins.items()):
        try:
            installed_version = version(normalized_name)
        except PackageNotFoundError:
            problems.append(f"MISSING {display_name}=={expected_version}")
            continue
        if installed_version != expected_version:
            problems.append(
                f"MISMATCH {display_name}: expected {expected_version}, found {installed_version}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-pip-check",
        action="store_true",
        help="only compare distribution versions; do not invoke 'python -m pip check'",
    )
    args = parser.parse_args(argv)

    try:
        pins = _read_pins(CONSTRAINTS_PATH)
    except (OSError, ValueError) as exc:
        print(f"baseline verifier error: {exc}", file=sys.stderr)
        return 2

    problems = _verify_versions(pins)
    if problems:
        print("factory dependency baseline mismatch:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    if not args.skip_pip_check:
        pip_check = subprocess.run([sys.executable, "-m", "pip", "check"], check=False)
        if pip_check.returncode != 0:
            return pip_check.returncode

    print(f"factory dependency baseline verified ({len(pins)} pinned distributions)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
