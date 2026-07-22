"""
roi_selector.py - GUI ROI 영역 설정 도구 (다중 ROI 지원, Homography 연동, 버튼 UI + API)

사용법:
    python roi_selector.py [thermal_image.jpg]
    python roi_selector.py [thermal_image.jpg] [visual_image.jpg]

열화상 이미지를 띄우고 마우스 드래그로 ROI 영역을 지정합니다.
thermal_to_rgb.npy (Homography)가 존재하고 가시광 이미지가 있으면
가시광 이미지 위에서 ROI를 그리고, 저장 시 열화상 좌표(640×480)로 자동 변환합니다.

API 사용 (다른 모듈에서 import):
    from tools.roi_selector import (
        add_new_roi, select_next_roi, delete_selected_roi,
        reset_all_rois, undo_last_action, save_and_exit, quit_without_save,
        rois, selected_idx, is_running,
    )

조작:
    마우스 드래그 : ROI 영역 선택
    하단 버튼 클릭 : New / Next / Del / Undo / Reset / Save / Quit
    단축키        : N(New)  Tab(Next)  Del(삭제)  Z(Undo)  R(Reset)  S(Save)  Q(종료)
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

# ── UI 상수 ──
BUTTON_BAR_HEIGHT = 44

# ── 다중 ROI 상태 ──
rois: list[dict] = []           # [{"name": str, "x1": int, "y1": int, "x2": int, "y2": int}, ...]
selected_idx: int = 0           # 현재 선택된 ROI 인덱스
roi_start = None
roi_end = None
dragging = False
scale = 1.0
# ── Homography / Visual 모드 ──
H_inv = None                    # inverse homography (visual→thermal)
use_visual = False              # 가시광 이미지 표시 중이면 True
thermal_resolution = (640, 480) # 최종 저장 좌표계 기준
# ── 실행 상태 ──
_running = False                # main loop 실행 중 여부
_quit_flag = False              # Q / ESC (저장 없이 종료)
_save_flag = False              # S (저장 후 종료)

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

# ── 버튼 정의 (label, RGB_color, action_key) ──
_BUTTONS = [
    ("New",   (50, 180, 50),  "new"),
    ("Next",  (50, 100, 180), "next"),
    ("Del",   (50, 50, 200),  "delete"),
    ("Undo",  (160, 160, 50), "undo"),
    ("Reset", (180, 80, 50),  "reset"),
    ("Save",  (50, 160, 50),  "save"),
    ("Quit",  (50, 50, 180),  "quit"),
]

# 런타임에 계산됨
_button_rects: list[tuple[int, int, int, int]] = []  # (x1, y1, x2, y2)
_canvas_h = 0  # 전체 캔버스 높이 (이미지 + 버튼 바)


# ───────────────────────────────────────────────────────────────
#  공개 API 함수 (다른 모듈에서 import 가능)
# ───────────────────────────────────────────────────────────────

def is_running() -> bool:
    """main loop 실행 중인지 여부"""
    return _running


def add_new_roi(name: str = "") -> int:
    """새 ROI 추가. name이 비어있으면 다이얼로그로 입력받음. 새 인덱스 반환 (-1이면 취소)."""
    global rois, selected_idx
    if not name:
        import tkinter.simpledialog as sd
        import tkinter as tk
        root = tk.Tk()
        root.withdraw()
        name = sd.askstring("New ROI", "ROI name:", parent=root)
        root.destroy()
        if not name:
            return -1
    rois.append({"name": name, "x1": 0, "y1": 0, "x2": 640, "y2": 480})
    selected_idx = len(rois) - 1
    print(f"Added '{name}' (select it and drag to set bounds)")
    return selected_idx


def select_next_roi() -> int:
    """다음 ROI 선택 (순환). 새 인덱스 반환."""
    global selected_idx
    if rois:
        selected_idx = (selected_idx + 1) % len(rois)
        print(f"Selected: {rois[selected_idx]['name']}")
    return selected_idx


def delete_selected_roi() -> int:
    """현재 선택된 ROI 삭제. 새 인덱스 반환 (-1이면 없음)."""
    global selected_idx
    if 0 <= selected_idx < len(rois):
        name = rois[selected_idx]["name"]
        del rois[selected_idx]
        if rois:
            selected_idx = min(selected_idx, len(rois) - 1)
        else:
            selected_idx = -1
        print(f"Deleted '{name}'")
    return selected_idx


def reset_all_rois() -> None:
    """모든 ROI 초기화."""
    global rois, selected_idx, roi_start, roi_end, dragging
    rois.clear()
    selected_idx = -1
    roi_start = None
    roi_end = None
    dragging = False
    print("All ROIs reset.")


def undo_last_action() -> int:
    """마지막 ROI 제거 (Undo). 새 인덱스 반환."""
    global rois, selected_idx
    if len(rois) > 0:
        name = rois[-1]["name"]
        rois.pop()
        selected_idx = min(selected_idx, len(rois) - 1) if rois else -1
        print(f"Undo: removed '{name}'")
    else:
        print("Nothing to undo.")
    return selected_idx


def save_and_exit() -> bool:
    """모든 ROI 저장 후 종료 플래그 설정. 성공 여부 반환."""
    global _save_flag
    if rois:
        _save_all_rois()
        _save_flag = True
        return True
    print("No ROI defined. Add at least one ROI with New button or drag on the image.")
    return False


def quit_without_save() -> None:
    """저장 없이 종료 플래그 설정."""
    global _quit_flag
    _quit_flag = True
    print("Quit without saving.")


# ───────────────────────────────────────────────────────────────
#  내부 유틸리티
# ───────────────────────────────────────────────────────────────

def _compute_button_rects(canvas_w: int) -> None:
    """버튼 위치 계산"""
    global _button_rects
    _button_rects.clear()
    n = len(_BUTTONS)
    btn_w = 68
    btn_h = 30
    gap = 5
    total_w = n * btn_w + (n - 1) * gap
    start_x = (canvas_w - total_w) // 2
    bar_y0 = _canvas_h - BUTTON_BAR_HEIGHT
    y = bar_y0 + (BUTTON_BAR_HEIGHT - btn_h) // 2
    x = start_x
    for _ in _BUTTONS:
        _button_rects.append((x, y, x + btn_w, y + btn_h))
        x += btn_w + gap


def _get_clicked_button(x: int, y: int) -> int | None:
    """클릭 좌표가 버튼 영역이면 인덱스 반환, 아니면 None"""
    for i, (bx1, by1, bx2, by2) in enumerate(_button_rects):
        if bx1 <= x <= bx2 and by1 <= y <= by2:
            return i
    return None


def _execute_action(action: str) -> None:
    """액션 키로 해당 함수 호출"""
    if action == "new":
        add_new_roi()
    elif action == "next":
        select_next_roi()
    elif action == "delete":
        delete_selected_roi()
    elif action == "undo":
        undo_last_action()
    elif action == "reset":
        reset_all_rois()
    elif action == "save":
        save_and_exit()
    elif action == "quit":
        quit_without_save()


def _draw_button_bar(disp: np.ndarray) -> None:
    """캔버스 하단에 버튼 바 그리기"""
    bar_y0 = _canvas_h - BUTTON_BAR_HEIGHT
    # 배경
    cv2.rectangle(disp, (0, bar_y0), (disp.shape[1], _canvas_h), (55, 55, 55), -1)
    # 구분선
    cv2.line(disp, (0, bar_y0), (disp.shape[1], bar_y0), (100, 100, 100), 1)
    # 버튼
    for i, ((label, rgb, _), (bx1, by1, bx2, by2)) in enumerate(zip(_BUTTONS, _button_rects)):
        bgr = (rgb[2], rgb[1], rgb[0])
        cv2.rectangle(disp, (bx1, by1), (bx2, by2), bgr, -1)
        cv2.rectangle(disp, (bx1, by1), (bx2, by2), (180, 180, 180), 1)
        ts = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 2)[0]
        tx = bx1 + (bx2 - bx1 - ts[0]) // 2
        ty = by1 + (by2 - by1 + ts[1]) // 2
        cv2.putText(disp, label, (tx, ty), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 2)


def resize_for_display(img, width):
    h, w = img.shape[:2]
    height = int(h * width / w)
    return cv2.resize(img, (width, height))


def mouse_callback(event, x, y, flags, param):
    global roi_start, roi_end, dragging, rois, selected_idx
    img_h = _canvas_h - BUTTON_BAR_HEIGHT

    # 버튼 바 클릭
    if y >= img_h:
        if event == cv2.EVENT_LBUTTONDOWN:
            btn_idx = _get_clicked_button(x, y)
            if btn_idx is not None:
                _execute_action(_BUTTONS[btn_idx][2])
        return

    # 이미지 영역: ROI 드래그
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
    if roi_start and roi_end:
        x1 = min(roi_start[0], roi_end[0])
        y1 = min(roi_start[1], roi_end[1])
        x2 = max(roi_start[0], roi_end[0])
        y2 = max(roi_start[1], roi_end[1])
        if x2 - x1 > 2 and y2 - y1 > 2:
            return (x1, y1, x2, y2)
    return None


def load_existing_rois():
    global rois, selected_idx
    c = load_config()
    rois.clear()
    if c.roi.rois:
        for entry in c.roi.rois:
            if isinstance(entry, dict):
                rois.append(entry)
            else:
                rois.append({
                    "name": entry.name,
                    "x1": entry.x1, "y1": entry.y1,
                    "x2": entry.x2, "y2": entry.y2,
                })
    elif None not in (c.roi.x1, c.roi.y1, c.roi.x2, c.roi.y2):
        rois.append({
            "name": "ROI-1",
            "x1": c.roi.x1, "y1": c.roi.y1,
            "x2": c.roi.x2, "y2": c.roi.y2,
        })
    selected_idx = 0 if rois else -1
    return len(rois)


def _save_all_rois():
    """config.json에 모든 ROI 저장 (visual 모드면 thermal 좌표로 변환)"""
    c = load_config(force_reload=True)
    entries = []
    for r in rois:
        if use_visual and H_inv is not None:
            corners_vis = np.array([
                [r["x1"], r["y1"]],
                [r["x2"], r["y2"]],
            ], dtype=np.float32).reshape(-1, 1, 2)
            thermal_pts = cv2.perspectiveTransform(corners_vis, H_inv).reshape(2, 2)
            tx1, ty1 = int(round(thermal_pts[0, 0])), int(round(thermal_pts[0, 1]))
            tx2, ty2 = int(round(thermal_pts[1, 0])), int(round(thermal_pts[1, 1]))
            x1 = max(0, min(min(tx1, tx2), thermal_resolution[0] - 1))
            y1 = max(0, min(min(ty1, ty2), thermal_resolution[1] - 1))
            x2 = max(0, min(max(tx1, tx2), thermal_resolution[0] - 1))
            y2 = max(0, min(max(ty1, ty2), thermal_resolution[1] - 1))
            print(f"  [{r['name']}] visual({r['x1']},{r['y1']})-({r['x2']},{r['y2']})"
                  f" → thermal({x1},{y1})-({x2},{y2})")
        else:
            x1, y1, x2, y2 = int(r["x1"]), int(r["y1"]), int(r["x2"]), int(r["y2"])
        entries.append({"name": r["name"], "x1": x1, "y1": y1, "x2": x2, "y2": y2})
    c.roi.rois = entries
    if entries:
        c.roi.x1 = entries[0]["x1"]
        c.roi.y1 = entries[0]["y1"]
        c.roi.x2 = entries[0]["x2"]
        c.roi.y2 = entries[0]["y2"]
    save_config(c)
    print(f"[roi_selector] {len(entries)} ROI(s) saved to config.json in thermal 640×480 coordinates:")
    for e in entries:
        print(f"  {e['name']}: ({e['x1']},{e['y1']})-({e['x2']},{e['y2']})")


def _get_color(idx: int) -> tuple:
    return ROI_COLORS[idx % len(ROI_COLORS)]


# ───────────────────────────────────────────────────────────────
#  main
# ───────────────────────────────────────────────────────────────

def main():
    global scale, selected_idx, rois, roi_start, roi_end, dragging
    global H_inv, use_visual, _running, _quit_flag, _save_flag
    global _canvas_h

    # ── 인자 파싱 ──
    thermal_path: str | None = None
    visual_path: str | None = None
    if len(sys.argv) >= 3:
        thermal_path = sys.argv[1]
        visual_path = sys.argv[2]
    elif len(sys.argv) == 2:
        thermal_path = sys.argv[1]
    else:
        jpg_files = sorted(glob.glob(os.path.join(DATASET_DIR, "*.jpg")))
        thermal_files = [f for f in jpg_files if "_visual" not in f]
        if thermal_files:
            thermal_path = thermal_files[-1]
            v = os.path.splitext(thermal_path)[0] + "_visual.jpg"
            if v in jpg_files:
                visual_path = v

    if thermal_path is None:
        print("Thermal 이미지를 찾을 수 없습니다.")
        print("사용법: python roi_selector.py [thermal.jpg] [visual.jpg]")
        sys.exit(1)

    # ── Homography 확인 ──
    HOMOGRAPHY_PATH = cfg.paths.homography_path
    if os.path.isfile(HOMOGRAPHY_PATH) and visual_path and os.path.isfile(visual_path):
        H = np.load(HOMOGRAPHY_PATH)
        if H.shape == (3, 3):
            H_inv = np.linalg.inv(H)
            img = cv2.imread(visual_path)
            use_visual = True
            display_res = img.shape[:2]
            print(f"Homography loaded → Visual mode: {os.path.basename(visual_path)}"
                  f" ({display_res[1]}×{display_res[0]})")
            print(f"  ROI coordinates will be auto-converted to thermal "
                  f"{thermal_resolution[0]}×{thermal_resolution[1]} on save.")
        else:
            print(f"Invalid homography shape {H.shape}, falling back to thermal.")
            use_visual = False
    else:
        use_visual = False

    if not use_visual:
        img = cv2.imread(thermal_path)
        H_inv = None
        print(f"Thermal mode: {os.path.basename(thermal_path)}")

    if img is None:
        print("이미지를 불러올 수 없습니다.")
        sys.exit(1)

    print(f"Loaded: {thermal_path if not use_visual else visual_path}  ({img.shape[1]}×{img.shape[0]})")
    print("  Drag mouse to set ROI | Buttons below | Shortcuts: N Tab Del Z R S Q")

    count = load_existing_rois()
    if count > 0:
        print(f"  Loaded {count} existing ROI(s): {[r['name'] for r in rois]}")

    img_disp = resize_for_display(img, DISPLAY_WIDTH)
    scale = img.shape[1] / img_disp.shape[1]

    # 캔버스 = 이미지 + 버튼 바
    _canvas_h = img_disp.shape[0] + BUTTON_BAR_HEIGHT
    _compute_button_rects(img_disp.shape[1])

    wintitle = "ROI Selector - Visual (H)" if use_visual else "ROI Selector - Thermal"
    cv2.namedWindow(wintitle)
    cv2.setMouseCallback(wintitle, mouse_callback)

    _running = True
    _quit_flag = False
    _save_flag = False

    while True:
        # 버튼 바 포함한 전체 캔버스
        disp = np.zeros((_canvas_h, img_disp.shape[1], 3), dtype=np.uint8)
        disp[0:img_disp.shape[0], 0:img_disp.shape[1]] = img_disp

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
        mode_text = "Visual mode (H→thermal)" if use_visual else "Thermal mode"
        sel_name = rois[selected_idx]["name"] if 0 <= selected_idx < len(rois) else "(none)"
        status = f"{mode_text} | Selected: {sel_name} | ROIs: {len(rois)}"
        cv2.putText(disp, status, (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (220, 220, 220), 1)

        # 버튼 바 그리기
        _draw_button_bar(disp)

        cv2.imshow(wintitle, disp)
        key = cv2.waitKey(1)

        # ── 종료 플래그 체크 (버튼 클릭으로 설정됨) ──
        if _quit_flag:
            break
        if _save_flag:
            break

        # ── 키보드 단축키 (기존 호환) ──
        if key == ord("q") or key == 27:
            quit_without_save()
            break

        elif key == ord("n"):
            add_new_roi()

        elif key == 9:  # Tab
            select_next_roi()

        elif key == 127 or key == 8:  # Del / Backspace
            delete_selected_roi()

        elif key == ord("r"):
            reset_all_rois()

        elif key == ord("z"):
            undo_last_action()

        elif key == ord("s"):
            if save_and_exit():
                break

    cv2.destroyAllWindows()
    _running = False


if __name__ == "__main__":
    main()
