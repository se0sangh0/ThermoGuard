# thermal_monitoring — GUI 연동 API 레퍼런스

GUI 설계 담당자가 `thermal_monitoring` 패키지의 기능을 직접 호출할 수 있도록
공개 API를 정리한 문서입니다. 모든 함수는 import만으로 즉시 사용 가능합니다.

---

## 임포트 규칙

```python
# 패키지 최상위
from thermal_monitoring import load_config, save_config, AppConfig, setup_encoding, get_logger

# 서브패키지
from thermal_monitoring.capture import CaptureSession, extract_from_jpeg, raw2temp, probe_thermal_from_url
from thermal_monitoring.data import run_check, run_metadata, run_cleanup, run_cleanup_if_due
from thermal_monitoring.analysis import (
    load_roi_config, extract_roi_from_npy, extract_all_rois_from_npy,
    evaluate_threshold, evaluate_with_state,
    create_overlay, save_overlay, send_alarm,
    Status, MonitorState, RoiResult, RoiConfig,
)
from thermal_monitoring.pipeline import MonitorSequencer, run_pipeline
```

---

## 1. 설정 (Config)

### 1.1 설정 로드

```python
from thermal_monitoring import load_config

cfg = load_config()
print(cfg.camera.ip)              # "192.168.0.51"
print(cfg.roi.baseline_temp)      # 23.0
print(cfg.camera.capture_interval_sec)  # 30.0
print(cfg.camera.warning_interval_sec)  # 5.0
for roi in cfg.roi.rois:
    print(roi.name, roi.x1, roi.y1, roi.x2, roi.y2)
```

### 1.2 설정 저장

```python
from thermal_monitoring import load_config, save_config

cfg = load_config(force_reload=True)
cfg.camera.ip = "192.168.0.100"
save_config(cfg)
```

### 1.3 AppConfig 타입 힌트

```python
from thermal_monitoring import load_config, AppConfig

cfg: AppConfig = load_config()
# Pydantic dataclass — 모든 필드에 IDE 자동완성 및 타입 체크 지원
```

### 1.4 Windows 인코딩 설정

Windows에서 print/로그 출력이 깨질 때 호출합니다 (내부에서 자동 감지).

```python
from thermal_monitoring import setup_encoding

setup_encoding()   # stdout → UTF-8 TextIOWrapper
```

### 1.5 config.json 구조

```jsonc
{
  "camera": {
    "ip": "192.168.0.51",           // 카메라 IP
    "capture_interval_sec": 30.0,   // 평상시 캡처 주기
    "warning_interval_sec": 5.0     // 과열 감지 시 캡처 주기
  },
  "identity": {
    "camera_id": "CAM-01",          // 카메라 식별자
    "robot_id": "Robot-01"          // 로봇 식별자
  },
  "roi": {
    "x1": 0, "y1": 0,              // 하위 호환 ROI 좌상단
    "x2": 640, "y2": 480,           // 하위 호환 ROI 우하단
    "baseline_temp": 35.0,          // 정상 기준 온도
    "warning_delta": 15.0,          // baseline+delta = Warning 임계값
    "critical_delta": 25.0,         // baseline+delta = Critical 임계값
    "rois": [                       // ★ 다중 ROI (비어있으면 x1~y2 폴백)
      { "name": "Joint-1", "x1": 130, "y1": 100, "x2": 300, "y2": 250 },
      { "name": "Joint-2", "x1": 310, "y1": 200, "x2": 500, "y2": 400 }
    ]
  },
  "monitoring": {
    "process_interval_sec": 10.0,   // 파일 스캔 주기
    "integrity_interval_sec": 60.0, // 무결성 검사 주기
    "metadata_interval_sec": 120.0, // 메타데이터 갱신 주기
    "max_processed_cache": 10000,   // 처리 파일 캐시
    "alarm_cooldown_sec": 600,      // 알림 쿨다운 (10분)
    "cleanup_retention_days": 2     // 데이터 보존 기간 (일)
  },
  "hotspot": {
    "min_size": 3,                  // 95th 경로 최소 클러스터 크기
    "min_size_max": 10              // max 경로 최소 클러스터 크기
  },
  "paths": {
    "dataset_dir": "thermal_dataset",
    "overlay_dir": "thermal_dataset/overlay",
    "homography_path": "thermal_to_rgb.npy"
  },
  "display": {
    "roi_display_width": 640,
    "roi_display_height": 480,
    "display_width": 800
  },
  "tools": {
    "exiftool_path": "",           // "" = 자동 감지
    "mode": "both"                 // "both" | "thermal"
  }
}
```

