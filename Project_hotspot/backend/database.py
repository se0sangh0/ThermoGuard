import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import sessionmaker

BACKEND_ENV_VAR = "THERMOGUARD_BACKEND_ENV"
_DATABASE_ENV_KEYS = (
    "DB_HOST",
    "DB_PORT",
    "DB_NAME",
    "DB_USER",
    "DB_PASSWORD",
)


def _backend_env_path() -> Path:
    """Avoid accidentally loading the dashboard's release-level .env file."""

    configured = os.environ.get(BACKEND_ENV_VAR, "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured))).resolve(
            strict=False
        )
    return Path(__file__).with_name(".env")


def _load_backend_environment() -> None:
    """Load dotenv values only when PID 1 has not supplied a complete set.

    The factory unit keeps ``/etc/thermoguard/hotspot-backend.env`` root-only.
    systemd reads that file before dropping privileges and passes all five DB
    variables to the service account.  Trying to open the same protected file
    again from Python would fail with ``PermissionError`` even though the
    required values are already present.  Development and root-run schema
    preflight still load the explicitly selected dotenv file when it is
    readable.
    """

    if all(os.environ.get(name) is not None for name in _DATABASE_ENV_KEYS):
        return
    env_path = _backend_env_path()
    if env_path.is_file() and os.access(env_path, os.R_OK):
        load_dotenv(dotenv_path=env_path, override=False)


_load_backend_environment()

DB_HOST = os.getenv("DB_HOST")
DB_PORT = os.getenv("DB_PORT")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")


def _required_database_setting(name: str, value: str | None) -> str:
    """Fail without including a value that might be a credential."""

    if value is None or not value.strip():
        raise RuntimeError(f"Backend environment is missing required setting: {name}")
    return value


def _database_port(value: str | None) -> int:
    """Validate the text value from an environment file before URL creation."""

    raw_port = _required_database_setting("DB_PORT", value)
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise RuntimeError("Backend environment has an invalid DB_PORT") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("Backend environment has an invalid DB_PORT")
    return port


def build_database_url(
    *,
    host: str | None,
    port: str | None,
    database: str | None,
    username: str | None,
    password: str | None,
) -> URL:
    """Build a SQLAlchemy URL without hand-formatting untrusted credentials.

    ``URL.create`` preserves the original username/password values and escapes
    them only when SQLAlchemy renders a connection string.  This avoids treating
    ``@``, ``:``, ``/``, ``?`` or ``#`` within a credential as URL delimiters.
    """

    return URL.create(
        drivername="mysql+pymysql",
        username=_required_database_setting("DB_USER", username),
        password=_required_database_setting("DB_PASSWORD", password),
        host=_required_database_setting("DB_HOST", host),
        port=_database_port(port),
        database=_required_database_setting("DB_NAME", database),
    )


DATABASE_URL = build_database_url(
    host=DB_HOST,
    port=DB_PORT,
    database=DB_NAME,
    username=DB_USER,
    password=DB_PASSWORD,
)

engine = create_engine(
    DATABASE_URL,
    pool_pre_ping=True
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine
)
