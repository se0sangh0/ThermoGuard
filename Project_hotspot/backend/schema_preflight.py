"""Read-only database schema preflight for a ThermoGuard deployment.

This command intentionally performs only metadata reads (``SHOW TABLES`` and,
when requested, ``SHOW CREATE TABLE``).  It never creates, alters, migrates,
or writes to the database.  Run it from the backend runtime environment before
enabling dashboard capture on a factory line.

Examples:
    python schema_preflight.py
    python schema_preflight.py --json
    python schema_preflight.py --manifest /path/to/schema_manifest.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import text


DEFAULT_MANIFEST_PATH = Path(__file__).with_name("schema_manifest.json")
_VALID_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_AUTO_INCREMENT_VALUE = re.compile(r"\bAUTO_INCREMENT=\d+\b")


def load_schema_manifest(manifest_path: Path) -> dict[str, Any]:
    """Load and validate the read-only baseline manifest.

    The manifest deliberately contains table names and a DDL fingerprint only;
    it must never contain connection settings, seed data, or production values.
    """
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read schema manifest: {manifest_path}") from exc

    if not isinstance(payload, dict):
        raise ValueError("Schema manifest must be a JSON object")

    required_tables = payload.get("required_tables")
    if not isinstance(required_tables, list) or not required_tables:
        raise ValueError("Schema manifest must contain a non-empty required_tables list")

    if not all(
        isinstance(table, str) and _VALID_IDENTIFIER.fullmatch(table)
        for table in required_tables
    ):
        raise ValueError("Schema manifest contains an invalid table name")

    if len(required_tables) != len(set(required_tables)):
        raise ValueError("Schema manifest contains duplicate table names")

    fingerprint = payload.get("schema_fingerprint")
    if fingerprint is not None:
        if not isinstance(fingerprint, dict):
            raise ValueError("Schema manifest has an invalid schema_fingerprint")
        if fingerprint.get("algorithm") != "sha256":
            raise ValueError("Schema fingerprint algorithm must be sha256")
        if fingerprint.get("normalization") != "show_create_table_v1":
            raise ValueError("Schema fingerprint normalization is unsupported")
        value = fingerprint.get("value")
        if not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
            raise ValueError("Schema fingerprint must be a lowercase SHA-256 value")

    return payload


def load_required_tables(manifest_path: Path) -> tuple[str, ...]:
    """Load and validate the explicit set of tables required by this release."""
    return tuple(load_schema_manifest(manifest_path)["required_tables"])


def check_required_tables(engine: Any, required_tables: Iterable[str]) -> dict[str, list[str]]:
    """Compare available tables with the manifest using one read-only query."""
    with engine.connect() as connection:
        available_tables = {str(row[0]) for row in connection.execute(text("SHOW TABLES"))}

    required = sorted(set(required_tables))
    missing = sorted(set(required) - available_tables)
    return {
        "available_tables": sorted(available_tables),
        "missing_tables": missing,
        "required_tables": required,
    }


def _normalize_show_create_table(ddl: str) -> str:
    """Remove only the volatile AUTO_INCREMENT counter from table DDL.

    MariaDB advances that counter as production data is written.  Treating it
    as part of a fingerprint would make an unchanged schema look different
    after normal operation.  The remaining DDL, including indexes and foreign
    keys, is retained as a drift signal.
    """
    normalized = re.sub(r"\s+", " ", ddl).strip()
    return _AUTO_INCREMENT_VALUE.sub("AUTO_INCREMENT=<dynamic>", normalized)


def calculate_schema_fingerprint(engine: Any, required_tables: Iterable[str]) -> str:
    """Return a deterministic SHA-256 of the required tables' DDL.

    ``SHOW CREATE TABLE`` is a metadata read.  No DDL or data-changing
    statement is issued by this function.
    """
    canonical_parts: list[str] = []
    with engine.connect() as connection:
        for table in sorted(set(required_tables)):
            if not _VALID_IDENTIFIER.fullmatch(table):
                raise ValueError(f"Invalid table name in schema manifest: {table!r}")
            row = connection.execute(text(f"SHOW CREATE TABLE `{table}`")).one()
            if len(row) < 2 or not isinstance(row[1], str):
                raise ValueError(f"Unexpected SHOW CREATE TABLE result for {table}")
            canonical_parts.append(
                f"{table}\n{_normalize_show_create_table(row[1])}\n"
            )

    canonical_schema = "".join(canonical_parts).encode("utf-8")
    return hashlib.sha256(canonical_schema).hexdigest()


def _load_engine() -> Any:
    """Support both ``python schema_preflight.py`` and package imports."""
    try:
        from .database import engine  # type: ignore[import-not-found]
    except ImportError:
        from database import engine

    return engine


def _print_human_result(result: dict[str, Any]) -> None:
    if result["missing_tables"]:
        print("SCHEMA NOT READY")
        print("Missing required tables: " + ", ".join(result["missing_tables"]))
        return

    if result.get("fingerprint_matches") is False:
        print("SCHEMA DRIFT DETECTED")
        print("Required tables exist but their structure differs from the baseline")
        return

    print(
        "SCHEMA READY "
        f"({len(result['required_tables'])} required tables present; "
        f"{len(result['available_tables'])} tables discovered)"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only ThermoGuard database schema preflight",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="Path to an explicit schema manifest JSON file",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a machine-readable result",
    )
    parser.add_argument(
        "--verify-fingerprint",
        action="store_true",
        help=(
            "Also compare normalized SHOW CREATE TABLE output with the "
            "source-controlled baseline (read-only)"
        ),
    )
    args = parser.parse_args(argv)

    try:
        manifest = load_schema_manifest(args.manifest)
        required_tables = tuple(manifest["required_tables"])
        result = check_required_tables(_load_engine(), required_tables)

        if args.verify_fingerprint:
            fingerprint = manifest.get("schema_fingerprint")
            if fingerprint is None:
                raise ValueError("Schema manifest has no fingerprint baseline")
            if not result["missing_tables"]:
                actual_fingerprint = calculate_schema_fingerprint(
                    _load_engine(),
                    required_tables,
                )
                result["expected_fingerprint"] = fingerprint["value"]
                result["actual_fingerprint"] = actual_fingerprint
                result["fingerprint_matches"] = (
                    actual_fingerprint == fingerprint["value"]
                )
    except Exception as exc:
        # Driver exceptions can contain connection details.  Keep CLI output
        # actionable without leaking configuration values to terminal logs.
        failure = {
            "status": "error",
            "reason": "database_or_manifest_unavailable",
            "error_type": type(exc).__name__,
        }
        if args.json:
            print(json.dumps(failure, ensure_ascii=False, sort_keys=True))
        else:
            print(
                "SCHEMA PREFLIGHT ERROR "
                f"({failure['reason']}; {failure['error_type']})",
                file=sys.stderr,
            )
        return 2

    if result["missing_tables"]:
        result["status"] = "not_ready"
    elif args.verify_fingerprint and not result.get("fingerprint_matches"):
        result["status"] = "drifted"
    else:
        result["status"] = "ready"
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        _print_human_result(result)
    return 0 if result["status"] == "ready" else 1


if __name__ == "__main__":
    raise SystemExit(main())
