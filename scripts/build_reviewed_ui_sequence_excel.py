"""Create a corrected XLSX copy of the user-provided UI specification."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import uno
from com.sun.star.beans import PropertyValue


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("/home/autoit/Downloads/UI명세서.xls")
OUTPUT = ROOT / "docs" / "UI명세서_교정본.xlsx"
TEMP_OUTPUT = ROOT / "docs" / ".UI명세서_교정본_생성중.xlsx"
PROFILE = Path("/tmp/thermoguard-reviewed-ui-spec-profile")
PORT = 2091


def prop(name, value):
    item = PropertyValue()
    item.Name = name
    item.Value = value
    return item


def connect():
    local_ctx = uno.getComponentContext()
    resolver = local_ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_ctx
    )
    for _ in range(80):
        try:
            return resolver.resolve(
                f"uno:socket,host=127.0.0.1,port={PORT};urp;StarOffice.ComponentContext"
            )
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("LibreOffice UNO 연결에 실패했습니다.")


def set_row(sheet, row_number: int, values: tuple[str, ...]):
    row = row_number - 1
    for column, value in enumerate(values):
        sheet.getCellByPosition(column, row).String = value


def main():
    if not SOURCE.exists():
        raise FileNotFoundError(SOURCE)

    PROFILE.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen(
        [
            "libreoffice",
            f"-env:UserInstallation=file://{PROFILE}",
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            f"--accept=socket,host=127.0.0.1,port={PORT};urp;StarOffice.ServiceManager",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, "HOME": "/tmp"},
    )

    try:
        ctx = connect()
        desktop = ctx.ServiceManager.createInstanceWithContext(
            "com.sun.star.frame.Desktop", ctx
        )
        doc = desktop.loadComponentFromURL(
            uno.systemPathToFileUrl(str(SOURCE)),
            "_blank",
            0,
            (prop("Hidden", True),),
        )
        sheet = doc.getSheets().getByName("UI명세서")

        # 기존 표의 모호하거나 현재 코드와 다른 부분을 교정한다.
        set_row(sheet, 4, ("", "", "상태 및 API 연결 안정성", "카메라·Backend 연결 품질 확인", "카메라·Backend HTTP >> GUI", "비동기 · 주기(30초) + 연결 이벤트", "HTTP 성공률·Timeout·4xx·5xx와 전체 분석 상태를 표시"))
        set_row(sheet, 5, ("", "", "카메라 연결 여부", "카메라 촬영 가능 상태 확인", "카메라 HTTP >> GUI", "비동기 · 시작/설정 저장/재연결", "헤더 시스템 상태와 API 안정성 표시에 통합"))
        set_row(sheet, 6, ("", "", "가시광 이미지", "최신 가시광 촬영 영상 확인", "카메라 >> 로컬 파일 >> GUI", "촬영: 정상 30초/과열 5초 · GUI 반영: 30초", "최신 유효 촬영 쌍의 가시광 이미지를 표시"))
        set_row(sheet, 7, ("", "", "열화상 이미지", "ROI·핫스팟·온도 분포 확인", "카메라 >> 로컬 분석 >> GUI", "촬영: 정상 30초/과열 5초 · GUI 반영: 30초", "최신 열화상에 ROI·핫스팟 분석 오버레이를 표시"))
        set_row(sheet, 8, ("", "", "가시광 이미지 촬영 시각", "가시광 영상 촬영 시점 확인", "로컬 촬영 시각 >> GUI", "비동기 · 유효 분석 결과 적용 시", "최신 유효 촬영 쌍의 시각으로 갱신"))
        set_row(sheet, 9, ("", "", "열화상 이미지 촬영 시각", "열화상 촬영 시점 확인", "로컬 촬영 시각 >> GUI", "비동기 · 유효 분석 결과 적용 시", "최신 유효 촬영 쌍의 시각으로 갱신"))
        set_row(sheet, 10, ("", "", "촬영 정지/시작 버튼", "자동 촬영 제어", "GUI >> CaptureSession >> 카메라", "비동기 · 비주기(사용자 요청)", "촬영 중이면 정지하고 정지 상태면 연결 확인 후 재시작"))
        set_row(sheet, 11, ("", "", "새로고침 버튼", "즉시 재촬영·분석", "GUI >> 카메라 >> 로컬 분석", "비동기 · 비주기(사용자 요청)", "Thermal·Visual을 즉시 촬영하고 이미지·시각·분석 결과를 갱신"))
        set_row(sheet, 12, ("", "", "미확인 알림 버튼", "알림·확인 상태 조회", "GUI >> FastAPI >> alert_events", "비동기 · 열기/기간 변경/새로고침", "1시간·1일·3일·7일을 선택해 알림을 조회하고 선택 알림을 확인 처리"))
        set_row(sheet, 13, ("", "", "온도 추이 버튼", "현재 상태·온도 그래프 확인", "GUI >> FastAPI >> roi_measurements", "비동기 · 열기/기간 변경 + 30초 동기화", "1시간·1일·3일·7일 선택 기간의 과거 DB 기록과 실시간 측정값을 병합해 게이지·그래프 표시"))
        set_row(sheet, 14, ("세팅(환경설정)", "일반", "카메라 주소", "FLIR 카메라 연결 대상 설정", "GUI >> config.json·FastAPI >> cameras", "동기 · 비주기(설정 저장)", "저장 시 카메라 주소를 config.json과 DB 카메라 식별 정보에 반영하고 연결을 재확인"))
        set_row(sheet, 15, ("", "", "데이터 저장 폴더", "촬영·온도·오버레이 저장 위치 설정", "GUI >> config.json·로컬 파일시스템", "동기 · 비주기(설정 저장)", "폴더 생성·쓰기 가능 여부를 확인하고 캡처 세션의 저장 위치를 즉시 변경"))
        set_row(sheet, 16, ("", "감시영역", "ROI 설정", "감시 설비 영역 지정", "OpenCV GUI >> config.json·FastAPI >> roi_definitions", "동기 · 비주기(ROI 저장)", "가시광 이미지에서 ROI를 선택하고 변환된 좌표를 로컬 설정과 DB에 동기화"))
        set_row(sheet, 17, ("", "", "캘리브레이션", "Thermal·Visual 좌표계 보정", "OpenCV GUI >> Homography NPY", "동기 · 비주기(설정 저장)", "두 영상의 대응점으로 Homography를 계산해 로컬 NPY 파일에 저장"))
        set_row(sheet, 18, ("", "고급설정", "정상 기준 온도", "정상 판정 기준값 설정", "GUI >> config.json·FastAPI >> threshold_profiles", "동기 · 비주기(설정 저장)", "저장 즉시 ROI별 Threshold·그래프 기준선·게이지·최신 재분석에 반영"))
        set_row(sheet, 19, ("", "", "경고 상승폭", "Warning 판정 상대 기준 설정", "GUI >> config.json·FastAPI >> threshold_profiles", "동기 · 비주기(설정 저장)", "정상 기준+경고 상승폭을 저장하고 기준선·게이지·최신 재분석에 즉시 반영"))
        set_row(sheet, 20, ("", "", "위험 상승폭", "Critical 판정 상대 기준 설정", "GUI >> config.json·FastAPI >> threshold_profiles", "동기 · 비주기(설정 저장)", "정상 기준+위험 상승폭을 저장하고 기준선·게이지·최신 재분석에 즉시 반영"))
        set_row(sheet, 21, ("", "알림 전송 설정", "Telegram Bot 로그인", "Bot Token·Chat ID 검증", "GUI >> Telegram Bot API·로컬 .env", "비동기 · 비주기(사용자 요청)", "Telegram 사용자 계정 로그인이 아니라 Bot Token·Chat ID를 Telegram API로 검증하고 로컬 저장"))
        set_row(sheet, 22, ("", "", "Telegram Bot 로그아웃", "저장된 Bot 인증정보 제거", "GUI >> 로컬 .env", "동기 · 비주기(사용자 요청)", "Bot Token·Chat ID를 제거하고 Telegram 알림 전송을 비활성화"))
        set_row(sheet, 23, ("", "", "알림 전송 활성화", "Critical Telegram 알림 사용 여부", "GUI >> 로컬 notifier 설정", "동기 · 비주기(사용자 요청)", "Bot 인증정보가 있을 때 Critical 알림 전송을 활성화·비활성화"))
        set_row(sheet, 24, ("", "저장", "환경설정 저장", "설정 저장·DB 동기화·런타임 즉시 적용", "GUI >> config.json·FastAPI·ProductDashboard", "동기 저장 + 비동기 재분석", "설비 계층·Threshold를 DB와 동기화하고 그래프·게이지를 갱신한 뒤 최신 데이터를 재분석"))
        set_row(sheet, 25, ("운영 로그", "팝업", "운영 로그 목록", "앱 실행 중 동작·오류 확인", "런타임 메모리 >> GUI", "동기 · 비주기(팝업 열기)", "DB가 아닌 self.operating_logs 메모리의 최근 1,000건을 표시하며 앱 재시작 시 초기화"))
        set_row(sheet, 26, ("DB 연동", "측정·알림", "측정·알림·전달 결과 저장", "운영 이력 영구 보관", "GUI/Dispatcher >> FastAPI >> MySQL", "비동기 · 측정/알림 발생 시", "POST /api/measurements로 측정·alert_events를 저장하고 Telegram 결과는 POST /api/notification-deliveries로 저장"))

        used = sheet.getCellRangeByName("A1:G26")
        used.IsTextWrapped = True
        used.VertJustify = 2
        for row in range(1, 26):
            sheet.getRows().getByIndex(row).OptimalHeight = True

        doc.storeAsURL(
            uno.systemPathToFileUrl(str(TEMP_OUTPUT)),
            (
                prop("FilterName", "Calc MS Excel 2007 XML"),
                prop("Overwrite", True),
            ),
        )
        doc.close(True)
        os.replace(TEMP_OUTPUT, OUTPUT)
    finally:
        process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    main()
