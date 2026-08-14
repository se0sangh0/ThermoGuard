# ThermoGuard

FLIR A50 Bi-spectrum 카메라로 산업용 로봇의 열 상태를 감시하고, 측정 결과를
FastAPI와 MariaDB에 저장하며 Critical 이벤트를 Telegram으로 알리는 시제품입니다.

현재 운영 범위는 **카메라 1대와 로봇 1대**입니다. DB 스키마와 일부 분석 코드는
확장 가능한 형태를 유지하지만, 장비 선택이나 여러 로봇 간 ID 비교는 현재 운영의
핵심 요구사항이 아닙니다. 한 카메라 안에 여러 ROI를 설정하는 것은 지원합니다.

> 운영 정책: 수집·분석·알림·DB 기록은 프로젝트 루트의
> `python dashboard.py` 한 경로로만 실행합니다. `monitor.py`, `pipeline.py`,
> `Project_hotspot/backend/collector/`는 과거 구현을 보관하지만 의도적으로
> 실행을 차단합니다. FastAPI 백엔드는 대시보드의 지원 서비스이므로 유지합니다.

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
| `dashboard.py` | **유일한 운영 진입점.** 수집, 분석, 설정, DB 연동, 이벤트 확인, Telegram을 통합 |
| `monitor.py` | 비운영 경로. 실행 시 대시보드 사용 안내 후 종료 |
| `pipeline.py` | 비운영 경로. 실행 시 대시보드 사용 안내 후 종료 |
| `Project_hotspot/backend/app.py` | FastAPI 백엔드 |
| `Project_hotspot/backend/database.py` | MariaDB 연결 설정 |

## 프로젝트 구조

```text
ThermoGuard/
├── dashboard.py
├── monitor.py                 # 차단된 과거 CLI 진입점
├── pipeline.py                # 차단된 과거 배치 진입점
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
│   │   └── ...                # 보관된 비운영 구현
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
│       └── collector/            # 차단된 과거 수집기 stub
│
├── tests/
├── logs/
└── thermal_dataset/
```

## 설치

공장 전환 기준으로 검증할 Python 버전은 **3.10–3.12**이며, 현재 현장 런타임
기준선은 3.10입니다. 임의의 전역 Python 대신 승인된 전용 가상환경을 사용합니다.

실제 라인 전환 후보 가상환경은 일반 `requirements.txt`가 아니라
[고정 의존성 기준선](requirements/README.ko.md)의 후보 환경 절차로 만들고 검증합니다.

```bash
python -m pip install -r requirements.txt
python -m pip install pytest
```

Linux에서는 GUI와 FLIR 메타데이터 처리를 위해 시스템 패키지가 추가로 필요할 수
있습니다.

```bash
sudo apt install exiftool python3-tk libgl1
```

개발 환경에서는 환경변수 파일을 만들고 실제 값을 입력합니다.

```bash
cp .env.example .env
```

```dotenv
BOT_TOKEN=
CHAT_ID=
TELEGRAM_ENABLED=false
FASTAPI_URL=http://127.0.0.1:8000
```

공장 장비에서는 불변 릴리스 안에 `.env`나 `config.json`을 만들지 않습니다.
`/var/lib/thermoguard/dashboard.env`와 `/var/lib/thermoguard/config.json`을 쓰고,
각각 `THERMOGUARD_DASHBOARD_ENV`, `THERMOGUARD_CONFIG`로 지정합니다. 대시보드
운영자는 코드에 고정된 `/run/thermoguard/dashboard.lock`을 공용으로 사용합니다.
GUI는 `dashboard.env`와 `config.json`의 소유자인 전용 `thermoguard` 런타임 계정으로
실행합니다. `thermoguard` 그룹은 root가 만든 lock 파일을 읽기 위한 용도일 뿐, 다른
계정에 비밀 파일이나 lock 파일의 쓰기 권한을 부여하지 않습니다.
권한·런처·tmpfiles의 정확한 설치는
[공장 전환 런북](deployment/FACTORY_RUNBOOK.ko.md)을 따릅니다.

