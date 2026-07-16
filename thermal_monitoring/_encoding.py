"""
_encoding.py - Windows UTF-8 인코딩 설정 (내부 유틸)

Windows 환경에서 stdout/stderr 인코딩이 cp949일 때
UTF-8로 강제 전환합니다. 타입 체커 경고를 피하기 위해
reconfigure 대신 TextIOWrapper로 감싸는 방식을 사용합니다.
"""

import io
import sys


def setup_encoding() -> None:
    if sys.platform == "win32":
        if sys.stdout.encoding != "utf-8":
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
        if sys.stderr.encoding != "utf-8":
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
