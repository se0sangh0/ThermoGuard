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


#Thermal 이미지 클릭 이벤트
def click_thermal(event, x, y, flags, param):
    global pair_state
    if event == cv2.EVENT_LBUTTONDOWN:
        if pair_state == "thermal":
            thermal_pts.append([x, y])
            pair_state = "rgb"
            print(f"[{len(thermal_pts)}] Thermal : {x}, {y} -> RGB 클릭 대기")
        else:
            print("[경고] RGB 포인트를 먼저 클릭하세요.")

#RGB 이미지 클릭 이벤트
def click_rgb(event, x, y, flags, param):
    global pair_state
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
    global thermal_pts, rgb_pts, pair_state
    thermal_pts.clear()
    rgb_pts.clear()
    pair_state = "thermal"

    cfg = load_config()
    DATASET_DIR = cfg.paths.dataset_dir
    DISPLAY_WIDTH = cfg.display.display_width

    # 이미지 경로 결정
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

    while True:
        t = thermal_disp.copy()
        r = rgb_disp.copy()

        for pt in thermal_pts:
            dp = (int(pt[0] / thermal_scale), int(pt[1] / thermal_scale))
            cv2.circle(t, dp, 5, (0, 0, 255), -1)
        for pt in rgb_pts:
            dp = (int(pt[0] / rgb_scale), int(pt[1] / rgb_scale))
            cv2.circle(r, dp, 5, (0, 0, 255), -1)

        for i, pt in enumerate(thermal_pts):
            dp = (int(pt[0] / thermal_scale), int(pt[1] / thermal_scale))
            cv2.putText(t, str(i + 1), dp, cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        for i, pt in enumerate(rgb_pts):
            dp = (int(pt[0] / rgb_scale), int(pt[1] / rgb_scale))
            cv2.putText(r, str(i + 1), dp, cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)

        cv2.putText(t, "Thermal | S: save  R: reset  Z: undo  Q: quit",
                    (10, t.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)
        cv2.putText(r, "RGB | S: save  R: reset  Z: undo  Q: quit",
                    (10, r.shape[0] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (200, 200, 200), 1)

        cv2.imshow("Thermal", t)
        cv2.imshow("RGB", r)

        key = cv2.waitKey(1)

        if key == ord('q') or key == 27:
            print("Quit without saving.")
            cv2.destroyAllWindows()
            return
        elif key == ord('r'):
            thermal_pts.clear()
            rgb_pts.clear()
            pair_state = "thermal"
            print("[Reset] All points cleared.")
        elif key == ord('z'):
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
        elif key == ord('s'):
            if len(thermal_pts) < 4:
                print(f"[Save] Need at least 4 point pairs, currently have {len(thermal_pts)}")
            elif len(thermal_pts) != len(rgb_pts):
                print(f"[Save] Point count mismatch: thermal={len(thermal_pts)} vs rgb={len(rgb_pts)}")
            else:
                break

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
            # GUI-UPDATE: 실행 위치가 아니라 config.json의 저장 경로를 따른다.
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
