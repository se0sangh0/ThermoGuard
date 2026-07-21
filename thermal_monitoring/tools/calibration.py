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
import tkinter as tk
from tkinter import messagebox, ttk

from ..config import load_config

thermal_pts = []
rgb_pts = []
pair_state = "thermal"  # "thermal" or "rgb"
t_mouse_x, t_mouse_y = -1, -1  # Thermal 창 마우스 (원본 해상도)
r_mouse_x, r_mouse_y = -1, -1  # RGB 창 마우스 (원본 해상도)

ZOOM_SIZE = 80      # 확대경 영역 크기 (px)
ZOOM_SCALE = 3.0    # 확대 배율
MAX_MEAN_ERROR_PX = 5.0
MAX_POINT_ERROR_PX = 10.0

    MAX_W = 590
    MAX_H = 470
    MAX_MEAN_ERROR_PX = 5.0
    MAX_POINT_ERROR_PX = 10.0
    ZOOM_SOURCE_SIZE = 60
    ZOOM_DISPLAY_SIZE = 180

    def __init__(self, thermal_path: str, rgb_path: str, output_path: str):
        self.thermal_path = thermal_path
        self.rgb_path = rgb_path
        self.output_path = output_path
        self.thermal_pts: list[list[int]] = []
        self.rgb_pts: list[list[int]] = []
        self.next_side = "thermal"
        self.saved = False
        self._zoom_photos: dict[str, ImageTk.PhotoImage] = {}
        self._panel_sizes: dict[str, tuple[int, int]] = {}

        thermal_bgr = cv2.imread(thermal_path)
        rgb_bgr = cv2.imread(rgb_path)
        if thermal_bgr is None:
            raise FileNotFoundError(f"Thermal 이미지를 불러올 수 없습니다: {thermal_path}")
        if rgb_bgr is None:
            raise FileNotFoundError(f"가시광 이미지를 불러올 수 없습니다: {rgb_path}")

        self.thermal_rgb = cv2.cvtColor(thermal_bgr, cv2.COLOR_BGR2RGB)
        self.visible_rgb = cv2.cvtColor(rgb_bgr, cv2.COLOR_BGR2RGB)

    def __init__(self):
        parent = tk._default_root
        if parent is None:
            self.window = tk.Tk()
            self._owns_root = True
        else:
            self.window = tk.Toplevel(parent)
            self.window.transient(parent)
            self._owns_root = False

        self.action = None
        self.window.title("Thermal-RGB 캘리브레이션")
        self.window.geometry("640x230")
        self.window.resizable(False, False)
        self.window.protocol("WM_DELETE_WINDOW", lambda: self.request("quit"))

        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text="Thermal → 가시광 순서로 동일한 지점을 선택하세요.",
            font=("맑은 고딕", 13, "bold"),
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text="마우스 위치는 영상 우측 하단에 3배 확대됩니다. "
                 "정확한 검증을 위해 화면 전체에 6쌍 이상을 고르게 선택하는 것을 권장합니다.",
            wraplength=600,
            justify="left",
        ).pack(anchor="w", pady=(6, 12))

        self.status_var = tk.StringVar(value="다음 작업: Thermal 이미지에서 첫 번째 대응점 선택")
        ttk.Label(frame, textvariable=self.status_var, foreground="#145ea8").pack(anchor="w", pady=(0, 12))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="종료  (Esc)", command=self.close).pack(side="right", padx=4)
        ttk.Button(buttons, text="전체 리셋  (R)", command=self.reset).pack(side="right", padx=4)
        ttk.Button(buttons, text="마지막 점 취소  (Z)", command=self.undo).pack(side="right", padx=4)
        ttk.Button(buttons, text="저장  (Ctrl+S)", command=self.save).pack(side="right", padx=4)

    def _image_panel(self, parent, title: str, array: np.ndarray, side: str):
        panel = tk.Frame(parent, bg="white", highlightbackground="#d9e2ea", highlightthickness=1)
        panel.pack(side="left", fill="both", expand=True, padx=(0, 6) if side == "thermal" else (6, 0))
        tk.Label(panel, text=title, bg="white", fg="#1f2e3d",
                 font=("맑은 고딕", 11, "bold")).pack(anchor="w", padx=10, pady=8)
        pil, scale = self._fit_image(array, self.MAX_W, self.MAX_H)
        photo = ImageTk.PhotoImage(pil)
        canvas = tk.Canvas(panel, width=pil.width, height=pil.height, bg="#151b22",
                           highlightthickness=0, cursor="cross")
        canvas.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        canvas.create_image(0, 0, image=photo, anchor="nw", tags="base")
        canvas.bind("<Button-1>", lambda event, selected=side: self._click(selected, event.x, event.y))
        canvas.bind("<Motion>", lambda event, selected=side: self._show_magnifier(selected, event.x, event.y))
        canvas.bind("<Leave>", lambda _event, selected=side: self._hide_magnifier(selected))
        self._panel_sizes[side] = (pil.width, pil.height)
        return canvas, photo, scale

    def _show_magnifier(self, side: str, display_x: int, display_y: int):
        """Show a pixel-preserving magnifier without covering the click target."""
        scale = self.thermal_scale if side == "thermal" else self.visible_scale
        source = self.thermal_rgb if side == "thermal" else self.visible_rgb
        panel_w, panel_h = self._panel_sizes[side]
        canvas = self.thermal_canvas if side == "thermal" else self.visible_canvas

        if not (0 <= display_x < panel_w and 0 <= display_y < panel_h):
            self._hide_magnifier(side)
            return

        original_x = int(display_x / scale)
        original_y = int(display_y / scale)
        half = self.ZOOM_SOURCE_SIZE // 2
        x1 = max(0, original_x - half)
        y1 = max(0, original_y - half)
        x2 = min(source.shape[1], original_x + half)
        y2 = min(source.shape[0], original_y + half)
        if x2 <= x1 or y2 <= y1:
            return

        crop = Image.fromarray(source[y1:y2, x1:x2]).resize(
            (self.ZOOM_DISPLAY_SIZE, self.ZOOM_DISPLAY_SIZE),
            Image.Resampling.NEAREST,
        )
        photo = ImageTk.PhotoImage(crop)
        self._zoom_photos[side] = photo

        margin = 10
        zoom_x = panel_w - self.ZOOM_DISPLAY_SIZE - margin
        zoom_y = panel_h - self.ZOOM_DISPLAY_SIZE - margin
        # Move the magnifier to the opposite side when the pointer approaches it.
        if display_x >= panel_w / 2 and display_y >= panel_h / 2:
            zoom_x = margin
            zoom_y = margin

        canvas.delete("magnifier")
        canvas.create_image(zoom_x, zoom_y, image=photo, anchor="nw", tags="magnifier")
        canvas.create_rectangle(
            zoom_x, zoom_y,
            zoom_x + self.ZOOM_DISPLAY_SIZE,
            zoom_y + self.ZOOM_DISPLAY_SIZE,
            outline="#00d7ff", width=2, tags="magnifier",
        )
        center_x = zoom_x + self.ZOOM_DISPLAY_SIZE // 2
        center_y = zoom_y + self.ZOOM_DISPLAY_SIZE // 2
        gap = 4
        arm = 14
        for x_start, y_start, x_end, y_end in (
            (center_x - arm, center_y, center_x - gap, center_y),
            (center_x + gap, center_y, center_x + arm, center_y),
            (center_x, center_y - arm, center_x, center_y - gap),
            (center_x, center_y + gap, center_x, center_y + arm),
        ):
            canvas.create_line(
                x_start, y_start, x_end, y_end,
                fill="#ffff00", width=1, tags="magnifier",
            )
        canvas.create_text(
            zoom_x + 6, zoom_y + 6,
            text="확대 보기",
            anchor="nw", fill="#00ffff",
            font=("맑은 고딕", 9, "bold"), tags="magnifier",
        )

    def _hide_magnifier(self, side: str):
        canvas = self.thermal_canvas if side == "thermal" else self.visible_canvas
        canvas.delete("magnifier")
        self._zoom_photos.pop(side, None)

    def _bind_shortcuts(self):
        self.window.bind("<Control-s>", lambda _event: self.save())
        self.window.bind("<Control-S>", lambda _event: self.save())
        self.window.bind("z", lambda _event: self.undo())
        self.window.bind("Z", lambda _event: self.undo())
        self.window.bind("r", lambda _event: self.reset())
        self.window.bind("R", lambda _event: self.reset())
        self.window.bind("<Escape>", lambda _event: self.close())

    def _click(self, side: str, display_x: int, display_y: int):
        if side != self.next_side:
            expected = "Thermal" if self.next_side == "thermal" else "가시광"
            self.status_var.set(f"순서 확인: 먼저 {expected} 이미지의 대응점을 선택하세요.")
            return

        scale = self.thermal_scale if side == "thermal" else self.visible_scale
        point = [round(display_x / scale), round(display_y / scale)]
        if side == "thermal":
            self.thermal_pts.append(point)
            self.next_side = "rgb"
        else:
            self.rgb_pts.append(point)
            self.next_side = "thermal"
        self._redraw()

    def _redraw(self):
        self._draw_points(self.thermal_canvas, self.thermal_pts, self.thermal_scale)
        self._draw_points(self.visible_canvas, self.rgb_pts, self.visible_scale)
        expected = "Thermal 이미지 클릭" if self.next_side == "thermal" else "가시광 이미지 클릭"
        self.status_var.set(f"다음 작업: {expected}")
        complete = min(len(self.thermal_pts), len(self.rgb_pts))
        pending = " · Thermal 점 선택 후 가시광 대기" if len(self.thermal_pts) != len(self.rgb_pts) else ""
        self.count_var.set(f"완성된 대응점 {complete}쌍 / 최소 4쌍{pending}")

    @staticmethod
    def _draw_points(canvas: tk.Canvas, points: list[list[int]], scale: float):
        canvas.delete("point")
        for index, (x, y) in enumerate(points, start=1):
            dx, dy = x * scale, y * scale
            # A small, hollow crosshair keeps the exact selected pixel visible.
            gap, arm = 2, 6
            canvas.create_line(dx - arm, dy, dx - gap, dy, fill="#ff3030", width=1, tags="point")
            canvas.create_line(dx + gap, dy, dx + arm, dy, fill="#ff3030", width=1, tags="point")
            canvas.create_line(dx, dy - arm, dx, dy - gap, fill="#ff3030", width=1, tags="point")
            canvas.create_line(dx, dy + gap, dx, dy + arm, fill="#ff3030", width=1, tags="point")
            canvas.create_text(dx + 8, dy - 8, text=str(index), fill="#00ff55",
                               font=("맑은 고딕", 9, "bold"), tags="point")

    def undo(self):
        if self.next_side == "rgb" and self.thermal_pts:
            self.thermal_pts.pop()
            self.next_side = "thermal"
        elif self.next_side == "thermal" and self.rgb_pts:
            self.rgb_pts.pop()
            self.next_side = "rgb"
        else:
            next_step = "가시광 이미지에서 같은 대응점 선택"
        self.status_var.set(f"완성된 대응점: {complete}쌍 / 다음 작업: {next_step}")
        self.window.update_idletasks()
        self.window.update()

    def close(self):
        if self.window.winfo_exists():
            self.window.destroy()


