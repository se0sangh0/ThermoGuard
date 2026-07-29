# ThermoGuard 내부 연동 가이드

이 문서는 과거 `API_GUIDE.md`로 작성됐던 내용을 현재 구현에 맞게 정리한
**애플리케이션 내부 연동 문서**입니다.

`thermal_monitoring`은 별도의 외부 HTTP API 제품이 아니라 Python 패키지입니다.
따라서 이 문서에서는 공개 API라는 표현 대신 패키지 진입점, 모듈 책임, Product
Dashboard와 기존 FastAPI 백엔드 사이의 연동 계약을 설명합니다. 백엔드의 HTTP
경로 자체는 FastAPI의 `/docs`에서 확인합니다.

## 적용 범위

- 운영 장비: FLIR 카메라 1대, 로봇 1대
- 분석 단위: 한 카메라 안의 하나 이상의 ROI
- 설정 저장: `config.json`
- 측정 및 이벤트 저장: `Project_hotspot/backend/app.py`를 거쳐 MariaDB에 기록
- 알림: 상태 머신이 승인한 Critical 이벤트만 Telegram 전송

DB 스키마는 확장 가능성을 유지하지만 현재 Product Dashboard에서 여러 카메라나
여러 로봇을 선택·비교하는 운영 절차는 다루지 않습니다.

## 진입점과 책임

| 진입점 | 책임 |
|---|---|
| `python dashboard.py` | 실제 운영용 통합 GUI |
| `python monitor.py` | 실시간 감시 시퀀서 |
| `python pipeline.py` | 기존 파일 배치 분석 |
| `uvicorn app:app ...` | FastAPI 백엔드 실행 |

Product Dashboard가 담당하는 작업:

- FLIR 수집 시작과 중지
- Thermal JPEG의 온도 행렬 변환
- ROI별 온도와 핫스팟 분석
- 이미지 품질 판정과 오버레이 표시
- 장비, ROI, 임계값의 Backend 동기화
- 측정값 전송과 DB 이벤트 조회
- Critical Telegram 전송
- 캘리브레이션과 ROI 도구 실행

GUI에서 이 흐름을 다시 조합한 별도 통합 클래스를 만들 필요는 없습니다.
기능을 추가할 때는 `ProductDashboard`의 기존 위임 구조를 유지합니다.

## 패키지 진입점

패키지 최상위에는 설정과 공통 유틸리티만 노출합니다.

```python
from thermal_monitoring import (
    AppConfig,
    get_logger,
    load_config,
    save_config,
    setup_encoding,
)
```

수집:

```python
from thermal_monitoring.capture import (
    CaptureSession,
    extract_from_jpeg,
    probe_thermal_from_url,
    raw2temp,
)
```

분석:

```python
from thermal_monitoring.analysis import (
    MonitorState,
    RoiConfig,
    RoiResult,
    Status,
    apply_roi_state_updates,
    create_overlay,
    evaluate_rois_with_state,
    evaluate_threshold,
    evaluate_with_state,
    extract_all_rois_from_npy,
    extract_roi_from_npy,
    load_roi_config,
    save_overlay,
    send_alarm,
)
```

데이터 관리와 파이프라인:

```python
from thermal_monitoring.data import (
    run_check,
    run_cleanup,
    run_cleanup_if_due,
    run_metadata,
)
from thermal_monitoring.pipeline import (
    MonitorSequencer,
    monitor_main,
    pipeline_main,
    run_pipeline,
)
```

`thermal_monitoring.analysis._get_roi_bounds_list`처럼 이름이 밑줄로 시작하는
함수는 패키지에서 현재 노출되더라도 내부 구현 세부사항으로 취급합니다.

## 설정 연동

설정 타입은 Pydantic 모델이 아니라 Python `dataclass`입니다.

```python
from thermal_monitoring import load_config, save_config

cfg = load_config()
cfg.camera.ip = "192.168.0.51"
cfg.monitoring.alarm_cooldown_sec = 600.0
save_config(cfg)
```

`load_config()`는 프로세스 안에서 값을 캐시합니다. 파일을 외부에서 바꾼 뒤 다시
읽어야 한다면 다음처럼 강제 갱신합니다.

```python
cfg = load_config(force_reload=True)
```

### 문자열 ID와 DB ID

