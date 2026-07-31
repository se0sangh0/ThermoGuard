"""Build a readable multi-sheet Excel UI specification using LibreOffice UNO."""

from __future__ import annotations

import csv
import os
import subprocess
import time
from pathlib import Path

import uno
from com.sun.star.beans import PropertyValue
from com.sun.star.table import BorderLine2, BorderLineStyle


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "ThermoGuard_UI_명세서_테이블.csv"
OUTPUT = ROOT / "docs" / "ThermoGuard_UI_명세서_테이블_최신본.xlsx"
TEMP_OUTPUT = ROOT / "docs" / ".ThermoGuard_UI_명세서_테이블_생성중.xlsx"
PROFILE = Path("/tmp/thermoguard-ui-spec-lo-profile")
PORT = 2083
HEADERS = (
    "항목", "형태", "표시", "입력값", "목적", "동작",
    "데이터", "사용 함수", "자료형", "인터페이스",
)

FUNCTIONS = {
    "공통/화면 구분": "ProductDashboard._build_ui",
    "공통/시스템 상태": "ProductDashboard._set_system_state",
    "공통/마지막 갱신": "ProductDashboard._apply_analysis_result",
    "공통/API 안정성": "ProductDashboard._update_connection_stability_display",
    "헤더/화면 제목": "ProductDashboard._build_header",
    "제어/촬영 시작·정지": "ProductDashboard.toggle_capture",
    "제어/새로고침": "ProductDashboard.capture_and_refresh",
    "제어/운영 로그": "ProductDashboard.open_operating_log",
    "제어/환경설정": "ProductDashboard.open_settings",
    "영상/가시광 이미지": "ProductDashboard._show_image",
    "영상/열화상 이미지": "ProductDashboard._show_image",
    "영상/가시광 촬영 시각": "ProductDashboard._update_values_with_result",
    "영상/열화상 촬영 시각": "ProductDashboard._update_values_with_result",
    "하단/미확인 알림": "ProductDashboard.open_alert_history",
    "하단/온도 추이": "ProductDashboard._toggle_temperature_trend",
    "시작/환경설정 자동 표시": "미구현(명세 예정)",
    "시작/기존 설정으로 시작": "미구현(명세 예정)",
    "시작/앱 종료": "ProductDashboard.on_close",
    "환경설정/카메라 주소": "SettingsDialog.save",
    "환경설정/데이터 저장 폴더": "SettingsDialog._browse_dataset_dir",
    "환경설정/카메라 연결 상태": "ProductDashboard._connection_result",
    "환경설정/Backend 등록 상태": "SettingsDialog.save",
    "환경설정/연결 테스트": "ProductDashboard._check_connection_async",
    "감시영역/ROI 목록": "SettingsDialog.open_roi_editor",
    "감시영역/ROI 편집": "SettingsDialog.open_roi_editor",
    "감시영역/캘리브레이션": "SettingsDialog.open_calibration",
    "감시영역/ROI 동기화 확인": "SettingsDialog.save",
    "감시영역/캘리브레이션 상태": "SettingsDialog.open_calibration",
    "온도기준/정상 기준 온도": "SettingsDialog.save",
    "온도기준/경고 상승폭": "SettingsDialog.save",
    "온도기준/위험 상승폭": "SettingsDialog.save",
    "온도기준/최소 핫스팟 크기": "SettingsDialog.save",
    "온도기준/최대 핫스팟 판단 크기": "SettingsDialog.save",
    "온도기준/알람 재전송 제한시간": "SettingsDialog.save",
    "온도기준/경고 기준 미리보기": "미구현(명세 예정)",
    "온도기준/위험 기준 미리보기": "미구현(명세 예정)",
    "알림설정/Telegram 상태": "SettingsDialog._refresh_telegram_controls",
    "알림설정/알림 전송 상태": "SettingsDialog._refresh_telegram_controls",
    "알림설정/Bot Token": "SettingsDialog._login_telegram",
    "알림설정/Chat ID": "SettingsDialog._login_telegram",
    "알림설정/Telegram 로그인": "SettingsDialog._login_telegram",
    "알림설정/로그아웃": "SettingsDialog._logout_telegram",
    "알림설정/알림 활성화": "SettingsDialog._toggle_telegram_delivery",
    "알림설정/테스트 메시지": "미구현(명세 예정)",
    "고급설정/Backend URL": "SettingsDialog.save",
    "고급설정/Backend 사용": "SettingsDialog.save",
    "고급설정/API Timeout": "SettingsDialog.save",
    "고급설정/정상 촬영 주기": "SettingsDialog.save",
    "고급설정/경고 촬영 주기": "SettingsDialog.save",
    "고급설정/영상 모드": "SettingsDialog.save",
    "고급설정/데이터 보관 기간": "SettingsDialog.save",
    "환경설정/기본값 복원": "미구현(명세 예정)",
    "환경설정/취소": "SettingsDialog.close",
    "환경설정/저장 및 적용": "SettingsDialog.save",
    "환경설정/즉시 동기화": "ProductDashboard.apply_saved_settings_immediately",
    "환경설정/저장 결과": "SettingsDialog.save",
    "알림팝업/조회 범위": "ProductDashboard._render_alert_cards",
    "알림팝업/필터": "ProductDashboard._render_alert_cards",
    "알림팝업/새로고침": "ProductDashboard._refresh_alert_history",
    "알림팝업/알림 목록": "ProductDashboard._merge_backend_alerts",
    "알림팝업/선택 알림 확인 처리": "ProductDashboard._acknowledge_selected_alert",
    "그래프팝업/조회 범위": "ProductDashboard._sync_temperature_history",
    "그래프팝업/현재 상태 게이지": "ProductDashboard._draw_status_gauge",
    "그래프팝업/온도 그래프": "ProductDashboard._draw_temperature_trend",
    "그래프팝업/데이터 축약": "ProductDashboard._downsample_temperature_history",
    "그래프팝업/기준선": "ProductDashboard._draw_temperature_trend",
    "운영로그/로그 필터": "ProductDashboard.open_operating_log",
    "운영로그/로그 목록": "ProductDashboard._add_operating_log",
    "공통/팝업 중복 방지": "ProductDashboard.open_alert_history",
    "공통/백그라운드 작업": "ThreadPoolExecutor.submit",
    "공통/오류 메시지": "tkinter.messagebox.showerror",
    "데이터연동/로컬 설정 저장": "save_config",
    "데이터연동/설비 계층 조회": "asset_api_client._find_asset_hierarchy",
    "데이터연동/설비 계층 등록": "asset_api_client.register_asset_hierarchy",
    "데이터연동/ROI 조회·저장": "roi_api_client.sync_rois",
    "데이터연동/Threshold 조회·저장": "threshold_api_client.sync_threshold_profiles",
    "데이터연동/측정 결과 저장": "TelegramDispatcher.post_measurement",
    "데이터연동/알림 이력 조회": "ProductDashboard._sync_events_from_backend",
    "데이터연동/알림 확인 처리": "ProductDashboard._acknowledge_event_backend",
    "데이터연동/온도 이력 조회": "ProductDashboard._sync_temperature_history",
    "데이터연동/Telegram 사진 전송": "TelegramDispatcher._dispatch",
    "데이터연동/Telegram 전달 결과 저장": "save_delivery_result",
    "데이터연동/촬영 파일 저장": "CaptureSession",
    "데이터연동/캘리브레이션 저장": "calibration.run_calibration",
}