---

## 2. 카메라 캡처 (Capture)

### 2.1 CaptureSession 클래스

백그라운드 스레드에서 FLIR A50의 Thermal + Visual 이미지를 주기적으로 캡처합니다.

```python
from thermal_monitoring.capture import CaptureSession

# 생성 (모든 인자 생략 가능 → config.json 값 사용)
session = CaptureSession(
    cam_ip="192.168.0.51",     # 생략 시 config.json
    mode="both",               # "both" or "thermal"
    interval=30.0,             # 캡처 주기 (초)
    save_dir="thermal_dataset",
    log_callback=my_log_func,  # callable(str) → GUI Activity Log
    probe_callback=my_probe,   # callable(float) → bool (프로브 콜백)
)

# 시작/중지
session.start()              # 백그라운드 스레드 시작
session.stop()               # 종료 (join 포함, blocking)
session.request_stop()       # 종료 요청만 (non-blocking, UI 블로킹 방지용)
session.running              # bool 속성

# 최근 캡처 결과 (property)
session.last_saved_pair      # (thermal_path, visual_path) | (None, None)
```

#### 2.1.1 알람용 일회성 캡처 — `capture_both_once()`

세션 실행 중 알람 발생 시 Thermal + Visual을 동시에 한 번 캡처하고 디스크에 저장합니다.

```python
thermal_path, visual_path = session.capture_both_once()
# → ("thermal_dataset/20260722120000.jpg", "thermal_dataset/20260722120000_visual.jpg")
# 실패 시 (None, None) 반환
# mode="thermal"이면 visual_path는 항상 None
# 세션이 실행 중이 아닐 때 호출하면 (None, None) 반환
```

### 2.2 캡처 주기 동적 전환

```python
session.set_warning_mode(True)   # 1초 고속 캡처
session.set_warning_mode(False)  # 30초 평상시 캡처
```

### 2.3 probe_callback 시그니처

```python
def my_probe(max_temp: float) -> bool:
    """프로브가 감지한 최고 온도를 받아 Warning 판정"""
    if max_temp >= 38.0:
        session.set_warning_mode(True)
        return True   # True → 대기 중단 후 즉시 풀캡처
    return False
```

### 2.4 경량 프로브 (단독 사용)

```python
from thermal_monitoring.capture import probe_thermal_from_url

temp = probe_thermal_from_url("http://192.168.0.51/api/image/current?imgformat=JPEG")
# → 36.2 (float) 또는 None (실패 시)
```

### 2.5 Planck 변환 (저수준 API)

FLIR A50 Raw 데이터 → 온도(°C) 직접 변환이 필요할 때 사용합니다.

```python
from thermal_monitoring.capture import raw2temp

# raw: uint16 numpy 배열 또는 스칼라
# params: exiftool 메타데이터에서 추출한 Planck 파라미터 dict
thermal = raw2temp(raw_np, **planck_params)
# → np.float32 ndarray (°C)
```

```python
from thermal_monitoring.capture import extract_from_jpeg

# JPEG 파일 경로 → 온도 행렬 + 메타데이터
thermal, meta = extract_from_jpeg("path/to/thermal.jpg")
# thermal: np.float32 ndarray (온도 °C)
# meta: {"timestamp": "...", "distance_cm": 100, "ambient_temp": 20.0}
```

---

## 3. ROI 분석 (Analysis)

### 3.1 단일 ROI 추출

