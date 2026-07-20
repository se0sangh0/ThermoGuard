"""
logger.py - 중앙 로깅 모듈 (Daily Rotating File Logger)

일자별 로그 파일(logs/YYYY-MM-DD.log)을 자동 생성하며,
모든 모듈에서 공유하는 스레드 안전 로거를 제공합니다.

사용법:
    from logger import get_logger
    log = get_logger("capture")
    log.info("Connected to camera")
    log.error("Connection timeout", exc_info=True)

로그 포맷:
    2026-07-20 14:32:15.123 [INFO ] [capture] message
    2026-07-20 14:32:20.456 [ERROR] [capture] Connection timeout
      Traceback (most recent call last):
        ...

설정:
    config.json에 monitoring.log_dir로 로그 디렉토리 지정 가능 (기본: logs/)
"""

import logging
import os
import sys
import threading
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from typing import Optional

_log_dir = "logs"
_lock = threading.Lock()
_loggers: dict[str, logging.Logger] = {}
_initialized = False


def _get_log_dir() -> str:
    try:
        from .config import load_config
        cfg_dir = load_config().monitoring.log_dir if hasattr(load_config().monitoring, 'log_dir') else ""
        return cfg_dir or "logs"
    except Exception:
        return "logs"


def _init_root_logger():
    """최초 1회만 실행되는 초기화. 루트 로거에 핸들러를 설정한다."""
    global _initialized
    if _initialized:
        return

    with _lock:
        if _initialized:
            return

        log_dir = _get_log_dir()
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "app.log")

        # 포매터: 타임스탬프, 레벨, 모듈명, 쓰레드명, 메시지
        formatter = logging.Formatter(
            fmt="%(asctime)s.%(msecs)03d [%(levelname)-5s] [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )

        # TimedRotatingFileHandler: 자정에 파일 롤링, 30일 보존
        file_handler = TimedRotatingFileHandler(
            filename=log_file,
            when="midnight",
            interval=1,
            backupCount=30,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        file_handler.setLevel(logging.DEBUG)

        # 콘솔 핸들러 (INFO 이상만 출력)
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        console_handler.setLevel(logging.INFO)

        # 루트 로거 설정
        root = logging.getLogger()
        root.setLevel(logging.DEBUG)
        root.handlers.clear()
        root.addHandler(file_handler)
        root.addHandler(console_handler)

        # 외부 라이브러리 DEBUG 로그 노이즈 제거
        for noisy in ["urllib3", "PIL", "requests"]:
            logging.getLogger(noisy).setLevel(logging.WARNING)

        _initialized = True


def get_logger(name: str) -> logging.Logger:
    """
    모듈별 로거를 반환.
    name 예시: "capture", "pipeline.monitor", "analysis.roi"
    """
    _init_root_logger()

    if name not in _loggers:
        with _lock:
            if name not in _loggers:
                logger = logging.getLogger(name)
                logger.setLevel(logging.DEBUG)
                # propagate=False로 중복 출력 방지
                logger.propagate = True
                _loggers[name] = logger

    return _loggers[name]
