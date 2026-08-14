# Historical incident report

> 이 문서는 2026-07-30의 구버전 서비스 경로 문제를 기록한 자료입니다. 현재
> 운영 경로는 `python dashboard.py`와 현 워크스페이스의 `hotspot-backend.service`
> 조합입니다. 아래의 구형 collector 또는 이전 작업 경로 지침을 운영 절차로
> 사용하지 마세요.

# ThermoGuard FastAPI `notification_deliveries` 원인 분석 및 해결

작성 기준일: 2026-07-30

## 1. 최종 진단

`app.py`에 API 코드가 있는데도 `/docs`에 보이지 않는 직접적인 원인은
**확인한 소스 파일과 실제 서비스가 실행하는 소스 파일이 서로 다르기 때문**이다.

현재 컴퓨터에는 아래 두 백엔드가 동시에 존재한다.

| 구분 | 경로 | `notification-deliveries` API |
|---|---|---|
| 현재 ThermoGuard 작업본 | `/home/autoit/Desktop/Hotspot_guard/ThermoGuard/Project_hotspot/backend/app.py` | GET/POST 있음 |
| 기존 운영 복사본 | `/home/autoit/Project_hotspot/backend/app.py` | GET/POST 없음 |

현재 등록된 systemd 서비스는 최신 작업본이 아니라 기존 운영 복사본을 가리킨다.

```ini
WorkingDirectory=/home/autoit/Project_hotspot/backend
ExecStart=/home/autoit/Project_hotspot/backend/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

따라서 `hotspot-backend.service`를 시작하면 다음 파일이 실행된다.

```text
/home/autoit/Project_hotspot/backend/app.py
```

이 파일에는 `/api/notification-deliveries` 라우트가 없다. 이 상태에서
Swagger를 열면 해당 API가 표시되지 않고, API 호출 결과도 `404 Not Found`가
된다.

---

## 2. API와 테이블의 정확한 이름

정확한 복수형 이름은 다음과 같다.

```text
FastAPI 경로: /api/notification-deliveries
DB 테이블:    notification_deliveries
```

다음 이름은 현재 코드에 존재하지 않는다.

```text
/api/notification_delivers
notification_delivers
```

즉 `/docs`에서 찾아야 하는 이름은 `notification_delivers`가 아니라
`notification-deliveries`이다.

최신 작업본에는 실제로 다음 두 라우트가 존재한다.

```text
GET  /api/notification-deliveries
POST /api/notification-deliveries
```

---

## 3. 확인된 증거

### 3.1 최신 작업본의 라우트

최신 `app.py`에는 다음 위치에 구현되어 있다.

```text
416행: @app.get("/api/notification-deliveries")
670행: @app.post("/api/notification-deliveries")
```

### 3.2 기존 운영 복사본의 라우트

`/home/autoit/Project_hotspot/backend/app.py`에는 아래 라우트들이 있지만,
`notification-deliveries`는 없다.

```text
/api/health
/api/db-test
/api/tables
/api/cameras
/api/measurements
/api/alerts
/api/dashboard/summary
/api/rois
/api/thresholds
```

### 3.3 애플리케이션 로그

기존 로그에는 아래 순서가 기록되어 있다.

```text
backend POST ok: capture_id=3892 alert_id=176
sendPhoto success
save_delivery_result ENTER: alert_id=176 success=True http_status=200
POST http://127.0.0.1:8000/api/notification-deliveries
404 Client Error: Not Found
```

이 로그가 의미하는 것은 다음과 같다.

1. 측정값 저장은 성공했다.
2. `alert_events`도 생성되어 `alert_id=176`을 받았다.
3. Telegram 사진 전송도 HTTP 200으로 성공했다.
4. 전송 결과 기록 API를 호출했다.
5. 실행 중인 FastAPI에 해당 경로가 없어 404가 반환됐다.
6. `notification_deliveries` INSERT 코드까지 도달하지 못했다.

따라서 Telegram 설정이나 MariaDB INSERT 자체가 최초 원인이 아니다.
현재 확인되는 1차 실패 지점은 FastAPI 라우팅이다.

---

## 4. 첨부 문서와 실제 컴퓨터 상태의 차이

첨부 문서
`/home/autoit/Downloads/ThermoGuard_FastAPI_실행_문제해결_정리.md`는
새 서비스 이름을 다음과 같이 안내한다.

```text
thermoguard-backend.service
```

하지만 실제 컴퓨터에는 이 서비스가 없다.

```text
thermoguard-backend.service: NOT FOUND
```

현재 실제로 존재하는 서비스는 다음과 같다.

```text
hotspot-backend.service
```

따라서 아래 명령은 현재 등록된 백엔드를 재시작하지 못한다.

```bash
sudo systemctl restart thermoguard-backend
```

현재 등록된 서비스를 조작하는 이름은 다음과 같다.

```bash
sudo systemctl restart hotspot-backend.service
```

다만 현재 `hotspot-backend.service`는 구버전 디렉터리를 가리키므로, 설정을
수정하지 않고 단순히 재시작하면 구버전 API가 다시 실행된다.

---

## 5. 현재 프로세스 상태

분석 시점에는 다음 프로세스가 확인되지 않았다.

```text
uvicorn 프로세스 없음
8000번 포트 LISTEN 없음
```

따라서 현재 `/docs`가 열리지 않거나 이전에 보던 문서와 달라지는 것은
자연스러운 상태이다. 서버를 다시 실행할 때 어느 디렉터리의 `app.py`를
실행하는지가 중요하다.

---

## 6. 권장 해결 방식

서비스를 새로 하나 더 만들기보다, 기존 `hotspot-backend.service`가 최신
작업본을 실행하도록 변경하는 것을 권장한다.

서비스를 두 개 만들면 두 서비스가 모두 8000번 포트를 사용하려고 하여
`Address already in use`가 발생할 수 있다.

### 6.1 먼저 최신 작업본을 8001번 포트에서 검증

운영 서비스 설정을 변경하기 전에 최신 백엔드가 정상적으로 import되고
Swagger에 라우트가 표시되는지 검증한다.

```bash
cd /home/autoit/Desktop/Hotspot_guard/ThermoGuard/Project_hotspot/backend
source venv/bin/activate
python -m uvicorn app:app --host 127.0.0.1 --port 8001
```

다른 터미널에서 다음 명령을 실행한다.

```bash
curl -s http://127.0.0.1:8001/openapi.json \
  | grep -o '/api/notification-deliveries' \
  | sort -u