```python
from thermal_monitoring.analysis import load_roi_config, extract_roi_from_npy, RoiResult

config = load_roi_config()
result: RoiResult = extract_roi_from_npy("thermal_dataset/20260720_thermal.npy", config)

# RoiResult 필드
result.max_temp          # float: 최고 온도 (°C)
result.mean_temp         # float: 평균 온도 (°C)
result.hot_temp_95       # float: 95th percentile (°C)
result.roi_bounds        # tuple: (x1, y1, x2, y2)
result.over_temp_pixels  # int: baseline+delta 초과 픽셀 수
result.max_hotspot_size  # int: 가장 큰 클러스터 크기
result.hotspot_centroids # list[(int, int, float)]: 핫스팟 [(x, y, temp), ...]
result.roi_name           # str: ROI 이름 (다중 ROI일 때)
```

### 3.2 다중 ROI 추출 (★ 권장)

```python
from thermal_monitoring.analysis import extract_all_rois_from_npy

results = extract_all_rois_from_npy("thermal_dataset/20260720_thermal.npy", config)
# → list[RoiResult] — config.json의 rois 배열 순서와 동일

for r in results:
    print(f"{r.roi_name}: max={r.max_temp:.1f}°C, hotspots={len(r.hotspot_centroids)}")

# 가장 높은 온도의 ROI 선택
worst = max(results, key=lambda r: r.hot_temp_95)
```

---

## 4. Threshold 판정

### 4.1 단순 판정 (Stateless)

```python
from thermal_monitoring.analysis import evaluate_threshold, Status

status = evaluate_threshold(
    hot_temp=50.2,        # 95th percentile
    max_temp=55.3,        # ROI 최고 온도
    baseline=35.0,        # baseline
    warning_delta=15.0,   # warning 임계값
    critical_delta=25.0,  # critical 임계값
    max_hotspot_size=15,  # 클러스터 크기 (노이즈 필터링용)
)
# → Status.WARNING, Status.CRITICAL, Status.NORMAL
```

### 4.2 상태 머신 판정 (알림 쿨다운 포함)

```python
from thermal_monitoring.analysis import evaluate_with_state, Status, MonitorState

state = MonitorState()
new_status, do_alarm = evaluate_with_state(
    hot_temp=50.2,
    max_temp=55.3,
    mean_temp=42.1,
    baseline=config.baseline_temp,
    warning_delta=config.warning_delta,
    critical_delta=config.critical_delta,
    state=state,
    max_hotspot_size=15,
)
# new_status: Status enum
# do_alarm:  bool — True면 Telegram 알림 전송해야 함

if do_alarm:
    state.last_alarm_time = time.time()  # 쿨다운 타이머 갱신
```

### 4.3 판정 기준 (이중 경로)

| 경로 | 기준 | 클러스터 | 결과 |
|------|------|----------|------|
| 95th | ≥ baseline + warning_delta | ≥ 3px | Warning |
| 95th | ≥ baseline + critical_delta | ≥ 3px | Critical |
| max | ≥ baseline + critical_delta | ≥ 10px | Warning (보완) |

---

## 5. 오버레이 시각화

### 5.1 오버레이 생성

```python
from thermal_monitoring.analysis import create_overlay, save_overlay

overlay_img = create_overlay(
    thermal_jpg_path="thermal_dataset/20260720.jpg",
    visual_jpg_path="thermal_dataset/20260720_visual.jpg",
    roi_bounds=(130, 100, 528, 420),
    max_temp=55.3,
    mean_temp=42.1,
    hot_temp=50.2,
    status="Warning",
    hotspot_centroids=[(320, 240, 55.3), (310, 235, 52.1)],
    roi_bounds_list=[(130, 100, 300, 250), (310, 200, 528, 420)],  # ★ 다중 ROI
    roi_names=["Joint-1", "Joint-2"],                                 # ★ ROI 이름
)
# → np.ndarray (BGR 이미지)

# 저장
path = save_overlay("20260720", overlay_img)
# → "thermal_dataset/overlay/20260720_overlay.jpg"
```

**참고**: `roi_bounds_list`와 `roi_names`를 전달하면 모든 ROI 영역이 표시됩니다.
단일 ROI일 때는 생략해도 됩니다. 전달은 항상 해도 무방합니다 (길이 1이면 추가 박스 없음).

