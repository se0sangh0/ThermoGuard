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
| 데이터 수집 주기 | 10초 |

## 프로젝트 구조

```
project/
├── capture.py              # FLIR A50 이미지 캡처 (Thermal + RGB)
├── thermal_utils.py        # 열화상 온도 추출 유틸 (Planck 변환)
├── metadata.py             # CSV 메타데이터 생성/업데이트
├── checking.py             # 데이터셋 무결성 검사 및 복구
├── calibration.py          # Thermal-RGB Homography 캘리브레이션 도구
├── product_design.md       # 제품 설계 계획안
├── experiment_config.json  # 실험 설정
├── requirements.txt        # 의존성 패키지
├── docs/
│   └── 카메라_도면.png      # FLIR A50 카메라 도면
└── thermal_dataset/        # 수집된 데이터셋
    ├── *.jpg               # Thermal + RGB 원본 이미지
    ├── *_thermal.npy       # 픽셀별 온도 행렬
    ├── *_visual.jpg        # 가시광 이미지
    └── metadata.csv        # 데이터셋 메타데이터
```

## 설치 및 설정

```bash
# 의존성 설치
pip install -r requirements.txt

# 실험 설정 (experiment_config.json)
{
    "experiment_id": "EXP001",
    "condition": "normal",
    "target_temp": 25.0,
    "angle_deg": 0,
    "notes": ""
}
```

## ✅ 완료된 작업

### 1. FLIR A50 이미지 캡처 (`capture.py`)
- 카메라에서 Thermal + RGB 이미지를 10초 간격으로 수집
- 네트워크 오류 핸들링 (Timeout, ConnectionError)
- `thermal_dataset/` 디렉토리에 타임스탬프 기반 파일명으로 저장

### 2. 온도 행렬 추출 (`thermal_utils.py`)
- FLIR Radiometric JPEG에서 exiftool을 이용해 Raw Thermal Image 추출
- Planck 방정식을 통한 실제 온도값(°C) 변환
- 방사율, 대기 온도, 습도 등 환경 파라미터 반영
- 출력: `(H, W)` shape의 `float32` numpy 배열

### 3. 메타데이터 관리 (`metadata.py`)
- JPG-NPY 파일쌍 스캔 후 `metadata.csv` 자동 생성/업데이트
- 각 수집 건별 온도 통계(min, max, mean) 기록
- 실험 설정(experiment_id, condition 등)과 연동
- 기존 레코드 중복 방지

### 4. 데이터셋 무결성 검사 (`checking.py`)
- NPY 누락 시 JPG에서 자동 복구
- JPG가 없는 고아 NPY 파일 자동 정리
- 최종 상태 요약 리포트 출력

### 5. Thermal-RGB 캘리브레이션 (`calibration.py`)
- Thermal/RGB 이미지에 대응점을 클릭하여 Homography 행렬 계산
- OpenCV 기반 GUI (두 개의 창에서 번갈아가며 클릭)
- `Z`키로 Undo, `Q`키로 종료
- 결과 행렬을 `thermal_to_rgb.npy`로 저장
- 평균 재투영 오차 출력

### 6. 데이터셋 수집
- `thermal_dataset/`에 Thermal/RGB 이미지 및 온도 행렬(.npy) 수집 완료
- 실험 조건: `normal` 상태 기준 데이터

## 🚧 현재 작업 중

- 온도 분석 결과를 기반으로 Threshold 판단 로직 구현 검토 중

## 📋 앞으로 작업할 내용

### Phase 1 — MVP (현재 단계)

| 작업 | 상태 | 설명 |
|------|------|------|
| ROI 설정 자동화 | ⬜ | 캘리브레이션 결과 기반 Thermal 좌표계 ROI 자동 매핑 |
| 온도 분석 파이프라인 | ⬜ | max_temp, mean_temp, 95th percentile 통계 계산 |
| Threshold 판단 로직 | ⬜ | 기준 온도 + 15°C 초과 시 Warning 판정 |
| 상태 머신 | ⬜ | Normal → Warning → Critical 상태 전이 |
| Overlay 시각화 | ⬜ | RGB 이미지에 과열 부위 표시 및 온도 레이블 |
| Telegram 알림 | ⬜ | 상태 변화 시 봇 메시지 전송 (10분 쿨다운 포함) |
| 이력 관리 | ⬜ | 온도 트렌드 저장 및 추세 분석 |

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

- 10초 주기로 FLIR A50 카메라에 REST API 요청
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
| 카메라 IP / 네트워크 정보 | 외부 노출 금지 내부용 사설 ip 사용 권장(192.168 대역) |
| Telegram Bot Token / Chat ID | 구현 시 환경변수로 분리, 코드에 하드코딩 금지 |
| `thermal_to_rgb.npy` | 캘리브레이션 데이터 — 공유 금지 |

### .gitignore 확인사항

```gitignore
# 아래 항목들이 .gitignore에 등록되어 있는지 확인
/thermal_dataset
/thermal_to_rgb.npy
/test.py
/.vscode
/__pycache__
```

## 라이선스

Private — All rights reserved.
