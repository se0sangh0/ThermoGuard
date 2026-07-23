"""
calibration.py - Thermal-RGB Homography 캘리브레이션 도구

Thermal 이미지와 RGB 이미지에서 대응점을 선택하여 Homography 행렬을 계산합니다.
계산된 행렬은 thermal_to_rgb.npy로 저장됩니다.

조작:
    마우스 클릭 (좌우 교차) : Thermal → RGB 대응점 선택
    S             : 현재까지의 점으로 Homography 계산 및 저장
    R             : 모든 선택점 초기화
    Z             : 마지막 선택점 취소 (Undo)
    ESC / Q       : 종료 (변경사항 저장 안 함)
"""

import cv2
import numpy as np
import sys
import os
import glob

from ..config import load_config

thermal_pts = []
rgb_pts = []
pair_state = "thermal"  # "thermal" or "rgb"
t_mouse_x, t_mouse_y = -1, -1  # Thermal 창 마우스 (원본 해상도)
r_mouse_x, r_mouse_y = -1, -1  # RGB 창 마우스 (원본 해상도)

ZOOM_SIZE = 80      # 확대경 영역 크기 (px)
ZOOM_SCALE = 3.0    # 확대 배율
MAX_MEAN_ERROR_PX = 5.0   # 평균 재투영 오차 허용 기준
MAX_POINT_ERROR_PX = 10.0  # 개별 최대 오차 허용 기준
BUTTON_BAR_HEIGHT = 44
_BUTTONS = [
    ("Save",  (50, 160, 50), "save"),
    ("Undo",  (160, 160, 50), "undo"),
    ("Reset", (180, 80, 50), "reset"),
    ("Quit",  (50, 50, 180), "quit"),
]


def draw_crosshair(img, x, y, color, size=4, thickness=1, gap=2):
    """정밀 마커: 십자선 + 얇은 원 (중심점이 빈 공간으로 보임)"""
    cv2.line(img, (x - size, y), (x - gap, y), color, thickness)
    cv2.line(img, (x + gap, y), (x + size, y), color, thickness)
    cv2.line(img, (x, y - size), (x, y - gap), color, thickness)
    cv2.line(img, (x, y + gap), (x, y + size), color, thickness)
    cv2.circle(img, (x, y), size, color, 1)


def click_thermal(event, x, y, flags, param):
    global pair_state, t_mouse_x, t_mouse_y
    if event == cv2.EVENT_MOUSEMOVE or event == cv2.EVENT_LBUTTONDOWN:
        t_mouse_x, t_mouse_y = x, y
    if event == cv2.EVENT_LBUTTONDOWN:
        if pair_state == "thermal":
            thermal_pts.append([x, y])
            pair_state = "rgb"
            print(f"[{len(thermal_pts)}] Thermal : {x}, {y} -> RGB 클릭 대기")
        else:
            print("[경고] RGB 포인트를 먼저 클릭하세요.")


def click_rgb(event, x, y, flags, param):
    global pair_state, r_mouse_x, r_mouse_y
    if event == cv2.EVENT_MOUSEMOVE or event == cv2.EVENT_LBUTTONDOWN:
        r_mouse_x, r_mouse_y = x, y
    if event == cv2.EVENT_LBUTTONDOWN:
        if pair_state == "rgb":
            rgb_pts.append([x, y])
            pair_state = "thermal"
            print(f"[{len(rgb_pts)}] RGB : {x}, {y} -> Thermal 클릭 대기")
        else:
            print("[경고] Thermal 포인트를 먼저 클릭하세요.")


def resize_for_display(img, width):
    h, w = img.shape[:2]
    height = int(h * width / w)
    return cv2.resize(img, (width, height))


def _compute_button_rects(canvas_width: int, image_height: int):
    """ROI 설정 도구와 같은 방식으로 하단 버튼 위치를 계산한다."""
    button_width, button_height, gap = 92, 30, 8
    total_width = len(_BUTTONS) * button_width + (len(_BUTTONS) - 1) * gap
    start_x = (canvas_width - total_width) // 2
    y = image_height + (BUTTON_BAR_HEIGHT - button_height) // 2
    return [
        (start_x + index * (button_width + gap), y,
         start_x + index * (button_width + gap) + button_width, y + button_height)
        for index in range(len(_BUTTONS))
    ]