```text
identity.camera_id      화면·파일용 문자열 코드
identity.robot_id       화면·Telegram용 문자열 코드
identity.db_camera_id   cameras.camera_id 정수 외래키
identity.db_robot_id    robots.robot_id 정수 외래키
roi.rois[].db_roi_id    roi_definitions.roi_id 정수 외래키
```

문자열 ID를 MariaDB 외래키 위치에 보내면 안 됩니다. Product Dashboard에서 장비와
ROI 정보를 저장하면 `asset_api_client.py`와 `roi_api_client.py`가 정수 DB ID를
확보하여 설정에 반영합니다.

현재 단일 카메라 운영의 장비 재사용 순서:

1. `camera_code`가 같은 레코드를 찾습니다.
2. 카메라 IP가 같은 레코드를 찾습니다.
3. DB에 카메라가 정확히 한 대만 있으면 그 레코드를 사용합니다.
4. 재사용할 레코드가 없을 때만 누락된 장비 계층을 생성합니다.

이 규칙은 기존 `app.py`나 DB 구조를 바꾸지 않고 중복 INSERT를 줄이기 위한 내부
처리입니다.

## 수집 연동

```python
from thermal_monitoring.capture import CaptureSession

capture = CaptureSession(
    cam_ip="192.168.0.51",
    mode="both",
    interval=30.0,
    save_dir="thermal_dataset",
)
capture.start()
```

주요 제어:

```python
capture.set_warning_mode(True)   # warning_interval_sec, 기본 5초
capture.set_warning_mode(False)  # capture_interval_sec, 기본 30초

thermal_path, visual_path = capture.last_saved_pair

capture.request_stop()           # 중단 신호만 설정, GUI를 기다리게 하지 않음
capture.stop()                   # 중단 후 캡처 스레드 join 시도
```

Warning 모드는 이름과 무관하게 Warning과 Critical에서 사용하는 빠른 수집
모드입니다. 이 모드 전환 자체는 Telegram 전송을 의미하지 않습니다.

`capture_both_once()`는 세션이 실행 중일 때만 동작합니다. `mode="thermal"`이면
Visual 경로는 `None`이며 실패 시 `(None, None)`을 반환합니다.

## ROI 분석과 상태 판정

다중 ROI 분석이 기본 연동 방식입니다.

```python
from thermal_monitoring.analysis import (
    MonitorState,
    apply_roi_state_updates,
    evaluate_rois_with_state,
    extract_all_rois_from_npy,
    load_roi_config,
)

roi_cfg = load_roi_config()
roi_results = extract_all_rois_from_npy(
    "thermal_dataset/sample_thermal.npy",
    roi_cfg,
)

state = MonitorState(alarm_cooldown=600.0)
per_roi, worst, do_alarm = evaluate_rois_with_state(
    roi_results,
    baseline=roi_cfg.baseline_temp,
    warning_delta=roi_cfg.warning_delta,
    critical_delta=roi_cfg.critical_delta,
    state=state,
)
apply_roi_state_updates(state, per_roi)
```

`do_alarm`의 의미:

| 판정 | `do_alarm` |
|---|---|
| Normal | 항상 `False` |
| Warning | 항상 `False` |
| Critical 최초 진입 | 쿨다운 조건을 만족하면 `True` |
| Critical 유지 | `False` |
| 쿨다운 중 재진입 | `False` |

Threshold는 95th percentile 경로와 max 온도 보완 경로를 함께 사용하고,
`max_hotspot_size`로 작은 노이즈를 걸러냅니다. 실제 기준은
`thermal_monitoring/analysis/threshold.py`의 `evaluate_threshold()`가 단일
진실 공급원입니다.

## 오버레이

```python
from thermal_monitoring.analysis import create_overlay, save_overlay

overlay = create_overlay(
    thermal_jpg_path="thermal_dataset/sample.jpg",
    visual_jpg_path="thermal_dataset/sample_visual.jpg",
    roi_bounds=(0, 0, 640, 480),
    max_temp=64.2,
    mean_temp=26.1,
    hot_temp=30.8,
    status="Critical",
    hotspot_centroids=[(320, 240, 64.2)],
    roi_bounds_list=[(0, 0, 640, 480)],
    roi_names=["ROI-1"],
)
saved_path = save_overlay("sample", overlay)
```

