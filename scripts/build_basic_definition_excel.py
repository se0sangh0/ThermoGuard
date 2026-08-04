"""Build the ThermoGuard basic protocol definition in XLS, XLSX and CSV."""

from __future__ import annotations

import csv
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
CSV_OUTPUT = DOCS / "ThermoGuard_기본정의.csv"
XLS_OUTPUT = DOCS / "ThermoGuard_기본정의.xls"
XLSX_OUTPUT = DOCS / "ThermoGuard_기본정의.xlsx"
PROFILE = Path("/tmp/thermoguard-basic-definition-lo-profile")
PORT = 2097

HEADERS = ("항목", "값", "예제", "서버측")
ROWS = (
    ("메시지 종류", "REQ", "GET /api/temperature-trend?hours=24&limit=100", "클라이언트가 FastAPI 또는 카메라에 보내는 조회·저장·변경 요청"),
    ("메시지 종류", "RES", '{"count": 1, "points": [...] }', "REQ 처리 결과를 JSON 또는 이미지 바이너리로 응답"),
    ("메시지 종류", "EVT", 'status="critical", do_alarm=true', "Critical 상태 전환 및 ROI별 쿨다운 통과 시 alert_events 생성"),
    ("메시지 종류", "NOTI", "Telegram sendPhoto / sendMessage", "TelegramDispatcher가 Critical 알림을 전송하고 결과를 FastAPI에 기록"),
    ("데이터 구조", "JSON", '{"camera_id":1,"roi_id":1,"max_temp":58.4,"status":"critical"}', "FastAPI 요청·응답의 기본 구조, Content-Type: application/json"),
    ("데이터 구조", "Query Parameter", "hours=1|24|72|168, limit=100", "온도 추이·알림 이력의 기간 및 조회 건수 지정"),
    ("데이터 구조", "Path Parameter", "/api/alerts/{alert_id}", "특정 알림 또는 Threshold 식별자 지정"),
    ("데이터 구조", "multipart/form-data", "photo=<overlay.jpg>, caption=<알림 내용>", "Telegram Bot API sendPhoto 전송에 사용"),
    ("데이터 구조", "ISO 8601 / DB DATETIME(6)", "2026-08-04T10:30:15+09:00", "API 시각 표현 및 MariaDB 측정·발생·확인 시각 저장"),
    ("데이터 구조", "float / integer / boolean", "max_temp=58.4, roi_id=1, do_alarm=true", "온도는 실수, 식별자·픽셀 수는 정수, 알람 승인 여부는 boolean"),
    ("응답 코드", "200 OK", '{"status":"created","measurement_id":123}', "정상 조회·생성·변경. 현재 일부 업무 오류도 HTTP 200으로 반환되므로 status/error 확인 필요"),
    ("응답 코드", "400 Bad Request", '{"detail":"잘못된 요청"}', "요청 형식 또는 허용 범위가 잘못된 경우"),
    ("응답 코드", "404 Not Found", '{"detail":"Not Found"}', "등록되지 않은 API 경로 또는 리소스 요청"),
    ("응답 코드", "422 Unprocessable Entity", '{"detail":[{"loc":["body","roi_id"],"msg":"Field required"}]}', "FastAPI 입력 모델 검증 실패"),
    ("응답 코드", "500 Internal Server Error", '{"detail":"Internal Server Error"}', "처리되지 않은 서버·DB 예외. 로그와 DB 연결 상태 확인"),
    ("응답 코드", "업무 오류(JSON)", '{"status":"error","error":"적용 가능한 threshold profile이 없습니다."}', "현 구현의 일부 API는 HTTP 200과 함께 status=error를 반환"),
    ("이미지 종류", "Thermal Image", "YYYYMMDDHHMMSS_FFFFFF.jpg", "FLIR Radiometric JPEG. 온도 행렬 추출과 ROI 분석의 원본"),
    ("이미지 종류", "Visual Image", "YYYYMMDDHHMMSS_FFFFFF_visual.jpg", "FLIR 가시광 JPEG. 정상 모드에서 Thermal과 함께 촬영"),
    ("이미지 종류", "Overlay Image", "overlay/YYYYMMDDHHMMSS_FFFFFF_overlay.jpg", "ROI·핫스팟 결과를 합성한 대시보드 및 Telegram용 JPEG"),
    ("이미지 종류", "Thermal Matrix", "YYYYMMDDHHMMSS_FFFFFF_thermal.npy", "Radiometric JPEG에서 추출한 float32 섭씨 온도 행렬"),
    ("이미지 종류", "Telegram Photo", "sendPhoto: overlay.jpg", "Critical 알림 시 이미지와 캡션 전송, 실패 시 sendMessage로 폴백"),
)


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