```

정상 결과:

```text
/api/notification-deliveries
```

브라우저에서도 확인할 수 있다.

```text
http://127.0.0.1:8001/docs
```

검증이 끝나면 Uvicorn을 실행한 터미널에서 `Ctrl+C`로 종료한다.

### 6.2 기존 서비스 설정 백업

```bash
sudo cp /etc/systemd/system/hotspot-backend.service \
  /etc/systemd/system/hotspot-backend.service.backup-20260730
```

### 6.3 기존 서비스 수정

```bash
sudo nano /etc/systemd/system/hotspot-backend.service
```

서비스를 다음 내용으로 맞춘다.

```ini
[Unit]
Description=Hotspot Guard FastAPI Backend
After=network-online.target mariadb.service
Wants=network-online.target
Requires=mariadb.service

[Service]
Type=simple
User=autoit
Group=autoit
WorkingDirectory=/home/autoit/Desktop/Hotspot_guard/ThermoGuard/Project_hotspot/backend
ExecStart=/home/autoit/Desktop/Hotspot_guard/ThermoGuard/Project_hotspot/backend/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

핵심 변경점은 다음 두 줄이다.

```ini
WorkingDirectory=/home/autoit/Desktop/Hotspot_guard/ThermoGuard/Project_hotspot/backend
ExecStart=/home/autoit/Desktop/Hotspot_guard/ThermoGuard/Project_hotspot/backend/venv/bin/python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

### 6.4 설정 반영 및 서비스 시작

```bash
sudo systemctl daemon-reload
sudo systemctl enable hotspot-backend.service
sudo systemctl restart hotspot-backend.service
```

상태를 확인한다.

```bash
sudo systemctl status hotspot-backend.service --no-pager -l
```

정상 상태:

```text
Active: active (running)
```

실패할 경우 로그를 확인한다.

```bash
sudo journalctl -u hotspot-backend.service -n 100 --no-pager
```

---

## 7. 수정 후 반드시 확인할 사항

### 7.1 실행 중인 명령 확인

```bash
pgrep -af uvicorn
```

출력에 최신 경로의 가상환경 또는 최신 작업 디렉터리에서 실행된 프로세스가
확인되어야 한다.

### 7.2 8000번 포트 확인

```bash
sudo ss -ltnp | grep ':8000'
```

Uvicorn 하나만 8000번 포트를 사용해야 한다.

### 7.3 실행 중인 OpenAPI 문서 확인

```bash
curl -s http://127.0.0.1:8000/openapi.json \
  | grep -o '/api/notification-deliveries' \
  | sort -u