### 5.2 디스플레이 (GUI에 표시)

```python
# OpenCV BGR → PIL RGB → Tkinter PhotoImage
import cv2
from PIL import Image, ImageTk

overlay_img = create_overlay(...)
rgb = cv2.cvtColor(overlay_img, cv2.COLOR_BGR2RGB)
pil_img = Image.fromarray(rgb)
photo = ImageTk.PhotoImage(pil_img)
your_label.configure(image=photo)
```

---

## 6. Telegram 알림

```python
from thermal_monitoring.analysis import send_alarm

success = send_alarm(
    image_path="thermal_dataset/overlay/20260720_overlay.jpg",
    temp=50.2,
    status="Warning",
    robot_id="Robot-01",
)
# → True (전송 성공) / False (실패)
# .env 파일에 BOT_TOKEN, CHAT_ID 필수
```

---

## 7. 데이터 관리

### 7.1 무결성 검사 (Check Dataset)

```python
from thermal_monitoring.data import run_check, CheckResult

result: CheckResult = run_check(
    save_dir="thermal_dataset",
    log_callback=my_log_func,  # Optional — 진행 상황 콜백
)
# result.total_jpg, result.total_npy, result.paired
# result.missing_npy, result.fixed, result.failed
# result.orphan_npy, result.removed
```

### 7.2 메타데이터 생성 (Generate Metadata)

```python
from thermal_monitoring.data import run_metadata, MetadataResult

result: MetadataResult = run_metadata(
    save_dir="thermal_dataset",
    log_callback=my_log_func,
)
# result.total_pairs, result.existing, result.new
# → metadata.csv 생성/갱신
```

### 7.3 데이터 정리 (Cleanup Dataset)

```python
from thermal_monitoring.data import run_cleanup, CleanupResult

result: CleanupResult = run_cleanup(
    save_dir="thermal_dataset",
    retention_days=2,        # 보존 기간 (기본 2일)
    log_callback=my_log_func,
)
# result.removed_pairs, result.preserved_alarms (알람 이력 보존)
# result.freed_bytes, result.removed_orphan_npy, ...

# 자동 모드 (1시간에 한 번만 실행)
from thermal_monitoring.data import run_cleanup_if_due
result = run_cleanup_if_due(save_dir="thermal_dataset")
# → CleanupResult 또는 None (아직 실행 시점이 아닐 때)
```

---

## 8. 실시간 감시 시퀀서

### 8.1 MonitorSequencer — 실시간 감시

```python
from thermal_monitoring.pipeline import MonitorSequencer

monitor = MonitorSequencer(
    cam_ip="192.168.0.51",
    capture_interval=30.0,     # 평상시 캡처 주기 (기본값: 1.0)
)
monitor.start()   # 블로킹 실행 (캡처 + 분석 + 알림 통합)
monitor.stop()    # 종료
```

### 8.2 run_pipeline — 배치 분석

thermal_dataset의 모든 파일쌍을 순회하며 ROI 분석 → Threshold 판정 → 오버레이 → 알림을 일괄 처리합니다.

```python
from thermal_monitoring.pipeline import run_pipeline
from thermal_monitoring.pipeline.pipeline import scan_pairs

# 전체 파이프라인 실행
run_pipeline()

# 파일쌍만 스캔 (분석 없이 목록만)
pairs = scan_pairs()
# → [{"base": "20260722120000", "thermal_jpg": "...", "visual_jpg": "...", "npy": "..."}, ...]
```

---

## 9. 로깅

```python
from thermal_monitoring import get_logger

log = get_logger("my_gui")
log.info("GUI started")
log.warning("Camera disconnected")
log.error("Analysis failed: %s", exception, exc_info=True)

# 로그 출력 위치: logs/app.log (자정 롤링, 30일 보존)
# 콘솔에도 INFO 이상 출력
```

---

## 10. GUI 통합 예제 (완전한 흐름)

