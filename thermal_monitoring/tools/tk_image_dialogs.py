"""Resizable Tkinter dialog for ROI editing."""

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


def calibration_hull_canvas_points(
    hull: np.ndarray | None,
    image_rect: ImageRect,
) -> list[int]:
    """Convert the saved Visual calibration hull to Tk canvas coordinates."""
    if hull is None:
        return []
    points = np.asarray(hull, dtype=np.float32).reshape(-1, 2)
    if len(points) < 3:
        return []
    canvas_points: list[int] = []
    for x, y in points:
        canvas_points.extend(image_rect.to_canvas(float(x), float(y)))
    return canvas_points


def roi_coordinate_text(roi: dict) -> str:
    """환경설정 ROI 편집창에 표시할 좌표 문자열."""
    x1, y1 = int(roi["x1"]), int(roi["y1"])
    x2, y2 = int(roi["x2"]), int(roi["y2"])
    return f"({x1}, {y1})-({x2}, {y2}) · {abs(x2 - x1)}×{abs(y2 - y1)} px"


def transformed_roi_bounds(roi: dict, homography: np.ndarray) -> dict:
    """ROI 사각형 네 꼭짓점을 변환하고 축 정렬 좌표 범위를 반환한다."""
    corners = np.array([
        [roi["x1"], roi["y1"]],
        [roi["x2"], roi["y1"]],
        [roi["x2"], roi["y2"]],
        [roi["x1"], roi["y2"]],
    ], dtype=np.float32).reshape(-1, 1, 2)
    transformed = cv2.perspectiveTransform(corners, homography).reshape(-1, 2)
    return {
        "x1": int(round(transformed[:, 0].min())),
        "y1": int(round(transformed[:, 1].min())),
        "x2": int(round(transformed[:, 0].max())),
        "y2": int(round(transformed[:, 1].max())),
    }


def thermal_bounds_for_roi(roi: dict, inverse_homography: np.ndarray) -> dict:
    """기존 ROI는 저장 원본을, 편집된 ROI는 Visual 역변환값을 반환한다."""
    original = roi.get("_thermal_bounds")
    if original is not None:
        return dict(original)
    return transformed_roi_bounds(roi, inverse_homography)


def recommended_window_size(
    screen_width: int,
    screen_height: int,
    width_ratio: float,
    height_ratio: float,
    minimum: tuple[int, int],
    maximum: tuple[int, int],
) -> tuple[int, int]:
    """Return a resolution-aware initial size in Tk logical pixels."""
    width = max(minimum[0], min(int(screen_width * width_ratio), maximum[0]))
    height = max(minimum[1], min(int(screen_height * height_ratio), maximum[1]))
    return width, height