INTERFACES = {
    "공통/화면 구분": "로컬 Tkinter",
    "공통/시스템 상태": "로컬 분석 상태 → Tkinter",
    "공통/마지막 갱신": "로컬 datetime → Tkinter",
    "공통/API 안정성": "HTTP 호출 결과 누적 → Tkinter",
    "헤더/화면 제목": "로컬 고정 문자열 → Tkinter",
    "제어/촬영 시작·정지": "Tkinter command → CaptureSession",
    "제어/새로고침": "Tkinter command → 카메라 HTTP 촬영",
    "제어/운영 로그": "Tkinter Toplevel · 메모리 목록",
    "제어/환경설정": "Tkinter Toplevel",
    "영상/가시광 이미지": "카메라 HTTP JPEG → OpenCV/Pillow → Tkinter Canvas",
    "영상/열화상 이미지": "카메라 HTTP Radiometric JPEG → OpenCV → Tkinter Canvas",
    "영상/가시광 촬영 시각": "로컬 파일 시각 → Tkinter",
    "영상/열화상 촬영 시각": "로컬 파일 시각 → Tkinter",
    "하단/미확인 알림": "Tkinter command → GET /api/alerts?days=7",
    "하단/온도 추이": "Tkinter command → GET /api/temperature-trend?days=7",
    "시작/환경설정 자동 표시": "미구현 · 예정 Tkinter modal",
    "시작/기존 설정으로 시작": "미구현 · 예정 로컬 YAML 검증",
    "시작/앱 종료": "Tkinter WM_DELETE_WINDOW → 로컬 자원 종료",
    "환경설정/카메라 주소": "Tkinter 입력 → YAML + REST API → cameras",
    "환경설정/데이터 저장 폴더": "Tkinter 입력 → 로컬 파일시스템 + YAML",
    "환경설정/카메라 연결 상태": "HTTP GET 카메라 이미지 URL → Tkinter 상태",
    "환경설정/Backend 등록 상태": "REST JSON → factories/production_lines/robots/cameras",
    "환경설정/연결 테스트": "HTTP GET 카메라 이미지 URL",
    "감시영역/ROI 목록": "YAML + GET /api/rois → Tkinter/OpenCV",
    "감시영역/ROI 편집": "OpenCV UI → YAML + POST /api/rois",
    "감시영역/캘리브레이션": "OpenCV UI → 로컬 NPY 파일",
    "감시영역/ROI 동기화 확인": "GET /api/rois + POST /api/rois → roi_definitions",
    "감시영역/캘리브레이션 상태": "로컬 NPY 파일 → Tkinter",
    "온도기준/정상 기준 온도": "Tkinter 입력 → YAML + GET/PATCH/POST /api/thresholds",
    "온도기준/경고 상승폭": "Tkinter 입력 → YAML + GET/PATCH/POST /api/thresholds",
    "온도기준/위험 상승폭": "Tkinter 입력 → YAML + GET/PATCH/POST /api/thresholds",
    "온도기준/최소 핫스팟 크기": "미구현 UI · 설정값은 Threshold REST 필드로 지원",
    "온도기준/최대 핫스팟 판단 크기": "미구현 UI · 설정값은 Threshold REST 필드로 지원",
    "온도기준/알람 재전송 제한시간": "미구현 UI · 설정값은 Threshold REST 필드로 지원",
    "온도기준/경고 기준 미리보기": "미구현 · 예정 로컬 계산",
    "온도기준/위험 기준 미리보기": "미구현 · 예정 로컬 계산",
    "알림설정/Telegram 상태": "로컬 설정 → Telegram Bot API → Tkinter",
    "알림설정/알림 전송 상태": "Tkinter command → 로컬 notifier 설정",
    "알림설정/Bot Token": "Tkinter 비밀번호 입력 → Telegram Bot API",
    "알림설정/Chat ID": "Tkinter 입력 → Telegram Bot API",
    "알림설정/Telegram 로그인": "HTTPS Telegram Bot API getMe/sendMessage",
    "알림설정/로그아웃": "Tkinter command → 로컬 인증정보 제거",
    "알림설정/알림 활성화": "Tkinter command → 로컬 notifier 설정",
    "알림설정/테스트 메시지": "미구현 UI · notifier.send_text 사용 가능",
    "고급설정/Backend URL": "Tkinter 입력 → YAML → 이후 REST 기준 URL",
    "고급설정/Backend 사용": "Tkinter 체크 → YAML → REST 호출 활성/비활성",
    "고급설정/API Timeout": "Tkinter 입력 → YAML → requests timeout",
    "고급설정/정상 촬영 주기": "미구현 UI · config/cameras 필드는 지원",
    "고급설정/경고 촬영 주기": "미구현 UI · config/cameras 필드는 지원",
    "고급설정/영상 모드": "미구현 UI · config/cameras 필드는 지원",
    "고급설정/데이터 보관 기간": "미구현 UI · cleanup 설정 필드는 지원",
    "환경설정/기본값 복원": "미구현 · 통신 없음",
    "환경설정/취소": "Tkinter command · 통신 없음",
    "환경설정/저장 및 적용": "Tkinter → YAML + REST JSON + 런타임 함수",
    "환경설정/즉시 동기화": "로컬 함수 호출 + 비동기 재분석 + 카메라 HTTP 확인",
    "환경설정/저장 결과": "운영 로그 또는 Tkinter messagebox",
    "알림팝업/조회 범위": "GET /api/alerts?days=7&limit=5000",
    "알림팝업/필터": "로컬 메모리 필터 · 통신 없음",
    "알림팝업/새로고침": "HTTP GET /api/alerts?days=7&limit=5000 → alert_events",
    "알림팝업/알림 목록": "REST JSON → Tkinter Treeview",
    "알림팝업/선택 알림 확인 처리": "HTTP PATCH /api/alerts/{alert_id} → alert_events",
    "그래프팝업/조회 범위": "HTTP GET /api/temperature-trend?days=7 → roi_measurements",
    "그래프팝업/현재 상태 게이지": "로컬 분석 상태 → Tkinter Canvas",
    "그래프팝업/온도 그래프": "REST JSON + 실시간 메모리 → Tkinter Canvas",
    "그래프팝업/데이터 축약": "로컬 메모리 연산 · 통신 없음",
    "그래프팝업/기준선": "YAML Threshold → Tkinter Canvas",
    "운영로그/로그 목록": "로컬 메모리 + Python logging",
    "운영로그/로그 필터": "미구현 · 현재 전체 로컬 메모리 목록 표시",
    "공통/팝업 중복 방지": "Tkinter 창 참조 · 통신 없음",
    "공통/백그라운드 작업": "ThreadPoolExecutor → Tkinter root.after",
    "공통/오류 메시지": "예외/HTTP 오류 → Tkinter messagebox",
    "데이터연동/로컬 설정 저장": "로컬 YAML 파일 쓰기",
    "데이터연동/설비 계층 조회": "HTTP GET /api/cameras → cameras + 상위 JOIN",
    "데이터연동/설비 계층 등록": "HTTP POST /api/factories·production-lines·robots·cameras → 각 DB 테이블",
    "데이터연동/ROI 조회·저장": "HTTP GET/POST /api/rois → roi_definitions",
    "데이터연동/Threshold 조회·저장": "HTTP GET/POST/PATCH /api/thresholds → threshold_profiles",
    "데이터연동/측정 결과 저장": "HTTP POST /api/measurements → captures·analysis_runs·roi_measurements·alert_events",
    "데이터연동/알림 이력 조회": "HTTP GET /api/alerts?days=7 → alert_events",
    "데이터연동/알림 확인 처리": "HTTP PATCH /api/alerts/{alert_id} → alert_events",
    "데이터연동/온도 이력 조회": "HTTP GET /api/temperature-trend?days=7 → roi_measurements",
    "데이터연동/Telegram 사진 전송": "HTTPS POST Telegram Bot API sendPhoto",
    "데이터연동/Telegram 전달 결과 저장": "HTTP POST /api/notification-deliveries → notification_deliveries",
    "데이터연동/촬영 파일 저장": "카메라 HTTP → 로컬 JPG·NPY·overlay 파일",
    "데이터연동/캘리브레이션 저장": "OpenCV 대응점 → 로컬 Homography NPY",
}