`paths.homography_path`에 유효한 보정 행렬이 있으면 Visual 좌표계로 변환합니다.
Visual 이미지가 없으면 Thermal 이미지에 직접 표시합니다.

## 캘리브레이션과 ROI 도구

Product Dashboard 환경설정의 캘리브레이션 버튼은 다음 기존 함수를 직접
호출합니다.

```python
from thermal_monitoring.tools.calibration import run_calibration

saved = run_calibration(
    thermal_image_path,
    visual_image_path,
    event_pump=event_pump,
    display_bounds=display_bounds,
)
```

대시보드 내부에 별도 캘리브레이션 알고리즘이나 별도 캘리브레이션 API가 있는
구조가 아닙니다. 창 크기와 배치는
`thermal_monitoring/tools/calibration.py`의 `run_calibration()`에서 전달받은
화면 영역을 기준으로 계산합니다.

ROI 저장은 현재 다음 순서를 따릅니다.

```text
완성된 Thermal/Visual 쌍 확인
  → Homography 파일 확인
  → ROI 편집 창
  → GET /api/rois로 기존 버전 확인
  → 좌표가 같으면 기존 roi_id 재사용
  → 좌표가 바뀌면 새 버전 POST
  → db_roi_id를 config.json에 저장
```

ROI와 캘리브레이션 도구는 동시에 실행하지 않습니다.

## Product Dashboard와 Backend

Dashboard는 MariaDB에 직접 연결하지 않습니다. 다음 기존 FastAPI 경로만
사용합니다.

| 목적 | 메서드와 경로 |
|---|---|
| 연결 상태 | `GET /api/health` |
| 카메라 확인 | `GET /api/cameras` |
| 장비 계층 저장 | `POST /api/factories`, `/api/production-lines`, `/api/robots`, `/api/cameras` |
| ROI 동기화 | `GET/POST /api/rois` |
| 임계값 동기화 | `GET/POST/PATCH /api/thresholds` |
| 측정 기록 | `POST /api/measurements` |
| 이벤트 조회·확인 | `GET /api/alerts`, `PATCH /api/alerts/{alert_id}` |
| 전송 결과 기록 | `POST /api/notification-deliveries` |

### 측정 요청 전제조건

`TelegramDispatcher.post_measurement()`는 다음 조건을 확인합니다.

```text
backend.enabled == true
identity.db_camera_id is not None
measurement_roi.db_roi_id is not None
Backend에 적용 가능한 활성 threshold profile이 있음
```

조건이 없으면 측정을 전송하지 않거나 Backend가 오류 응답을 반환합니다.
`config.json`의 문자열 `camera_id`와 ROI 이름만으로는 DB 측정을 저장할 수
없습니다.

### DB 쓰기 순서

`POST /api/measurements`가 성공하면 Backend가 한 트랜잭션 안에서 다음 순서로
기록합니다.

```text
captures
  → analysis_runs
  → roi_measurements
  → alert_events (do_alarm=true인 경우만)
```

응답의 `alert_id`는 `do_alarm=false`이면 `null`입니다.

Telegram 작업자는 측정 POST 완료를 기다려 `alert_id`를 받은 뒤 알림을
전송합니다. `alert_id`가 있어야 전송 성공 또는 실패를
`notification_deliveries`에 연결할 수 있습니다.

## Telegram 연동

필수 환경변수:

```dotenv
BOT_TOKEN=
CHAT_ID=
TELEGRAM_ENABLED=true
FASTAPI_URL=http://127.0.0.1:8000
```

Product Dashboard에서는 `TelegramDispatcher.maybe_dispatch()`가 다음을 모두
만족하는 프레임만 전송합니다.

- 상태 머신의 `alarm` 값이 `True`
- 현재 상태가 Critical
- 이미지 품질이 정상
- Telegram 로그인 정보가 설정됨
- 알림 전송이 활성화됨
- 동일 프레임을 이미 전송하지 않음

전송 실패 건은 60초 백오프 후 재시도 대상으로 유지됩니다. Warning 프레임을
Telegram으로 보내는 호출을 추가하면 현재 제품 규칙과 맞지 않습니다.