def apply_border(cell_range):
    line = BorderLine2()
    line.Color = 0x5B6B73
    line.LineStyle = BorderLineStyle.SOLID
    line.LineWidth = 18
    cell_range.TopBorder = line
    cell_range.BottomBorder = line
    cell_range.LeftBorder = line
    cell_range.RightBorder = line


def write_csv():
    DOCS.mkdir(parents=True, exist_ok=True)
    with CSV_OUTPUT.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(HEADERS)
        writer.writerows(ROWS)


def build_spreadsheet():
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
        sheet = doc.getSheets().getByIndex(0)
        sheet.Name = "기본정의"

        sheet.getCellRangeByName("A1:D1").merge(True)
        title = sheet.getCellByPosition(0, 0)
        title.String = "ThermoGuard 기본정의"
        title.CharHeight = 18
        title.CharWeight = 150
        title.CharColor = 0x17365D
        title.HoriJustify = 2
        title.VertJustify = 2
        sheet.getRows().getByIndex(0).Height = 1100

        metadata = (
            ("프로젝트명", "ThermoGuard", "문서명", "기본정의"),
            ("작성일", date.today().isoformat(), "Version", "1.0"),
        )
        for row_index, values in enumerate(metadata, start=1):
            for column_index, value in enumerate(values):
                cell = sheet.getCellByPosition(column_index, row_index)
                cell.String = value
                if column_index in (0, 2):
                    cell.CellBackColor = 0xD9EAD3
                    cell.CharWeight = 150
                apply_border(cell)

        header_row = 4
        for column_index, value in enumerate(HEADERS):
            cell = sheet.getCellByPosition(column_index, header_row)
            cell.String = value
            cell.CharWeight = 150
            cell.CellBackColor = 0xB6D7A8
            cell.HoriJustify = 2
            cell.VertJustify = 2
            apply_border(cell)

        category_colors = {
            "메시지 종류": 0xD9EAF7,
            "데이터 구조": 0xFFF2CC,
            "응답 코드": 0xF4CCCC,
            "이미지 종류": 0xD9D2E9,
        }
        for row_offset, values in enumerate(ROWS, start=header_row + 1):
            for column_index, value in enumerate(values):
                cell = sheet.getCellByPosition(column_index, row_offset)
                cell.String = str(value)
                cell.IsTextWrapped = True
                cell.VertJustify = 2
                if column_index == 0:
                    cell.CellBackColor = category_colors[values[0]]
                    cell.CharWeight = 150
                    cell.HoriJustify = 2
                apply_border(cell)
            sheet.getRows().getByIndex(row_offset).OptimalHeight = True

        widths = (3800, 5200, 9800, 12000)
        for column_index, width in enumerate(widths):
            sheet.getColumns().getByIndex(column_index).Width = width
        sheet.getRows().getByIndex(header_row).Height = 650

        sheet.getCellRangeByName(f"A{header_row + 1}:D{header_row + len(ROWS) + 1}").IsTextWrapped = True
        sheet.createCursor().gotoEndOfUsedArea(True)

        doc.storeAsURL(
            uno.systemPathToFileUrl(str(XLS_OUTPUT)),
            (prop("FilterName", "MS Excel 97"), prop("Overwrite", True)),
        )
        doc.storeAsURL(
            uno.systemPathToFileUrl(str(XLSX_OUTPUT)),
            (prop("FilterName", "Calc MS Excel 2007 XML"), prop("Overwrite", True)),
        )
        doc.close(True)
    finally:
        process.terminate()
        process.wait(timeout=10)


def main():
    write_csv()
    build_spreadsheet()
    print(CSV_OUTPUT)
    print(XLS_OUTPUT)
    print(XLSX_OUTPUT)


if __name__ == "__main__":
    main()
