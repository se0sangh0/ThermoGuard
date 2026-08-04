"""Build ThermoGuard REST message/topic definitions as XLS, XLSX and CSV."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import time
from datetime import date
from pathlib import Path

import uno
from com.sun.star.beans import PropertyValue
from com.sun.star.table import BorderLine2, BorderLineStyle


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
CSV_OUTPUT = DOCS / "ThermoGuard_메시지토픽_목록.csv"
FIELD_CSV_OUTPUT = DOCS / "ThermoGuard_메시지토픽_필드정의.csv"
XLS_OUTPUT = DOCS / "ThermoGuard_메시지토픽_정의_이미지폼.xls"
XLSX_OUTPUT = DOCS / "ThermoGuard_메시지토픽_정의_이미지폼.xlsx"
PROFILE = Path("/tmp/thermoguard-message-topic-lo-profile")
PORT = 2103

CATALOG_HEADERS = (
    "인터페이스명", "메시지명", "구분", "Method", "Topic / Path",
    "송신", "수신", "데이터형식", "버전", "설명",
)

CATALOG = (
    ("서버 상태 확인", "REQ_HEALTH_CHECK", "REQ", "GET", "/api/health", "GUI/운영자", "FastAPI", "Query 없음", "1.0", "FastAPI 실행 상태 확인"),
    ("서버 상태 응답", "RES_HEALTH_CHECK", "RES", "200", "/api/health", "FastAPI", "GUI/운영자", "JSON", "1.0", "server/device 상태 반환"),
    ("Thermal 이미지 요청", "REQ_THERMAL_IMAGE", "REQ", "GET", "/api/image/current?imgformat=JPEG", "CaptureSession", "FLIR A50", "HTTP", "1.0", "Radiometric Thermal JPEG 요청"),
    ("Thermal 이미지 응답", "RES_THERMAL_IMAGE", "RES", "200", "/api/image/current?imgformat=JPEG", "FLIR A50", "CaptureSession", "image/jpeg", "1.0", "온도 분석용 Thermal JPEG"),
    ("Visual 이미지 요청", "REQ_VISUAL_IMAGE", "REQ", "GET", "/api/image/current?imgformat=JPEG_visual", "CaptureSession", "FLIR A50", "HTTP", "1.0", "가시광 JPEG 요청"),
    ("Visual 이미지 응답", "RES_VISUAL_IMAGE", "RES", "200", "/api/image/current?imgformat=JPEG_visual", "FLIR A50", "CaptureSession", "image/jpeg", "1.0", "화면·Overlay용 Visual JPEG"),
    ("측정 이벤트 저장", "EVT_MEASUREMENT", "EVT", "POST", "/api/measurements", "TelegramDispatcher", "FastAPI", "JSON", "2.0", "ROI 측정·상태·do_alarm 저장"),
    ("측정 저장 응답", "RES_MEASUREMENT", "RES", "200", "/api/measurements", "FastAPI", "TelegramDispatcher", "JSON", "2.0", "capture/analysis/measurement/alert ID 반환"),
    ("온도 추이 요청", "REQ_TEMPERATURE_TREND", "REQ", "GET", "/api/temperature-trend", "ProductDashboard", "FastAPI", "Query", "1.0", "1시간·1일·3일·7일 최대온도 추이 조회"),
    ("온도 추이 응답", "RES_TEMPERATURE_TREND", "RES", "200", "/api/temperature-trend", "FastAPI", "ProductDashboard", "JSON", "1.0", "capture별 전체 ROI 최대온도 목록"),
    ("알림 목록 요청", "REQ_ALERT_LIST", "REQ", "GET", "/api/alerts", "ProductDashboard", "FastAPI", "Query", "1.0", "기간별 알림 이력 조회"),
    ("알림 목록 응답", "RES_ALERT_LIST", "RES", "200", "/api/alerts", "FastAPI", "ProductDashboard", "JSON", "1.0", "Critical alert_events와 확인 상태 반환"),
    ("알림 확인 요청", "REQ_ALERT_ACK", "REQ", "PATCH", "/api/alerts/{alert_id}", "ProductDashboard", "FastAPI", "JSON", "1.0", "선택 알림 acknowledged 처리"),
    ("알림 확인 응답", "RES_ALERT_ACK", "RES", "200", "/api/alerts/{alert_id}", "FastAPI", "ProductDashboard", "JSON", "1.0", "갱신된 event_status 반환"),
    ("Threshold 동기화", "REQ_THRESHOLD_SYNC", "REQ", "POST/PATCH", "/api/thresholds[/{threshold_id}]", "SettingsDialog", "FastAPI", "JSON", "1.0", "ROI별 기준·경고·위험 온도 저장"),
    ("Threshold 응답", "RES_THRESHOLD_SYNC", "RES", "200", "/api/thresholds[/{threshold_id}]", "FastAPI", "SettingsDialog", "JSON", "1.0", "생성·갱신 결과와 threshold_id 반환"),
    ("ROI 동기화", "REQ_ROI_SYNC", "REQ", "POST", "/api/rois", "SettingsDialog", "FastAPI", "JSON", "1.0", "ROI 좌표와 활성 상태 저장"),
    ("Critical 알림", "NOTI_CRITICAL_ALARM", "NOTI", "POST", "Telegram Bot API /sendPhoto", "TelegramDispatcher", "Telegram API", "multipart/form-data", "1.0", "Critical 전환·쿨다운 통과 시 이미지 알림"),
    ("Critical 텍스트 폴백", "NOTI_CRITICAL_TEXT", "NOTI", "POST", "Telegram Bot API /sendMessage", "TelegramDispatcher", "Telegram API", "JSON/Form", "1.0", "이미지 전송 실패 시 텍스트 알림"),
    ("알림 전달 결과", "NOTI_TELEGRAM_RESULT", "NOTI", "POST", "/api/notification-deliveries", "TelegramDispatcher", "FastAPI", "JSON", "1.0", "Telegram 성공·실패·재시도 결과 DB 저장"),
)

DETAIL_HEADERS = (
    "메시지명", "전송형식", "파라미터명", "파라미터ID", "데이터타입",
    "레벨", "필수", "예제", "데이터형식 / 서버측 설명",
)

DETAILS = (
    ("EVT_MEASUREMENT", "JSON Body", "카메라 ID", "camera_id", "Integer", 1, "Y", 1, "cameras.camera_id, ROI 소유 카메라와 일치해야 함"),
    ("EVT_MEASUREMENT", "JSON Body", "ROI ID", "roi_id", "Integer", 1, "Y", 1, "roi_definitions.roi_id, 활성 ROI만 허용"),
    ("EVT_MEASUREMENT", "JSON Body", "최소 온도", "min_temp", "Float", 1, "N", 31.2, "ROI 유효 픽셀 최소 섭씨 온도"),
    ("EVT_MEASUREMENT", "JSON Body", "최대 온도", "max_temp", "Float", 1, "Y", 58.4, "ROI 최대 섭씨 온도"),
    ("EVT_MEASUREMENT", "JSON Body", "평균 온도", "mean_temp", "Float", 1, "Y", 42.7, "ROI 평균 섭씨 온도"),
    ("EVT_MEASUREMENT", "JSON Body", "95백분위 온도", "percentile_95_temp", "Float", 1, "Y", 54.9, "핫스팟 판정에 사용"),
    ("EVT_MEASUREMENT", "JSON Body", "초과 픽셀 수", "over_temp_pixels", "Integer", 1, "N", 37, "경고 기준 초과 픽셀 수, 기본 0"),
    ("EVT_MEASUREMENT", "JSON Body", "최대 핫스팟 크기", "max_hotspot_size", "Integer", 1, "N", 18, "연결요소 기준 최대 클러스터 픽셀 수"),
    ("EVT_MEASUREMENT", "JSON Body", "상태", "status", "String", 1, "Y", "critical", "normal | warning | critical"),
    ("EVT_MEASUREMENT", "JSON Body", "알고리즘 버전", "algorithm_version", "String", 1, "N", "v2.0", "분석 알고리즘 버전"),
    ("EVT_MEASUREMENT", "JSON Body", "알람 승인", "do_alarm", "Boolean", 1, "N", True, "Critical 상태 전환+쿨다운 통과 시 true"),
    ("EVT_MEASUREMENT", "JSON Body", "알람 문구", "alarm_message", "String", 1, "N", "58.4°C · critical", "alert_events.message 저장"),
    ("RES_MEASUREMENT", "JSON Body", "처리 상태", "status", "String", 1, "Y", "created", "created | error"),
    ("RES_MEASUREMENT", "JSON Body", "촬영 ID", "capture_id", "Integer", 1, "Y", 6103, "captures.capture_id"),
    ("RES_MEASUREMENT", "JSON Body", "분석 ID", "analysis_id", "Integer", 1, "Y", 6103, "analysis_runs.analysis_id"),
    ("RES_MEASUREMENT", "JSON Body", "측정 ID", "measurement_id", "Integer", 1, "Y", 6103, "roi_measurements.measurement_id"),
    ("RES_MEASUREMENT", "JSON Body", "온도 상태", "temperature_status", "String", 1, "Y", "critical", "normal | warning | critical"),
    ("RES_MEASUREMENT", "JSON Body", "경고 온도", "warning_temp", "Float", 1, "Y", 50.0, "baseline_temp + warning_delta"),
    ("RES_MEASUREMENT", "JSON Body", "위험 온도", "critical_temp", "Float", 1, "Y", 60.0, "baseline_temp + critical_delta"),
    ("RES_MEASUREMENT", "JSON Body", "알림 ID", "alert_id", "Integer/null", 1, "N", 101, "do_alarm=true일 때만 alert_events.alert_id"),
    ("REQ_TEMPERATURE_TREND", "Query", "조회 시간", "hours", "Integer", 1, "Y", 24, "1 | 24 | 72 | 168시간"),
    ("REQ_TEMPERATURE_TREND", "Query", "최대 건수", "limit", "Integer", 1, "N", 1000, "1~150000 범위"),
    ("REQ_ALERT_LIST", "Query", "조회 시간", "hours", "Integer", 1, "Y", 168, "1 | 24 | 72 | 168시간"),
    ("REQ_ALERT_LIST", "Query", "최대 건수", "limit", "Integer", 1, "N", 5000, "서버 상한 5000"),
    ("REQ_ALERT_ACK", "Path", "알림 ID", "alert_id", "Integer", 1, "Y", 101, "alert_events.alert_id"),
    ("REQ_ALERT_ACK", "JSON Body", "이벤트 상태", "event_status", "String", 1, "Y", "acknowledged", "open | acknowledged | resolved"),
    ("REQ_THRESHOLD_SYNC", "JSON Body", "카메라 ID", "camera_id", "Integer", 1, "Y", 1, "Threshold 적용 카메라"),
    ("REQ_THRESHOLD_SYNC", "JSON Body", "ROI ID", "roi_id", "Integer", 1, "N", 1, "null이면 카메라 공통, 값이 있으면 ROI 전용"),
    ("REQ_THRESHOLD_SYNC", "JSON Body", "기준 온도", "baseline_temp", "Float", 1, "Y", 35.0, "정상 기준 섭씨 온도"),
    ("REQ_THRESHOLD_SYNC", "JSON Body", "경고 상승폭", "warning_delta", "Float", 1, "Y", 15.0, "경고 온도=baseline+warning_delta"),
    ("REQ_THRESHOLD_SYNC", "JSON Body", "위험 상승폭", "critical_delta", "Float", 1, "Y", 25.0, "위험 온도=baseline+critical_delta"),
    ("REQ_THRESHOLD_SYNC", "JSON Body", "알람 쿨다운", "alarm_cooldown_sec", "Integer", 1, "N", 600, "ROI별 Telegram 재알림 제한시간"),
    ("NOTI_TELEGRAM_RESULT", "JSON Body", "알림 ID", "alert_id", "Integer", 1, "Y", 101, "전송 대상 alert_events.alert_id"),
    ("NOTI_TELEGRAM_RESULT", "JSON Body", "전달 상태", "delivery_status", "String", 1, "Y", "success", "success | failed"),
    ("NOTI_TELEGRAM_RESULT", "JSON Body", "HTTP 상태", "http_status", "Integer", 1, "N", 200, "Telegram API HTTP 응답 코드"),
    ("NOTI_TELEGRAM_RESULT", "JSON Body", "재시도 횟수", "retry_count", "Integer", 1, "N", 0, "기본 0"),
    ("NOTI_TELEGRAM_RESULT", "JSON Body", "오류 내용", "error_message", "String", 1, "N", None, "실패 사유, 성공이면 null"),
)

EXAMPLES = {
    "EVT_MEASUREMENT": {
        "camera_id": 1, "roi_id": 1, "min_temp": 31.2, "max_temp": 58.4,
        "mean_temp": 42.7, "percentile_95_temp": 54.9,
        "over_temp_pixels": 37, "max_hotspot_size": 18,
        "status": "critical", "algorithm_version": "v2.0",
        "do_alarm": True, "alarm_message": "58.4°C · critical",
    },
    "REQ_ALERT_ACK": {"event_status": "acknowledged"},
    "RES_MEASUREMENT": {
        "status": "created", "capture_id": 6103, "analysis_id": 6103,
        "measurement_id": 6103, "temperature_status": "critical",
        "warning_temp": 50.0, "critical_temp": 60.0,
        "alert_id": 101, "do_alarm": True, "algorithm_version": "v2.0",
    },
    "REQ_THRESHOLD_SYNC": {
        "camera_id": 1, "roi_id": 1, "baseline_temp": 35.0,
        "warning_delta": 15.0, "critical_delta": 25.0,
        "min_hotspot_size": 3, "min_hotspot_size_max": 10,
        "alarm_cooldown_sec": 600,
    },
    "NOTI_TELEGRAM_RESULT": {
        "alert_id": 101, "delivery_status": "success", "http_status": 200,
        "retry_count": 0, "error_message": None,
    },
}


def prop(name, value):
    item = PropertyValue(); item.Name = name; item.Value = value
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


def border(cell_range):
    line = BorderLine2(); line.Color = 0x55626A
    line.LineStyle = BorderLineStyle.SOLID; line.LineWidth = 18
    cell_range.TopBorder = line; cell_range.BottomBorder = line
    cell_range.LeftBorder = line; cell_range.RightBorder = line


def set_cell(sheet, column, row, value, *, background=None, bold=False, center=False):
    cell = sheet.getCellByPosition(column, row)
    cell.String = "" if value is None else str(value)
    cell.IsTextWrapped = True; cell.VertJustify = 2
    if background is not None: cell.CellBackColor = background
    if bold: cell.CharWeight = 150
    if center: cell.HoriJustify = 2
    border(cell)
    return cell


def write_csv_files():
    DOCS.mkdir(parents=True, exist_ok=True)
    with CSV_OUTPUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle); writer.writerow(CATALOG_HEADERS); writer.writerows(CATALOG)
    with FIELD_CSV_OUTPUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle); writer.writerow(DETAIL_HEADERS); writer.writerows(DETAILS)


def add_title(sheet, title, subtitle, last_column):
    sheet.getCellRangeByPosition(0, 0, last_column, 0).merge(True)
    cell = sheet.getCellByPosition(0, 0); cell.String = title
    cell.CharHeight = 17; cell.CharWeight = 150; cell.CharColor = 0x17365D
    cell.HoriJustify = 2; cell.VertJustify = 2
    sheet.getRows().getByIndex(0).Height = 1000
    set_cell(sheet, 0, 1, "작성일", background=0xD9EAD3, bold=True, center=True)
    set_cell(sheet, 1, 1, date.today().isoformat())
    set_cell(sheet, 2, 1, "Version", background=0xD9EAD3, bold=True, center=True)
    set_cell(sheet, 3, 1, "1.0")
    if last_column >= 4:
        sheet.getCellRangeByPosition(4, 1, last_column, 1).merge(True)
        set_cell(sheet, 4, 1, subtitle)


def add_catalog_sheet(doc):
    sheet = doc.getSheets().getByIndex(0); sheet.Name = "00_메시지 목록"
    add_title(sheet, "ThermoGuard 메시지 토픽 정의", "HTTP REST 경로를 Topic 역할로 정의", 9)
    for col, header in enumerate(CATALOG_HEADERS):
        set_cell(sheet, col, 3, header, background=0xB6D7A8, bold=True, center=True)
    colors = {"REQ": 0xD9EAF7, "RES": 0xD9EAD3, "EVT": 0xFFF2CC, "NOTI": 0xF4CCCC}
    for row, values in enumerate(CATALOG, start=4):
        for col, value in enumerate(values):
            set_cell(sheet, col, row, value, background=colors[values[2]] if col == 2 else None,
                     bold=col in (0, 1), center=col in (2, 3, 8))
        sheet.getRows().getByIndex(row).OptimalHeight = True
    for col, width in enumerate((5200, 6200, 2200, 2600, 9000, 5000, 5000, 4500, 2200, 10000)):
        sheet.getColumns().getByIndex(col).Width = width


def add_category_sheet(doc, category, message_name):
    """Create the same two-part form shown in the supplied spreadsheet image."""
    sheets = doc.getSheets(); sheets.insertNewByName(category, len(sheets))
    sheet = sheets.getByName(category)
    add_title(sheet, f"ThermoGuard {category} 메시지 정의", "상단 목록 / 하단 전송형식", 9)

    category_rows = [row for row in CATALOG if row[2] == category]
    split_at = (len(category_rows) + 1) // 2
    left_rows, right_rows = category_rows[:split_at], category_rows[split_at:]
    for start_col in (0, 5):
        for offset, header in enumerate(("인터페이스명", "메시지명", "개정 버전")):
            set_cell(sheet, start_col + offset, 3, header,
                     background=0xB6D7A8, bold=True, center=True)
    for row_index, entry in enumerate(left_rows, start=4):
        for offset, value in enumerate((entry[0], entry[1], entry[8])):
            set_cell(sheet, offset, row_index, value, bold=offset == 1)
    for row_index, entry in enumerate(right_rows, start=4):
        for offset, value in enumerate((entry[0], entry[1], entry[8])):
            set_cell(sheet, 5 + offset, row_index, value, bold=offset == 1)

    detail_row = max(15, 5 + max(len(left_rows), len(right_rows)))
    endpoint = next(row[4] for row in CATALOG if row[1] == message_name)
    method = next(row[3] for row in CATALOG if row[1] == message_name)
    sheet.getCellRangeByPosition(0, detail_row, 1, detail_row).merge(True)
    set_cell(sheet, 0, detail_row, "전송형식", background=0x76D7EA, bold=True, center=True)
    for col, header in enumerate(("파라미터명", "파라미터ID", "데이터타입", "레벨", "필수", "데이터형식"), start=2):
        set_cell(sheet, col, detail_row, header, background=0x76D7EA, bold=True, center=True)

    fields = [row for row in DETAILS if row[0] == message_name]
    first_data_row = detail_row + 1
    last_data_row = first_data_row + max(1, len(fields)) - 1
    sheet.getCellRangeByPosition(0, first_data_row, 1, last_data_row).merge(True)
    example_text = (
        f"메시지명: {message_name}\n"
        f"Method: {method}\n"
        f"Topic / Path: {endpoint}\n\n"
        + json.dumps(EXAMPLES[message_name], ensure_ascii=False, indent=2)
    )
    set_cell(sheet, 0, first_data_row, example_text)
    sheet.getCellByPosition(0, first_data_row).VertJustify = 0

    for row_index, values in enumerate(fields, start=first_data_row):
        detail_values = (
            values[2], values[3], values[4], values[5], values[6],
            f"예제: {values[7]}\n{values[8]}",
        )
        for col, value in enumerate(detail_values, start=2):
            set_cell(sheet, col, row_index, value, center=col in (4, 5, 6))
        sheet.getRows().getByIndex(row_index).Height = 900

    note_row = last_data_row + 2
    sheet.getCellRangeByPosition(0, note_row, 7, note_row).merge(True)
    set_cell(sheet, 0, note_row,
             "※ ThermoGuard는 MQTT Broker를 사용하지 않으며 Topic / Path는 실제 HTTP REST 경로를 의미합니다.",
             background=0xFFF2CC)
    for col, width in enumerate((6500, 6500, 4700, 5000, 3600, 2000, 2200, 12000, 2500, 2500)):
        sheet.getColumns().getByIndex(col).Width = width


def build_excel():
    PROFILE.mkdir(parents=True, exist_ok=True)
    process = subprocess.Popen([
        "libreoffice", f"-env:UserInstallation=file://{PROFILE}", "--headless",
        "--nologo", "--nodefault", "--norestore",
        f"--accept=socket,host=127.0.0.1,port={PORT};urp;StarOffice.ServiceManager",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
       env={**os.environ, "HOME": "/tmp"})
    try:
        ctx = connect()
        desktop = ctx.ServiceManager.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)
        doc = desktop.loadComponentFromURL("private:factory/scalc", "_blank", 0, ())
        add_catalog_sheet(doc)
        for category, message in (
            ("REQ", "REQ_THRESHOLD_SYNC"),
            ("RES", "RES_MEASUREMENT"),
            ("EVT", "EVT_MEASUREMENT"),
            ("NOTI", "NOTI_TELEGRAM_RESULT"),
        ):
            add_category_sheet(doc, category, message)
        doc.storeAsURL(uno.systemPathToFileUrl(str(XLS_OUTPUT)),
                       (prop("FilterName", "MS Excel 97"), prop("Overwrite", True)))
        doc.storeAsURL(uno.systemPathToFileUrl(str(XLSX_OUTPUT)),
                       (prop("FilterName", "Calc MS Excel 2007 XML"), prop("Overwrite", True)))
        doc.close(True)
    finally:
        process.terminate(); process.wait(timeout=10)


def main():
    write_csv_files(); build_excel()
    for path in (CSV_OUTPUT, FIELD_CSV_OUTPUT, XLS_OUTPUT, XLSX_OUTPUT): print(path)


if __name__ == "__main__":
    main()
