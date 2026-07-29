# ThermoGuard

FLIR A50 Bi-spectrum 카메라로 산업용 로봇의 열 상태를 감시하고, 측정 결과를
FastAPI와 MariaDB에 저장하며 Critical 이벤트를 Telegram으로 알리는 시제품입니다.

현재 운영 범위는 **카메라 1대와 로봇 1대**입니다. DB 스키마와 일부 분석 코드는
확장 가능한 형태를 유지하지만, 장비 선택이나 여러 로봇 간 ID 비교는 현재 운영의
핵심 요구사항이 아닙니다. 한 카메라 안에 여러 ROI를 설정하는 것은 지원합니다.

## 동작 흐름

```text
FLIR A50
  → Thermal/Visual 이미지 수집
  → Thermal JPEG에서 온도 행렬(.npy) 추출
  → ROI별 온도 및 핫스팟 분석
  → Normal / Warning / Critical 판정
  → Product Dashboard 표시
  → FastAPI를 통해 MariaDB 기록
  → Critical 알람 승인 시 Telegram 전송
```

상태별 동작은 다음과 같습니다.

| 상태 | 기본 수집 주기 | DB 측정 기록 | Telegram |
|---|---:|---|---|
| Normal | 30초 | 기록 | 전송 안 함 |
| Warning | 5초 | 기록 | 전송 안 함 |
| Critical | 5초 | 기록 | 상태 전환 및 쿨다운 조건 충족 시 전송 |

Warning은 빠른 재촬영을 위한 상태이며 알람 전송 조건이 아닙니다.
`alert_events`는 분석 상태만으로 생성되지 않고, 상태 머신이 Critical 알람을
승인하여 측정 요청의 `do_alarm`이 `true`일 때 생성됩니다.

## 주요 실행 파일

| 파일 | 역할 |
|---|---|
| `dashboard.py` | 권장 운영 진입점. 수집, 분석, 설정, DB 연동, 이벤트 확인, Telegram을 통합 |
| `monitor.py` | GUI 없이 실시간 감시 시퀀서 실행 |
| `pipeline.py` | 저장된 데이터셋을 대상으로 배치 분석 |
| `Project_hotspot/backend/app.py` | FastAPI 백엔드 |
| `Project_hotspot/backend/database.py` | MariaDB 연결 설정 |

## 프로젝트 구조

```text
ThermoGuard/
├── dashboard.py
├── monitor.py
├── pipeline.py
├── config.json
├── .env.example
├── requirements.txt
│
├── thermal_monitoring/
│   ├── config.py                 # 통합 설정 dataclass와 JSON 입출력
│   ├── logger.py                 # logs/app.log 기록
│   ├── capture/
│   │   ├── capture.py            # FLIR 이미지 수집
│   │   └── thermal_utils.py      # 온도 행렬 추출과 Planck 변환
│   ├── analysis/
│   │   ├── roi.py                # ROI 통계와 핫스팟 분석
│   │   ├── threshold.py          # 상태 머신과 Critical 알람 승인
│   │   ├── overlay.py            # 분석 오버레이
│   │   └── notifier.py           # Telegram 전송과 전송 결과 기록
│   ├── data/
│   │   ├── checking.py           # 데이터 무결성 검사
│   │   ├── metadata.py           # metadata.csv 관리
│   │   ├── cleanup.py            # Normal 데이터 보존 기간 적용
│   │   └── quality.py            # 이미지 품질 검사
│   ├── pipeline/
│   │   ├── monitor.py
│   │   └── pipeline.py
│   └── tools/
│       ├── product_dashboard.py  # 운영 대시보드 본체
│       ├── telegram_dispatcher.py
│       ├── asset_api_client.py   # 장비 DB ID 등록·재사용
│       ├── roi_api_client.py     # ROI DB 동기화
│       ├── calibration.py        # Thermal↔RGB Homography 계산
│       ├── roi_selector.py
│       └── tk_image_dialogs.py
│
├── Project_hotspot/
│   └── backend/
│       ├── app.py                # FastAPI 라우트와 DB 기록 로직
│       ├── database.py
│       └── collector/
│
├── tests/
├── logs/
└── thermal_dataset/
```

## 설치

Python 3.12 환경을 권장합니다.

```bash
python -m pip install -r requirements.txt
python -m pip install pytest
```

Linux에서는 GUI와 FLIR 메타데이터 처리를 위해 시스템 패키지가 추가로 필요할 수
있습니다.

