"""thermal/visual 이미지 쌍의 품질 검사 유틸리티.

해상도 중복/역전 감지, 동일 이미지 판별 등 데이터 무결성을
검증하며, CLI/GUI 양쪽에서 공유하는 순수 함수로 구성된다.
"""

from __future__ import annotations

import cv2


def assess_image_quality(
    thermal_img,
    visual_img,
    *,
    visual_mode: bool = True,
) -> tuple[bool, str]:
    """thermal/visual 이미지 쌍이 서로 유효한 쌍인지 검사한다.

    Args:
        thermal_img: cv2.imread 로 읽은 열화상 이미지 (None 허용)
        visual_img: cv2.imread 로 읽은 가시광 이미지 (None 허용)
        visual_mode: False면 visual 검사를 건너뛰고 thermal만 확인

    Returns:
        (ok: bool, reason: str) — ok가 True면 "정상", False면 실패 사유
    """
    if thermal_img is None or thermal_img.size == 0:
        return False, "열화상 이미지 누락 또는 읽기 실패"

    if not visual_mode:
        return True, "정상"

    if visual_img is None or visual_img.size == 0:
        return False, "가시광 이미지 누락 또는 읽기 실패"

    thermal_shape = thermal_img.shape[:2]
    visual_shape = visual_img.shape[:2]
    if thermal_shape == visual_shape:
        return False, "가시광·열화상 영상 종류 중복 의심(동일 해상도)"

    # 현재 카메라 데이터 규격은 Visual이 Thermal보다 고해상도다.
    # 역전되면 파일 종류가 뒤바뀌었을 가능성이 높다.
    if thermal_img.size >= visual_img.size:
        return False, "가시광·열화상 영상 종류 혼동 의심(해상도 역전)"

    thermal_small = cv2.resize(thermal_img, (160, 120))
    visual_small = cv2.resize(visual_img, (160, 120))
    mean_difference = float(cv2.absdiff(thermal_small, visual_small).mean())
    if mean_difference < 3.0:
        return False, "가시광·열화상 동일 영상 감지"

    return True, "정상"
