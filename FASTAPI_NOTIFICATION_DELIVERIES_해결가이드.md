# FastAPI `notification_deliveries` 기록 문제 해결 가이드

## 1. 문제 원인

현재 컴퓨터에는 FastAPI 백엔드가 두 군데에 있습니다.

### systemd 서비스가 실행하는 구버전

```text
/home/autoit/Project_hotspot/backend/app.py
```

### 현재 수정 중인 최신 프로젝트

```text
/home/autoit/Desktop/Hotspot_guard/ThermoGuard/Project_hotspot/backend/app.py
```

최신 프로젝트의 `app.py`에는 다음 API가 구현되어 있습니다.

```text
GET  /api/notification-deliveries
POST /api/notification-deliveries
```

하지만 `hotspot-backend.service`는 구버전 경로를 기준으로 FastAPI를
실행합니다. 구버전에는 위 API가 없기 때문에 Swagger UI에도 나타나지 않고,
텔레그램 전송 후 전송 결과를 기록하려고 하면 `404 Not Found`가 발생합니다.

```text
텔레그램 전송 성공
→ POST /api/notification-deliveries
→ 404 Not Found
→ notification_deliveries INSERT가 실행되지 않음
```

> 실제 테이블 이름은 `notification_delivers`가 아니라
> `notification_deliveries`입니다.

---

## 2. 권장 해결 방법

systemd가 현재 최신 프로젝트를 실행하도록 서비스 설정을 변경합니다.

### 2-1. 현재 서비스 설정 백업

터미널에서 다음 명령을 실행합니다.

```bash
sudo cp /etc/systemd/system/hotspot-backend.service \
  /etc/systemd/system/hotspot-backend.service.backup
```

### 2-2. 서비스 설정 열기

```bash
sudo nano /etc/systemd/system/hotspot-backend.service
```

다음 두 항목을 찾습니다.

```ini
WorkingDirectory=/home/autoit/Project_hotspot/backend
ExecStart=/home/autoit/Project_hotspot/backend/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

아래와 같이 변경합니다.

```ini
WorkingDirectory=/home/autoit/Desktop/Hotspot_guard/ThermoGuard/Project_hotspot/backend
ExecStart=/home/autoit/Project_hotspot/backend/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

`WorkingDirectory`는 최신 소스 경로로 변경하고, `ExecStart`의 `uvicorn`은
현재 설치되어 있는 기존 가상환경을 그대로 사용합니다.

설정 전체의 핵심 형태는 다음과 같습니다.

```ini
[Service]
WorkingDirectory=/home/autoit/Desktop/Hotspot_guard/ThermoGuard/Project_hotspot/backend
ExecStart=/home/autoit/Project_hotspot/backend/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000
```

`nano`에서는 `Ctrl+O`, `Enter`로 저장하고 `Ctrl+X`로 종료합니다.

### 2-3. systemd 설정 반영 및 재시작

```bash
sudo systemctl daemon-reload
sudo systemctl restart hotspot-backend.service
```

### 2-4. 서비스 상태 확인

```bash
sudo systemctl status hotspot-backend.service --no-pager -l
```

다음과 같이 표시되면 정상입니다.

```text
Active: active (running)
```

실패했다면 로그를 확인합니다.

```bash
sudo journalctl -u hotspot-backend.service -n 100 --no-pager
```

---

## 3. Swagger에서 API 확인

브라우저에서 다음 주소를 엽니다.

```text
http://127.0.0.1:8000/docs
```

아래 API 두 개가 보이는지 확인합니다.

```text
GET  /api/notification-deliveries
POST /api/notification-deliveries
```

터미널에서는 OpenAPI 문서를 이용해 확인할 수 있습니다.

```bash
curl -s http://127.0.0.1:8000/openapi.json \
  | grep -o '/api/notification-deliveries'
```

경로가 출력되면 실행 중인 FastAPI에 라우트가 정상 등록된 것입니다.

---

## 4. API 직접 테스트

먼저 최근 `alert_id`를 확인합니다.