```python
from thermal_monitoring import load_config, get_logger
from thermal_monitoring.capture import CaptureSession
from thermal_monitoring.analysis import (
    load_roi_config, extract_all_rois_from_npy,
    evaluate_with_state, MonitorState, Status,
    create_overlay, send_alarm,
)
from thermal_monitoring.data import run_check, run_metadata, run_cleanup

_log = get_logger("gui")

class MyDashboard:
    def __init__(self):
        self.cfg = load_config()
        self.state = MonitorState()
        self.session = None  # CaptureSession

    def start_monitoring(self):
        roi_config = load_roi_config()

        def probe_callback(max_temp):
            if max_temp >= roi_config.baseline_temp + roi_config.warning_delta:
                self.session.set_warning_mode(True)
                return True
            return False

        self.session = CaptureSession(
            cam_ip=self.cfg.camera.ip,
            interval=self.cfg.camera.capture_interval_sec,
            probe_callback=probe_callback,
        )
        self.session.start()

    def process_latest(self, npy_path, thermal_jpg, visual_jpg):
        """최신 NPY 파일 분석 → 오버레이 반환"""
        config = load_roi_config()

        # 다중 ROI 분석
        results = extract_all_rois_from_npy(npy_path, config)
        worst = max(results, key=lambda r: r.hot_temp_95)

        # 핫스팟 통합
        all_hotspots = []
        for r in results:
            all_hotspots.extend(r.hotspot_centroids)
        worst.hotspot_centroids = all_hotspots

        # 판정
        new_status, do_alarm = evaluate_with_state(
            hot_temp=worst.hot_temp_95,
            max_temp=worst.max_temp,
            mean_temp=worst.mean_temp,
            baseline=config.baseline_temp,
            warning_delta=config.warning_delta,
            critical_delta=config.critical_delta,
            state=self.state,
            max_hotspot_size=worst.max_hotspot_size,
        )
        self.state.status = new_status

        # 오버레이
        overlay = create_overlay(
            thermal_jpg_path=thermal_jpg,
            visual_jpg_path=visual_jpg,
            roi_bounds=worst.roi_bounds,
            max_temp=worst.max_temp,
            mean_temp=worst.mean_temp,
            hot_temp=worst.hot_temp_95,
            status=new_status.value,
            hotspot_centroids=all_hotspots,
            roi_bounds_list=[(r.roi_bounds) for r in results],
            roi_names=[r.roi_name for r in results],
        )

        # 알림
        if do_alarm:
            send_alarm(
                image_path=thermal_jpg,
                temp=worst.hot_temp_95,
                status=new_status.value,
                robot_id=self.cfg.identity.robot_id,
            )
            self.state.last_alarm_time = time.time()

        return overlay, new_status.value, worst

    def check_dataset(self):
        return run_check(save_dir=self.cfg.paths.dataset_dir)

    def generate_metadata(self):
        return run_metadata(save_dir=self.cfg.paths.dataset_dir)

    def cleanup(self):
        return run_cleanup(save_dir=self.cfg.paths.dataset_dir)
```

---

## 11. 자주 하는 질문

**Q: ROI 영역을 GUI에서 어떻게 표시하나요?**  
`config.json`의 `roi.rois` 배열을 순회하며 각 `{name, x1, y1, x2, y2}`에 사각형을 그리면 됩니다. `create_overlay()` 호출 시 `roi_bounds_list`로 전달하면 자동으로 그려집니다.

**Q: Thermal 이미지와 Visual 이미지가 정렬되지 않아요.**  
캘리브레이션 후 `thermal_to_rgb.npy`가 생성되면 `create_overlay()`가 자동으로 Homography 변환을 적용합니다. 캘리브레이션은 `tools.py`의 "Calibrate" 버튼이나 `run_calibration()` 함수로 실행합니다. (import: `from thermal_monitoring.tools.calibration import run_calibration`)

**Q: GUI에서 오래 걸리는 작업은 어떻게 처리하나요?**  
`run_check()`, `run_metadata()`, `run_cleanup()`은 내부적으로 파일 I/O가 있으므로 `threading.Thread`로 백그라운드 실행을 권장합니다. `log_callback` 인자로 진행 상황을 GUI에 전달할 수 있습니다.
