import gi
import numpy as np
import requests

gi.require_version("Aravis", "0.8")
from gi.repository import Aravis


# ============================================================
# 기본 설정
# ============================================================

CAMERA_ID = 1
ROI_ID = 4

# DB roi_definitions에 저장된 ROI-01 좌표
X1 = 100
Y1 = 80
X2 = 350
Y2 = 280

# FastAPI 주소
API_URL = "http://127.0.0.1:8000/api/measurements"


# ============================================================
# 1. FLIR A50 검색
# ============================================================

Aravis.update_device_list()

num_devices = Aravis.get_n_devices()

if num_devices == 0:
    raise RuntimeError("FLIR A50 카메라를 찾을 수 없습니다.")

device_id = Aravis.get_device_id(0)

print()
print("========================================")
print(" FLIR A50 카메라 검색")
print("========================================")
print(f"카메라 발견: {device_id}")


# ============================================================
# 2. 카메라 연결
# ============================================================

camera = Aravis.Camera.new(device_id)

print(f"Vendor: {camera.get_vendor_name()}")
print(f"Model: {camera.get_model_name()}")


# ============================================================
# 3. 카메라 설정
# ============================================================

# A50 Radiometric 해상도
camera.set_region(
    0,
    0,
    464,
    348
)

# 16bit radiometric 영상
camera.set_pixel_format_from_string(
    "Mono16"
)

# 0.01 Kelvin 단위의 Temperature Linear
camera.set_string(
    "IRFormat",
    "TemperatureLinear10mK"
)


# ============================================================
# 4. 현재 설정 확인
# ============================================================

x, y, width, height = camera.get_region()

pixel_format = camera.get_pixel_format_as_string()
ir_format = camera.get_string("IRFormat")

print()
print("========================================")
print(" 카메라 설정")
print("========================================")
print(f"Width: {width}")
print(f"Height: {height}")
print(f"PixelFormat: {pixel_format}")
print(f"IRFormat: {ir_format}")


# ============================================================
# 5. Payload / Stream 생성
# ============================================================

payload_size = camera.get_payload()

print(f"Payload: {payload_size}")

stream = camera.create_stream(
    None,
    None
)

if stream is None:
    raise RuntimeError("GigE Vision Stream 생성에 실패했습니다.")


# ============================================================
# 6. 버퍼 준비
# ============================================================

for _ in range(20):

    buffer = Aravis.Buffer.new_allocate(
        payload_size
    )

    stream.push_buffer(buffer)


# ============================================================
# 7. 영상 획득 시작
# ============================================================

camera.start_acquisition()

print()
print("프레임 대기 중...")


# ============================================================
# 8. 실제 프레임 수신
# ============================================================

buffer = stream.timeout_pop_buffer(
    3_000_000
)

if buffer is None:

    camera.stop_acquisition()

    raise RuntimeError(
        "FLIR 프레임 수신 시간이 초과되었습니다."
    )


if buffer.get_status() != Aravis.BufferStatus.SUCCESS:

    camera.stop_acquisition()

    raise RuntimeError(
        f"FLIR Buffer 오류: {buffer.get_status()}"
    )


# ============================================================
# 9. 실제 이미지 크기 확인
# ============================================================

width = buffer.get_image_width()
height = buffer.get_image_height()

print(
    f"수신 이미지: {width} x {height}"
)


# ============================================================
# 10. RAW Mono16 데이터 가져오기
# ============================================================

data = buffer.get_data()

raw = np.frombuffer(
    data,
    dtype=np.uint16
)

expected_pixels = (
    width * height
)


if raw.size < expected_pixels:

    camera.stop_acquisition()

    raise RuntimeError(
        f"픽셀 수 부족: "
        f"{raw.size} / {expected_pixels}"
    )


raw_image = raw[
    :expected_pixels
].reshape(
    height,
    width
)


# ============================================================
# 11. RAW 값 확인
# ============================================================

print()
print("========================================")
print(" RAW 확인")
print("========================================")

print(
    "raw dtype:",
    raw_image.dtype
)

print(
    "raw shape:",
    raw_image.shape
)

print(
    "raw min:",
    raw_image.min()
)

print(
    "raw max:",
    raw_image.max()
)

print(
    "raw mean:",
    raw_image.mean()
)

print(
    "처음 20개:",
    raw_image.ravel()[:20]
)

print(
    "0이 아닌 픽셀 수:",
    np.count_nonzero(raw_image)
)


# ============================================================
# 12. RAW → 실제 섭씨 온도로 변환
#
# TemperatureLinear10mK
#
# RAW × 0.01 = Kelvin
# Kelvin - 273.15 = Celsius
# ============================================================

temp_image = (
    raw_image.astype(np.float32)
    * 0.01
) - 273.15


# ============================================================
# 13. 전체 화면 온도 계산
# ============================================================

full_min = float(
    np.min(temp_image)
)

full_max = float(
    np.max(temp_image)
)