저수준 `send_alarm()`을 직접 호출할 수는 있지만 운영 코드에서는 상태 머신과
DB `alert_id` 연계를 보장하는 `TelegramDispatcher`를 거치는 것이 원칙입니다.
`FASTAPI_URL`은 전송 결과를 `notification_deliveries`에 기록할 Backend 주소이며,
생략하면 `http://127.0.0.1:8000`을 사용합니다. Dashboard의 `backend.url`을 원격
주소로 바꿨다면 이 값도 같은 Backend를 가리키도록 설정해야 합니다.

## 데이터 관리

```python
from thermal_monitoring.data import (
    run_check,
    run_cleanup,
    run_cleanup_if_due,
    run_metadata,
)

check_result = run_check(save_dir="thermal_dataset")
metadata_result = run_metadata(save_dir="thermal_dataset")
cleanup_result = run_cleanup(
    save_dir="thermal_dataset",
    retention_days=2,
)
```

`run_cleanup_if_due()`의 내부 실행 간격은 현재 15분입니다. Product Dashboard는
불필요한 호출을 줄이기 위해 1시간마다 실행 여부를 확인합니다. 이 간격은
`MonitoringConfig`의 필드가 아니며 `cleanup_interval_sec` 설정도 존재하지
않습니다. 설정에서 조정하는 값은 `cleanup_retention_days`입니다.

정리 작업은 보존 기간이 지난 Normal 파일 쌍과 고아 파일을 제거하고, 메타데이터에
Warning/Critical 이력이 있는 쌍은 보존합니다.

## 로그

```python
from thermal_monitoring import get_logger

log = get_logger("custom.component")
log.info("component started")
```

기본 로그 파일은 `logs/app.log`입니다.

```bash
tail -f logs/app.log
rg "measurement POST|alert_id|Telegram|notification" logs/app.log
```

운영 Backend가 systemd 서비스라면:

```bash
sudo journalctl -u hotspot-backend.service -n 200 --no-pager
sudo journalctl -u hotspot-flir-collector.service -n 200 --no-pager
```

## 검증

```bash
python -m pytest -q
```

핵심 회귀 테스트:

| 테스트 | 확인 내용 |
|---|---|
| `test_asset_api_client.py` | 단일 카메라 재사용과 장비 ID 저장 |
| `test_roi_api_client.py` | 카메라 ID 해석과 ROI 버전 동기화 |
| `test_backend_measurement_contract.py` | 측정/이벤트 요청 계약 |
| `test_telegram_dispatcher.py` | Critical 전송과 `alert_id` 연결 |
| `test_threshold.py` | Warning 미전송, Critical 상태 전환과 쿨다운 |
| `test_product_dashboard_calibration.py` | 기존 캘리브레이션 함수 호출 |
| `test_dashboard_regressions.py` | Dashboard 회귀 조건 |

최종 확인 결과는 **48 passed, 1 skipped**입니다.

## 문제 해결

### `1062 Duplicate entry`

장비 저장 전에 `config.json`의 DB ID가 비어 있더라도 Backend의 카메라 목록을
조회하여 기존 단일 카메라를 재사용합니다. 계속 발생하면 `/api/cameras` 결과와
`identity.db_camera_id`, `identity.db_robot_id`를 함께 확인합니다.

### `적용 가능한 threshold profile이 없습니다`

측정의 `camera_id`와 `roi_id`에 연결된 활성 profile이 없다는 뜻입니다.
Product Dashboard 환경설정에서 장비와 ROI를 먼저 저장한 뒤 임계값을 다시
저장합니다.

### `roi_measurements`는 있는데 `alert_events`가 없음

다음은 정상 동작입니다.

- Warning 측정
- Critical 유지 상태
- 알람 쿨다운 중
- 상태 머신이 `do_alarm=false`로 판정한 측정

Critical 최초 진입이어야 하는데도 없다면 `logs/app.log`에서 `do_alarm`,
`alert_id`, `backend POST`를 함께 확인합니다.

### Telegram은 왔는데 `notification_deliveries`가 없음

전송 시점에 Backend 측정 POST가 성공하여 `alert_id`를 반환했는지 확인합니다.
`alert_id=None`이면 Telegram은 전송될 수 있어도 DB 전송 이력과 연결할 수
없습니다.

MariaDB 조회 시 실제 컬럼명을 먼저 확인합니다.

```sql
DESCRIBE notification_deliveries;
SELECT *
FROM notification_deliveries
ORDER BY delivery_id DESC
LIMIT 30;
```

`notification_deliveries`에는 `created_at`이 없습니다.