def apply_adaptive_geometry(
    window,
    parent,
    width_ratio: float,
    height_ratio: float,
    minimum: tuple[int, int],
    maximum: tuple[int, int],
) -> None:
    """Size a dialog for the current Tk display and center it over its parent."""
    parent.update_idletasks()
    width, height = recommended_window_size(
        parent.winfo_screenwidth(),
        parent.winfo_screenheight(),
        width_ratio,
        height_ratio,
        minimum,
        maximum,
    )
    center_x = parent.winfo_rootx() + parent.winfo_width() // 2
    center_y = parent.winfo_rooty() + parent.winfo_height() // 2
    window.geometry(
        f"{width}x{height}+{center_x - width // 2}+{center_y - height // 2}",
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
    def __init__(self, parent, thermal_path: str, visual_path: str, save_handler=None):
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
        self.save_handler = save_handler

        homography_path = Path(self.cfg.paths.homography_path)
        if not homography_path.exists():
            raise FileNotFoundError("캘리브레이션 정보가 없습니다. 캘리브레이션을 먼저 실행하세요.")
        calib_data = np.load(homography_path, allow_pickle=True)
        if isinstance(calib_data, np.ndarray) and calib_data.ndim == 0:
            calib_data = calib_data.item()
        if isinstance(calib_data, dict):
            self.homography = calib_data["H"]
            self._calib_visual_pts = calib_data.get("visual_pts")
        else:
            self.homography = calib_data
            self._calib_visual_pts = None
        if self.homography.shape != (3, 3):
            raise ValueError("캘리브레이션 행렬 형식이 올바르지 않습니다.")
        self.inverse_homography = np.linalg.inv(self.homography)
        
        # convex hull for calibration boundary check
        self._calib_hull = None
        if self._calib_visual_pts is not None and len(self._calib_visual_pts) >= 3:
            self._calib_hull = cv2.convexHull(
                self._calib_visual_pts.reshape(-1, 1, 2).astype(np.float32)
            )
        
        self.image = Image.open(visual_path).convert("RGB")
        self.source_width, self.source_height = self.image.size
        self._load_rois()

        self.win = tk.Toplevel(parent)
        self.win.title("ROI 설정 · 작업 중")
        apply_adaptive_geometry(
            self.win,
            parent,
            width_ratio=0.70,
            height_ratio=0.70,
            minimum=(600, 360),
            maximum=(1400, 900),
        )
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
            # thermal(640x480) ROI 네 꼭짓점을 visual 좌표로 변환
            thermal = np.array([
                [x1, y1], [x2, y1], [x2, y2], [x1, y2],
            ], dtype=np.float32).reshape(-1, 1, 2)
            visual = cv2.perspectiveTransform(thermal, self.homography).reshape(-1, 2)
            self.rois.append({
                "name": name,
                "x1": int(round(visual[:, 0].min())),
                "y1": int(round(visual[:, 1].min())),
                "x2": int(round(visual[:, 0].max())),
                "y2": int(round(visual[:, 1].max())),
                # 화면 표시용 Visual AABB와 별도로 기존 Thermal ROI를 보존한다.
                # 사용자가 이 ROI를 다시 그리면 새 dict로 교체되어 이 값이 제거된다.
                "_thermal_bounds": {
                    "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                },
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
        hull_points = calibration_hull_canvas_points(
            self._calib_hull,
            self.image_rect,
        )
        if hull_points:
            # 저장 시 pointPolygonTest에 사용하는 것과 동일한 영역을 먼저 그린다.
            self.canvas.create_polygon(
                *hull_points,
                fill="#e2a93b",
                stipple="gray25",
                outline="#ffd166",
                width=2,
                dash=(6, 4),
            )
            label_x, label_y = hull_points[0], hull_points[1]
            self.canvas.create_text(
                label_x + 6,
                max(self.image_rect.y + 12, label_y - 12),
                text="캘리브레이션 ROI 설정 가능 영역",
                fill="#ffd166",
                anchor="w",
                font=("맑은 고딕", 9, "bold"),
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
            drag_roi = {
                "x1": min(self.drag_start[0], self.drag_end[0]),
                "y1": min(self.drag_start[1], self.drag_end[1]),
                "x2": max(self.drag_start[0], self.drag_end[0]),
                "y2": max(self.drag_start[1], self.drag_end[1]),
            }
            self.status.configure(
                text=(
                    "설정 중(Thermal 640×480 좌표): "
                    f"{roi_coordinate_text(transformed_roi_bounds(drag_roi, self.inverse_homography))}"
                ),
            )
        elif 0 <= self.selected < len(self.rois):
            selected = self.rois[self.selected]
            thermal_bounds = thermal_bounds_for_roi(
                selected, self.inverse_homography,
            )
            self.status.configure(
                text=(
                    f"선택: {selected['name']} · Thermal 640×480 좌표: "
                    f"{roi_coordinate_text(thermal_bounds)} · 전체 {len(self.rois)}개"
                ),
            )
        else:
            self.status.configure(text=f"선택: 없음 · 전체 {len(self.rois)}개")

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
            # hull boundary check: 네 꼭짓점이 모두 hull 안에 있어야 통과
            if self._calib_hull is not None:
                for corner in (
                    (roi["x1"], roi["y1"]), (roi["x2"], roi["y1"]),
                    (roi["x2"], roi["y2"]), (roi["x1"], roi["y2"]),
                ):
                    if cv2.pointPolygonTest(self._calib_hull, corner, False) < 0:
                        messagebox.showwarning(
                            "ROI 범위 초과",
                            f"'{roi['name']}' ROI가 캘리브레이션 영역 밖에 있습니다.\n\n"
                            f"호모그래피 변환이 정확하지 않은 영역입니다.\n"
                            f"ROI를 캘리브레이션 대응점 범위 안으로 옮기세요.",
                            parent=self.win,
                        )
                        return False

            # 사각형 네 꼭짓점을 모두 변환한 후 축 정렬 바운딩 박스 사용.
            thermal_bounds = thermal_bounds_for_roi(
                roi, self.inverse_homography,
            )
            raw_x1 = thermal_bounds["x1"]
            raw_y1 = thermal_bounds["y1"]
            raw_x2 = thermal_bounds["x2"]
            raw_y2 = thermal_bounds["y2"]

            # 클램핑 → 원하지 않은 위치로 바뀌므로 저장 거부
            x1 = max(0, min(raw_x1, 639))
            y1 = max(0, min(raw_y1, 479))
            x2 = max(0, min(raw_x2, 639))
            y2 = max(0, min(raw_y2, 479))

            if raw_x1 != x1 or raw_y1 != y1 or raw_x2 != x2 or raw_y2 != y2:
                messagebox.showwarning(
                    "ROI 범위 초과",
                    f"'{roi['name']}' ROI가 열화상 카메라 시야를 벗어납니다.\n\n"
                    f"변환 좌표: ({raw_x1},{raw_y1})-({raw_x2},{raw_y2})\n"
                    f"열화상 범위: 0~639 × 0~479\n\n"
                    f"ROI를 카메라 중앙 쪽으로 옮기거나\n"
                    f"캘리브레이션 대응점을 카메라 전체에 고르게 다시 지정하세요.",
                    parent=self.win,
                )
                return False

            if x1 >= x2 or y1 >= y2:
                messagebox.showwarning(
                    "ROI 설정",
                    f"'{roi['name']}' 영역이 유효하지 않습니다.\n"
                    f"좌표: ({x1},{y1})-({x2},{y2})\n"
                    "ROI 박스가 너무 작거나 화면 밖으로 벗어났습니다.",
                    parent=self.win,
                )
                return False
            entries.append(RoiEntry(
                name=roi["name"],
                x1=x1, y1=y1, x2=x2, y2=y2,
            ))
        if not entries:
            return False
        if self.save_handler is not None:
            try:
                self.save_handler(entries)
            except Exception as exc:
                messagebox.showerror(
                    "ROI DB 저장 실패",
                    "ROI를 데이터베이스에 저장하지 못했습니다.\n"
                    "입력한 영역은 유지되므로 연결 상태를 확인한 뒤 다시 저장하세요.\n\n"
                    f"{exc}",
                    parent=self.win,
                )
                return
        self.cfg.roi.rois = entries
        first = entries[0]
        self.cfg.roi.x1, self.cfg.roi.y1 = first.x1, first.y1
        self.cfg.roi.x2, self.cfg.roi.y2 = first.x2, first.y2
        try:
            save_config(self.cfg)
        except Exception as exc:
            messagebox.showerror(
                "ROI 저장 실패",
                f"config.json 저장 중 오류가 발생했습니다.\n{exc}",
                parent=self.win,
            )
            return
        self.result = True
        self.close()

    def close(self):
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        if self.win.winfo_exists():
            self.win.destroy()


def show_roi_dialog(
    parent,
    thermal_path: str,
    visual_path: str,
    save_handler=None,
) -> bool:
    return RoiTkDialog(
        parent,
        thermal_path,
        visual_path,
        save_handler=save_handler,
    ).show()
