# ThermoGuard 의존성 기준선

이 디렉터리는 공장 전환 후보 환경을 **같은 패키지 버전으로 재구성·검증**하기 위한
source-controlled 기준선이다. 실행 중인 장비의 전역 Python, 서비스 가상환경 또는
카메라/DB에는 이 파일을 적용하지 않는다.

## 기준 범위

- 관측 기준은 2026-08-13의 Linux x86_64, CPython 3.10.12이다.
- `factory-runtime.in`은 현재 지원되는 Dashboard + FastAPI/MariaDB 경로의 직접
  의존성만 선언한다. 차단된 legacy collector/Flask 백업은 의도적으로 포함하지
  않는다.
- `factory-runtime.constraints.txt`에는 위 직접 의존성과 실제 런타임 의존성 closure의
  정확한 버전을 고정했다. Dashboard 관련 패키지는 저장소 테스트에서 사용한 Python
  환경, Backend 관련 패키지는 `Project_hotspot/backend/venv`에서 읽었다.
- `factory-test.in`은 동일 후보 환경에서 회귀 테스트를 실행할 때만 추가한다.

이는 wheel 해시, 운영체제 패키지, CPU 아키텍처까지 잠그는 공급망 artifact는 아니다.
따라서 다른 Python minor 버전, 다른 Linux 배포판 또는 다른 CPU에서는 이 파일을
그대로 승인 기준으로 사용하면 안 된다. 새 플랫폼은 별도의 후보 환경에서 다시
검증하고, 승인된 내부 wheelhouse와 SHA-256 목록을 별도로 생성해야 한다.

## 후보 환경 준비 절차

아래 작업은 실제 서비스와 분리된 **새 후보 릴리스/가상환경**에서만 수행한다.
`pip install`을 실행 중인 `/opt/thermoguard/venv`에 적용하지 않는다.

```bash
python3.10 -m venv /opt/thermoguard/candidates/<release-id>/venv
/opt/thermoguard/candidates/<release-id>/venv/bin/python -m pip install \
  -r requirements/factory-test.in
/opt/thermoguard/candidates/<release-id>/venv/bin/python \
  requirements/verify_factory_baseline.py
```

버전 검증 스크립트는 constraint 목록의 배포판 버전과 `pip check`만 읽기 전용으로
검사한다. 이 검사가 통과한 뒤, 해당 후보 환경에서 회귀 테스트와 공장 전환 런북의
오프라인 점검을 수행하고 결과·`pip freeze --all`·Python 버전·OS 패키지 버전을
변경 기록에 남긴다. 그 후보가 승인되기 전에는 `current` 링크나 systemd service를
바꾸지 않는다.

## 시스템 및 벤더 의존성

다음 항목은 pip constraint에 포함되지 않으며, 현장 OS와 장비에 맞춰 별도 검증한다.

- `python3-tk`, `libgl1`, `exiftool`: GUI, OpenCV, FLIR 메타데이터 처리에 필요할 수
  있는 OS 패키지
- `PySpin`/Spinnaker는 비운영 legacy 모듈에만 남아 있으며 공장 Dashboard
  기준선의 의존성이 아니다. 운영 경로는 FLIR GigE 카메라의 HTTP REST
  이미지 엔드포인트와 ExifTool을 사용한다.

`camera.gige_enabled`는 기존 설정 호환을 위해 읽지만, Dashboard 시작·preflight를
`PySpin` 설치 여부로 차단하지 않는다.
