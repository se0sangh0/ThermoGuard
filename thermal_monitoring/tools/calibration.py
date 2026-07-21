"""Thermal-RGB 대응점 기반 Homography 캘리브레이션 GUI."""

from __future__ import annotations

import glob
import os
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from ..config import load_config


class CalibrationDialog:
    """두 영상을 한 창에서 대응점으로 연결하는 버튼 기반 도구."""

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

        parent = tk._default_root
        if parent is None:
            self.window = tk.Tk()
            self._owns_root = True
        else:
            self.window = tk.Toplevel(parent)
            self._owns_root = False
            self.window.transient(parent)

        self.window.title("Thermal-RGB 캘리브레이션")
        self.window.configure(bg="#f3f6f9")
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        self._build_ui()
        self._bind_shortcuts()
        self._redraw()

    @staticmethod
    def _fit_image(array: np.ndarray, max_w: int, max_h: int):
        h, w = array.shape[:2]
        scale = min(max_w / w, max_h / h, 1.0)
        size = (max(1, int(w * scale)), max(1, int(h * scale)))
        image = Image.fromarray(array).resize(size, Image.Resampling.LANCZOS)
        return image, scale

    def _build_ui(self):
        header = tk.Frame(self.window, bg="#0b2038", padx=18, pady=12)
        header.pack(fill="x")
        tk.Label(header, text="Thermal-RGB 위치 보정", bg="#0b2038", fg="white",
                 font=("맑은 고딕", 16, "bold")).pack(anchor="w")
        tk.Label(header, text="동일한 위치를 Thermal → 가시광 순서로 클릭하세요. 최소 4쌍이 필요합니다.",
                 bg="#0b2038", fg="#c7d6e5", font=("맑은 고딕", 10)).pack(anchor="w", pady=(3, 0))

        images = tk.Frame(self.window, bg="#f3f6f9", padx=12, pady=12)
        images.pack(fill="both", expand=True)

        self.thermal_canvas, self.thermal_photo, self.thermal_scale = self._image_panel(
            images, "① Thermal 이미지", self.thermal_rgb, "thermal")
        self.visible_canvas, self.visible_photo, self.visible_scale = self._image_panel(
            images, "② 가시광 이미지", self.visible_rgb, "rgb")

        status_frame = tk.Frame(self.window, bg="white", highlightbackground="#d9e2ea",
                                highlightthickness=1, padx=14, pady=9)
        status_frame.pack(fill="x", padx=12, pady=(0, 8))
        self.status_var = tk.StringVar()
        tk.Label(status_frame, textvariable=self.status_var, bg="white", fg="#1f2e3d",
                 font=("맑은 고딕", 10, "bold")).pack(side="left")
        self.count_var = tk.StringVar()
        tk.Label(status_frame, textvariable=self.count_var, bg="white", fg="#647587",
                 font=("맑은 고딕", 10)).pack(side="right")

        buttons = tk.Frame(self.window, bg="#f3f6f9", padx=12, pady=(0, 12))
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
            self.status_var.set("취소할 선택점이 없습니다.")
            return
        self._redraw()

    def reset(self):
        if not self.thermal_pts and not self.rgb_pts:
            self.status_var.set("초기화할 선택점이 없습니다.")
            return
        if messagebox.askyesno("전체 리셋", "선택한 모든 대응점을 삭제할까요?", parent=self.window):
            self.thermal_pts.clear()
            self.rgb_pts.clear()
            self.next_side = "thermal"
            self._redraw()

    def _legacy_save(self):
        if len(self.thermal_pts) != len(self.rgb_pts):
            messagebox.showwarning("저장할 수 없음", "Thermal과 가시광 대응점을 한 쌍으로 완성하세요.", parent=self.window)
            return
        if len(self.thermal_pts) < 4:
            messagebox.showwarning("저장할 수 없음", f"최소 4쌍이 필요합니다. 현재 {len(self.thermal_pts)}쌍입니다.", parent=self.window)
            return

        thermal = np.asarray(self.thermal_pts, dtype=np.float32)
        visible = np.asarray(self.rgb_pts, dtype=np.float32)
        matrix, _ = cv2.findHomography(thermal, visible)
        if matrix is None:
            messagebox.showerror("계산 실패", "Homography를 계산할 수 없습니다. 점이 한 직선에 몰리지 않도록 다시 선택하세요.", parent=self.window)
            return

        output = Path(self.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        np.save(output, matrix)
        projected = cv2.perspectiveTransform(thermal.reshape(-1, 1, 2), matrix).reshape(-1, 2)
        error = np.linalg.norm(projected - visible, axis=1)
        self.saved = True
        messagebox.showinfo(
            "캘리브레이션 저장 완료",
            f"대응점 {len(thermal)}쌍을 저장했습니다.\n평균 재투영 오차: {error.mean():.2f}px\n저장 위치: {output}",
            parent=self.window,
        )
        self.window.destroy()

    def save(self):
        """Validate reprojection error before saving the homography matrix."""
        if len(self.thermal_pts) != len(self.rgb_pts):
            messagebox.showwarning(
                "저장할 수 없음",
                "Thermal과 가시광 대응점을 같은 개수로 선택하세요.",
                parent=self.window,
            )
            return
        if len(self.thermal_pts) < 4:
            messagebox.showwarning(
                "저장할 수 없음",
                f"최소 4쌍의 대응점이 필요합니다. 현재 {len(self.thermal_pts)}쌍입니다.",
                parent=self.window,
            )
            return

        thermal = np.asarray(self.thermal_pts, dtype=np.float32)
        visible = np.asarray(self.rgb_pts, dtype=np.float32)
        matrix, _ = cv2.findHomography(thermal, visible)
        if matrix is None:
            messagebox.showerror(
                "캘리브레이션 계산 실패",
                "Homography를 계산할 수 없습니다. 대응점이 한쪽에 몰리거나 일직선이 되지 않도록 다시 선택하세요.",
                parent=self.window,
            )
            return

        projected = cv2.perspectiveTransform(
            thermal.reshape(-1, 1, 2), matrix
        ).reshape(-1, 2)
        errors = np.linalg.norm(projected - visible, axis=1)
        mean_error = float(errors.mean())
        max_error = float(errors.max())
        rmse = float(np.sqrt(np.mean(np.square(errors))))
        accepted = (
            mean_error <= self.MAX_MEAN_ERROR_PX
            and max_error <= self.MAX_POINT_ERROR_PX
        )
        metrics = (
            f"대응점: {len(thermal)}쌍\n"
            f"평균 오차: {mean_error:.2f}px\n"
            f"최대 오차: {max_error:.2f}px\n"
            f"RMSE: {rmse:.2f}px\n\n"
            f"허용 기준: 평균 {self.MAX_MEAN_ERROR_PX:.1f}px 이하, "
            f"최대 {self.MAX_POINT_ERROR_PX:.1f}px 이하"
        )

        if not accepted:
            messagebox.showwarning(
                "캘리브레이션 보정 필요",
                "캘리브레이션 오차가 허용 기준보다 큽니다.\n"
                "현재 결과는 저장하지 않았습니다.\n\n"
                f"{metrics}\n\n"
                "[전체 리셋] 버튼을 누른 뒤, 특징이 분명하고 화면 전체에 "
                "고르게 분포된 대응점을 선택하여 다시 진행하세요.",
                parent=self.window,
            )
            self.status_var.set(
                f"보정 필요 - 평균 {mean_error:.2f}px / 최대 {max_error:.2f}px. "
                "전체 리셋 후 다시 진행하세요."
            )
            return

        output = Path(self.output_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        np.save(output, matrix)
        self.saved = True
        messagebox.showinfo(
            "캘리브레이션 정상 완료",
            "오차가 허용 기준 이내여서 보정값을 저장했습니다.\n\n"
            f"{metrics}\n\n저장 위치: {output}",
            parent=self.window,
        )
        self.window.destroy()

    def close(self):
        if (self.thermal_pts or self.rgb_pts) and not self.saved:
            if not messagebox.askyesno("저장하지 않고 종료", "선택한 대응점을 저장하지 않고 종료할까요?", parent=self.window):
                return
        self.window.destroy()

    def show(self):
        self.window.update_idletasks()
        x = max(0, (self.window.winfo_screenwidth() - self.window.winfo_reqwidth()) // 2)
        y = max(0, (self.window.winfo_screenheight() - self.window.winfo_reqheight()) // 2)
        self.window.geometry(f"+{x}+{y}")
        if self._owns_root:
            self.window.mainloop()
        else:
            self.window.grab_set()
            self.window.wait_window()
        return self.saved


def _resolve_paths(thermal_path=None, rgb_path=None):
    cfg = load_config()
    if thermal_path:
        return thermal_path, rgb_path or os.path.splitext(thermal_path)[0] + "_visual.jpg", cfg
    if len(sys.argv) >= 2:
        thermal = sys.argv[1]
        visible = sys.argv[2] if len(sys.argv) >= 3 else os.path.splitext(thermal)[0] + "_visual.jpg"
        return thermal, visible, cfg
    thermal_files = sorted(p for p in glob.glob(os.path.join(cfg.paths.dataset_dir, "*.jpg")) if "_visual" not in p)
    if not thermal_files:
        return None, None, cfg
    thermal = thermal_files[-1]
    return thermal, os.path.splitext(thermal)[0] + "_visual.jpg", cfg


def run_calibration(thermal_path=None, rgb_path=None):
    thermal_path, rgb_path, cfg = _resolve_paths(thermal_path, rgb_path)
    if not thermal_path or not os.path.isfile(thermal_path):
        messagebox.showerror("캘리브레이션", "Thermal 이미지를 찾을 수 없습니다.")
        return False
    if not rgb_path or not os.path.isfile(rgb_path):
        messagebox.showerror("캘리브레이션", "대응하는 가시광 이미지를 찾을 수 없습니다.")
        return False
    return CalibrationDialog(thermal_path, rgb_path, cfg.paths.homography_path).show()


if __name__ == "__main__":
    run_calibration()