full_mean = float(
    np.mean(temp_image)
)

full_p95 = float(
    np.percentile(
        temp_image,
        95
    )
)


print()
print("========================================")
print(" 실제 FLIR 전체 화면 온도")
print("========================================")

print(
    f"최저 온도: {full_min:.2f} °C"
)

print(
    f"평균 온도: {full_mean:.2f} °C"
)

print(
    f"95% 온도: {full_p95:.2f} °C"
)

print(
    f"최고 온도: {full_max:.2f} °C"
)


# ============================================================
# 14. ROI 좌표 유효성 검사
#
# DB:
# roi_id = 4
#
# x1 = 100
# y1 = 80
# x2 = 350
# y2 = 280
# ============================================================

if not (
    0 <= X1 < X2 <= width
    and
    0 <= Y1 < Y2 <= height
):

    camera.stop_acquisition()

    raise RuntimeError(
        "ROI 좌표가 이미지 범위를 벗어났습니다. "
        f"ROI=({X1},{Y1})~({X2},{Y2}), "
        f"IMAGE={width}x{height}"
    )


# ============================================================
# 15. ROI-01 영역 잘라내기
#
# NumPy 이미지는
#
# image[y, x]
#
# 순서라는 점이 중요함.
# ============================================================

roi_temp = temp_image[
    Y1:Y2,
    X1:X2
]


if roi_temp.size == 0:

    camera.stop_acquisition()

    raise RuntimeError(
        "ROI 영역에 픽셀이 없습니다."
    )


# ============================================================
# 16. ROI 온도 계산
# ============================================================

roi_min = float(
    np.min(roi_temp)
)

roi_max = float(
    np.max(roi_temp)
)

roi_mean = float(
    np.mean(roi_temp)
)

roi_p95 = float(
    np.percentile(
        roi_temp,
        95
    )
)


print()
print("========================================")
print(" ROI-01 온도 분석")
print("========================================")

print(
    f"ROI ID: {ROI_ID}"
)

print(
    f"ROI 범위: "
    f"({X1}, {Y1}) ~ "
    f"({X2}, {Y2})"
)

print(
    f"ROI 크기: "
    f"{roi_temp.shape[1]} x "
    f"{roi_temp.shape[0]}"
)

print(
    f"ROI 최저: {roi_min:.2f} °C"
)

print(
    f"ROI 평균: {roi_mean:.2f} °C"
)

print(
    f"ROI 95%: {roi_p95:.2f} °C"
)

print(
    f"ROI 최고: {roi_max:.2f} °C"
)


# ============================================================
# 17. 주변 온도 계산
#
# 지금은 전체 영상 평균값을 ambient로 사용
# ============================================================

ambient_temp = full_mean


# ============================================================
# 18. ROI 최고온도 - 주변온도 차이
# ============================================================

delta_temp = (
    roi_max
    - ambient_temp
)


print()
print("========================================")
print(" 온도 차이 계산")
print("========================================")

print(
    f"주변 온도: {ambient_temp:.2f} °C"
)

print(
    f"ROI 최고온도: {roi_max:.2f} °C"
)

print(
    f"Delta: {delta_temp:.2f} °C"
)


# ============================================================
# 19. FastAPI로 보낼 JSON 데이터 생성
# ============================================================

payload = {

    "camera_id": CAMERA_ID,

    "roi_id": ROI_ID,

    "min_temp": roi_min,

    "max_temp": roi_max,

    "mean_temp": roi_mean,

    "percentile_95_temp": roi_p95,

    "ambient_temp": ambient_temp,

    "delta_temp": delta_temp,

    # 이 두 값은 나중에 hotspot 분석 로직을 붙이면 계산
    "over_temp_pixels": 0,

    "max_hotspot_size": 0
}


print()
print("========================================")
print(" FastAPI 전송 데이터")
print("========================================")

print(payload)


# ============================================================
# 20. FastAPI POST
# ============================================================

try:

    response = requests.post(
        API_URL,
        json=payload,
        timeout=10
    )

    # HTTP 4xx / 5xx면 예외 발생
    response.raise_for_status()


    print()
    print("========================================")
    print(" FastAPI 응답")
    print("========================================")

    print(
        response.json()
    )


except requests.exceptions.ConnectionError:

    print()
    print("========================================")
    print(" FastAPI 연결 실패")
    print("========================================")

    print(
        "FastAPI 서버가 실행 중인지 확인하세요."
    )

    print(
        "서버 주소:",
        API_URL
    )


except requests.exceptions.Timeout:

    print(
        "FastAPI 요청 시간이 초과되었습니다."
    )


except requests.exceptions.RequestException as e:

    print(
        f"FastAPI 요청 오류: {e}"
    )


# ============================================================
# 21. 카메라 획득 종료
# ============================================================

camera.stop_acquisition()


print()
print("========================================")
print(" FLIR A50 처리 완료")
print("========================================")

print(
    "FLIR → ROI → FastAPI 전송 완료"
)