def prop(name, value):
    item = PropertyValue()
    item.Name = name
    item.Value = value
    return item


def sheet_name(item: str) -> str:
    prefix = item.split("/", 1)[0]
    return {
        "공통": "01_메인·공통",
        "헤더": "01_메인·공통",
        "제어": "01_메인·공통",
        "영상": "01_메인·공통",
        "하단": "01_메인·공통",
        "시작": "02_시작 환경설정",
        "환경설정": "03_일반 환경설정",
        "감시영역": "04_ROI·캘리브레이션",
        "온도기준": "05_온도·감지 기준",
        "알림설정": "06_알림 전송",
        "고급설정": "07_고급 설정",
        "알림팝업": "08_알림 확인",
        "로봇팝업": "09_로봇 상태",
        "그래프팝업": "10_온도 그래프",
        "운영로그": "11_운영 로그",
        "데이터연동": "12_데이터 연동",
    }.get(prefix, "12_기타")


def connect():
    local_ctx = uno.getComponentContext()
    resolver = local_ctx.ServiceManager.createInstanceWithContext(
        "com.sun.star.bridge.UnoUrlResolver", local_ctx
    )
    for _ in range(50):
        try:
            return resolver.resolve(
                f"uno:socket,host=127.0.0.1,port={PORT};urp;StarOffice.ComponentContext"
            )
        except Exception:
            time.sleep(0.1)
    raise RuntimeError("LibreOffice UNO 연결에 실패했습니다.")


