"""Host-local process lock for the production dashboard.

The dashboard owns a camera, its dataset directory, and the alert path.  A
PID file alone is not safe here: it can be left behind after a crash.  The
advisory ``flock`` below is held by the process itself and is released by the
kernel when that process exits.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:  # The factory target is Linux.  Keep the import error explicit elsewhere.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on unsupported hosts
    fcntl = None


# This is intentionally *not* XDG_RUNTIME_DIR: that directory is per user and
# would allow two operator accounts to start competing dashboard instances on
# one host.  The factory tmpfiles rule creates both this file and its parent as
# root-owned objects before any dashboard starts.  The dashboard only opens
# this specific file; it never gets permission to replace it with a new lock.
DEFAULT_LOCK_PATH = Path("/run/thermoguard/dashboard.lock")


class DashboardAlreadyRunningError(RuntimeError):
    """Raised before UI/camera initialization when another dashboard owns it."""


class DashboardLockConfigurationError(RuntimeError):
    """Raised when the host-wide lock has not been safely provisioned."""


class DashboardRuntimeAuthorizationError(RuntimeError):
    """Raised when factory camera I/O is requested outside dashboard runtime."""


_dashboard_runtime_scope: ContextVar[bool] = ContextVar(
    "thermoguard_dashboard_runtime_scope",
    default=False,
)


@contextmanager
def dashboard_runtime_scope():
    """Authorize factory camera I/O while the dashboard owns the host lock.

    This is an operational guard, not a security sandbox against arbitrary
    local Python code.  It prevents a supported library import or forgotten
    script from accidentally becoming a second camera owner in a commissioned
    install.  The only production entry point establishes it immediately
    inside :class:`DashboardInstanceLock`.
    """

    token = _dashboard_runtime_scope.set(True)
    try:
        yield
    finally:
        _dashboard_runtime_scope.reset(token)


def dashboard_runtime_authorized() -> bool:
    """Return whether this thread is executing under the dashboard lock scope."""

    return _dashboard_runtime_scope.get()


def default_lock_path() -> Path:
    """Return the host-wide single-dashboard lock location.

    A caller that needs an isolated lock (for example, a unit test) can pass a
    path directly to :class:`DashboardInstanceLock`.  Environment overrides
    are deliberately not supported: a shell user could otherwise bypass the
    shared factory lock and start a second camera owner.
    """

    return DEFAULT_LOCK_PATH


def inspect_default_lock_provisioning() -> str | None:
    """Return a provisioning problem, or ``None`` when the factory lock is safe.

    The result is read-only so preflight can verify exactly the same host-wide
    lock contract enforced immediately before the dashboard opens Tk or reaches
    the camera.
    """

    path = DEFAULT_LOCK_PATH
    parent = path.parent
    try:
        parent_stat = parent.lstat()
    except OSError:
        return f"lock directory is not provisioned: {parent}"
    if stat.S_ISLNK(parent_stat.st_mode) or not stat.S_ISDIR(parent_stat.st_mode):
        return f"lock directory must be a real directory: {parent}"
    if parent_stat.st_uid != 0 or stat.S_IMODE(parent_stat.st_mode) & (
        stat.S_IWGRP | stat.S_IWOTH
    ):
        return "lock directory must be root-owned and not writable by group or others"

    try:
        file_stat = path.lstat()
    except OSError:
        return f"lock file is not provisioned: {path}"
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISREG(file_stat.st_mode):
        return f"lock file must be a real regular file: {path}"
    if file_stat.st_uid != 0 or stat.S_IMODE(file_stat.st_mode) & (
        stat.S_IWGRP | stat.S_IWOTH):
        return "lock file must be root-owned and not writable by group or others"
    if not os.access(parent, os.X_OK) or not os.access(path, os.R_OK):
        return "dashboard account lacks read/search access to the provisioned lock"
    return None


class DashboardInstanceLock:
    """Non-blocking, crash-safe lock held for one dashboard process."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_lock_path()
        self._fd: Optional[int] = None

    @property
    def acquired(self) -> bool:
        return self._fd is not None

    def acquire(self) -> None:
        if self._fd is not None:
            return
        if fcntl is None:  # pragma: no cover - factory deployment is Linux
            raise DashboardLockConfigurationError(
                "ThermoGuard dashboard lock requires Linux flock support."
            )

        factory_lock = self.path == DEFAULT_LOCK_PATH
        parent = self.path.parent
        if factory_lock:
            provisioning_problem = inspect_default_lock_provisioning()
            if provisioning_problem:
                raise DashboardLockConfigurationError(
                    "ThermoGuard dashboard lock is unsafe or unprovisioned: "
                    f"{provisioning_problem}. Install "
                    "deployment/tmpfiles.d/thermoguard.conf before starting the dashboard."
                )
        if parent.is_symlink() or not parent.is_dir():
            raise DashboardLockConfigurationError(
                "ThermoGuard dashboard lock directory is not provisioned: "
                f"{parent}. Install deployment/tmpfiles.d/thermoguard.conf "
                "before starting the dashboard."
            )
        # The factory lock is a root-provisioned, read-only-to-dashboard file.
        # Exclusive flock works on a read-only descriptor on the local Linux
        # filesystem, and this prevents an operator account from unlinking or
        # rewriting the very lock used to prevent duplicate camera ownership.
        # Explicit paths remain writable/createable for isolated unit tests and
        # library callers; production code always uses DEFAULT_LOCK_PATH.
        flags = os.O_RDONLY if factory_lock else os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.path, flags, 0o660)
        except OSError as exc:
            raise DashboardLockConfigurationError(
                "Cannot open the root-provisioned ThermoGuard dashboard lock. "
                "Verify deployment/tmpfiles.d/thermoguard.conf and the "
                f"dashboard account's read access to {self.path}."
            ) from exc
        try:
            file_stat = os.fstat(fd)
            if not stat.S_ISREG(file_stat.st_mode):
                raise DashboardLockConfigurationError(
                    f"ThermoGuard dashboard lock is not a regular file: {self.path}"
                )
            if factory_lock:
                file_mode = stat.S_IMODE(file_stat.st_mode)
                if file_stat.st_uid != 0 or file_mode & (stat.S_IWGRP | stat.S_IWOTH):
                    raise DashboardLockConfigurationError(
                        "ThermoGuard dashboard lock must be root-owned and not "
                        "writable by group or others"
                    )
            elif file_stat.st_uid == os.getuid():
                os.fchmod(fd, 0o660)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            owner = self._read_owner(fd)
            os.close(fd)
            suffix = f" (current owner: {owner})" if owner else ""
            raise DashboardAlreadyRunningError(
                "ThermoGuard dashboard is already running on this host. "
                "Do not start a second instance; use the existing dashboard window."
                f"{suffix}"
            ) from exc
        except Exception:
            os.close(fd)
            raise

        self._fd = fd
        if factory_lock:
            # The file is intentionally immutable to the dashboard user.  PID
            # metadata is only a convenience, never worth weakening the lock.
            return
        metadata = {
            "pid": os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        encoded = (json.dumps(metadata, ensure_ascii=False) + "\n").encode("utf-8")
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, encoded)
        os.fsync(fd)

    @staticmethod
    def _read_owner(fd: int) -> str:
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 4096).decode("utf-8", errors="replace").strip()
            if not raw:
                return "unknown"
            value = json.loads(raw)
            pid = value.get("pid", "unknown")
            started_at = value.get("started_at", "unknown")
            return f"PID {pid}, started {started_at}"
        except Exception:
            return "unknown"

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def __enter__(self) -> "DashboardInstanceLock":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _traceback) -> None:
        self.release()
