"""Resizable Tkinter dialogs for ROI editing and Thermal/RGB calibration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import cv2
import numpy as np
from PIL import Image, ImageTk

from ..config import RoiEntry, load_config, save_config


try:
    _LANCZOS = Image.Resampling.LANCZOS
except AttributeError:  # Pillow < 9.1
    _LANCZOS = Image.LANCZOS


@dataclass(frozen=True)
class ImageRect:
    x: int
    y: int
    width: int
    height: int
    scale: float

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x < self.x + self.width and self.y <= y < self.y + self.height

    def to_source(self, x: int, y: int) -> tuple[int, int]:
        return (
            int((x - self.x) / self.scale),
            int((y - self.y) / self.scale),
        )

    def to_canvas(self, x: float, y: float) -> tuple[int, int]:
        return (
            self.x + int(x * self.scale),
            self.y + int(y * self.scale),
        )


def fit_image_rect(
    source_width: int,
    source_height: int,
    area_x: int,
    area_y: int,
    area_width: int,
    area_height: int,
) -> ImageRect:
    """Fit an image inside an area without changing its aspect ratio."""
    usable_width = max(1, area_width)
    usable_height = max(1, area_height)
    scale = min(usable_width / source_width, usable_height / source_height)
    width = max(1, int(source_width * scale))
    height = max(1, int(source_height * scale))
    return ImageRect(
        area_x + (usable_width - width) // 2,
        area_y + (usable_height - height) // 2,
        width,
        height,
        scale,
    )


def _roi_values(entry) -> tuple[str, int, int, int, int]:
    if isinstance(entry, dict):
        return (
            entry.get("name", "ROI"),
            int(entry["x1"]), int(entry["y1"]),
            int(entry["x2"]), int(entry["y2"]),
        )
    return entry.name, entry.x1, entry.y1, entry.x2, entry.y2


class RoiTkDialog:
    def __init__(self, parent, thermal_path: str, visual_path: str):
        self.parent = parent
        self.cfg = load_config(force_reload=True)
        self.thermal_path = thermal_path
        self.visual_path = visual_path
        self.result = False
        self.rois: list[dict] = []
        self.selected = -1
        self.undo_stack: list[list[dict]] = []
        self.drag_start: tuple[int, int] | None = None
        self.drag_end: tuple[int, int] | None = None
        self.photo = None
        self.image_rect: ImageRect | None = None
        self._redraw_id = None

        homography_path = Path(self.cfg.paths.homography_path)
        if not homography_path.exists():
            raise FileNotFoundError("캘리브레이션 정보가 없습니다. 캘리브레이션을 먼저 실행하세요.")
        self.homography = np.load(homography_path)
        if self.homography.shape != (3, 3):
            raise ValueError("캘리브레이션 행렬 형식이 올바르지 않습니다.")
        self.inverse_homography = np.linalg.inv(self.homography)
        self.image = Image.open(visual_path).convert("RGB")
        self.source_width, self.source_height = self.image.size
        self._load_rois()

        self.win = tk.Toplevel(parent)
        self.win.title("ROI 설정 · 작업 중")
        self.win.geometry("800x430")
        self.win.minsize(600, 360)
        self.win.resizable(True, True)
        self.win.transient(parent)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.columnconfigure(0, weight=1)
        self.win.rowconfigure(1, weight=1)

        header = ttk.Frame(self.win, padding=(12, 8))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            header,
            text="가시광 이미지에서 감시 영역을 지정하세요.",
            font=("맑은 고딕", 10, "bold"),
        ).pack(side="left")
        self.status = ttk.Label(header, text="")
        self.status.pack(side="right")

        self.canvas = tk.Canvas(self.win, background="#0b1014", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=10)
        self.canvas.bind("<Configure>", self._schedule_redraw)
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)

        toolbar = ttk.Frame(self.win, padding=10)
        toolbar.grid(row=2, column=0, sticky="ew")
        for column, (text, command) in enumerate((
            ("New", self.add),
            ("Next", self.next),
            ("Del", self.delete),
            ("Undo", self.undo),
            ("Reset", self.reset),
            ("Save", self.save),
            ("Quit", self.close),
        )):
            toolbar.columnconfigure(column, weight=1)
            ttk.Button(toolbar, text=text, command=command).grid(
                row=0, column=column, sticky="ew", padx=3,
            )

        for key, command in (
            ("<Key-n>", self.add),
            ("<Tab>", self.next),
            ("<Delete>", self.delete),
            ("<Key-z>", self.undo),
            ("<Key-r>", self.reset),
            ("<Key-s>", self.save),
            ("<Key-q>", self.close),
            ("<Escape>", self.close),
        ):
            self.win.bind(key, lambda _event, fn=command: fn())

    def _load_rois(self):
        entries = self.cfg.roi.rois or []
        if not entries:
            entries = [RoiEntry(
                name="ROI-1",
                x1=self.cfg.roi.x1,
                y1=self.cfg.roi.y1,
                x2=self.cfg.roi.x2,
                y2=self.cfg.roi.y2,
            )]
        for entry in entries:
            name, x1, y1, x2, y2 = _roi_values(entry)
            thermal = np.array([[x1, y1], [x2, y2]], dtype=np.float32).reshape(-1, 1, 2)
            visual = cv2.perspectiveTransform(thermal, self.homography).reshape(2, 2)
            self.rois.append({
                "name": name,
                "x1": int(round(visual[0, 0])),
                "y1": int(round(visual[0, 1])),
                "x2": int(round(visual[1, 0])),
                "y2": int(round(visual[1, 1])),
            })
        self.selected = 0 if self.rois else -1

    def show(self) -> bool:
        self.win.grab_set()
        self.win.focus_force()
        self.win.wait_window()
        return self.result

    def _snapshot(self):
        self.undo_stack.append([dict(item) for item in self.rois])
        del self.undo_stack[:-30]

    def _schedule_redraw(self, _event=None):
        if self._redraw_id:
            self.win.after_cancel(self._redraw_id)
        self._redraw_id = self.win.after(40, self.redraw)

    def redraw(self):
        self._redraw_id = None
        width = max(1, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        self.image_rect = fit_image_rect(
            self.source_width, self.source_height, 0, 0, width, height,
        )
        resized = self.image.resize(
            (self.image_rect.width, self.image_rect.height), _LANCZOS,
        )
        self.photo = ImageTk.PhotoImage(resized)
        self.canvas.delete("all")
        self.canvas.create_image(
            self.image_rect.x,
            self.image_rect.y,
            image=self.photo,
            anchor="nw",
        )
        for index, roi in enumerate(self.rois):
            x1, y1 = self.image_rect.to_canvas(roi["x1"], roi["y1"])
            x2, y2 = self.image_rect.to_canvas(roi["x2"], roi["y2"])
            color = "#00ff55" if index == self.selected else "#38a3ff"
            self.canvas.create_rectangle(
                x1, y1, x2, y2,
                outline=color,
                width=3 if index == self.selected else 1,
            )
            self.canvas.create_text(
                x1 + 4, max(self.image_rect.y + 10, y1 - 10),
                text=roi["name"], fill=color, anchor="w",
            )
        if self.drag_start and self.drag_end:
            x1, y1 = self.image_rect.to_canvas(*self.drag_start)
            x2, y2 = self.image_rect.to_canvas(*self.drag_end)
            self.canvas.create_rectangle(x1, y1, x2, y2, outline="#ffff00", width=2)
        selected_name = self.rois[self.selected]["name"] if 0 <= self.selected < len(self.rois) else "없음"
        self.status.configure(text=f"선택: {selected_name} · 전체 {len(self.rois)}개")

    def _source_point(self, event) -> tuple[int, int] | None:
        if not self.image_rect or not self.image_rect.contains(event.x, event.y):
            return None
        x, y = self.image_rect.to_source(event.x, event.y)
        return (
            max(0, min(x, self.source_width - 1)),
            max(0, min(y, self.source_height - 1)),
        )

    def _press(self, event):
        point = self._source_point(event)
        if point:
            self.drag_start = self.drag_end = point

    def _drag(self, event):
        point = self._source_point(event)
        if self.drag_start and point:
            self.drag_end = point
            self.redraw()

    def _release(self, event):
        point = self._source_point(event)
        if not self.drag_start or not point:
            self.drag_start = self.drag_end = None
            self.redraw()
            return
        self.drag_end = point
        x1, x2 = sorted((self.drag_start[0], self.drag_end[0]))
        y1, y2 = sorted((self.drag_start[1], self.drag_end[1]))
        if x2 - x1 > 5 and y2 - y1 > 5:
            self._snapshot()
            if 0 <= self.selected < len(self.rois):
                name = self.rois[self.selected]["name"]
                self.rois[self.selected] = {
                    "name": name, "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                }
            else:
                self.rois.append({
                    "name": f"ROI-{len(self.rois) + 1}",
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                })
                self.selected = len(self.rois) - 1
        self.drag_start = self.drag_end = None
        self.redraw()

    def add(self):
        name = simpledialog.askstring(
            "ROI 추가",
            "ROI 이름을 입력하세요.",
            initialvalue=f"ROI-{len(self.rois) + 1}",
            parent=self.win,
        )
        if not name:
            return
        self._snapshot()
        self.rois.append({
            "name": name,
            "x1": 0, "y1": 0,
            "x2": self.source_width - 1,
            "y2": self.source_height - 1,
        })
        self.selected = len(self.rois) - 1
        self.redraw()

    def next(self):
        if self.rois:
            self.selected = (self.selected + 1) % len(self.rois)
            self.redraw()

    def delete(self):
        if 0 <= self.selected < len(self.rois):
            self._snapshot()
            self.rois.pop(self.selected)
            self.selected = min(self.selected, len(self.rois) - 1)
            self.redraw()

    def undo(self):
        if self.undo_stack:
            self.rois = self.undo_stack.pop()
            self.selected = min(max(0, self.selected), len(self.rois) - 1)
            self.redraw()

    def reset(self):
        if self.rois:
            self._snapshot()
            self.rois.clear()
            self.selected = -1
            self.redraw()

    def save(self):
        if not self.rois:
            messagebox.showwarning("ROI 설정", "ROI를 하나 이상 지정하세요.", parent=self.win)
            return
        entries = []
        for roi in self.rois:
            visual = np.array([
                [roi["x1"], roi["y1"]],
                [roi["x2"], roi["y2"]],
            ], dtype=np.float32).reshape(-1, 1, 2)
            thermal = cv2.perspectiveTransform(
                visual, self.inverse_homography,
            ).reshape(2, 2)
            tx1, ty1 = thermal[0]
            tx2, ty2 = thermal[1]
            entries.append(RoiEntry(
                name=roi["name"],
                x1=max(0, min(int(round(min(tx1, tx2))), 639)),
                y1=max(0, min(int(round(min(ty1, ty2))), 479)),
                x2=max(0, min(int(round(max(tx1, tx2))), 639)),
                y2=max(0, min(int(round(max(ty1, ty2))), 479)),
            ))
        self.cfg.roi.rois = entries
        first = entries[0]
        self.cfg.roi.x1, self.cfg.roi.y1 = first.x1, first.y1
        self.cfg.roi.x2, self.cfg.roi.y2 = first.x2, first.y2
        save_config(self.cfg)
        self.result = True
        self.close()

    def close(self):
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        if self.win.winfo_exists():
            self.win.destroy()


class CalibrationTkDialog:
    MAX_MEAN_ERROR_PX = 5.0
    MAX_POINT_ERROR_PX = 10.0

    def __init__(self, parent, thermal_path: str, visual_path: str):
        self.parent = parent
        self.cfg = load_config(force_reload=True)
        self.thermal = Image.open(thermal_path).convert("RGB")
        self.visual = Image.open(visual_path).convert("RGB")
        self.thermal_pts: list[list[int]] = []
        self.visual_pts: list[list[int]] = []
        self.next_side = "visual"
        self.result = False
        self.visual_photo = None
        self.thermal_photo = None
        self.visual_rect: ImageRect | None = None
        self.thermal_rect: ImageRect | None = None
        self._redraw_id = None

        self.win = tk.Toplevel(parent)
        self.win.title("캘리브레이션 · 작업 중")
        self.win.geometry("900x520")
        self.win.minsize(680, 400)
        self.win.resizable(True, True)
        self.win.transient(parent)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.win.columnconfigure(0, weight=1)
        self.win.rowconfigure(1, weight=1)

        header = ttk.Frame(self.win, padding=(12, 8))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(
            header,
            text="왼쪽 가시광과 오른쪽 열화상에서 같은 위치를 번갈아 선택하세요.",
            font=("맑은 고딕", 10, "bold"),
        ).pack(side="left")
        self.status = ttk.Label(header)
        self.status.pack(side="right")

        self.canvas = tk.Canvas(self.win, background="#0b1014", highlightthickness=0)
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=10)
        self.canvas.bind("<Configure>", self._schedule_redraw)
        self.canvas.bind("<Button-1>", self._click)

        toolbar = ttk.Frame(self.win, padding=10)
        toolbar.grid(row=2, column=0, sticky="ew")
        for column, (text, command) in enumerate((
            ("Save", self.save),
            ("Undo", self.undo),
            ("Reset", self.reset),
            ("Quit", self.close),
        )):
            toolbar.columnconfigure(column, weight=1)
            ttk.Button(toolbar, text=text, command=command).grid(
                row=0, column=column, sticky="ew", padx=4,
            )
        for key, command in (
            ("<Key-s>", self.save),
            ("<Key-z>", self.undo),
            ("<Key-r>", self.reset),
            ("<Key-q>", self.close),
            ("<Escape>", self.close),
        ):
            self.win.bind(key, lambda _event, fn=command: fn())

    def show(self) -> bool:
        self.win.grab_set()
        self.win.focus_force()
        self.win.wait_window()
        return self.result

    def _schedule_redraw(self, _event=None):
        if self._redraw_id:
            self.win.after_cancel(self._redraw_id)
        self._redraw_id = self.win.after(40, self.redraw)

    def redraw(self):
        self._redraw_id = None
        width = max(2, self.canvas.winfo_width())
        height = max(1, self.canvas.winfo_height())
        half = width // 2
        self.visual_rect = fit_image_rect(
            *self.visual.size, 0, 0, half - 4, height,
        )
        self.thermal_rect = fit_image_rect(
            *self.thermal.size, half + 4, 0, width - half - 4, height,
        )
        self.visual_photo = ImageTk.PhotoImage(self.visual.resize(
            (self.visual_rect.width, self.visual_rect.height), _LANCZOS,
        ))
        self.thermal_photo = ImageTk.PhotoImage(self.thermal.resize(
            (self.thermal_rect.width, self.thermal_rect.height), _LANCZOS,
        ))
        self.canvas.delete("all")
        self.canvas.create_image(
            self.visual_rect.x, self.visual_rect.y,
            image=self.visual_photo, anchor="nw",
        )
        self.canvas.create_image(
            self.thermal_rect.x, self.thermal_rect.y,
            image=self.thermal_photo, anchor="nw",
        )
        self.canvas.create_line(half, 0, half, height, fill="#768390")
        self.canvas.create_text(12, 14, text="가시광", fill="white", anchor="w")
        self.canvas.create_text(half + 12, 14, text="열화상", fill="white", anchor="w")
        for index, point in enumerate(self.visual_pts):
            self._draw_point(self.visual_rect, point, index)
        for index, point in enumerate(self.thermal_pts):
            self._draw_point(self.thermal_rect, point, index)
        waiting = "가시광 점 선택" if self.next_side == "visual" else "대응 열화상 점 선택"
        self.status.configure(
            text=f"{waiting} · 완료 {min(len(self.visual_pts), len(self.thermal_pts))}쌍",
        )

    def _draw_point(self, rect: ImageRect, point, index):
        x, y = rect.to_canvas(*point)
        self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5, outline="#00ff55", width=2)
        self.canvas.create_line(x - 8, y, x + 8, y, fill="#ff4040")
        self.canvas.create_line(x, y - 8, x, y + 8, fill="#ff4040")
        self.canvas.create_text(x + 9, y - 9, text=str(index + 1), fill="#00ff55")

    def _click(self, event):
        if self.next_side == "visual":
            if not self.visual_rect or not self.visual_rect.contains(event.x, event.y):
                return
            self.visual_pts.append(list(self.visual_rect.to_source(event.x, event.y)))
            self.next_side = "thermal"
        else:
            if not self.thermal_rect or not self.thermal_rect.contains(event.x, event.y):
                return
            self.thermal_pts.append(list(self.thermal_rect.to_source(event.x, event.y)))
            self.next_side = "visual"
        self.redraw()

    def undo(self):
        if self.next_side == "thermal" and self.visual_pts:
            self.visual_pts.pop()
            self.next_side = "visual"
        elif self.next_side == "visual" and self.thermal_pts:
            self.thermal_pts.pop()
            self.next_side = "thermal"
        self.redraw()

    def reset(self):
        self.visual_pts.clear()
        self.thermal_pts.clear()
        self.next_side = "visual"
        self.redraw()

    def save(self):
        if len(self.visual_pts) < 4 or len(self.visual_pts) != len(self.thermal_pts):
            messagebox.showwarning(
                "캘리브레이션",
                f"대응점을 최소 4쌍 선택하세요.\n현재 {min(len(self.visual_pts), len(self.thermal_pts))}쌍",
                parent=self.win,
            )
            return
        thermal = np.array(self.thermal_pts, dtype=np.float32)
        visual = np.array(self.visual_pts, dtype=np.float32)
        homography, _ = cv2.findHomography(thermal, visual)
        if homography is None:
            messagebox.showerror(
                "캘리브레이션",
                "보정 행렬을 계산할 수 없습니다. 점을 다시 선택하세요.",
                parent=self.win,
            )
            return
        projected = cv2.perspectiveTransform(
            thermal.reshape(-1, 1, 2), homography,
        ).reshape(-1, 2)
        errors = np.linalg.norm(projected - visual, axis=1)
        mean_error = float(errors.mean())
        max_error = float(errors.max())
        if mean_error > self.MAX_MEAN_ERROR_PX or max_error > self.MAX_POINT_ERROR_PX:
            messagebox.showwarning(
                "캘리브레이션 보정 필요",
                "캘리브레이션 오차가 허용 기준보다 큽니다.\n"
                "보정값을 저장하지 않았습니다.\n\n"
                f"평균 오차: {mean_error:.2f}px\n"
                f"최대 오차: {max_error:.2f}px\n\n"
                "Reset 후 화면 전체에 고르게 대응점을 다시 선택하세요.",
                parent=self.win,
            )
            return
        np.save(self.cfg.paths.homography_path, homography)
        self.result = True
        messagebox.showinfo(
            "캘리브레이션 완료",
            f"보정값을 저장했습니다.\n\n평균 오차: {mean_error:.2f}px\n최대 오차: {max_error:.2f}px",
            parent=self.win,
        )
        self.close()

    def close(self):
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        if self.win.winfo_exists():
            self.win.destroy()


def show_roi_dialog(parent, thermal_path: str, visual_path: str) -> bool:
    return RoiTkDialog(parent, thermal_path, visual_path).show()


def show_calibration_dialog(parent, thermal_path: str, visual_path: str) -> bool:
    return CalibrationTkDialog(parent, thermal_path, visual_path).show()