Telegram 자격 증명과 활성화 여부는 Dashboard 설정 화면에서도 외부
`dashboard.env`에 저장할 수 있습니다. `FASTAPI_URL`은 전송 결과 감사 기록을
시도할 Backend 주소이며, 생략하면 `http://127.0.0.1:8000`을 사용합니다. DB
자격 증명은 별도의 backend 환경 파일에만 두며, `.env`는 저장소에 커밋하지
않습니다. 기본값은 `TELEGRAM_ENABLED=false`입니다.

## 실행

### 1. MariaDB와 백엔드

DB 스키마가 준비된 MariaDB를 먼저 실행한 뒤 백엔드를 시작합니다.

```bash
cd Project_hotspot/backend
uvicorn app:app --host 127.0.0.1 --port 8000
```

기본 확인 주소:

- 프로세스 상태(liveness): `http://127.0.0.1:8000/api/health`
- DB 준비(readiness): `http://127.0.0.1:8000/api/ready`
- Swagger UI: `http://127.0.0.1:8000/docs`

systemd로 설치된 운영 장비에서는 다음 서비스 로그를 확인합니다.

```bash
sudo systemctl status hotspot-backend.service
sudo journalctl -u hotspot-backend.service -n 200 --no-pager
```

`hotspot-flir-collector.service`는 대시보드와 중복 수집을 만들 수 있으므로
**disabled 상태로 유지**합니다. 현재 저장소의 collector 스크립트도 안전하게
종료하도록 차단되어 있습니다.

### 2. Product Dashboard

대시보드는 호스트 단위 단일 인스턴스 잠금을 사용하며, 유효하지 않은 설정·마운트
루트 데이터 경로·잘못된 임계값에서는 카메라에 접근하지 않고 종료합니다. 공장에서는
외부 설정·환경 파일과 공용 lock을 지정하는 런처로만 실행합니다. 전환 전에는 다음
읽기 전용 점검을 먼저 통과해야 합니다.

```bash
sudo -u thermoguard env \
  THERMOGUARD_CONFIG=/var/lib/thermoguard/config.json \
  THERMOGUARD_DASHBOARD_ENV=/var/lib/thermoguard/dashboard.env \
  THERMOGUARD_LOG_DIR=/var/log/thermoguard \
  THERMOGUARD_FACTORY_MODE=1 \
  /opt/thermoguard/venv/bin/python -m thermal_monitoring.preflight --online
```

`--online`은 카메라와 `/api/ready`를 조회하므로 승인된 시험 창에서만 사용합니다.
DB 17개 필수 테이블과 DDL fingerprint는 backend 가상환경에서 별도로 읽기 전용
확인합니다.

```bash
cd Project_hotspot/backend
python schema_preflight.py --json --verify-fingerprint
```

`drifted`, `not_ready`, 또는 exit code 1/2이면 DB를 자동 변경하지 말고 전환을
중단합니다. 그 다음에만 실행합니다.

```bash
/usr/local/bin/thermoguard-dashboard
```

개발 환경에서만 프로젝트 루트의 `python dashboard.py`를 직접 실행할 수 있습니다.
공장 런처의 전체 내용과 `/run/thermoguard` provision은
[공장 전환 런북](deployment/FACTORY_RUNBOOK.ko.md)에 있습니다.

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

`monitor.py`, `pipeline.py`, `python -m thermal_monitoring.capture.capture`는 운영
데이터와 알림을 중복 처리하지 않도록 차단되어 있습니다. 수집·분석·수동 촬영은
대시보드에서만 수행합니다. 대량 무결성 복구·metadata 재생성·삭제 보존 작업은
자동 타이머에서 실행하지 않으며, 백업과 승인된 별도 유지보수 절차에서만 수행합니다.

## 통합 설정

