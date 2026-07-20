# Robot Thermal Monitoring System

산업용 다관절 로봇의 이상 발열을 조기 감지하여 예방 정비(Predictive Maintenance)를 지원하는 시스템입니다.
FLIR A50 Bi-spectrum 카메라의 열화상 이미지를 분석하여 로봇의 과열 상태를 실시간 모니터링하고 Telegram으로 알림을 전송합니다.

## 시스템 개요

```
FLIR A50 → REST Snapshot → Temperature Matrix (.npy) → ROI 설정 → 온도 분석 → Threshold 판단 → Overlay → Telegram 알림
```

## 사용 장비

| 항목 | 내용 |
|------|------|
| 카메라 | FLIR A50 Bi-spectrum (Thermal + Visible) |
| Thermal 해상도 | 640 × 480 |
| RGB 해상도 | 2592 × 1944 |
| 데이터 수집 주기 | 1초 (config.json에서 조정 가능) |

## 프로젝트 구조

```
project/
├── monitor.py              # 진입점: 실시간 감시 시퀀서 → thermal_monitoring.pipeline.monitor
├── tools.py                # 진입점: 통합 운영 도구 GUI → thermal_monitoring.tools.tools
├── pipeline.py             # 진입점: 배치 분석 파이프라인 → thermal_monitoring.pipeline.pipeline
│
├── thermal_monitoring/     # 메인 패키지
│   ├── __init__.py
│   ├── config.py           # 통합 설정 모듈 (모든 설정의 단일 진실 공급원)
│   ├── _encoding.py        # Windows UTF-8 인코딩 유틸
│   │
│   ├── capture/            # 🎥 데이터 수집
│   │   ├── __init__.py
│   │   ├── capture.py      # FLIR A50 이미지 캡처 (CaptureSession)
│   │   └── thermal_utils.py # Planck 변환, exiftool 유틸
│   │
│   ├── data/               # 🗄️ 데이터 관리
│   │   ├── __init__.py
│   │   ├── checking.py     # 데이터셋 무결성 검사 및 복구
│   │   └── metadata.py     # CSV 메타데이터 생성/업데이트
│   │
│   ├── analysis/           # 📊 핵심 분석
│   │   ├── __init__.py
│   │   ├── roi.py          # ROI 온도 통계 + 핫스팟 클러스터
│   │   ├── threshold.py    # 상태 머신 (Normal/Warning/Critical)
│   │   ├── overlay.py      # Thermal/RGB 오버레이 이미지 생성
│   │   └── notifier.py     # Telegram 알림 전송
│   │
│   ├── pipeline/           # 🔄 파이프라인
│   │   ├── __init__.py
│   │   ├── monitor.py      # 실시간 감시 시퀀서 (MonitorSequencer)
│   │   └── pipeline.py     # 배치 분석 파이프라인 (run_pipeline)
│   │
│   └── tools/              # 🛠️ 운영 도구
│       ├── __init__.py
│       ├── tools.py        # 통합 모니터링 대시보드 GUI (MonitoringDashboard)
│       ├── roi_selector.py # GUI ROI 영역 설정 도구
│       └── calibration.py  # Thermal-RGB Homography 캘리브레이션
│
├── tests/                  # 🧪 테스트
│   ├── __init__.py
│   ├── test_threshold.py   # Threshold 시뮬레이션 테스트
│   └── test_overlay.py     # 오버레이 생성 테스트
│
├── config.json             # 통합 설정 파일 (gitignore, 자동 생성)
├── .env.example            # 환경변수 템플릿 (BOT_TOKEN, CHAT_ID)
├── requirements.txt        # 의존성 패키지
├── product_design.md       # 제품 설계 계획안
└── thermal_dataset/        # 수집된 데이터셋
    ├── *.jpg               # Thermal 원본 이미지
    ├── *_thermal.npy       # 픽셀별 온도 행렬
    ├── *_visual.jpg        # 가시광 이미지
    ├── metadata.csv        # 데이터셋 메타데이터
    └── overlay/            # 오버레이 출력 이미지
```

### 패키지 임포트

```python
# 최상위 API
from thermal_monitoring import load_config, setup_encoding

# 서브패키지별 API
from thermal_monitoring.capture import CaptureSession, extract_from_jpeg
from thermal_monitoring.data import run_check, run_metadata
from thermal_monitoring.analysis import extract_roi_from_npy, evaluate_threshold, create_overlay, send_alarm
from thermal_monitoring.pipeline import MonitorSequencer, run_pipeline
```