def style_sheet(doc, sheet, row_count: int, index: int):
    header = sheet.getCellRangeByPosition(0, 0, 9, 0)
    header.CharWeight = 150.0
    header.CharColor = 0xFFFFFF
    header.CellBackColor = 0x17365D
    header.HoriJustify = 2
    header.VertJustify = 2
    header.IsTextWrapped = True

    widths = (4200, 2600, 5000, 4200, 4800, 7200, 4300, 6000, 3000, 8500)
    for column_index, width in enumerate(widths):
        column = sheet.getColumns().getByIndex(column_index)
        column.Width = width

    line = BorderLine2()
    line.Color = 0xB8C4CE
    line.LineStyle = BorderLineStyle.SOLID
    line.LineWidth = 18

    if row_count > 1:
        body = sheet.getCellRangeByPosition(0, 1, 9, row_count - 1)
        body.IsTextWrapped = True
        body.VertJustify = 2
        body.TopBorder = line
        body.BottomBorder = line
        body.LeftBorder = line
        body.RightBorder = line

        for row in range(1, row_count):
            row_range = sheet.getCellRangeByPosition(0, row, 9, row)
            row_range.CellBackColor = 0xF3F7FA if row % 2 else 0xFFFFFF
            item_cell = sheet.getCellByPosition(0, row)
            item_cell.CharWeight = 150.0
            item_cell.CharColor = 0x17365D

    header.TopBorder = line
    header.BottomBorder = line
    header.LeftBorder = line
    header.RightBorder = line

    rows = sheet.getRows()
    rows.getByIndex(0).Height = 900
    for row in range(1, row_count):
        rows.getByIndex(row).OptimalHeight = True

    controller = doc.getCurrentController()
    controller.setActiveSheet(sheet)
    controller.freezeAtPosition(0, 1)

    db_ranges = doc.DatabaseRanges
    range_name = f"UI_SPEC_FILTER_{index}"
    db_ranges.addNewByName(
        range_name,
        sheet.getCellRangeByPosition(0, 0, 9, max(0, row_count - 1)).getRangeAddress(),
    )
    db_ranges.getByName(range_name).AutoFilter = True