`config.json`은 애플리케이션의 단일 설정 파일입니다. 운영 대시보드는
`0 < warning_delta < critical_delta`, 전용 데이터 하위 폴더, 유효 ROI/주기/URL을
엄격하게 검증합니다. 잘못된 파일을 기본값으로 덮어쓰지 않습니다.

새 설치는 저장소의 전체 예시인 [config.example.json](config.example.json)을 복사해
승인된 값을 채운 뒤 `preflight`로 검증합니다. 문서에 있는 일부 JSON 조각을 복사해
새 설정을 만들면 엄격한 스키마를 충족하지 못할 수 있습니다. 공장에서는 다음처럼
릴리스 밖의 일반 파일을 사용합니다. 예시의 데이터셋·오버레이·보정 행렬 경로도
`/var/lib/thermoguard` 아래의 절대 경로이므로, 현장 전용 볼륨 하위 경로로 바꿀 경우
세 경로를 함께 검토합니다.

```bash
install -o thermoguard -g thermoguard -m 0640 \
  config.example.json /var/lib/thermoguard/config.json
THERMOGUARD_CONFIG=/var/lib/thermoguard/config.json \
THERMOGUARD_DASHBOARD_ENV=/var/lib/thermoguard/dashboard.env \
THERMOGUARD_LOG_DIR=/var/log/thermoguard \
THERMOGUARD_FACTORY_MODE=1 \
  /opt/thermoguard/venv/bin/python -m thermal_monitoring.preflight
```

Dashboard의 설정 저장은 `THERMOGUARD_CONFIG`가 가리키는 파일에 원자적으로
반영됩니다. 수동 편집과 Dashboard 저장을 동시에 수행하지 말고, 변경 전 백업·두 명
교차 검토·preflight를 운영 절차로 둡니다.

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
| 프로세스 상태 | `GET /api/health` | FastAPI liveness 확인 |
| DB 준비 상태 | `GET /api/ready` | DB read-only readiness 확인 |
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

ROI 저장이 완료되면 각 `db_roi_id`에 대응하는 활성 threshold profile을 즉시
생성하거나 갱신합니다. 기존 설치에서 프로필이 누락된 채 측정이 시작됐더라도
Backend가 해당 오류를 반환하면 프로필을 동기화한 뒤 같은 측정을 한 번만
재시도합니다.

`roi_measurements`까지 기록되는데 `alert_events`가 없을 수 있는 정상적인 경우도
있습니다. Warning이거나, Critical 상태가 계속 유지 중이거나, 알람 쿨다운 중이면
`do_alarm=false`이므로 이벤트를 만들지 않습니다. Telegram 전송 결과는 백엔드가
반환한 `alert_id`가 있을 때만 `notification_deliveries`에 연결할 수 있습니다.

Critical Telegram 전송은 느리거나 사용할 수 없는 DB 저장을 기다리지 않고 즉시
시도합니다. 따라서 Telegram은 도착했지만 `alert_id`가 아직 없어
`notification_deliveries` 감사 이력이 없을 수 있습니다. Telegram 전달 성공과 DB
기록 성공은 서로 독립적으로 확인해야 하며, 전송 이력은 best-effort 감사 정보입니다.

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

`THERMOGUARD_LOG_DIR` 환경변수로만 로그 위치를 명시적으로 바꿀 수 있습니다.
로그 초기화는 `config.json`을 생성·수정하지 않습니다.

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
python -m pytest -q \
  --deselect tests/test_overlay.py::OverlayIntegrationTests::test_latest_dataset_overlay
```

의존성을 별도 설치하지 않는 `uv` 실행 예:

```bash
uv run --with-requirements requirements.txt --with pytest python -m pytest -q \
  --deselect tests/test_overlay.py::OverlayIntegrationTests::test_latest_dataset_overlay
```

현장 전환 기록에는 해당 승인 릴리스에서 나온 실제 test 결과와 실행 환경을 남깁니다.
과거의 고정 pass 개수로 새 릴리스를 판정하지 않습니다.

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
