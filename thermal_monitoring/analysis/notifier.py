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

import requests

# ------------------------------------------------------------
# .env 파일 로드 (python-dotenv 없이 직접 파싱)
# ------------------------------------------------------------
def _load_dotenv(dotenv_path: str = ".env") -> None:
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

TELEGRAM_API = "https://api.telegram.org"


def _is_configured() -> bool:
    return bool(BOT_TOKEN and CHAT_ID)


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
        raise RuntimeError("BOT_TOKEN and CHAT_ID not configured. Set them in .env file.")

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
            else:
                print(f"[Telegram] sendPhoto failed: {resp.status_code} {resp.text}")
        except Exception:
            print(f"[Telegram] sendPhoto error - falling back to text")
    else:
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
                print(f"[Telegram] sendMessage failed: {resp.status_code} {resp.text}")
        except Exception:
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