## 빠른 시작

```bash
# 0. conda 환경 활성화 (권장)
conda activate test

# 1. 의존성 설치
pip install -r requirements.txt

#    (리눅스 환경일 경우 exiftool도 별도 설치 필요)
#    sudo apt install exiftool

# 2. 환경변수 설정 (Telegram 알림용)
cp .env.example .env
# → .env 파일에 BOT_TOKEN, CHAT_ID 입력

# 3. 첫 실행 시 config.json 자동 생성
#    (기존 roi_config.json, experiment_config.json이 있으면 자동 이관)
python -c "from thermal_monitoring.config import load_config; load_config()"

# 4. 통합 설정 확인 및 수정
#    config.json에서 카메라IP, ROI좌표, 임계값, 쿨다운 등 모든 설정 관리
code config.json   # 또는 nano config.json

# 5. ROI 영역 설정 (GUI)
python -m thermal_monitoring.tools.roi_selector

# 6. 캘리브레이션 (Thermal ↔ RGB 매핑, 공장 설치 시 필수)
python -m thermal_monitoring.tools.calibration

# 7a. 실시간 감시 시퀀서 실행 (캡처 + 분석 + 알림 통합)
python monitor.py

# 7b. (또는) GUI 통합 모니터링 대시보드
python tools.py

# 7c. (또는) 기존 데이터셋 배치 분석
python pipeline.py

# 8. 단일 이미지 오버레이 확인
python -m tests.test_overlay
```

## 통합 설정 (config.json)

모든 설정이 `config.json` 하나로 통합 관리됩니다. 최초 실행 시 기존 `roi_config.json` 및 `experiment_config.json`에서 자동 이관(migration)되며, 원본 파일은 `.bak`으로 백업됩니다.

```jsonc
{
  "camera": {
    "ip": "192.168.0.51",           // FLIR 카메라 IP
    "capture_interval_sec": 1.0      // 캡처 주기 (초)
  },
  "identity": {
    "camera_id": "CAM-01",           // 카메라 식별자 (metadata.csv)
    "robot_id": "Robot-01"           // 로봇 식별자 (metadata.csv, 알림)
  },
  "roi": {
    "x1": 0, "y1": 0,               // Thermal 이미지(640x480) 기준 ROI 좌상단
    "x2": 640, "y2": 480,           // ROI 우하단
    "baseline_temp": 35.0,           // 정상 기준 온도 (°C)
    "warning_delta": 15.0,           // baseline + warning = Warning 임계값
    "critical_delta": 25.0           // baseline + critical = Critical 임계값
  },
  "monitoring": {
    "process_interval_sec": 2.0,     // 신규 파일 스캔 주기
    "integrity_interval_sec": 60.0,  // 무결성 검사 주기
    "metadata_interval_sec": 120.0,  // 메타데이터 업데이트 주기
    "max_processed_cache": 10000,    // 처리된 파일 캐시 크기
    "alarm_cooldown_sec": 600        // 알림 쿨다운 (초, 기본 10분)
  },
  "hotspot": {
    "min_size": 3,                   // 95th 경로 최소 클러스터 크기 (px)
    "min_size_max": 10               // max 온도 경로 최소 클러스터 크기 (px)
  },
  "paths": {
    "dataset_dir": "thermal_dataset",
    "overlay_dir": "thermal_dataset/overlay",
    "homography_path": "thermal_to_rgb.npy"
  },
  "display": {
    "roi_display_width": 640,        // Thermal 이미지 표시 너비
    "roi_display_height": 480,
    "display_width": 800             // GUI 표시 너비
  },
  "tools": {
    "exiftool_path": "",             // "" = 자동 감지
    "mode": "both"                   // 캡처 모드: "both" | "thermal"
  }
}
```

**설정 변경이 필요할 때:**
- `config.json`을 직접 편집하거나 `python -m thermal_monitoring.tools.roi_selector`로 ROI 좌표 변경
- 설정은 `monitor.py`, `tools.py`, `pipeline.py` 실행 시 자동으로 반영됨
- Telegram 시크릿(BOT_TOKEN, CHAT_ID)은 `.env`에 별도 관리

## ✅ 완료된 작업

### 데이터 수집