```

정상 결과:

```text
/api/notification-deliveries
```

### 7.4 Swagger 확인

```text
http://127.0.0.1:8000/docs
```

아래 두 동작이 같은 경로 아래 표시되어야 한다.

```text
GET  /api/notification-deliveries
POST /api/notification-deliveries
```

Swagger는 같은 경로를 한 번 접힌 형태로 보여줄 수 있으므로 경로를 펼쳐 GET과
POST가 모두 있는지 확인한다.

---

## 8. API 직접 저장 테스트

### 8.1 실제 `alert_id` 확인

MariaDB에서 다음 SQL을 실행한다.

```sql
SELECT alert_id, event_status, occurred_at
FROM alert_events
ORDER BY alert_id DESC
LIMIT 10;
```

### 8.2 전송 결과 API 호출

아래 `176`은 예시이다. 반드시 DB에 실제로 존재하는 `alert_id`로 바꾼다.

```bash
curl -i -X POST \
  http://127.0.0.1:8000/api/notification-deliveries \
  -H 'Content-Type: application/json' \
  -d '{
    "alert_id": 176,
    "delivery_status": "success",
    "http_status": 200,
    "retry_count": 0,
    "error_message": null
  }'
```

정상 응답:

```json
{
  "status": "created",
  "delivery_id": 1,
  "alert_id": 176,
  "delivery_status": "success"
}
```

주의할 점은 현재 FastAPI 코드가 일부 업무 오류도 HTTP 200과 함께
`{"status": "error"}`로 반환한다는 것이다. HTTP 상태만 보지 말고 JSON
본문의 `status`가 `created`인지 확인해야 한다.

### 8.3 DB 확인

```sql
SELECT
    delivery_id,
    alert_id,
    attempted_at,
    delivery_status,
    http_status,
    retry_count,
    sent_at,
    error_message
FROM notification_deliveries
ORDER BY delivery_id DESC
LIMIT 30;
```

---

## 9. 실제 Telegram 연동 결과 확인

Critical 알람을 발생시킨 뒤 다음 로그를 확인한다.

```bash
cd /home/autoit/Desktop/Hotspot_guard/ThermoGuard
grep -E \
  'backend POST ok|sendPhoto success|save_delivery_result|notification_deliveries|notification delivery API' \
  logs/app.log | tail -n 50
```

정상 흐름:

```text
backend POST ok: ... alert_id=숫자
sendPhoto success
save_delivery_result ENTER: alert_id=숫자
notification_deliveries 저장 성공: alert_id=숫자 delivery_id=숫자
```

비정상 흐름별 의미:

| 로그 | 의미 |
|---|---|
| `alert_id is None — skip save_delivery_result` | 측정 API에서 알람 이벤트 ID를 받지 못함 |
| `404 Client Error: Not Found` | 실행 중인 FastAPI에 라우트가 없음 |
| `alert_id=...인 경고 이벤트가 없습니다` | 전달한 ID와 `alert_events`가 일치하지 않음 |
| `Connection refused` | FastAPI가 실행 중이 아니거나 URL/포트가 틀림 |
| `notification_deliveries 저장 성공` | Telegram 전송 결과 DB 기록 완료 |

---

## 10. 설정 변경 후에도 `/docs`에 없을 때

다음 순서로 확인한다.

### 10.1 브라우저 캐시가 아닌 OpenAPI 원본 확인

```bash
curl -s http://127.0.0.1:8000/openapi.json \
  | grep 'notification-deliveries'