```sql
SELECT alert_id, event_status, occurred_at
FROM alert_events
ORDER BY alert_id DESC
LIMIT 10;
```

조회된 실제 `alert_id`를 사용하여 API를 호출합니다. 아래 예시의 `176`은
반드시 현재 DB에 존재하는 값으로 바꿉니다.

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

정상 응답 예시는 다음과 같습니다.

```json
{
  "status": "created",
  "delivery_id": 1,
  "alert_id": 176,
  "delivery_status": "success"
}
```

응답 HTTP 상태가 `200`이어도 JSON의 `status`가 `error`일 수 있으므로
응답 본문까지 확인해야 합니다.

---

## 5. DB 기록 확인

MariaDB에서 다음 SQL을 실행합니다.

```sql
DESCRIBE notification_deliveries;

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

텔레그램 전송 성공 건은 일반적으로 다음과 같이 기록됩니다.

```text
delivery_status = success
http_status     = 200
sent_at         = 전송 성공 시각
error_message   = NULL
```

---

## 6. 실제 텔레그램 연동 확인

ThermoGuard를 실행하고 Critical 알람을 한 번 발생시킨 뒤 애플리케이션 로그를
확인합니다.

```bash
cd /home/autoit/Desktop/Hotspot_guard/ThermoGuard
grep -E 'sendPhoto success|notification_deliveries|notification delivery API' \
  logs/app.log | tail -n 30
```

정상일 때는 다음 흐름이 기록됩니다.

```text
sendPhoto success
save_delivery_result ENTER
notification_deliveries 저장 성공
```

DB에서도 같은 `alert_id`의 전송 기록이 생성되었는지 확인합니다.

---

## 7. 오류별 확인 방법

### Swagger에 API가 계속 보이지 않는 경우

실제로 실행 중인 프로세스의 명령을 확인합니다.

```bash
ps -ef | grep '[u]vicorn'
```

8000번 포트를 사용하는 프로세스도 확인합니다.

```bash
sudo ss -ltnp | grep ':8000'
```

수동으로 실행한 구버전 `uvicorn`이 8000번 포트를 먼저 사용하고 있다면 해당
프로세스를 정상 종료한 뒤 systemd 서비스를 다시 시작합니다.

### `404 Not Found`

실행 중인 FastAPI가 최신 `app.py`를 읽지 않고 있다는 뜻입니다.
`WorkingDirectory`와 실행 중인 `uvicorn` 프로세스를 다시 확인합니다.

### `alert_id=...인 경고 이벤트가 없습니다`

전달한 `alert_id`가 `alert_events` 테이블에 존재하지 않습니다. API 테스트 시
DB에서 실제로 조회한 `alert_id`를 사용해야 합니다.

### `500` 또는 JSON의 `status: error`

백엔드 로그와 테이블 구조를 확인합니다.

```bash
sudo journalctl -u hotspot-backend.service -n 100 --no-pager
```

```sql
DESCRIBE notification_deliveries;
```

---

## 8. 문제 발생 시 롤백

변경한 서비스가 실행되지 않을 경우 백업한 설정으로 복구합니다.

```bash
sudo cp /etc/systemd/system/hotspot-backend.service.backup \
  /etc/systemd/system/hotspot-backend.service

sudo systemctl daemon-reload
sudo systemctl restart hotspot-backend.service
sudo systemctl status hotspot-backend.service --no-pager -l
```

---

## 9. 최종 점검 목록

- [ ] `hotspot-backend.service`의 `WorkingDirectory`가 최신 프로젝트 경로이다.
- [ ] `hotspot-backend.service`가 `active (running)` 상태이다.
- [ ] `/docs`에 GET `/api/notification-deliveries`가 보인다.
- [ ] `/docs`에 POST `/api/notification-deliveries`가 보인다.
- [ ] 직접 POST 테스트 결과가 `status: created`이다.
- [ ] `notification_deliveries` 테이블에 테스트 행이 생성된다.
- [ ] Critical 알람 시 텔레그램이 도착한다.
- [ ] 같은 `alert_id`의 Telegram 전송 결과가 DB에 기록된다.

