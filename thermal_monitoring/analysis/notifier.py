"""
notifier.py - Telegram 알림 전송 모듈

.env 파일 또는 환경변수에서 BOT_TOKEN, CHAT_ID를 불러옵니다.
설정되지 않은 경우 RuntimeError를 발생시킵니다.

사용법:
    from notifier import send_alarm

    send_alarm(image_path="overlay.jpg", temp=55.3, status="Warning", robot_id="Robot-01")
"""

import os
import sys
from pathlib import Path
from typing import Optional

import requests

from ..logger import get_logger

_log = get_logger("analysis.notifier")

# ------------------------------------------------------------
# .env 파일 로드 (python-dotenv 없이 직접 파싱)
# ------------------------------------------------------------
DOTENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def _load_dotenv(dotenv_path: str | Path = DOTENV_PATH) -> None:
    """최소 .env 파싱 -- KEY=VALUE 형식의 줄만 처리"""
    if not os.path.isfile(dotenv_path):
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
TELEGRAM_ENABLED = os.environ.get("TELEGRAM_ENABLED", "true").strip().lower() in {
    "1", "true", "yes", "on",
}

TELEGRAM_API = "https://api.telegram.org"


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
    """Update only Telegram keys while preserving unrelated local settings."""
    dotenv_path = dotenv_path or DOTENV_PATH
    existing_lines = []
    if dotenv_path.exists():
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

    dotenv_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = dotenv_path.with_name(f"{dotenv_path.name}.tmp")
    temporary.write_text(
        "\n".join(updated_lines).rstrip() + ("\n" if updated_lines else ""),
        encoding="utf-8",
    )
    temporary.replace(dotenv_path)


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

    BOT_TOKEN = bot_token
    CHAT_ID = chat_id
    TELEGRAM_ENABLED = bool(enabled)
    os.environ["BOT_TOKEN"] = BOT_TOKEN
    os.environ["CHAT_ID"] = CHAT_ID
    os.environ["TELEGRAM_ENABLED"] = "true" if TELEGRAM_ENABLED else "false"
    if persist:
        _update_dotenv({
            "BOT_TOKEN": BOT_TOKEN,
            "CHAT_ID": CHAT_ID,
            "TELEGRAM_ENABLED": os.environ["TELEGRAM_ENABLED"],
        })


def set_enabled(enabled: bool, *, persist: bool = True) -> None:
    """Enable or disable delivery without deleting the saved login."""
    global TELEGRAM_ENABLED
    if enabled and not _is_configured():
        raise RuntimeError("Telegram 로그인 후 알림 전송을 활성화하세요.")
    TELEGRAM_ENABLED = bool(enabled)
    os.environ["TELEGRAM_ENABLED"] = "true" if TELEGRAM_ENABLED else "false"
    if persist:
        _update_dotenv({"TELEGRAM_ENABLED": os.environ["TELEGRAM_ENABLED"]})


def logout(*, persist: bool = True) -> None:
    """Remove Telegram credentials from memory, environment and local .env."""
    global BOT_TOKEN, CHAT_ID, TELEGRAM_ENABLED
    BOT_TOKEN = ""
    CHAT_ID = ""
    TELEGRAM_ENABLED = False
    os.environ.pop("BOT_TOKEN", None)
    os.environ.pop("CHAT_ID", None)
    os.environ["TELEGRAM_ENABLED"] = "false"
    if persist:
        _update_dotenv({
            "BOT_TOKEN": None,
            "CHAT_ID": None,
            "TELEGRAM_ENABLED": "false",
        })


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
        return False, f"Telegram 연결 실패: {exc}"


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
        f"Robot   : {robot_id}\n"
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
        f"Robot     : {robot_id}\n"
        f"Max Temp  : {max_temp:.1f}\u2103\n"
        f"Mean Temp : {mean_temp:.1f}\u2103\n"
        f"Hot (95th): {temp:.1f}\u2103\n"
        f"Status    : {status}"
    )


# ------------------------------------------------------------
# 전송 함수
# ------------------------------------------------------------
def send_alarm(
    image_path: str,
    temp: float,
    status: str,
    robot_id: str = "Robot-01",
) -> bool:
    """
    과열 알림 전송 (이미지 + 캡션).

    이미지가 없거나 전송 실패 시 텍스트만 전송합니다.
    환경변수가 없으면 콘솔에 dry-run 출력 후 True 반환.
    """
    caption = build_caption(temp, status, robot_id)

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

    _log.info("send_alarm: robot=%s status=%s temp=%.1fC image=%s",
              robot_id, status, temp, image_path)

    # 1. 이미지 + 캡션 전송 시도
    photo_sent = False
    if os.path.isfile(image_path):
        try:
            with open(image_path, "rb") as photo:
                resp = requests.post(
                    f"{TELEGRAM_API}/bot{BOT_TOKEN}/sendPhoto",
                    data={"chat_id": CHAT_ID, "caption": caption},
                    files={"photo": photo},
                    timeout=30,
                )
            if resp.status_code == 200:
                photo_sent = True
                _log.info("sendPhoto success: robot=%s temp=%.1f°C", robot_id, temp)
            else:
                _log.error("sendPhoto failed: HTTP %d %s", resp.status_code, resp.text)
                print(f"[Telegram] sendPhoto failed: {resp.status_code} {resp.text}")
        except Exception as e:
            _log.error("sendPhoto exception: %s", e)
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
            if resp.status_code == 200:
                photo_sent = True
            else:
                _log.error("sendMessage fallback failed: HTTP %d %s", resp.status_code, resp.text)
                print(f"[Telegram] sendMessage failed: {resp.status_code} {resp.text}")
        except Exception as e:
            _log.error("sendMessage exception: %s", e)
            print(f"[Telegram] sendMessage error")

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