| 모듈 | 설명 |
|------|------|
| `config.py` | **통합 설정 모듈** — 모든 설정의 단일 진실 공급원, config.json 읽기/쓰기, 구버전 파일 자동 이관 |
| `tools.py` | 통합 모니터링 대시보드 GUI — 환경 설정(카메라 IP, 연결 상태, 디렉토리, 저장 주기), 실시간 감지 화면(Thermal/Visual 오버레이 이미지 + 핫스팟 마커), 로그 테이블(감지 시간/위치/온도/경고/알림), ROI 설정·캘리브레이션 연동 |
| `capture.py` | FLIR A50에서 Thermal + RGB 이미지 수집 (`CaptureSession` 클래스, GUI/스크립트 겸용) |
| `thermal_utils.py` | Radiometric JPEG에서 exiftool로 Raw Thermal 추출, Planck 변환으로 실제 °C 환산 |
| `checking.py` | 데이터셋 무결성 검사 — NPY 누락 시 JPG에서 복구, 고아 NPY 정리 (`run_check()` 함수) |
| `metadata.py` | JPG-NPY 파일쌍 스캔 후 `metadata.csv` 자동 생성, 실험 설정 연동 (`run_metadata()` 함수) |
| `calibration.py` | OpenCV GUI로 Thermal ↔ RGB 대응점 지정, Homography 행렬 계산 |
| `roi_selector.py` | Thermal 이미지에 마우스 드래그로 ROI 지정, `config.json` 자동 저장 |

### 분석 파이프라인

| 모듈 | 설명 |
|------|------|
| `monitor.py` | **실시간 감시 시퀀서 (CLI)** — 백그라운드 캡처 + 신규 이미지 자동 분석 + 무결성 검사 + 메타데이터 + 알림 |
| `roi.py` | `.npy`에서 ROI 영역 온도 통계(max, mean, 95th) 추출 + connected components 클러스터 분석 + **모든 핫스팟 중심좌표 추출** |
| `threshold.py` | 이중 경로 상태 판정 (95th percentile + max 온도) + 클러스터 크기 기반 노이즈 필터링, 상태 변화 시 알림 쿨다운 |
| `overlay.py` | Thermal/RGB 이미지에 ROI 박스 + 온도 정보 + **모든 핫스팟 마커** 표시, Homography 기반 좌표 변환 |
| `pipeline.py` | 배치 분석 파이프라인: ROI → Threshold → Overlay → Telegram 알림 (기존 데이터셋 순회) |

### 알림

| 모듈 | 설명 |
|------|------|
| `notifier.py` | Telegram 이미지+캡션 전송, 실패 시 텍스트 폴백, `.env` 기반 토큰 관리 |
| `pipeline.py` | 상태 변화 감지 시 `notifier.py` 호출하여 자동 알림 |

### 판정 기준 상세 (이중 경로)

**경로 1 — 95th percentile (넓은 영역 과열)**
- `95th >= baseline + warning_delta` **AND** cluster ≥ 3px → Warning
- `95th >= baseline + critical_delta` **AND** cluster ≥ 3px → Critical

**경로 2 — max 온도 (국소 고온 보완)**
- `max >= baseline + critical_delta` **AND** cluster ≥ 10px → 최소 Warning
- ROI 대비 소수 픽셀만 과열되어 95th가 낮게 나오는 경우를 보완
- cluster 임계치를 10px로 상향해 노이즈 방지

**공통**
- 1~2픽셀 크기의 국소 과열 → 센서 노이즈로 간주, 무시
- 클러스터 분석: `cv2.connectedComponentsWithStats` 사용
- 상태 변화 시에만 알림, 쿨다운 10분

## 🚧 현재 작업 중

- **DB 설계** — 온도 이력(test.py 히스토리)을 저장할 DB 스키마 설계 (동료 작업 대기)
- **웹 대시보드** — 실시간 온도 트렌드, ROI 오버레이, 알림 상태 표시 (동료 작업 대기)
- **실시간 모니터링 루프** — DB + 대시보드 완료 후 `pipeline.py`를 실시간 모드로 전환 예정
- **공장 라인 실증 테스트** — 실제 로봇 발열 데이터 확보 시 검증

## 📋 앞으로 작업할 내용

### Phase 1 — MVP (현재 단계)

