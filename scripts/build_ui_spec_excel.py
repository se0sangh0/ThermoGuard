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
PROFILE = Path("/tmp/thermoguard-ui-spec-lo-profile")
PORT = 2083
HEADERS = ("항목", "형태", "표시", "입력값", "목적", "동작", "데이터", "자료형")


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
    header = sheet.getCellRangeByPosition(0, 0, 7, 0)
    header.CharWeight = 150.0
    header.CharColor = 0xFFFFFF
    header.CellBackColor = 0x17365D
    header.HoriJustify = 2
    header.VertJustify = 2
    header.IsTextWrapped = True

    widths = (4200, 2600, 5000, 4200, 4800, 7200, 4300, 3000)
    for column_index, width in enumerate(widths):
        column = sheet.getColumns().getByIndex(column_index)
        column.Width = width

    line = BorderLine2()
    line.Color = 0xB8C4CE
    line.LineStyle = BorderLineStyle.SOLID
    line.LineWidth = 18

    if row_count > 1:
        body = sheet.getCellRangeByPosition(0, 1, 7, row_count - 1)
        body.IsTextWrapped = True
        body.VertJustify = 2
        body.TopBorder = line
        body.BottomBorder = line
        body.LeftBorder = line
        body.RightBorder = line

        for row in range(1, row_count):
            row_range = sheet.getCellRangeByPosition(0, row, 7, row)
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
        sheet.getCellRangeByPosition(0, 0, 7, max(0, row_count - 1)).getRangeAddress(),
    )
    db_ranges.getByName(range_name).AutoFilter = True


def main():
    with SOURCE.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))

    grouped = {}
    for row in rows:
        grouped.setdefault(sheet_name(row["항목"]), []).append(row)

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

        for sheet_index, (name, sheet_rows) in enumerate(grouped.items()):
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
                    sheet.getCellByPosition(col, row_index).String = row[title]

            style_sheet(doc, sheet, len(sheet_rows) + 1, sheet_index)

        sheets.moveByName(next(iter(grouped)), 0)
        doc.getCurrentController().setActiveSheet(sheets.getByIndex(0))
        doc.storeAsURL(
            uno.systemPathToFileUrl(str(OUTPUT)),
            (
                prop("FilterName", "Calc MS Excel 2007 XML"),
                prop("Overwrite", True),
            ),
        )
        doc.close(True)
    finally:
        process.terminate()
        process.wait(timeout=10)


if __name__ == "__main__":
    main()
