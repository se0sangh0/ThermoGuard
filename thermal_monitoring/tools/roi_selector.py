"""
roi_selector.py - GUI ROI 영역 설정 도구 (다중 ROI 지원)

사용법:
    python roi_selector.py [thermal_image.jpg]

Thermal 이미지를 띄우고 마우스 드래그로 ROI 영역을 지정합니다.
선택 완료 후 S 키를 누르면 config.json이 자동 업데이트됩니다.

조작:
    마우스 드래그 : ROI 영역 선택
    N             : 새 ROI 추가 (이름 입력)
    Tab / 숫자키  : 다음 ROI 선택 (순환)
    Del           : 선택된 ROI 삭제
    ESC / Q       : 종료 (변경사항 저장 안 함)
    S             : 모든 ROI를 config.json에 저장 후 종료
    R             : 모든 ROI 초기화
    Z             : 마지막 작업 취소 (Undo)
"""

import json
import os
import sys
import glob

import cv2
import numpy as np

from ..config import load_config, save_config, RoiEntry

cfg = load_config()
DATASET_DIR = cfg.paths.dataset_dir
DISPLAY_WIDTH = cfg.display.display_width

# 다중 ROI 상태
rois: list[dict] = []           # [{"name": str, "x1": int, "y1": int, "x2": int, "y2": int}, ...]
selected_idx: int = 0           # 현재 선택된 ROI 인덱스
# 현재 드래그 상태
roi_start = None
roi_end = None
dragging = False
scale = 1.0

ROI_COLORS = [
    (0, 255, 0),    # 초록
    (255, 0, 0),    # 파랑
    (0, 200, 255),  # 주황
    (255, 0, 255),  # 마젠타
    (0, 255, 255),  # 시안
    (128, 255, 0),  # 라임
    (255, 128, 0),  # 주황2
    (128, 0, 255),  # 보라
]


def resize_for_display(img, width):
    h, w = img.shape[:2]
    height = int(h * width / w)
    return cv2.resize(img, (width, height))