def _validate_and_save_calibration(cfg, parent) -> bool:
    """오차를 검사하고 허용 범위 이내인 경우에만 Homography를 저장한다."""
    if len(thermal_pts) != len(rgb_pts):
        messagebox.showwarning(
            "저장할 수 없음",
            "Thermal과 가시광 대응점을 같은 개수로 완성하세요.",
            parent=parent,
        )
        return False
    if len(thermal_pts) < 4:
        messagebox.showwarning(
            "저장할 수 없음",
            f"최소 4쌍의 대응점이 필요합니다. 현재 {len(thermal_pts)}쌍입니다.",
            parent=parent,
        )
        return False

    thermal_arr = np.asarray(thermal_pts, dtype=np.float32)
    rgb_arr = np.asarray(rgb_pts, dtype=np.float32)
    matrix, _ = cv2.findHomography(thermal_arr, rgb_arr)
    if matrix is None:
        messagebox.showerror(
            "캘리브레이션 계산 실패",
            "대응점이 한쪽에 몰리거나 일직선이 되지 않도록 다시 선택하세요.",
            parent=parent,
        )
        return False

    projected = cv2.perspectiveTransform(thermal_arr.reshape(-1, 1, 2), matrix).reshape(-1, 2)
    errors = np.linalg.norm(projected - rgb_arr, axis=1)
    mean_error = float(errors.mean())
    max_error = float(errors.max())
    rmse = float(np.sqrt(np.mean(np.square(errors))))
    metrics = (
        f"대응점: {len(thermal_arr)}쌍\n"
        f"평균 오차: {mean_error:.2f}px\n"
        f"최대 오차: {max_error:.2f}px\n"
        f"RMSE: {rmse:.2f}px\n\n"
        f"허용 기준: 평균 {MAX_MEAN_ERROR_PX:.1f}px 이하, "
        f"최대 {MAX_POINT_ERROR_PX:.1f}px 이하"
    )

    if mean_error > MAX_MEAN_ERROR_PX or max_error > MAX_POINT_ERROR_PX:
        messagebox.showwarning(
            "캘리브레이션 보정 필요",
            "캘리브레이션 오차가 허용 기준보다 큽니다.\n"
            "현재 결과는 저장하지 않았습니다.\n\n"
            f"{metrics}\n\n"
            "[전체 리셋] 후 특징이 명확하고 화면 전체에 고르게 분포된 대응점을 다시 선택하세요.",
            parent=parent,
        )
        return False

    output_path = cfg.paths.homography_path
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    np.save(output_path, matrix)
    messagebox.showinfo(
        "캘리브레이션 정상 완료",
        "오차가 허용 기준 이내여서 보정값을 저장했습니다.\n\n"
        f"{metrics}\n\n저장 위치: {output_path}",
        parent=parent,
    )
    return True


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

    def make_scaled_callback(original_callback, scale):
        def wrapper(event, x, y, flags, param):
            return original_callback(event, int(x * scale), int(y * scale), flags, param)
        return wrapper

    cv2.setMouseCallback("Thermal", make_scaled_callback(click_thermal, thermal_scale))
    cv2.setMouseCallback("RGB", make_scaled_callback(click_rgb, rgb_scale))
    controls = CalibrationControls()

    while True:
        t = thermal_disp.copy()
        r = rgb_disp.copy()

        # ── 마커: 십자선 + 얇은 원 (중심점 보임) ──
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

        # ── 확대경 (우하단, 현재 마우스 위치 주변) ──
        # Thermal 확대경
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

        # RGB 확대경
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

        cv2.imshow("Thermal", t)
        cv2.imshow("RGB", r)

        key = cv2.waitKey(1)
        try:
            controls.update_status()
        except tk.TclError:
            cv2.destroyAllWindows()
            return False

        action = controls.consume_action()
        if key == ord('s'):
            action = "save"
        elif key == ord('r'):
            action = "reset"
        elif key == ord('z'):
            action = "undo"
        elif key == ord('q') or key == 27:
            action = "quit"

        if action == "quit":
            print("Quit without saving.")
            cv2.destroyAllWindows()
            controls.close()
            return False
        elif action == "reset":
            thermal_pts.clear()
            rgb_pts.clear()
            pair_state = "thermal"
            print("[Reset] All points cleared.")
        elif action == "undo":
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
        elif action == "save":
            if _validate_and_save_calibration(cfg, controls.window):
                cv2.destroyAllWindows()
                controls.close()
                return True

    cv2.destroyAllWindows()

    thermal_arr = np.array(thermal_pts, dtype=np.float32)
    rgb_arr = np.array(rgb_pts, dtype=np.float32)

    if len(thermal_arr) != len(rgb_arr):
        print(f"Thermal({len(thermal_arr)})과 RGB({len(rgb_arr)}) 포인트 개수가 일치하지 않습니다.")
    elif len(thermal_arr) < 4:
        print(f"최소 4개의 대응점이 필요합니다. 현재: {len(thermal_arr)}개")
    else:
        H, _ = cv2.findHomography(thermal_arr, rgb_arr)
        if H is None:
            print("Homography 계산 실패 (점들이 동일선상에 있을 수 있습니다).")
        else:
            output_path = cfg.paths.homography_path
            np.save(output_path, H)
            print(f"Homography saved: {output_path}")
            print("Homography matrix:")
            print(H)
            projected = cv2.perspectiveTransform(thermal_arr.reshape(-1, 1, 2), H)
            error = np.linalg.norm(projected.reshape(-1, 2) - rgb_arr, axis=1)
            print(f"평균 재투영 오차: {error.mean():.4f}")
            print(f"최대 재투영 오차: {error.max():.4f}")
            for i, e in enumerate(error):
                print(i, e)

            pt = np.array([[[320, 240]]], dtype=np.float32)
            rgb_pt = cv2.perspectiveTransform(pt, H)
            print(rgb_pt)


if __name__ == "__main__":
    run_calibration()