```bash
sudo apt install exiftool python3-tk libgl1
```

환경변수 파일을 만들고 실제 값을 입력합니다.

```bash
cp .env.example .env
```

```dotenv
BOT_TOKEN=
CHAT_ID=
TELEGRAM_ENABLED=true
FASTAPI_URL=http://127.0.0.1:8000

DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=hotspot_guard
DB_USER=root
DB_PASSWORD=
```

Telegram 자격 증명과 활성화 여부는 Product Dashboard의 환경설정에서도 저장할 수
있습니다. `FASTAPI_URL`은 Telegram 전송 결과를 기록할 Backend 주소이며, 생략하면
`http://127.0.0.1:8000`을 사용합니다. `.env`는 저장소에 커밋하지 않습니다.

## 실행

### 1. MariaDB와 백엔드

DB 스키마가 준비된 MariaDB를 먼저 실행한 뒤 백엔드를 시작합니다.

```bash
cd Project_hotspot/backend
uvicorn app:app --host 0.0.0.0 --port 8000
```

기본 확인 주소:

- 상태 확인: `http://127.0.0.1:8000/api/health`
- Swagger UI: `http://127.0.0.1:8000/docs`

systemd로 설치된 운영 장비에서는 다음 서비스 로그를 확인합니다.

```bash
sudo systemctl status hotspot-backend.service
sudo journalctl -u hotspot-backend.service -n 200 --no-pager
sudo journalctl -u hotspot-flir-collector.service -n 200 --no-pager
```

### 2. Product Dashboard

프로젝트 루트에서 실행합니다.

```bash
python dashboard.py
```

권장 초기 설정 순서:

1. 환경설정에서 공장, 생산라인, 로봇, 카메라와 Backend 연결 정보를 저장합니다.
2. Thermal/Visual 이미지 쌍을 한 번 수집합니다.
3. 환경설정에서 **캘리브레이션**을 실행합니다.
4. 환경설정에서 **ROI 설정**을 열고 ROI를 저장합니다.
5. 임계값을 저장하고 모니터링을 시작합니다.

캘리브레이션 버튼은 대시보드 내부에 별도 보정 알고리즘을 구현하지 않습니다.
`thermal_monitoring.tools.calibration.run_calibration()`을 호출하며 결과는
`paths.homography_path`에 저장합니다. 캘리브레이션 창 크기 계산은
`thermal_monitoring/tools/calibration.py`의 `run_calibration()` 안에서
`max_window_width`, `max_window_height` 값으로 조정합니다.

GUI가 필요 없는 경우 다음 진입점을 사용할 수 있습니다.

```bash
python monitor.py
python pipeline.py
```

## 통합 설정

`config.json`은 애플리케이션의 단일 설정 파일입니다. 없는 항목은
`thermal_monitoring/config.py`의 dataclass 기본값으로 보완됩니다.

```json
{
  "camera": {
    "ip": "192.168.0.51",
    "capture_interval_sec": 30.0,
    "warning_interval_sec": 5.0
  },
  "identity": {
    "camera_id": "CAM-01",
    "robot_id": "Robot-01",
    "factory_name": "",
    "line_name": "",
    "robot_name": "",
    "factory_id": null,
    "line_id": null,
    "db_robot_id": null,
    "db_camera_id": null
  },
  "roi": {
    "x1": 0,
    "y1": 0,
    "x2": 640,
    "y2": 480,
    "baseline_temp": 35.0,
    "warning_delta": 15.0,
    "critical_delta": 25.0,
    "rois": [
      {
        "name": "ROI-1",
        "x1": 0,
        "y1": 0,
        "x2": 640,
        "y2": 480,
        "db_roi_id": null
      }
    ]
  },
  "monitoring": {
    "process_interval_sec": 10.0,
    "integrity_interval_sec": 60.0,
    "metadata_interval_sec": 120.0,
    "max_processed_cache": 10000,
    "alarm_cooldown_sec": 600.0,
    "cleanup_retention_days": 2
  },
  "hotspot": {
    "min_size": 3,
    "min_size_max": 10
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
    "exiftool_path": "",
    "mode": "both"
  },
  "backend": {
    "url": "http://127.0.0.1:8000",
    "enabled": true,
    "timeout_sec": 5.0
  }
}
```

다음 두 종류의 ID를 구분해야 합니다.