def mouse_callback(event, x, y, flags, param):
    global roi_start, roi_end, dragging, rois, selected_idx

    ox = int(x * scale)
    oy = int(y * scale)

    if event == cv2.EVENT_LBUTTONDOWN:
        roi_start = (ox, oy)
        roi_end = (ox, oy)
        dragging = True

    elif event == cv2.EVENT_MOUSEMOVE and dragging:
        roi_end = (ox, oy)

    elif event == cv2.EVENT_LBUTTONUP:
        dragging = False
        roi_end = (ox, oy)
        if roi_start and roi_end:
            x1 = min(roi_start[0], roi_end[0])
            y1 = min(roi_start[1], roi_end[1])
            x2 = max(roi_start[0], roi_end[0])
            y2 = max(roi_start[1], roi_end[1])
            if x2 - x1 > 5 and y2 - y1 > 5:
                if 0 <= selected_idx < len(rois):
                    rois[selected_idx] = {
                        "name": rois[selected_idx].get("name", "ROI"),
                        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    }
                else:
                    rois.append({
                        "name": f"ROI-{len(rois) + 1}",
                        "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    })
                    selected_idx = len(rois) - 1
                roi_start = None
                roi_end = None


def get_roi_box():
    """현재 ROI 박스 좌표 반환 (원본 기준), 없으면 None"""
    if roi_start and roi_end:
        x1 = min(roi_start[0], roi_end[0])
        y1 = min(roi_start[1], roi_end[1])
        x2 = max(roi_start[0], roi_end[0])
        y2 = max(roi_start[1], roi_end[1])
        if x2 - x1 > 2 and y2 - y1 > 2:
            return (x1, y1, x2, y2)
    return None


def load_existing_rois():
    """config.json에서 모든 저장된 ROI 불러오기"""
    global rois, selected_idx
    c = load_config()
    rois.clear()
    if c.roi.rois:
        rois.extend(c.roi.rois)
    elif None not in (c.roi.x1, c.roi.y1, c.roi.x2, c.roi.y2):
        # 하위 호환: 구버전 단일 ROI → 리스트로 변환
        rois.append({
            "name": "ROI-1",
            "x1": c.roi.x1, "y1": c.roi.y1,
            "x2": c.roi.x2, "y2": c.roi.y2,
        })
    selected_idx = 0 if rois else -1
    return len(rois)


def save_all_rois():
    """config.json에 모든 ROI 저장"""
    c = load_config(force_reload=True)
    entries = []
    for r in rois:
        entries.append({
            "name": r["name"],
            "x1": int(r["x1"]), "y1": int(r["y1"]),
            "x2": int(r["x2"]), "y2": int(r["y2"]),
        })
    c.roi.rois = entries
    # 첫 번째 ROI로 하위 호환 좌표도 유지
    if entries:
        c.roi.x1 = entries[0]["x1"]
        c.roi.y1 = entries[0]["y1"]
        c.roi.x2 = entries[0]["x2"]
        c.roi.y2 = entries[0]["y2"]
    save_config(c)
    print(f"[roi_selector] {len(entries)} ROI(s) saved to config.json:")
    for e in entries:
        print(f"  {e['name']}: ({e['x1']},{e['y1']})-({e['x2']},{e['y2']})")


def _get_color(idx: int) -> tuple:
    return ROI_COLORS[idx % len(ROI_COLORS)]


def main():
    global scale, selected_idx, rois, roi_start, roi_end, dragging

    if len(sys.argv) >= 2:
        img_path = sys.argv[1]
    else:
        jpg_files = sorted(glob.glob(os.path.join(DATASET_DIR, "*.jpg")))
        thermal_files = [f for f in jpg_files if "_visual" not in f]
        img_path = thermal_files[-1] if thermal_files else None

    if img_path is None:
        print("Thermal 이미지를 찾을 수 없습니다.")
        print("사용법: python roi_selector.py [thermal_image.jpg]")
        sys.exit(1)

    img = cv2.imread(img_path)
    if img is None:
        print(f"이미지를 불러올 수 없습니다: {img_path}")
        sys.exit(1)

    print(f"Loaded: {img_path}  ({img.shape[1]}x{img.shape[0]})")
    print("  Drag mouse to set ROI for selected area")
    print("  N = new ROI    Tab = next    Del = delete    S = save & exit")
    print("  R = reset all    Z = undo    Q = quit without saving")

    count = load_existing_rois()
    if count > 0:
        print(f"  Loaded {count} existing ROI(s): {[r['name'] for r in rois]}")

    img_disp = resize_for_display(img, DISPLAY_WIDTH)
    scale = img.shape[1] / img_disp.shape[1]

    cv2.namedWindow("ROI Selector - Thermal Image")
    cv2.setMouseCallback("ROI Selector - Thermal Image", mouse_callback)

    while True:
        disp = img_disp.copy()

        # 모든 ROI 박스 그리기
        for i, r in enumerate(rois):
            color = _get_color(i)
            thickness = 3 if i == selected_idx else 1
            dx1 = int(r["x1"] / scale)
            dy1 = int(r["y1"] / scale)
            dx2 = int(r["x2"] / scale)
            dy2 = int(r["y2"] / scale)
            cv2.rectangle(disp, (dx1, dy1), (dx2, dy2), color, thickness)
            label = f"{r['name']} {'◀' if i == selected_idx else ''}"
            cv2.putText(disp, label, (dx1, dy1 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

        # 현재 드래그 중인 박스
        box = get_roi_box()
        if box:
            x1, y1, x2, y2 = box
            dx1 = int(x1 / scale)
            dy1 = int(y1 / scale)
            dx2 = int(x2 / scale)
            dy2 = int(y2 / scale)
            color = _get_color(selected_idx) if 0 <= selected_idx < len(rois) else _get_color(len(rois))
            cv2.rectangle(disp, (dx1, dy1), (dx2, dy2), color, 2)
            w, h = x2 - x1, y2 - y1
            cv2.putText(disp, f"{w}x{h}", (dx1, dy1 - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

        # 상태 텍스트
        status_lines = [
            f"Selected: {rois[selected_idx]['name'] if 0 <= selected_idx < len(rois) else '(none)'}",
            f"ROIs: {len(rois)} | N: new  Tab: next  Del: delete  S: save  Q: quit",
        ]
        for i, line in enumerate(status_lines):
            cv2.putText(disp, line, (10, 20 + i * 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)

        cv2.imshow("ROI Selector - Thermal Image", disp)
        key = cv2.waitKey(1)

        if key == ord("q") or key == 27:
            print("Quit without saving.")
            break

        elif key == ord("n"):
            import tkinter.simpledialog as sd
            import tkinter as tk
            root = tk.Tk()
            root.withdraw()
            name = sd.askstring("New ROI", "ROI name:", parent=root)
            root.destroy()
            if name:
                rois.append({"name": name, "x1": 0, "y1": 0, "x2": 640, "y2": 480})
                selected_idx = len(rois) - 1
                print(f"Added '{name}' (select it and drag to set bounds)")

        elif key == 9:  # Tab
            if rois:
                selected_idx = (selected_idx + 1) % len(rois)
                print(f"Selected: {rois[selected_idx]['name']}")

        elif key == 127 or key == 8:  # Del or Backspace
            if 0 <= selected_idx < len(rois):
                name = rois[selected_idx]["name"]
                del rois[selected_idx]
                if rois:
                    selected_idx = min(selected_idx, len(rois) - 1)
                else:
                    selected_idx = -1
                print(f"Deleted '{name}'")

        elif key == ord("r"):
            rois.clear()
            selected_idx = -1
            roi_start = None
            roi_end = None
            dragging = False
            print("All ROIs reset.")

        elif key == ord("z"):
            if len(rois) > 0:
                name = rois[-1]["name"]
                rois.pop()
                selected_idx = min(selected_idx, len(rois) - 1) if rois else -1
                print(f"Undo: removed '{name}'")
            else:
                print("Nothing to undo.")

        elif key == ord("s"):
            if rois:
                save_all_rois()
                break
            else:
                print("No ROI defined. Add at least one ROI with N key or drag on the image.")

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