| 작업 | 상태 | 설명 |
|------|------|------|
| ROI 설정 (GUI) | ✅ | `roi_selector.py` — 이미지 드래그로 ROI 지정 |
| 온도 분석 파이프라인 | ✅ | `roi.py` — max/mean/95th + 클러스터 분석 |
| Threshold 판단 로직 | ✅ | `threshold.py` — 이중 경로 (95th + max), 클러스터 크기 기반 노이즈 필터링 |
| 상태 머신 | ✅ | Normal → Warning → Critical → Normal 상태 전이 |
| Telegram 알림 | ✅ | `notifier.py` — 이미지+캡션 전송, `.env` 토큰 관리 |
| Overlay 시각화 | ✅ | `overlay.py` — Thermal/RGB 이미지에 온도 정보 + 핫스팟 마커 표시, Homography 좌표 변환 |
| 통합 파이프라인 | ✅ | `pipeline.py` — ROI → Threshold → Overlay → 알림 |
| 통합 모니터링 GUI | ✅ | `tools.py` — 환경 설정 + 실시간 감지 화면 + 로그 테이블, ROI 설정·캘리브레이션 연동 |
| 이력 관리 | ⬜ | 온도 트렌드 DB 저장 — DB 설계 완료 후 진행 |
| 웹 대시보드 | ⬜ | 실시간 상태/트렌드/알림 표시 — 동료 작업 대기 |
| 실시간 모니터링 | ⬜ | DB + 대시보드 연동 후 `pipeline.py` 실시간 전환 |

### Phase 2 — 고도화

- Robot Detection AI 모델 적용
- 이상 탐지(Anomaly Detection) 모델
- 다중 카메라 지원
- RTSP 스트리밍 지원
- 다중 로봇 모니터링
- 카메라 제조사 확장

### Phase 3 — 예지보전

- 부품 단위 진단
- 예지보전 알고리즘
- 클라우드 연동
- AI 기반 이상 패턴 분석

## 기술 스택

| 구분 | 기술 |
|------|------|
| 언어 | Python 3.12 |
| 이미지 처리 | OpenCV, Pillow |
| 수치 연산 | NumPy |
| 데이터 포맷 | JPEG, NPY, CSV, JSON |
| 통신 | REST API (requests) |
| 알림 | Telegram Bot API |
| 메타데이터 추출 | exiftool |
| 카메라 | FLIR A50 (REST API) |

## 데이터 수집 방식

- `config.json`의 `camera.capture_interval_sec` 주기로 FLIR A50 카메라에 REST API 요청
- 수집 데이터: Thermal JPEG (radiometric) + Visual JPEG + Temperature Matrix (.npy)
- `.npy` 파일은 픽셀별 실제 온도 정보(°C)를 포함하여 별도 변환 불필요
- 실시간 스트리밍 대신 Snapshot 방식 채택 (네트워크 품질/방화벽 제약 고려)

## 알림 규칙

| 상태 | 메시지 전송 | 전송 정보 |
|------|------------|-----------|
| 평시 (Normal) | 없음 | - |
| 과열 (Warning) | 전송 | 로봇ID, 상태, 최고 온도, 발생 시간, 과열 범위 이미지 |
| 경보 (Critical) | 전송 | 로봇ID, 상태, 최고 온도, 발생 시간, 과열 범위 이미지 |

- 상태 변화 시에만 메시지 전송 (Normal → Warning → Critical → Normal)
- 연속 발송 방지를 위한 쿨다운: 10분

## 🔒 보안 및 개인정보 규칙

> **이 프로젝트에서 취득한 모든 데이터와 개인정보는 외부에 공유할 수 없습니다.**

| 항목 | 규칙 |
|------|------|
| `thermal_dataset/` | 수집된 이미지, 온도 행렬, 메타데이터 절대 외부 유출 금지 |
| 카메라 IP / 네트워크 정보 | 외부 노출 금지 (내부용 사설 IP 사용 권장: 192.168 대역) |
| Telegram Bot Token / Chat ID | `.env` 파일로 분리, 코드에 하드코딩 금지 |
| `thermal_to_rgb.npy` | 캘리브레이션 데이터 — 공유 금지 |

### .gitignore 확인사항

```gitignore
/.vscode
/thermal_dataset
/thermal_to_rgb.npy
/__pycache__
/.obsidian
*.pyc
.env
config.json
```

## 라이선스

Private — All rights reserved.
