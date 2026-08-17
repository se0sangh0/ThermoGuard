"""
notifier.py - Telegram 알림 전송 모듈

.env 파일 또는 환경변수에서 BOT_TOKEN, CHAT_ID를 불러옵니다.
설정되지 않은 경우 RuntimeError를 발생시킵니다.

사용법:
    from notifier import send_alarm

    send_alarm(image_path="overlay.jpg", temp=55.3, status="Warning", robot_id="Robot-01")
"""

import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Optional

import requests

from ..logger import get_logger

_log = get_logger("analysis.notifier")

# ------------------------------------------------------------
# .env 파일 로드 (python-dotenv 없이 직접 파싱)
# ------------------------------------------------------------
DASHBOARD_ENV_VAR = "THERMOGUARD_DASHBOARD_ENV"


def _default_dotenv_path() -> Path:
    """Return the dashboard-only credential file without release coupling."""

    configured = os.environ.get(DASHBOARD_ENV_VAR, "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured))).resolve(
            strict=False
        )
    if getattr(sys, "frozen", False):
        return Path("/var/lib/thermoguard/dashboard.env")
    return Path(__file__).resolve().parents[2] / ".env"


DOTENV_PATH = _default_dotenv_path()


def _load_dotenv(dotenv_path: str | Path = DOTENV_PATH) -> None:
    """최소 .env 파싱 -- KEY=VALUE 형식의 줄만 처리"""
    dotenv_path = Path(dotenv_path)
    if not dotenv_path.exists():
        return
    try:
        file_stat = dotenv_path.lstat()
    except OSError as exc:
        _log.warning("Telegram environment file cannot be inspected (%s)", type(exc).__name__)
        return
    if (
        dotenv_path.is_symlink()
        or not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_uid != os.getuid()
        or not os.access(dotenv_path, os.W_OK)
    ):
        _log.warning(
            "Ignoring Telegram environment file without safe local ownership: %s",
            dotenv_path,
        )
        return
    try:
        os.chmod(dotenv_path, 0o600)
    except OSError as exc:
        _log.warning("Telegram environment file cannot be hardened (%s)", type(exc).__name__)
        return
    with open(dotenv_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()

# ------------------------------------------------------------
# 설정
# ------------------------------------------------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
# Delivery is opt-in.  A credential-only file must not begin sending factory
# alarms until commissioning has explicitly enabled and recorded delivery.
TELEGRAM_ENABLED = os.environ.get("TELEGRAM_ENABLED", "false").strip().lower() in {
    "1", "true", "yes", "on",
}

TELEGRAM_API = "https://api.telegram.org"

FASTAPI_URL = os.environ.get(
    "FASTAPI_URL",
    "http://127.0.0.1:8000"
)

def _is_configured() -> bool:
    return bool(BOT_TOKEN and CHAT_ID)


def is_configured() -> bool:
    """Return whether Telegram credentials are available."""
    return _is_configured()


def is_enabled() -> bool:
    """Return whether dashboard Telegram delivery is enabled."""
    return bool(TELEGRAM_ENABLED and _is_configured())


def get_settings() -> dict:
    """Return the current runtime notification settings for the settings UI."""
    return {
        "bot_token": BOT_TOKEN,
        "chat_id": CHAT_ID,
        "configured": _is_configured(),
        "enabled": bool(TELEGRAM_ENABLED),
    }


def _update_dotenv(
    values: dict[str, Optional[str]],
    dotenv_path: Optional[Path] = None,
) -> None:
    """Atomically update Telegram keys in a protected local environment file."""
    dotenv_path = Path(dotenv_path or DOTENV_PATH)
    if dotenv_path.is_symlink():
        raise RuntimeError("Telegram environment file must not be a symbolic link.")
    existing_lines = []
    if dotenv_path.exists():
        file_stat = dotenv_path.lstat()
        if (
            dotenv_path.is_symlink()
            or not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_uid != os.getuid()
            or not os.access(dotenv_path, os.W_OK)
        ):
            raise RuntimeError(
                "Telegram environment file must be a writable regular file "
                "owned by the current user."
            )
        os.chmod(dotenv_path, 0o600)
        existing_lines = dotenv_path.read_text(encoding="utf-8").splitlines()

    pending = dict(values)
    updated_lines = []
    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            updated_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key not in pending:
            updated_lines.append(line)
            continue
        value = pending.pop(key)
        if value is not None:
            updated_lines.append(f"{key}={value}")

    for key, value in pending.items():
        if value is not None:
            updated_lines.append(f"{key}={value}")

    parent = dotenv_path.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=0o700)
        os.chmod(parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{dotenv_path.name}.",
        suffix=".tmp",
        dir=parent,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as env_file:
            os.fchmod(env_file.fileno(), 0o600)
            env_file.write(
                "\n".join(updated_lines).rstrip()
                + ("\n" if updated_lines else "")
            )
            env_file.flush()
            os.fsync(env_file.fileno())
        os.replace(temporary, dotenv_path)
        try:
            directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    except BaseException:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def configure(
    bot_token: str,
    chat_id: str,
    *,
    enabled: bool = False,
    persist: bool = True,
) -> None:
    """Apply verified credentials to the current process and optional local .env."""
    global BOT_TOKEN, CHAT_ID, TELEGRAM_ENABLED
    bot_token = bot_token.strip()
    chat_id = chat_id.strip()
    if not bot_token or not chat_id:
        raise ValueError("Bot Token과 Chat ID를 모두 입력하세요.")
    if "\n" in bot_token or "\r" in bot_token or "\n" in chat_id or "\r" in chat_id:
        raise ValueError("Bot Token과 Chat ID에는 줄바꿈을 포함할 수 없습니다.")

    next_enabled = bool(enabled)
    if persist:
        _update_dotenv({
            "BOT_TOKEN": bot_token,
            "CHAT_ID": chat_id,
            "TELEGRAM_ENABLED": "true" if next_enabled else "false",
        })
    BOT_TOKEN = bot_token
    CHAT_ID = chat_id
    TELEGRAM_ENABLED = next_enabled
    os.environ["BOT_TOKEN"] = BOT_TOKEN
    os.environ["CHAT_ID"] = CHAT_ID
    os.environ["TELEGRAM_ENABLED"] = "true" if TELEGRAM_ENABLED else "false"


def set_enabled(enabled: bool, *, persist: bool = True) -> None:
    """Enable or disable delivery without deleting the saved login."""
    global TELEGRAM_ENABLED
    if enabled and not _is_configured():
        raise RuntimeError("Telegram 로그인 후 알림 전송을 활성화하세요.")
    next_enabled = bool(enabled)
    if persist:
        _update_dotenv({"TELEGRAM_ENABLED": "true" if next_enabled else "false"})
    TELEGRAM_ENABLED = next_enabled
    os.environ["TELEGRAM_ENABLED"] = "true" if TELEGRAM_ENABLED else "false"


def logout(*, persist: bool = True) -> None:
    """Remove Telegram credentials from memory, environment and local .env."""
    global BOT_TOKEN, CHAT_ID, TELEGRAM_ENABLED
    if persist:
        _update_dotenv({
            "BOT_TOKEN": None,
            "CHAT_ID": None,
            "TELEGRAM_ENABLED": "false",
        })
    BOT_TOKEN = ""
    CHAT_ID = ""
    TELEGRAM_ENABLED = False
    os.environ.pop("BOT_TOKEN", None)
    os.environ.pop("CHAT_ID", None)
    os.environ["TELEGRAM_ENABLED"] = "false"


def test_connection(
    bot_token: Optional[str] = None,
    chat_id: Optional[str] = None,
    *,
    timeout: float = 10.0,
) -> tuple[bool, str]:
    """Validate the bot token and target chat without sending a message."""
    token = (bot_token if bot_token is not None else BOT_TOKEN).strip()
    target = (chat_id if chat_id is not None else CHAT_ID).strip()
    if not token or not target:
        return False, "Bot Token과 Chat ID를 모두 입력하세요."
    try:
        bot_response = requests.get(
            f"{TELEGRAM_API}/bot{token}/getMe",
            timeout=timeout,
        )
        if bot_response.status_code != 200:
            return False, f"Bot Token 확인 실패 (HTTP {bot_response.status_code})"
        chat_response = requests.get(
            f"{TELEGRAM_API}/bot{token}/getChat",
            params={"chat_id": target},
            timeout=timeout,
        )
        if chat_response.status_code != 200:
            return False, f"Chat ID 확인 실패 (HTTP {chat_response.status_code})"
        bot_name = bot_response.json().get("result", {}).get("username", "Telegram Bot")
        return True, f"@{bot_name} 연결 확인 완료"
    except requests.RequestException as exc:
        _log.warning("Telegram connection check failed (%s)", type(exc).__name__)
        return False, f"Telegram 연결 실패 ({type(exc).__name__})"


# ------------------------------------------------------------
# 메시지 생성
# ------------------------------------------------------------
def build_caption(
    temp: float,
    status: str,
    robot_id: str = "Robot-01",
) -> str:
    """Telegram 이미지 캡션용 메시지"""
    return (
        f"\u26a0\ufe0f Overheat Alarm\n\n"
        f"Temp    : {temp:.1f}\u2103\n"
        f"Status  : {status}"
    )


def build_text_message(
    temp: float,
    status: str,
    max_temp: float,
    mean_temp: float,
    robot_id: str = "Robot-01",
) -> str:
    """이미지 없이 텍스트만 전송할 때 사용 (fallback)"""
    return (
        f"\u26a0\ufe0f Overheat Alarm\n\n"
        f"Max Temp  : {max_temp:.1f}\u2103\n"
        f"Mean Temp : {mean_temp:.1f}\u2103\n"
        f"Hot (95th): {temp:.1f}\u2103\n"
        f"Status    : {status}"
    )

def save_delivery_result(
    alert_id: int,
    success: bool,
    http_status: int | None = None,
    error_message: str | None = None,
    retry_count: int = 0,
    backend_url: str | None = None,
) -> bool:
    """Telegram 전송 결과를 FastAPI를 통해 DB에 저장한다."""
    _log.debug(
        "[DBG-NOTIFIER] save_delivery_result ENTER: alert_id=%s success=%s "
        "http_status=%s error_present=%s",
        alert_id, success, http_status, bool(error_message),
    )

    target_url = (
        f"{(backend_url or FASTAPI_URL).rstrip('/')}"
        "/api/notification-deliveries"
    )

    try:
        response = requests.post(
            target_url,
            json={
                "alert_id": alert_id,
                "delivery_status": (
                    "success" if success else "failed"
                ),
                "http_status": http_status,
                "retry_count": retry_count,
                "error_message": error_message,
            },
            timeout=10,
        )

        response.raise_for_status()
        result = response.json()

        if result.get("status") != "created":
            _log.error(
                "notification delivery API returned an unexpected response "
                "for alert_id=%s",
                alert_id,
            )
            return False

        _log.info(
            "notification_deliveries 저장 성공: "
            "alert_id=%s delivery_id=%s",
            alert_id,
            result.get("delivery_id"),
        )
        return True

    except Exception as exc:
        _log.error(
            "notification delivery API call failed: "
            "alert_id=%s error_type=%s",
            alert_id,
            type(exc).__name__,
        )
        return False

# ------------------------------------------------------------
# 전송 함수
# ------------------------------------------------------------
def send_alarm(
    image_path: str,
    temp: float,
    status: str,
    robot_id: str = "Robot-01",
    alert_id: int | None = None,
    backend_url: str | None = None,
) -> bool:
    """
    과열 알림 전송 (이미지 + 캡션).

    이미지가 없거나 전송 실패 시 텍스트만 전송합니다.
    환경변수가 없으면 콘솔에 dry-run 출력 후 True 반환.
    """
    caption = build_caption(temp, status, robot_id)
    _log.debug(
        "[DBG-NOTIFIER] send_alarm ENTER: alert_id=%s status=%s temp=%.1f image=%s",
        alert_id, status, temp, bool(image_path and os.path.isfile(image_path)),
    )

    # --- dry-run (개발 중 테스트용) ---
    # if not _is_configured():
    #     print("[DRY-RUN] Telegram not configured.")
    #     print(f"  BOT_TOKEN={'***' if BOT_TOKEN else '(empty)'}")
    #     print(f"  CHAT_ID={'***' if CHAT_ID else '(empty)'}")
    #     print(f"  image={image_path}")
    #     print(caption)
    #     return True

    if not _is_configured():
        _log.warning("send_alarm skipped: Telegram not configured (BOT_TOKEN=%s, CHAT_ID=%s)",
                     "***" if BOT_TOKEN else "(empty)", "***" if CHAT_ID else "(empty)")
        raise RuntimeError("BOT_TOKEN and CHAT_ID not configured. Set them in .env file.")
    if not TELEGRAM_ENABLED:
        _log.info("send_alarm skipped: Telegram delivery disabled")
        return False

    _log.info(
        "send_alarm: status=%s temp=%.1fC image=%s",
        status, temp, image_path,
    )

    # 1. 이미지 + 캡션 전송 시도
    photo_sent = False
    last_http_status: int | None = None
    last_error_message: str | None = None

    if os.path.isfile(image_path):
        try:
            with open(image_path, "rb") as photo:
                resp = requests.post(
                    f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendPhoto",
                    data={"chat_id": CHAT_ID, "caption": caption},
                    files={"photo": photo},
                    timeout=30,
                )
            last_http_status = resp.status_code
            if resp.status_code == 200:
                photo_sent = True
                _log.info("sendPhoto success: temp=%.1f°C", temp)
            else:
                last_error_message = f"Telegram sendPhoto failed (HTTP {resp.status_code})"
                _log.error("sendPhoto failed: HTTP %d", resp.status_code)
                print(f"[Telegram] sendPhoto failed: {resp.status_code}")
        except Exception as e:
            last_error_message = f"Telegram sendPhoto exception ({type(e).__name__})"
            _log.error("sendPhoto exception (%s)", type(e).__name__)
            print(f"[Telegram] sendPhoto error - falling back to text")
    else:
        _log.warning("Image not found for alarm: %s", image_path)
        print(f"[Telegram] image not found: {image_path}")

    # 2. 이미지 전송 실패 시 텍스트만 전송
    if not photo_sent:
        try:
            resp = requests.post(
                f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage",
                data={"chat_id": CHAT_ID, "text": caption},
                timeout=30,
            )
            last_http_status = resp.status_code
            if resp.status_code == 200:
                photo_sent = True
            else:
                last_error_message = f"Telegram sendMessage failed (HTTP {resp.status_code})"
                _log.error("sendMessage fallback failed: HTTP %d", resp.status_code)
                print(f"[Telegram] sendMessage failed: {resp.status_code}")
        except Exception as e:
            last_error_message = f"Telegram sendMessage exception ({type(e).__name__})"
            _log.error("sendMessage exception (%s)", type(e).__name__)
            print(f"[Telegram] sendMessage error")

    if alert_id is not None:
        _log.debug(
            "[DBG-NOTIFIER] send_alarm: calling save_delivery_result alert_id=%s "
            "photo_sent=%s http_status=%s",
            alert_id, photo_sent, last_http_status,
        )
        save_delivery_result(
            alert_id=alert_id,
            success=photo_sent,
            http_status=last_http_status,
            error_message=(None if photo_sent else last_error_message),
            retry_count=0,
            backend_url=backend_url,
        )
    else:
        _log.debug(
            "[DBG-NOTIFIER] send_alarm: alert_id is None — skip save_delivery_result"
        )

    return photo_sent


def send_text(
    text: str,
) -> bool:
    """
    이미지 없이 텍스트만 전송.
    """
    # --- dry-run (개발 중 테스트용) ---
    # if not _is_configured():
    #     print(f"[DRY-RUN] Telegram text:\n{text}")
    #     return True

    if not _is_configured():
        raise RuntimeError("BOT_TOKEN and CHAT_ID not configured. Set them in .env file.")

    try:
        resp = requests.post(
            f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text},
            timeout=30,
        )
        return resp.status_code == 200
    except Exception:
        return False


# ------------------------------------------------------------
# 테스트 (직접 실행 시)
# ------------------------------------------------------------
# if __name__ == "__main__":
#     print("=== Notifier Dry-Run Test ===")
#     print()
#
#     send_alarm(
#         image_path="thermal_dataset/overlay_sample.jpg",
#         temp=55.3,
#         status="Warning",
#         robot_id="Robot-01",
#     )
#
#     print()
#
#     send_text("Test message from robot thermal monitor.")
