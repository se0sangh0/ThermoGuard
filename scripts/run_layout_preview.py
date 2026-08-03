#!/usr/bin/env python3
"""최근 정상 촬영본으로 대시보드 레이아웃만 안전하게 미리 본다.

카메라 연결, 자동 새로고침, 백엔드 저장, 텔레그램 전송은 수행하지 않는다.
원본 데이터는 읽기만 하고 임시 폴더에 최신 thermal/visual/NPY 세트를 복사한다.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
import tkinter as tk
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from thermal_monitoring.config import load_config
from thermal_monitoring.data.pairs import ensure_npy, latest_complete_pair
from thermal_monitoring.tools.product_dashboard import COLORS, ProductDashboard


class LayoutPreviewDashboard(ProductDashboard):
    """외부 통신과 주기 작업을 끈 단일 이미지 미리보기 대시보드."""

    def _check_connection_async(self, resume_monitoring: bool = False):
        self._connection_ok = None
        self._set_system_state("레이아웃 미리보기", COLORS["blue"])

    def _schedule_refresh(self, delay_ms=None):
        # 미리보기에서는 같은 파일을 반복 분석하거나 유지보수 작업을 하지 않는다.
        self.timer_id = None

    def _apply_analysis_result(self, result: dict, generation: int):
        # 과거 이미지가 위험 상태여도 알림 발송 대상으로 만들지 않는다.
        result["alarm"] = False
        super()._apply_analysis_result(result, generation)
        self._set_system_state("레이아웃 미리보기", COLORS["blue"])


def _copy_latest_complete_set(source_dir: Path, preview_dir: Path) -> str:
    pair = latest_complete_pair(source_dir)
    if pair is None:
        raise RuntimeError(f"열화상·실화상 완성 세트가 없습니다: {source_dir}")

    thermal, visual = pair
    npy = ensure_npy(thermal)
    for source in (thermal, visual, npy):
        shutil.copy2(source, preview_dir / source.name)
    return thermal.stem


def main() -> None:
    cfg = load_config(force_reload=True)
    source_dir = Path(cfg.paths.dataset_dir).expanduser().resolve()

    with tempfile.TemporaryDirectory(prefix="thermoguard-layout-preview-") as tmp:
        preview_dir = Path(tmp)
        capture_id = _copy_latest_complete_set(source_dir, preview_dir)

        root = tk.Tk()
        dashboard = LayoutPreviewDashboard(root)
        dashboard.cfg.paths.dataset_dir = str(preview_dir)
        dashboard.cfg.paths.overlay_dir = str(preview_dir / "overlay")
        dashboard.cfg.backend.enabled = False
        dashboard.capture_paused_by_user = True
        dashboard.root.title(f"로봇 열화상 모니터링 · 레이아웃 미리보기 ({capture_id})")
        dashboard._add_operating_log(
            "레이아웃 미리보기",
            "준비 완료",
            f"최근 정상 촬영본 {capture_id} · 외부 전송 비활성화",
        )
        dashboard._schedule_analysis()
        root.protocol("WM_DELETE_WINDOW", dashboard.on_close)
        root.mainloop()


if __name__ == "__main__":
    main()