def main():
    with SOURCE.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))

    grouped = {}
    for row in rows:
        grouped.setdefault(sheet_name(row["항목"]), []).append(row)
    ordered_groups = sorted(grouped.items(), key=lambda item: item[0])

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
        doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, ())
        sheets = doc.getSheets()
        first = sheets.getByIndex(0)

        for sheet_index, (name, sheet_rows) in enumerate(ordered_groups):
            if sheet_index == 0:
                first.setName(name)
                sheet = first
            else:
                sheets.insertNewByName(name, sheet_index)
                sheet = sheets.getByName(name)

            for col, title in enumerate(HEADERS):
                sheet.getCellByPosition(col, 0).String = title
            for row_index, row in enumerate(sheet_rows, start=1):
                for col, title in enumerate(HEADERS):
                    value = (
                        FUNCTIONS.get(row["항목"], "해당 없음")
                        if title == "사용 함수"
                        else INTERFACES.get(row["항목"], "로컬 Tkinter · 통신 없음")
                        if title == "인터페이스"
                        else row[title]
                    )
                    sheet.getCellByPosition(col, row_index).String = value

            style_sheet(doc, sheet, len(sheet_rows) + 1, sheet_index)

        sheets.moveByName(ordered_groups[0][0], 0)
        doc.getCurrentController().setActiveSheet(sheets.getByIndex(0))
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