- `camera_id`, `robot_id`: 화면, 파일, Telegram 문구에 사용하는 문자열 식별자
- `db_camera_id`, `db_robot_id`, `db_roi_id`: MariaDB 외래키로 사용하는 정수 ID

Product Dashboard에서 장비 정보를 저장하면 DB ID가 `config.json`에 반영됩니다.
현재 단일 카메라 운영에서는 문자열 코드나 IP가 일치하는 기존 카메라를 먼저
재사용하며, DB에 카메라가 정확히 한 대뿐인 경우 그 레코드를 재사용합니다. 이
동작은 중복 장비 INSERT로 인한 `1062 Duplicate entry` 오류를 피하기 위한
시제품 범위의 처리입니다.

## DB 기록 계약

Product Dashboard는 DB에 직접 INSERT하지 않고 기존 FastAPI 경로를 사용합니다.

| 기능 | 경로 | 주요 결과 |
|---|---|---|
| 상태 확인 | `GET /api/health` | Backend/DB 연결 확인 |
| 장비 조회·저장 | `GET/POST /api/cameras` 등 | 장비 DB ID 확보 |
| ROI 조회·저장 | `GET/POST /api/rois` | `db_roi_id` 확보 |
| 임계값 조회·저장 | `GET/POST/PATCH /api/thresholds` | 활성 threshold profile 유지 |
| 측정 저장 | `POST /api/measurements` | captures → analysis_runs → roi_measurements 기록 |
| 이벤트 조회·확인 | `GET /api/alerts`, `PATCH /api/alerts/{id}` | alert_events 표시 및 확인 |
| 전송 결과 | `POST /api/notification-deliveries` | Telegram 성공·실패 기록 |

측정 저장 전에 다음 값이 모두 준비되어야 합니다.

- `identity.db_camera_id`
- 각 ROI의 `db_roi_id`
- 해당 카메라 또는 ROI에 적용 가능한 활성 threshold profile

`roi_measurements`까지 기록되는데 `alert_events`가 없을 수 있는 정상적인 경우도
있습니다. Warning이거나, Critical 상태가 계속 유지 중이거나, 알람 쿨다운 중이면
`do_alarm=false`이므로 이벤트를 만들지 않습니다. Telegram 전송 결과는 백엔드가
반환한 `alert_id`가 있을 때만 `notification_deliveries`에 연결할 수 있습니다.

## 데이터와 로그

기본 데이터 위치:

```text
thermal_dataset/
├── *.jpg
├── *_visual.jpg
├── *_thermal.npy
├── metadata.csv
└── overlay/
```

애플리케이션 로그는 `logs/app.log`에 기록됩니다.

```bash
tail -f logs/app.log
rg "measurement POST|ALARM|Telegram|notification" logs/app.log
```

MariaDB 테이블의 실제 컬럼은 추정하지 말고 먼저 확인합니다.

```sql
DESCRIBE alert_events;
DESCRIBE notification_deliveries;
SELECT * FROM alert_events ORDER BY alert_id DESC LIMIT 30;
SELECT * FROM notification_deliveries ORDER BY delivery_id DESC LIMIT 30;
```

`notification_deliveries`에는 `created_at` 컬럼이 없으므로 정렬에는
`delivery_id`를 사용합니다.

## 테스트

전체 테스트:

```bash
python -m pytest -q
```

의존성을 별도 설치하지 않는 `uv` 실행 예:

```bash
uv run --with-requirements requirements.txt --with pytest python -m pytest -q
```

최종 확인 결과는 **48 passed, 1 skipped**입니다.

## 관련 문서

- [내부 연동 가이드](INTEGRATION_GUIDE.md)
- [아키텍처](ARCHITECTURE.md)
- [제품 설계 초안](product_design.md)
- [백엔드·FLIR·FastAPI 운영 정리](Hotspot_Guard_2026-07-27_백엔드_FLIR_FastAPI_사용설명서포함_전체정리.md)

## 보안

- `.env`, `config.json`, 운영 로그와 촬영 데이터에는 자격 증명이나 현장 정보가
  포함될 수 있으므로 외부에 공개하지 않습니다.
- Telegram Bot Token과 DB 비밀번호를 코드나 문서에 직접 입력하지 않습니다.
- 운영 로그를 전달할 때 IP, 토큰, Chat ID와 현장 식별 정보를 먼저 마스킹합니다.

## 라이선스

Private project. All rights reserved.