```

OpenAPI 원본에는 있는데 브라우저에만 안 보이면 강력 새로고침하거나 시크릿
창에서 `/docs`를 연다.

### 10.2 수동 Uvicorn과 systemd 중복 확인

```bash
pgrep -af uvicorn
sudo ss -ltnp | grep ':8000'
```

두 개 이상의 실행 시도가 있으면 어느 프로세스가 8000번 포트를 선점했는지
확인한다. 단순히 새 Uvicorn을 실행했다고 해서 기존 8000번 서버가 교체되는
것은 아니다.

### 10.3 systemd가 실제로 읽는 설정 확인

```bash
sudo systemctl cat hotspot-backend.service
sudo systemctl show hotspot-backend.service -p WorkingDirectory -p ExecStart
```

출력 경로가 최신 프로젝트 경로인지 확인한다.

### 10.4 실행된 Python이 import한 `app.py` 확인

최신 백엔드 디렉터리에서 다음 명령을 실행한다.

```bash
cd /home/autoit/Desktop/Hotspot_guard/ThermoGuard/Project_hotspot/backend
source venv/bin/activate
python -c "import app; print(app.__file__)"
```

정상 결과:

```text
/home/autoit/Desktop/Hotspot_guard/ThermoGuard/Project_hotspot/backend/app.py
```

라우트도 직접 확인할 수 있다.

```bash
python -c \
  "import app; print([(sorted(r.methods), r.path) for r in app.app.routes if 'notification' in r.path])"
```

정상 결과에는 GET과 POST가 모두 포함되어야 한다.

---

## 11. 롤백 방법

새 경로에서 서비스가 실행되지 않을 경우 백업 설정을 복구한다.

```bash
sudo cp \
  /etc/systemd/system/hotspot-backend.service.backup-20260730 \
  /etc/systemd/system/hotspot-backend.service

sudo systemctl daemon-reload
sudo systemctl restart hotspot-backend.service
sudo systemctl status hotspot-backend.service --no-pager -l
```

롤백하면 서버는 다시 구버전으로 실행되므로 `/api/notification-deliveries`는
다시 사라진다는 점에 주의한다.

---

## 12. 대안: 최신 파일을 기존 운영 경로에 복사

서비스 설정을 바꾸지 않고 최신 소스를 기존 운영 경로로 배포하는 방법도 있다.
하지만 두 폴더의 코드가 다시 달라질 가능성이 높아 권장 방식은 아니다.

이 방법을 사용할 경우 `app.py`만 무조건 덮어쓰면 안 된다. `database.py`,
환경 파일, 의존성 및 관련 모듈이 서로 호환되는지 먼저 확인해야 한다.

따라서 이 프로젝트에서는 다음 원칙을 권장한다.

```text
소스 기준 경로를 최신 프로젝트 한 곳으로 통일
systemd도 그 경로만 실행
수동 Uvicorn과 systemd를 동시에 사용하지 않음
```

---

## 13. 최종 결과 판단 기준

아래 조건을 모두 만족하면 문제가 해결된 것이다.

- [ ] 실제 서비스 이름이 `hotspot-backend.service`임을 확인했다.
- [ ] 서비스 `WorkingDirectory`가 최신 ThermoGuard 백엔드 경로이다.
- [ ] Uvicorn 프로세스가 하나만 실행된다.
- [ ] 8000번 포트를 Uvicorn 하나만 사용한다.
- [ ] `/openapi.json`에 `/api/notification-deliveries`가 존재한다.
- [ ] `/docs`에서 해당 경로의 GET과 POST가 보인다.
- [ ] 직접 POST 요청이 `status: created`를 반환한다.
- [ ] `notification_deliveries` 테이블에 행이 추가된다.
- [ ] 실제 Telegram 전송 후 동일한 `alert_id`의 전송 이력이 저장된다.

## 결론

현재 문제의 핵심은 FastAPI 코드 작성 누락이 아니다. 최신 작업본에는 필요한
GET/POST API와 DB INSERT 코드가 이미 있다.

문제는 다음 세 가지가 겹친 것이다.

1. 백엔드 소스가 두 경로에 중복되어 있다.
2. 실제 `hotspot-backend.service`는 API가 없는 구버전 경로를 실행한다.
3. 첨부 문서는 존재하지 않는 `thermoguard-backend.service` 이름을 안내한다.

기존 `hotspot-backend.service`의 실행 경로를 최신 프로젝트로 통일한 뒤
`daemon-reload`와 재시작을 수행하면 Swagger와 실제 POST 요청이 같은 최신
FastAPI 애플리케이션을 사용하게 된다.