def _draw_button_bar(canvas, image_height, button_rects):
    """Thermal 창 하단에 클릭 가능한 버튼 바를 그린다."""
    cv2.rectangle(
        canvas, (0, image_height), (canvas.shape[1], canvas.shape[0]),
        (55, 55, 55), -1,
    )
    cv2.line(canvas, (0, image_height), (canvas.shape[1], image_height), (100, 100, 100), 1)
    for (label, rgb, _), (x1, y1, x2, y2) in zip(_BUTTONS, button_rects):
        bgr = (rgb[2], rgb[1], rgb[0])
        cv2.rectangle(canvas, (x1, y1), (x2, y2), bgr, -1)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), (180, 180, 180), 1)
        size = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.52, 2)[0]
        text_x = x1 + (x2 - x1 - size[0]) // 2
        text_y = y1 + (y2 - y1 + size[1]) // 2
        cv2.putText(
            canvas, label, (text_x, text_y),
            cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255, 255, 255), 2,
        )


def run_calibration(thermal_path=None, rgb_path=None):
    """GUI 또는 CLI에서 호출 가능한 캘리브레이션 진입점."""
    global thermal_pts, rgb_pts, pair_state, t_mouse_x, t_mouse_y, r_mouse_x, r_mouse_y
    thermal_pts.clear()
    rgb_pts.clear()
    pair_state = "thermal"
    t_mouse_x = t_mouse_y = r_mouse_x = r_mouse_y = -1

    cfg = load_config()
    DATASET_DIR = cfg.paths.dataset_dir
    DISPLAY_WIDTH = cfg.display.display_width

    if thermal_path is None:
        if len(sys.argv) >= 3:
            thermal_path = sys.argv[1]
            rgb_path = sys.argv[2]
        elif len(sys.argv) == 2:
            thermal_path = sys.argv[1]
            rgb_path = os.path.splitext(thermal_path)[0] + "_visual.jpg"
        else:
            jpg_files = sorted(glob.glob(os.path.join(DATASET_DIR, "*.jpg")))
            visual_files = [f for f in jpg_files if "_visual" in f]
            thermal_files = [f for f in jpg_files if "_visual" not in f]
            if thermal_files:
                thermal_path = thermal_files[-1]
                matching_visual = os.path.splitext(thermal_path)[0] + "_visual.jpg"
                rgb_path = matching_visual if matching_visual in visual_files else (visual_files[-1] if visual_files else None)

    thermal = cv2.imread(thermal_path) if thermal_path else None
    rgb = cv2.imread(rgb_path) if rgb_path else None

    if thermal is None:
        print(f"Thermal 이미지를 불러올 수 없습니다: {thermal_path}")
        return
    if rgb is None:
        print(f"RGB 이미지를 불러올 수 없습니다: {rgb_path}")
        return

    # ── 동일 캡처 사이클 쌍인지 검증 ──
    t_stem = os.path.splitext(os.path.basename(thermal_path))[0]
    v_stem = os.path.splitext(os.path.basename(rgb_path))[0]
    if v_stem.endswith("_visual"):
        v_base = v_stem[:-len("_visual")]
    else:
        v_base = v_stem
    if t_stem != v_base:
        print(f"\n⚠  경고: Thermal과 Visual이 같은 촬영 시점이 아닙니다!")
        print(f"  Thermal  →  {os.path.basename(thermal_path)}")
        print(f"  Visual   →  {os.path.basename(rgb_path)}")
        print(f"  서로 다른 시점의 이미지로 캘리브레이션하면 로봇이 움직인")
        print(f"  만큼 Homography가 틀어져 오버레이 정합도가 떨어집니다.")
        print(f"\n  계속하려면 Enter, 중단하려면 Ctrl+C: ", end="")
        input()

    print(f"Thermal: {thermal_path}")
    print(f"RGB: {rgb_path}")
    print("  Click alternating: Thermal -> RGB -> Thermal -> ...")
    print("  S = compute & save   R = reset   Z = undo   ESC/Q = quit without saving")

    cv2.namedWindow("Thermal")
    cv2.namedWindow("RGB")

    thermal_disp = resize_for_display(thermal, DISPLAY_WIDTH)
    rgb_disp = resize_for_display(rgb, DISPLAY_WIDTH)

    thermal_scale = thermal.shape[1] / thermal_disp.shape[1]
    rgb_scale = rgb.shape[1] / rgb_disp.shape[1]
    button_rects = _compute_button_rects(thermal_disp.shape[1], thermal_disp.shape[0])
    button_action = {"value": None}

    def make_scaled_callback(original_callback, scale):
        def wrapper(event, x, y, flags, param):
            return original_callback(event, int(x * scale), int(y * scale), flags, param)
        return wrapper

    def thermal_callback(event, x, y, flags, param):
        if y >= thermal_disp.shape[0]:
            if event == cv2.EVENT_LBUTTONDOWN:
                for (_, _, action), (x1, y1, x2, y2) in zip(_BUTTONS, button_rects):
                    if x1 <= x <= x2 and y1 <= y <= y2:
                        button_action["value"] = action
                        break
            return
        return click_thermal(
            event, int(x * thermal_scale), int(y * thermal_scale), flags, param,
        )

    cv2.setMouseCallback("Thermal", thermal_callback)
    cv2.setMouseCallback("RGB", make_scaled_callback(click_rgb, rgb_scale))

    while True:
        t = thermal_disp.copy()
        r = rgb_disp.copy()

        for i, pt in enumerate(thermal_pts):
            dp = (int(pt[0] / thermal_scale), int(pt[1] / thermal_scale))
            draw_crosshair(t, *dp, (0, 0, 255), size=4)
            cv2.putText(t, str(i + 1), (dp[0] + 6, dp[1] - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)
        for i, pt in enumerate(rgb_pts):
            dp = (int(pt[0] / rgb_scale), int(pt[1] / rgb_scale))
            draw_crosshair(r, *dp, (0, 0, 255), size=4)
            cv2.putText(r, str(i + 1), (dp[0] + 6, dp[1] - 6),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 255, 0), 1)

        if t_mouse_x >= 0 and t_mouse_y >= 0:
            hs = ZOOM_SIZE // 2
            ox, oy = t_mouse_x, t_mouse_y
            x1, y1 = max(0, ox - hs), max(0, oy - hs)
            x2, y2 = min(thermal.shape[1], ox + hs), min(thermal.shape[0], oy + hs)
            if x2 > x1 and y2 > y1:
                roi = thermal[y1:y2, x1:x2]
                zoomed = cv2.resize(roi, None, fx=ZOOM_SCALE, fy=ZOOM_SCALE,
                                    interpolation=cv2.INTER_NEAREST)
                cx = int((ox - x1) * ZOOM_SCALE)
                cy = int((oy - y1) * ZOOM_SCALE)
                draw_crosshair(zoomed, cx, cy, (0, 255, 255), size=3, thickness=1, gap=1)
                zh, zw = zoomed.shape[:2]
                margin = 8
                px = t.shape[1] - zw - margin
                py = t.shape[0] - zh - margin
                if px >= 0 and py >= 0:
                    zoomed = cv2.rectangle(zoomed, (0, 0), (zw - 1, zh - 1), (255, 255, 0), 1)
                    t[py:py + zh, px:px + zw] = zoomed

        if r_mouse_x >= 0 and r_mouse_y >= 0:
            hs = ZOOM_SIZE // 2
            ox, oy = r_mouse_x, r_mouse_y
            x1, y1 = max(0, ox - hs), max(0, oy - hs)
            x2, y2 = min(rgb.shape[1], ox + hs), min(rgb.shape[0], oy + hs)
            if x2 > x1 and y2 > y1:
                roi = rgb[y1:y2, x1:x2]
                zoomed = cv2.resize(roi, None, fx=ZOOM_SCALE, fy=ZOOM_SCALE,
                                    interpolation=cv2.INTER_NEAREST)
                cx = int((ox - x1) * ZOOM_SCALE)
                cy = int((oy - y1) * ZOOM_SCALE)
                draw_crosshair(zoomed, cx, cy, (0, 255, 255), size=3, thickness=1, gap=1)
                zh, zw = zoomed.shape[:2]
                margin = 8
                px = r.shape[1] - zw - margin
                py = r.shape[0] - zh - margin
                if px >= 0 and py >= 0:
                    zoomed = cv2.rectangle(zoomed, (0, 0), (zw - 1, zh - 1), (255, 255, 0), 1)
                    r[py:py + zh, px:px + zw] = zoomed

        cv2.putText(t, "Thermal | S: save  R: reset  Z: undo  Q: quit",
                    (10, t.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        cv2.putText(r, "RGB | S: save  R: reset  Z: undo  Q: quit",
                    (10, r.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

        thermal_canvas = np.zeros(
            (t.shape[0] + BUTTON_BAR_HEIGHT, t.shape[1], 3), dtype=np.uint8,
        )
        thermal_canvas[:t.shape[0], :t.shape[1]] = t
        _draw_button_bar(thermal_canvas, t.shape[0], button_rects)

        cv2.imshow("Thermal", thermal_canvas)
        cv2.imshow("RGB", r)

        key = cv2.waitKey(1)
        action = button_action["value"]
        button_action["value"] = None

        if key == ord('q') or key == 27 or action == "quit":
            print("Quit without saving.")
            cv2.destroyAllWindows()
            return False
        elif key == ord('r') or action == "reset":
            thermal_pts.clear()
            rgb_pts.clear()
            pair_state = "thermal"
            print("[Reset] All points cleared.")
        elif key == ord('z') or action == "undo":
            if pair_state == "rgb" and thermal_pts:
                thermal_pts.pop()
                pair_state = "thermal"
                print(f"[Undo] Last thermal point removed ({len(thermal_pts)} remaining)")
            elif pair_state == "thermal" and rgb_pts:
                rgb_pts.pop()
                pair_state = "rgb"
                print(f"[Undo] Last RGB point removed ({len(rgb_pts)} remaining)")
            else:
                print("[Undo] Nothing to undo.")
        elif key == ord('s') or action == "save":
            if len(thermal_pts) < 4:
                print(f"[Save] Need at least 4 point pairs, currently have {len(thermal_pts)}")
            elif len(thermal_pts) != len(rgb_pts):
                print(f"[Save] Point count mismatch: thermal={len(thermal_pts)} vs rgb={len(rgb_pts)}")
            else:
                break

    cv2.destroyAllWindows()

    thermal_arr = np.array(thermal_pts, dtype=np.float32)
    rgb_arr = np.array(rgb_pts, dtype=np.float32)

    H, _ = cv2.findHomography(thermal_arr, rgb_arr)
    if H is None:
        print("Homography 계산 실패 (점들이 동일선상에 있을 수 있습니다).")
        return False

    # 재투영 오차 검증
    projected = cv2.perspectiveTransform(thermal_arr.reshape(-1, 1, 2), H).reshape(-1, 2)
    errors = np.linalg.norm(projected - rgb_arr, axis=1)
    mean_error = float(errors.mean())
    max_error = float(errors.max())

    print(f"평균 재투영 오차: {mean_error:.4f}")
    print(f"최대 재투영 오차: {max_error:.4f}")
    for i, e in enumerate(errors):
        print(f"  pair {i}: {e:.4f}")

    if mean_error > MAX_MEAN_ERROR_PX or max_error > MAX_POINT_ERROR_PX:
        print(f"[오차 초과] 평균 {mean_error:.2f}px / 최대 {max_error:.2f}px")
        print(f"허용 기준: 평균 {MAX_MEAN_ERROR_PX:.1f}px 이하, 최대 {MAX_POINT_ERROR_PX:.1f}px 이하")
        print("전체 리셋 후 특징이 명확하고 화면 전체에 고르게 분포된 대응점을 다시 선택하세요.")
        _show_error_dialog(mean_error, max_error, len(thermal_arr))
        return False

    output_path = cfg.paths.homography_path
    np.save(output_path, H)
    print(f"Homography saved: {output_path}")
    print("Homography matrix:")
    print(H)

    pt = np.array([[[320, 240]]], dtype=np.float32)
    rgb_pt = cv2.perspectiveTransform(pt, H)
    print(rgb_pt)
    return True


def _show_error_dialog(mean_error: float, max_error: float, pair_count: int):
    """Tkinter 경고창: 오차가 기준을 초과하면 알림."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            "캘리브레이션 보정 필요",
            "캘리브레이션 오차가 허용 기준보다 큽니다.\n"
            "보정값을 저장하지 않았습니다.\n\n"
            f"대응점: {pair_count}쌍\n"
            f"평균 오차: {mean_error:.2f}px\n"
            f"최대 오차: {max_error:.2f}px\n\n"
            f"허용 기준: 평균 {MAX_MEAN_ERROR_PX:.1f}px 이하, 최대 {MAX_POINT_ERROR_PX:.1f}px 이하\n\n"
            "전체 리셋(R) 후 화면 전체에 고르게 분포된 대응점을 다시 선택하세요.",
        )
        root.destroy()
    except Exception:
        pass


if __name__ == "__main__":
    run_calibration()
