# Historical deployment reference

> 이 문서는 구형 Aravis collector와 과거 배포 절차를 보존한 자료입니다. 현재
> ThermoGuard의 카메라 수집·분석·알림은 `python dashboard.py`만 사용합니다.
> `hotspot-flir-collector.service`를 활성화하거나 이 문서의 collector 명령을
> 실행하지 마세요. FastAPI 백엔드 서비스만 대시보드 지원용으로 유지합니다.

# Hotspot_Guard 시스템 사용 설명서

> 이 문서는 현재 구축된 **Jetson AGX Orin + FLIR A50 + FastAPI + MariaDB 기반 Hotspot_Guard 시스템을 실제로 어떻게 실행하고 확인하는지**를 처음 사용하는 사람도 따라 할 수 있도록 정리한 사용 설명서이다.  
> 아래 사용 설명서 이후에는 기존의 `Hotspot_Guard_2026-07-27_백엔드_FLIR_FastAPI_전체정리.md` 내용을 그대로 이어서 수록하였다.

---

# 0. 이 시스템은 무엇을 하는가?

Hotspot_Guard는 FLIR A50 열화상 카메라로 설비의 온도를 주기적으로 측정하고, 설정된 ROI 영역의 온도를 분석하여 서버와 데이터베이스에 자동 저장하는 시스템이다.

현재 시스템의 기본 흐름은 다음과 같다.

```text
FLIR A50
   │
   │ 실제 열화상 데이터
   ▼
Jetson AGX Orin
   │
   ▼
grab_flir_temperature.py
   │
   ├─ 열화상 프레임 수신
   ├─ RAW 데이터 확인
   ├─ 섭씨 온도 변환
   ├─ ROI 영역 추출
   └─ 최대/평균/95% 온도 계산
   │
   ▼
FastAPI
    │
    ├─ 측정 데이터 수신
    ├─ thermal_monitoring 판정 결과(status, do_alarm) 그대로 기록
    └─ MariaDB 저장
   │
   ▼
MariaDB
   │
   ├─ 측정 이력
   ├─ 분석 결과
   └─ 경고 이벤트
```

현재 `flir_collector.py`가 약 30초마다 이 과정을 반복한다.

---

# 1. 시스템 사용 전 준비사항

시스템을 사용하려면 다음 장비와 프로그램이 준비되어 있어야 한다.

```text
1. Jetson AGX Orin
2. FLIR A50
3. Jetson과 FLIR A50 간 Ethernet 연결
4. MariaDB
5. FastAPI Backend
6. Aravis
7. Python Collector
8. systemd 서비스
```

현재 프로젝트에서는 다음 IP를 사용한다.

```text
Jetson eno1 : 192.168.0.10
FLIR A50    : 192.168.0.51
```

FLIR A50와 Jetson이 같은 네트워크 대역에 있어야 한다.

---

# 2. 가장 먼저 해야 할 것 — FLIR 연결 확인

시스템을 실행하기 전에 Jetson이 FLIR A50를 찾을 수 있는지 확인한다.

```bash
ping 192.168.0.51
```

정상이라면 다음과 비슷한 응답이 계속 나온다.

```text
64 bytes from 192.168.0.51 ...
```

이 명령의 의미는 단순하다.

```text
Jetson
  │
  │ "192.168.0.51 장비가 살아 있습니까?"
  ▼
FLIR A50
  │
  └─ 응답
```

`ping`이 안 된다면 FastAPI나 Python 문제를 보기 전에 **카메라와 Jetson 사이의 네트워크 연결부터 해결해야 한다.**

---

# 3. Aravis에서 FLIR A50 인식 확인

다음 명령을 실행한다.

```bash
arv-tool-0.8
```

정상적으로 연결되어 있다면 다음과 같은 형태가 나타난다.

```text
FLIR Systems-FLIR A50-89807565 (192.168.0.51)
```

이것은 단순한 Ping보다 한 단계 더 중요한 확인이다.

```text
Ping 성공
→ 네트워크 장비로는 보임

Aravis 검색 성공
→ GigE Vision 카메라로 정상 인식됨
```

즉 FLIR 데이터를 실제 Python 코드에서 가져오기 위해서는 Aravis 검색까지 정상이어야 한다.

---

# 4. 현재 프로젝트 폴더로 이동하는 방법

프로젝트 최상위 폴더:

```bash
cd ~/Project_hotspot
```

현재 기본 구조:

```text
Project_hotspot/
├── backend/
│   ├── app.py
│   ├── database.py
│   ├── venv/
│   └── collector/
│       ├── grab_flir_temperature.py
│       ├── flir_collector.py
│       └── junk_backup/
├── database/
├── logs/
└── storage/
```

Backend 폴더 이동:

```bash
cd ~/Project_hotspot/backend
```

Collector 폴더 이동:

```bash
cd ~/Project_hotspot/backend/collector
```

---

# 5. 시스템 사용 방법은 크게 두 가지이다

현재 시스템은 두 가지 방식으로 사용할 수 있다.

## 방법 A. 수동 테스트

개발·디버깅할 때 사용한다.

```text
사람이 FastAPI 직접 실행
        ↓
사람이 FLIR 측정 프로그램 실행
        ↓
결과 확인
```

## 방법 B. 자동 운전

실제 운영 시 사용한다.

```text
Jetson 부팅
   ↓
systemd
   ├─ FastAPI 자동 실행
   └─ FLIR Collector 자동 실행
           ↓
       약 30초 반복
```

평상시에는 **방법 B를 사용하면 된다.**

---

# 6. 수동으로 FastAPI를 실행하는 방법

systemd를 사용하지 않고 직접 FastAPI를 테스트하고 싶을 때 사용한다.

Backend 폴더로 이동:

```bash
cd ~/Project_hotspot/backend
```

가상환경 실행:

```bash
source venv/bin/activate
```

FastAPI 실행:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

정상이라면:

```text
Application startup complete.
Uvicorn running on http://0.0.0.0:8000
```

이 나온다.

### 주의

이미 systemd의 `hotspot-backend.service`가 실행 중이라면 같은 8000번 포트를 다시 사용할 수 없다.

따라서 수동 테스트를 할 때는 먼저:

```bash
sudo systemctl stop hotspot-backend.service
```

로 자동 Backend 서비스를 멈춘 뒤 실행하는 것이 좋다.

테스트가 끝난 뒤에는:   

```bash
sudo systemctl start hotspot-backend.service
```

로 다시 켤 수 있다.

---

# 7. FastAPI가 정상인지 브라우저에서 확인

Jetson 자체 브라우저에서:

```text
http://127.0.0.1:8000/docs
```

로 접속한다.

Swagger 화면이 나오면 FastAPI가 정상적으로 실행 중이다.

이 화면에서는 직접 API를 테스트할 수 있다.

예:

```text
GET /api/health
GET /api/measurements
GET /api/alerts
POST /api/measurements
PATCH /api/thresholds/{id}
```

초심자가 API를 확인할 때는 `/docs`를 사용하는 것이 가장 쉽다.

---

# 8. FastAPI Health Check 확인

터미널에서는 다음처럼 확인할 수 있다.

```bash
curl http://127.0.0.1:8000/api/health
```

응답이 정상적으로 돌아오면:

```text
FastAPI 프로세스 실행
      +
HTTP 요청 처리 가능
```

상태라는 의미다.

---

# 9. FLIR A50를 1번만 측정하는 방법

Collector 자동 반복이 아니라 **현재 카메라 데이터를 한 번만 확인하고 싶을 때** 사용한다.

```bash
cd ~/Project_hotspot/backend/collector
```

실행:

```bash
/usr/bin/python3 grab_flir_temperature.py
```

여기서 `/usr/bin/python3`을 사용하는 이유는 FLIR 연결에 필요한 `gi`, Aravis Python 바인딩 등이 시스템 Python에 설치되어 있기 때문이다.

정상 실행되면 대략 다음 순서로 로그가 나온다.

```text
FLIR A50 카메라 검색
↓
카메라 설정
↓
프레임 대기
↓
RAW 확인
↓
전체 화면 온도
↓
ROI-01 온도
↓
FastAPI 전송 데이터
↓
FastAPI 응답
↓
FLIR → ROI → FastAPI 전송 완료
```

---

# 10. 실제 FLIR 측정값은 어디를 보면 되는가?

로그에서 다음 부분을 보면 된다.

```text
실제 FLIR 전체 화면 온도

최저 온도
평균 온도
95% 온도
최고 온도
```

그리고 실제 감시 대상 영역은:

```text
ROI-01 온도 분석
```

부분이다.

예:

```text
ROI 최저: 23.46 °C
ROI 평균: 27.26 °C
ROI 95%: 33.88 °C
ROI 최고: 35.14 °C
```

설비 이상 판단에서는 전체 영상보다 **ROI 영역의 온도**가 더 중요하다.

---

# 11. 현재 ROI 영역

현재 설정값:

```text
ROI ID = 4

X1 = 100
Y1 = 80
X2 = 350
Y2 = 280
```

영상은:

```text
464 × 348
```

크기이고, ROI는 그 안에서 다음 영역을 사용한다.

```text
(100,80)
    ┌────────────────────┐
    │                    │
    │       ROI-01       │
    │                    │
    └────────────────────┘
                       (350,280)
```

현재 ROI 크기는:

```text
250 × 200 pixel
```

이다.

---

# 12. 현재 온도 판단 기준

현재 FastAPI가 사용하는 기준은:

```text
Warning = 47°C
Critical = 55°C
```

이다.

판정 방식:

```text
ROI 최고 온도 < 47°C
→ Normal

47°C 이상 55°C 미만
→ Warning

55°C 이상
→ Critical
```

예를 들어:

```text
ROI 최고 = 35.14°C
```

라면:

```text
Normal
```

이다.

FastAPI 응답에서도:

```json
{
  "temperature_status": "normal",
  "warning_temp": 47.0,
  "critical_temp": 55.0
}
```

처럼 확인할 수 있다.

---

# 13. Warning 또는 Critical이 발생하면 어떻게 되는가?

정상일 경우:

```text
측정값 DB 저장
→ alert_id 없음
```

Warning 또는 Critical이면:

```text
측정값 DB 저장
     ↓
alert_events 생성
     ↓
향후 Telegram / Dashboard 알림 연결 가능
```

즉 현재 구조는 경고 이벤트까지 DB에서 관리할 수 있도록 설계되어 있다.

---

# 14. 30초 자동 측정 실행 방법

수동으로 Collector를 시험할 때는:

```bash
cd ~/Project_hotspot/backend/collector
/usr/bin/python3 flir_collector.py
```

을 사용한다.

Collector가 실행되면:

```text
FLIR 측정
→ FastAPI 전송
→ 성공
→ 30초 대기
→ 다시 FLIR 측정
```

을 반복한다.

정상 로그:

```text
FLIR 측정 및 FastAPI 전송 성공
다음 측정까지 30초 대기
```

멈추려면:

```text
Ctrl + C
```

를 누른다.

---

# 15. 실제 운영에서는 수동 실행하지 않아도 된다

현재 FastAPI와 Collector는 systemd 서비스로 등록되어 있다.

Backend 서비스:

```text
hotspot-backend.service
```

Collector 서비스:

```text
hotspot-flir-collector.service
```

따라서 정상적으로 설정되어 있다면 Jetson을 켜는 것만으로 자동 실행된다.

---

# 16. FastAPI 서비스 상태 확인

```bash
systemctl status hotspot-backend.service
```

정상:

```text
Active: active (running)
```

이 상태면 FastAPI가 실행 중이다.

---

# 17. FLIR Collector 서비스 상태 확인

```bash
systemctl status hotspot-flir-collector.service
```

정상:

```text
Active: active (running)
```

이면 30초 자동 수집 프로그램이 실행 중이다.

---

# 18. 두 서비스를 한 번에 확인

```bash
systemctl status hotspot-backend.service hotspot-flir-collector.service
```

둘 다:

```text
active (running)
```

이어야 한다.

---

# 19. 서비스 시작 / 중지 / 재시작 방법

FastAPI 중지:

```bash
sudo systemctl stop hotspot-backend.service
```

FastAPI 시작:

```bash
sudo systemctl start hotspot-backend.service
```

FastAPI 재시작:

```bash
sudo systemctl restart hotspot-backend.service
```

Collector 중지:

```bash
sudo systemctl stop hotspot-flir-collector.service
```

Collector 시작:

```bash
sudo systemctl start hotspot-flir-collector.service
```

Collector 재시작:

```bash
sudo systemctl restart hotspot-flir-collector.service
```

코드를 수정한 뒤 반영할 때는 보통 `restart`를 사용한다.

---

# 20. Collector 실시간 로그 보는 방법

```bash
journalctl -u hotspot-flir-collector.service -f
```

이 명령은 **서비스를 실행하는 명령이 아니라 이미 실행 중인 서비스의 로그를 보는 명령**이다.

따라서 여기서:

```text
Ctrl + C
```

를 눌러도 Collector 서비스 자체는 종료되지 않는다.

단지 로그 화면에서 빠져나오는 것이다.

---

# 21. 최근 Collector 로그만 확인

```bash
journalctl -u hotspot-flir-collector.service -n 50 --no-pager
```

이 명령은 최근 50줄을 한 번에 보여준다.

확인할 핵심 문구:

```text
수신 이미지: 464 x 348

ROI-01 온도 분석

FastAPI 응답

status: created

FLIR → ROI → FastAPI 전송 완료

FLIR 측정 및 FastAPI 전송 성공

다음 측정까지 30초 대기
```

이 순서가 보이면 정상이다.

---

# 22. Backend 로그 확인

FastAPI에서 문제가 생겼다면:

```bash
journalctl -u hotspot-backend.service -n 50 --no-pager
```

를 사용한다.

실시간 로그:

```bash
journalctl -u hotspot-backend.service -f
```

---

# 23. DB에 실제 데이터가 저장되는지 확인

MariaDB 접속 후:

```sql
USE hotspot_guard;
```

최근 ROI 측정값:

```sql
SELECT
    measurement_id,
    roi_id,
    min_temp,
    max_temp,
    mean_temp,
    percentile_95_temp,
    ambient_temp,
    delta_temp,
    status,
    measured_at
FROM roi_measurements
ORDER BY measurement_id DESC
LIMIT 10;
```

약 30초마다 새로운 행이 추가되고 있다면 자동 수집이 정상이다.

---

# 24. 시스템의 정상 동작을 판단하는 가장 쉬운 방법

아래 네 단계만 확인하면 된다.

### 1단계

```bash
systemctl status hotspot-backend.service
```

```text
active (running)
```

### 2단계

```bash
systemctl status hotspot-flir-collector.service
```

```text
active (running)
```

### 3단계

```bash
journalctl -u hotspot-flir-collector.service -n 30 --no-pager
```

```text
FastAPI 전송 성공
```

### 4단계

MariaDB에서 최신 `roi_measurements` 증가 확인.

이 네 개가 모두 정상이면 전체 시스템도 정상이라고 볼 수 있다.

---

# 25. Jetson을 재부팅한 뒤 사용하는 방법

현재 systemd가 활성화되어 있기 때문에:

```bash
sudo reboot
```

후 사람이 직접:

```bash
uvicorn ...
```

또는:

```bash
python3 flir_collector.py
```

를 실행할 필요가 없다.

재부팅 후에는 바로:

```bash
systemctl status hotspot-backend.service
```

```bash
systemctl status hotspot-flir-collector.service
```

만 확인한다.

둘 다 `active (running)`이면 자동 실행 성공이다.

---

# 26. 아주 중요한 주의 — Uvicorn을 두 번 실행하지 말 것

systemd FastAPI가 이미 실행 중인데 다시:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

을 실행하면:

```text
Port 8000 충돌
```

이 발생할 수 있다.

현재 운영 방식에서는:

```text
systemd가 FastAPI 실행
```

을 담당하므로 평상시 직접 Uvicorn을 실행하지 않는 것이 좋다.

직접 시험해야 할 때만 systemd를 먼저 중지한다.

```bash
sudo systemctl stop hotspot-backend.service
```

---

# 27. Python 코드를 수정했을 때

예를 들어:

```text
grab_flir_temperature.py
flir_collector.py
app.py
```

를 수정했다면 해당 서비스를 재시작해야 한다.

Backend 수정:

```bash
sudo systemctl restart hotspot-backend.service
```

Collector 수정:

```bash
sudo systemctl restart hotspot-flir-collector.service
```

둘 다 수정:

```bash
sudo systemctl restart hotspot-backend.service
sudo systemctl restart hotspot-flir-collector.service
```

---

# 28. systemd 서비스 파일을 수정했을 때

Python 파일 수정과 달리:

```text
/etc/systemd/system/*.service
```

파일을 수정했다면 먼저:

```bash
sudo systemctl daemon-reload
```

가 필요하다.

그다음 서비스를 다시 시작한다.

```bash
sudo systemctl restart hotspot-backend.service
```

또는:

```bash
sudo systemctl restart hotspot-flir-collector.service
```

---

# 29. 문제가 발생했을 때 확인 순서

무작정 코드를 수정하지 말고 아래 순서대로 확인한다.

```text
1. FLIR A50 Ping
2. Aravis 카메라 검색
3. FastAPI 상태
4. Collector 상태
5. Collector 로그
6. Backend 로그
7. MariaDB 저장 여부
```

명령으로 표현하면:

```bash
ping 192.168.0.51
```

```bash
arv-tool-0.8
```

```bash
systemctl status hotspot-backend.service
```

```bash
systemctl status hotspot-flir-collector.service
```

```bash
journalctl -u hotspot-flir-collector.service -n 50 --no-pager
```

```bash
journalctl -u hotspot-backend.service -n 50 --no-pager
```

이 순서로 보면 문제 발생 위치를 빠르게 좁힐 수 있다.

---

# 30. FLIR가 안 잡힐 때

확인:

```bash
ping 192.168.0.51
```

Ping부터 안 된다면:

```text
Ethernet 케이블
IP 설정
eno1 인터페이스
FLIR 전원
```

을 확인한다.

Ping은 되는데:

```bash
arv-tool-0.8
```

에서 카메라가 안 나온다면 GigE Vision 통신 또는 방화벽 문제를 의심한다.

---

# 31. FastAPI가 안 켜질 때

상태:

```bash
systemctl status hotspot-backend.service
```

로그:

```bash
journalctl -u hotspot-backend.service -n 100 --no-pager
```

포트 확인:

```bash
sudo ss -ltnp | grep :8000
```

기존 Uvicorn이 이미 8000을 사용 중인지 확인한다.

---

# 32. Collector가 안 돌아갈 때

상태:

```bash
systemctl status hotspot-flir-collector.service
```

로그:

```bash
journalctl -u hotspot-flir-collector.service -n 100 --no-pager
```

그리고 1회 측정을 직접 시험한다.

```bash
cd ~/Project_hotspot/backend/collector
/usr/bin/python3 grab_flir_temperature.py
```

이것이 성공하고 Collector만 실패한다면 Collector 또는 systemd 설정 문제이다.

1회 측정 자체도 실패한다면 FLIR/Aravis/Python 통신 문제이다.

---

# 33. 현재 파일별 사용 목적

## `app.py`

```text
FastAPI 서버 본체
```

평소 직접 실행하지 않고 systemd가 Uvicorn으로 실행한다.

---

## `database.py`

```text
FastAPI ↔ MariaDB 연결
```

보통 사용자가 직접 실행하는 파일이 아니다.

---

## `grab_flir_temperature.py`

```text
FLIR 실제 데이터 1회 측정 및 FastAPI 전송
```

디버깅할 때 직접 실행한다.

```bash
/usr/bin/python3 grab_flir_temperature.py
```

---

## `flir_collector.py`

```text
grab_flir_temperature.py를 약 30초마다 자동 실행
```

실제 운영에서는 systemd가 실행한다.

---

## `junk_backup/`

작업 중 잘못 생성된 파일을 안전하게 격리해둔 폴더이다.

실제 시스템 동작에는 사용하지 않는다.

---

# 34. 현재 시스템을 사용하는 사람의 실제 업무 흐름

평상시 사용자는 사실 많은 명령어를 입력할 필요가 없다.

### Jetson 전원 켜기

```text
전원 ON
```

### FLIR A50 연결 상태 확인

필요 시:

```bash
ping 192.168.0.51
```

### 서비스 상태 확인

```bash
systemctl status hotspot-backend.service
systemctl status hotspot-flir-collector.service
```

### 실시간 모니터링

```bash
journalctl -u hotspot-flir-collector.service -f
```

### 데이터 확인

FastAPI Swagger:

```text
http://127.0.0.1:8000/docs
```

또는 MariaDB에서 직접 조회.

이것이 현재 시스템의 기본 사용 방법이다.

---

# 35. 최종 사용자 관점 요약

현재 시스템은 다음처럼 생각하면 된다.

```text
[사용자]
   │
   │ Jetson과 FLIR 전원 ON
   ▼

[Jetson]
   │
   ├─ FastAPI 자동 시작
   ├─ MariaDB 동작
   └─ Collector 자동 시작
           │
           ▼
       FLIR A50
           │
      약 30초마다
           ▼
       실제 온도 측정
           │
           ▼
       ROI 분석
           │
           ▼
     위험 단계 판단
           │
           ▼
        DB 저장
```

따라서 최종 운영 환경에서는 **Jetson과 FLIR A50가 정상 연결되어 있고 두 systemd 서비스가 `active (running)` 상태인지 확인하는 것이 가장 중요하다.**

---

# 36. 빠른 명령어 모음

### Backend 상태

```bash
systemctl status hotspot-backend.service
```

### Collector 상태

```bash
systemctl status hotspot-flir-collector.service
```

### Collector 실시간 로그

```bash
journalctl -u hotspot-flir-collector.service -f
```

### Collector 최근 로그

```bash
journalctl -u hotspot-flir-collector.service -n 50 --no-pager
```

### Backend 최근 로그

```bash
journalctl -u hotspot-backend.service -n 50 --no-pager
```

### FLIR 연결 확인

```bash
ping 192.168.0.51
```

### Aravis 확인

```bash
arv-tool-0.8
```

### FLIR 1회 측정

```bash
cd ~/Project_hotspot/backend/collector
/usr/bin/python3 grab_flir_temperature.py
```

### Backend 재시작

```bash
sudo systemctl restart hotspot-backend.service
```

### Collector 재시작

```bash
sudo systemctl restart hotspot-flir-collector.service
```

### Jetson 재부팅

```bash
sudo reboot
```

---

# 37. 이 문서의 사용 방법

이 문서는 두 부분으로 구성되어 있다.

```text
1부
Hotspot_Guard 실제 사용 설명서

2부
2026-07-27 구축 과정 및 개념 전체 정리
```

처음 시스템을 사용하는 사람은 먼저 **1부 사용 설명서**를 읽고 실제 실행 방법을 익히는 것이 좋다.

FastAPI, MariaDB, Aravis, ROI, systemd가 각각 왜 필요한지 공부하거나 오늘 구축 과정을 다시 따라가려면 아래의 **기존 전체 정리 내용**을 읽으면 된다.

---

# 기존 전체 정리 내용

# Hotspot_Guard 백엔드·FLIR A50 연동 구축 정리
> 작성 기준: 2026-07-27  
> 환경: Jetson AGX Orin / Ubuntu 22.04 / FLIR A50 / FastAPI / MariaDB / Aravis / systemd

---

## 0. 오늘 한 작업의 최종 목표

오늘 작업의 목적은 단순히 웹 서버 하나를 띄우는 것이 아니라, **실제 FLIR A50 열화상 카메라의 온도 데이터를 Jetson AGX Orin에서 받아서 분석하고, FastAPI를 통해 MariaDB에 저장한 뒤, 이 과정을 자동으로 반복하도록 만드는 것**이었다.

최종 데이터 흐름은 다음과 같다.

```text
FLIR A50
  │
  │ GigE Vision
  ▼
Jetson AGX Orin
  │
  │ Aravis로 프레임 수신
  ▼
Mono16 RAW 데이터
  │
  │ TemperatureLinear10mK 변환
  ▼
실제 섭씨 온도 배열
  │
  ├─ 전체 화면 온도 계산
  │
  └─ ROI-01 영역 온도 계산
          │
          ▼
      FastAPI POST
          │
          ▼
   thermal_monitoring 판정 결과 수신
  (status, do_alarm 그대로 DB 저장)
          │
          ▼
       MariaDB
          │
          ├─ 측정값 저장
          └─ 경고 시 Alert 생성
```

그리고 최종적으로 `systemd`를 이용해 다음 구조까지 구현했다.

```text
Jetson 전원 ON
   │
   ├─ MariaDB 자동 실행
   ├─ FastAPI 자동 실행
   └─ FLIR Collector 자동 실행
             │
             └─ 약 30초마다 FLIR 측정 및 DB 저장
```

---

# 1. 프로젝트 폴더 구조

현재 주요 프로젝트 경로는 다음과 같다.

```text
/home/autoit/Project_hotspot/
├── backend/
│   ├── app.py
│   ├── database.py
│   ├── app_flask_backup.py
│   ├── venv/
│   └── collector/
│       ├── flir_collector.py
│       ├── grab_flir_temperature.py
│       └── junk_backup/
├── database/
├── logs/
└── storage/
```

각 파일 역할은 다음과 같다.

### `app.py`
FastAPI 백엔드의 핵심 파일이다.

웹 브라우저, 프론트엔드, FLIR Collector 등이 요청을 보내면 이를 받아서 처리하고 MariaDB와 연결한다.

### `database.py`
FastAPI가 MariaDB에 접속할 수 있도록 DB 연결 정보를 관리한다.

### `grab_flir_temperature.py`
FLIR A50에서 **실제 열화상 프레임 한 번을 읽고**, RAW 값을 섭씨 온도로 변환한 뒤 ROI 온도를 계산하여 FastAPI로 전송한다.

### `flir_collector.py`
`grab_flir_temperature.py`를 주기적으로 실행시키는 관리 프로그램이다.

현재 목적은 약 30초마다 다음 동작을 반복하는 것이다.

```text
FLIR 측정
→ ROI 분석
→ FastAPI 전송
→ DB 저장
→ 30초 대기
→ 다시 측정
```

---

# 2. FastAPI란 무엇인가?

## 2.1 초심자 관점에서 이해하기

FastAPI는 Python으로 만드는 **백엔드 서버 프로그램**이다.

예를 들어 웹 화면에서 현재 온도를 조회한다고 생각해 보자.

웹 화면이 MariaDB에 직접 들어가서 SQL을 실행하게 만들 수도 있을 것처럼 보이지만, 실제 시스템에서는 그렇게 구성하면 문제가 많다.

```text
[좋지 않은 구조]

웹브라우저
   │
   ▼
MariaDB 직접 접속
```

이렇게 하면 DB 계정과 비밀번호가 노출될 수 있고, 잘못된 SQL 요청으로 데이터가 손상될 가능성도 있다.

그래서 중간에 백엔드를 둔다.

```text
[정상적인 구조]

웹브라우저
   │
   │ HTTP 요청
   ▼
FastAPI
   │
   │ SQL
   ▼
MariaDB
```

FastAPI는 쉽게 말해서 **사용자·센서·웹페이지와 데이터베이스 사이에서 요청을 받아 처리하는 중간 관리자**다.

---

# 3. FastAPI가 이 프로젝트에서 왜 필요한가?

Hotspot_Guard에는 여러 구성 요소가 있다.

```text
FLIR A50
웹 Dashboard
MariaDB
향후 AI 분석 모듈
향후 Telegram 알림
```

이들이 모두 MariaDB에 직접 접속하도록 만들면 구조가 복잡해진다.

FastAPI를 가운데 두면 다음과 같이 단순화된다.

```text
                  ┌─ 웹 Dashboard
                  │
FLIR Collector ───┼─ FastAPI ─── MariaDB
                  │
                  ├─ AI 모듈
                  │
                  └─ 알림 시스템
```

각 프로그램은 DB 내부 구조를 완전히 알 필요 없이 FastAPI의 주소만 알면 된다.

예:

```text
GET  /api/measurements
POST /api/measurements
GET  /api/alerts
PATCH /api/alerts/1
```

---

# 4. API란 무엇인가?

API는 프로그램끼리 데이터를 주고받기 위한 약속이다.

예를 들어 FLIR Collector가 FastAPI에 아래 주소로 데이터를 보낸다.

```text
POST /api/measurements
```

그리고 JSON 형태로 데이터를 전달한다.

예:

```json
{
  "camera_id": 1,
  "roi_id": 4,
  "min_temp": 23.46,
  "max_temp": 35.14,
  "mean_temp": 27.26,
  "percentile_95_temp": 33.88,
  "ambient_temp": 25.85,
  "delta_temp": 9.29,
  "over_temp_pixels": 0,
  "max_hotspot_size": 0,
  "status": "normal",
  "algorithm_version": "v2.0",
  "do_alarm": false,
  "alarm_message": null
}
```

FastAPI는 이 JSON을 받아서 다음 일을 한다.

```text
1. 요청 데이터 읽기
2. camera_id / roi_id 확인
3. DB에서 Threshold 조회 (warning_temp, critical_temp 참조용)
4. thermal_monitoring이 판정한 status, do_alarm을 그대로 사용
5. captures 데이터 생성 (status에 따라 capture_mode, visual_status 결정)
6. analysis_runs 데이터 생성
7. roi_measurements 저장
8. Warning/Critical이면 alert_events 생성
9. 결과를 JSON으로 반환
```

---

# 5. GET / POST / PATCH가 무엇인가?

## GET

데이터를 **조회**할 때 사용한다.

예:

```text
GET /api/measurements
```

의미:

```text
"저장되어 있는 측정 데이터를 보여줘."
```

---

## POST

새로운 데이터를 **생성**할 때 사용한다.

예:

```text
POST /api/measurements
```

의미:

```text
"새로운 FLIR 측정값을 DB에 저장해줘."
```

현재 FLIR Collector가 사용하는 방식이다.

---

## PATCH

기존 데이터의 일부를 **수정**할 때 사용한다.

예:

```text
PATCH /api/thresholds/2
```

의미:

```text
"threshold_id=2의 설정 중 일부만 변경해줘."
```

---

# 6. Swagger `/docs`란?

FastAPI는 API를 만들면 자동으로 테스트 화면을 제공한다.

주소:

```text
http://127.0.0.1:8000/docs
```

이곳에서 다음을 직접 시험할 수 있다.

```text
GET
POST
PATCH
DELETE
```

즉 별도의 Postman 없이도 브라우저에서 API를 확인할 수 있다.

초심자 입장에서는 Swagger를 다음처럼 이해하면 된다.

```text
FastAPI 서버
   │
   ├─ 실제 API
   │
   └─ API 시험용 설명서 = /docs
```

---

# 7. FastAPI 실행 구조

개발 중에는 다음 명령으로 실행했다.

```bash
cd ~/Project_hotspot/backend
source venv/bin/activate
uvicorn app:app --host 0.0.0.0 --port 8000
```

각 명령의 의미는 다음과 같다.

### 프로젝트 폴더 이동

```bash
cd ~/Project_hotspot/backend
```

FastAPI 파일 `app.py`가 있는 곳으로 이동한다.

### Python 가상환경 실행

```bash
source venv/bin/activate
```

프로젝트 전용 Python 환경을 사용하는 것이다.

다른 프로젝트의 라이브러리와 충돌하지 않도록 FastAPI, SQLAlchemy, PyMySQL 등을 별도로 관리할 수 있다.

### Uvicorn 실행

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
```

`uvicorn`은 FastAPI 애플리케이션을 실제 네트워크 서버로 실행해주는 ASGI 서버이다.

```text
app:app
│   └─ app.py 안의 FastAPI 객체
└──── app.py 파일
```

`--host 0.0.0.0`은 Jetson 자신의 localhost뿐 아니라 네트워크를 통해 들어오는 요청도 받을 수 있도록 한다.

`--port 8000`은 FastAPI 서버가 8000번 포트를 사용한다는 뜻이다.

---

# 8. MariaDB 연결

FastAPI와 MariaDB 연결에는 `database.py`를 사용했다.

주요 DB 환경변수 구성은 다음과 같다.

```text
DB_HOST=127.0.0.1
DB_PORT=3306
DB_NAME=hotspot_guard
DB_USER=hotspot
DB_PASSWORD=...
```

구조:

```text
FastAPI
   │
database.py
   │
SQLAlchemy + PyMySQL
   │
MariaDB :3306
   │
hotspot_guard DB
```

MariaDB는 실제 데이터를 저장하는 DBMS이고, FastAPI는 데이터를 저장하거나 조회하는 백엔드 역할을 한다.

---

# 9. 주요 데이터베이스 구조

현재 `hotspot_guard` DB에는 여러 테이블이 구성되어 있다.

핵심 계층:

```text
factories
   │
production_lines
   │
robots
   │
cameras
```

그리고 카메라에서 발생한 데이터는 대략 다음 순서로 연결된다.

```text
cameras
   │
captures
   │
analysis_runs
   │
roi_measurements
   │
hotspots
   │
alert_events
```

주요 테이블 역할:

### `cameras`
FLIR 카메라 정보 관리.

### `roi_definitions`
카메라 영상에서 분석할 ROI 좌표 저장.

현재 주요 ROI:

```text
roi_id = 4
roi_name = ROI-01

x1 = 100
y1 = 80
x2 = 350
y2 = 280
```

### `threshold_profiles`
정상/경고/위험 판단 기준 관리.

현재 동작 확인 값:

```text
baseline_temp = 35°C
warning_delta = 12°C
critical_delta = 20°C
```

따라서:

```text
Warning = 35 + 12 = 47°C
Critical = 35 + 20 = 55°C
```

### `roi_measurements`
실제 ROI 온도 분석 결과 저장.

### `alert_events`
Warning 또는 Critical 발생 시 경고 이벤트 저장.

---

# 10. 위험도 판정 (thermal_monitoring 연동)

열화상 분석 엔진(`thermal_monitoring`)은 다음과 같은 이중 경로 판정을 수행한다.

- **95th percentile 경로**: `95th >= baseline + delta` AND 클러스터 ≥ 3px
- **max 온도 경로**: `max >= baseline + critical_delta` AND 클러스터 ≥ 10px

엔진이 상태 머신(Normal → Warning → Critical)과 쿨다운(600초)을 거쳐 최종 판정을 내리면, 그 결과를 `POST /api/measurements`로 FastAPI에 전달한다. FastAPI는 자체 판정을 하지 않고 전달받은 `status`, `do_alarm` 값을 그대로 DB에 기록한다.

```text
thermal_monitoring 판정 → status="critical", do_alarm=True → FastAPI → DB alert_events INSERT
thermal_monitoring 판정 → status="warning", do_alarm=False → FastAPI → DB roi_measurements만 저장
thermal_monitoring 판정 → status="normal",  do_alarm=False → FastAPI → DB roi_measurements만 저장
```

```json
{
  "status": "created",
  "capture_id": 14,
  "analysis_id": 14,
  "measurement_id": 14,
  "temperature_status": "normal",
  "warning_temp": 47.0,
  "critical_temp": 55.0,
  "alert_id": null,
  "do_alarm": false,
  "algorithm_version": "v2.0"
}
```

이 응답의 의미:

```text
DB 저장 성공
capture_id 14 생성
analysis_id 14 생성
measurement_id 14 생성
현재 상태 Normal
Warning 기준 47°C
Critical 기준 55°C
do_alarm=False → 알람 없음, alert_id 없음
```

---

# 11. FLIR A50 네트워크 연결

Jetson의 Ethernet:

```text
eno1
192.168.0.10/24
```

FLIR A50:

```text
192.168.0.51
```

확인:

```bash
ping 192.168.0.51
```

정상적으로 약 1 ms 수준의 응답을 확인했다.

또한:

```bash
ip neigh
```

를 이용해 FLIR A50의 MAC 주소가 정상적으로 잡히는 것도 확인했다.

---

# 12. Aravis란?

FLIR A50는 GigE Vision / GenICam 기반으로 접근할 수 있다.

Jetson에서는 Aravis를 이용해 카메라를 제어했다.

설치:

```bash
sudo apt install aravis-tools libaravis-dev gir1.2-aravis-0.8 -y
sudo apt install aravis-tools-cli -y
```

카메라 검색:

```bash
arv-tool-0.8
```

정상 결과:

```text
FLIR Systems-FLIR A50-89807565 (192.168.0.51)
```

이 결과가 나온다는 것은 Jetson이 FLIR A50를 GigE Vision 장치로 발견했다는 의미이다.

---

# 13. UFW 문제

처음에는 UFW 방화벽이 활성화되어 있었고, Aravis에서 카메라가 발견되지 않았다.

처음에는 GigE Vision discovery용 포트 등을 허용했지만, 스트리밍에는 추가 UDP 통신이 사용될 수 있기 때문에 단순히 특정 포트 하나만 허용한다고 항상 해결되는 것은 아니다.

테스트를 위해 UFW를 비활성화했을 때:

```bash
sudo ufw disable
```

Aravis에서 FLIR A50를 정상적으로 발견할 수 있었다.

현재 핵심 시스템 검증 동안에는 UFW 비활성 상태에서 동작을 확인했다.

운영 환경에서는 추후 카메라 전용 NIC와 필요한 통신 범위를 기준으로 방화벽 규칙을 다시 설정해야 한다.

---

# 14. FLIR A50 영상 설정

카메라 설정 확인 결과:

```text
Width = 464
Height = 348
PixelFormat = Mono16
IRFormat = TemperatureLinear10mK
Payload = 322944 bytes
```

Payload 검증:

```text
464 × 348 × 2 bytes
= 322944 bytes
```

`Mono16`이므로 한 픽셀당 16bit = 2byte를 사용한다.

---

# 15. TemperatureLinear10mK란?

FLIR에서 받은 RAW 값은 바로 섭씨가 아니다.

현재 카메라는:

```text
TemperatureLinear10mK
```

모드로 설정했다.

이 경우 변환식은 다음과 같이 사용했다.

```python
temp_image = (
    raw_image.astype(np.float32) * 0.01
) - 273.15
```

의미:

```text
RAW × 0.01
→ Kelvin

Kelvin - 273.15
→ Celsius
```

예를 들어 RAW 값이 약:

```text
29900
```

이라면:

```text
29900 × 0.01 = 299 K

299 - 273.15
≈ 25.85°C
```

정도가 된다.

---

# 16. Python에서 GI를 pip로 설치하면 안 됐던 문제

처음에:

```bash
pip install gi
```

방식을 시도했지만 실패했다.

Aravis의 Python 바인딩은 Ubuntu 시스템 패키지를 이용했다.

```bash
sudo apt install python3-gi python3-gi-cairo python3-numpy python3-requests -y
```

따라서 FLIR Collector는 일반 backend venv가 아니라 시스템 Python:

```text
/usr/bin/python3
```

을 사용하도록 구성했다.

이 점은 나중에 systemd에서도 중요하다.

---

# 17. `grab_flir_temperature.py` 역할

이 프로그램은 FLIR 데이터를 **1회 측정**하는 핵심 프로그램이다.

처리 흐름:

```text
1. Aravis 장치 검색
2. FLIR A50 연결
3. 464×348 / Mono16 설정
4. IRFormat = TemperatureLinear10mK
5. Stream 생성
6. Buffer 생성
7. Acquisition 시작
8. 프레임 수신
9. RAW uint16 배열 생성
10. Celsius 변환
11. 전체 영상 통계 계산
12. ROI-01 추출
13. ROI 온도 통계 계산
14. JSON 생성
15. FastAPI POST
16. 카메라 acquisition 종료
```

---

# 18. 실제 RAW 데이터 수신 확인

실제 로그:

```text
raw dtype: uint16
raw shape: (348, 464)

raw min: 29504
raw max: 30829
raw mean: 29900.3169

0이 아닌 픽셀 수: 161472
```

픽셀 수:

```text
464 × 348 = 161472
```

따라서 전체 픽셀 데이터가 정상적으로 들어온 것을 확인했다.

---

# 19. 실제 전체 화면 온도

실제 측정 로그:

```text
최저 온도: 21.89 °C
평균 온도: 25.85 °C
95% 온도: 33.00 °C
최고 온도: 35.14 °C
```

즉 임의의 테스트 값이 아니라 실제 FLIR A50 Radiometric 데이터가 Jetson으로 들어오고 있다는 것을 확인했다.

---

# 20. ROI란?

ROI = Region Of Interest.

열화상 영상 전체를 분석하지 않고 **관심 있는 설비 영역만 잘라서 분석하는 영역**이다.

현재 ROI:

```text
(100, 80)
      ┌───────────────┐
      │               │
      │    ROI-01     │
      │               │
      └───────────────┘
                    (350, 280)
```

Python NumPy 배열에서는:

```python
image[y, x]
```

순서로 접근한다.

따라서:

```python
roi_temp = temp_image[
    80:280,
    100:350
]
```

형태가 된다.

ROI 크기:

```text
X = 350 - 100 = 250
Y = 280 - 80 = 200

ROI = 250 × 200
```

---

# 21. 실제 ROI 측정값

오늘 확인된 실제 로그:

```text
ROI ID: 4
ROI 범위: (100, 80) ~ (350, 280)
ROI 크기: 250 x 200

ROI 최저: 23.46 °C
ROI 평균: 27.26 °C
ROI 95%: 33.88 °C
ROI 최고: 35.14 °C
```

주변 온도는 현재 임시로 전체 영상 평균을 사용한다.

```text
ambient_temp = 25.85°C
```

그리고:

```text
delta_temp
= ROI 최고온도 - 주변온도

= 35.14 - 25.85
≈ 9.29°C
```

현재 `ambient_temp`를 전체 프레임 평균으로 사용하는 것은 임시 설계이며, 향후 실제 주변 온도 정의를 별도로 정교화할 수 있다.

---

# 22. FastAPI로 실제 FLIR 데이터 전송

Python에서는 `requests` 라이브러리를 이용한다.

```python
response = requests.post(
    "http://127.0.0.1:8000/api/measurements",
    json=payload,
    timeout=10
)
```

의미:

```text
requests.post
→ HTTP POST 요청

127.0.0.1
→ 같은 Jetson 내부 FastAPI

8000
→ FastAPI 포트

/api/measurements
→ 측정 데이터 등록 API

json=payload
→ FLIR 분석 결과 전달
```

---

# 23. 오늘 실제 End-to-End 성공 결과

실제 로그:

```text
FastAPI 응답
{'status': 'created',
 'capture_id': 14,
 'analysis_id': 14,
 'measurement_id': 14,
 'temperature_status': 'normal',
 'warning_temp': 47.0,
 'critical_temp': 55.0,
 'alert_id': None}
```

그리고:

```text
FLIR A50 처리 완료
FLIR → ROI → FastAPI 전송 완료
```

이 결과로 다음 전체 흐름이 실제로 연결된 것을 확인했다.

```text
FLIR A50
→ Jetson
→ Aravis
→ RAW
→ Celsius
→ ROI
→ FastAPI
→ MariaDB
```

---

# 24. `grab` / `grap` 파일 문제

작업 중:

```text
grab_flir_temperature.py
grap_flir_temperature.py
```

두 파일이 존재했다.

`grap`은 오타 형태의 파일이었다.

또 코드 전체를 터미널에 잘못 붙여넣는 과정에서 Python 코드 조각이 **파일명으로 생성되는 사고**가 발생했다.

예:

```text
CAMERA_ID
API_URL
roi_max
raw dtype:
try:
except
...
```

이를 바로 삭제하지 않고 안전하게:

```text
junk_backup/
```

으로 이동시켰다.

최종적으로 collector 디렉터리는:

```text
flir_collector.py
grab_flir_temperature.py
junk_backup/
```

으로 정리했다.

현재 사용 파일은:

```text
grab_flir_temperature.py
```

이다.

확인:

```bash
grep -n "import requests" grab_flir_temperature.py
```

결과:

```text
3:import requests
```

그리고:

```bash
grep -n "requests.post" grab_flir_temperature.py
```

결과:

```text
511:    response = requests.post(
```

따라서 최신 FastAPI 전송 기능이 포함된 파일임을 확인했다.

---

# 25. `flir_collector.py`의 필요성

`grab_flir_temperature.py`는 한 번 실행하면:

```text
측정
→ 전송
→ 종료
```

한다.

하지만 실제 모니터링 시스템은 사람이 계속 명령어를 입력할 수 없다.

그래서 `flir_collector.py`가 필요하다.

동작:

```text
while 실행 중:
    grab_flir_temperature.py 실행

    성공:
        30초 대기

    실패:
        짧게 대기 후 재시도
```

즉:

```text
FLIR 1회 측정 프로그램
        ▲
        │
flir_collector.py
        │
        └─ 반복 실행 관리자
```

---

# 26. 30초 자동 측정 성공

실제 systemd 로그:

```text
FLIR 측정 및 FastAPI 전송 성공
다음 측정까지 30초 대기
```

따라서 단순히 한 번 측정하는 것이 아니라 **지속적으로 반복 수집하는 단계까지 성공**했다.

---

# 27. systemd란?

systemd는 Ubuntu/Linux에서 프로그램을 **서비스** 형태로 관리하는 시스템이다.

우리가 터미널에서 직접:

```bash
uvicorn ...
```

을 실행하면 터미널을 끄거나 Jetson을 재부팅하면 프로그램이 종료된다.

하지만 systemd에 등록하면:

```text
Jetson 켜짐
   │
systemd
   │
   ├─ FastAPI 자동 실행
   └─ FLIR Collector 자동 실행
```

이 가능하다.

또 프로그램이 비정상 종료되면 다시 실행하도록 만들 수 있다.

---

# 28. FastAPI systemd 서비스

서비스 파일:

```text
/etc/systemd/system/hotspot-backend.service
```

핵심 설정:

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

WorkingDirectory=/home/autoit/Project_hotspot/backend

ExecStart=/home/autoit/Project_hotspot/backend/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8000

Restart=always
RestartSec=5

Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

---

# 29. systemd 명령어의 의미

서비스 파일을 새로 만들거나 수정하면:

```bash
sudo systemctl daemon-reload
```

systemd에게:

```text
"서비스 설정 파일이 바뀌었으니 다시 읽어라."
```

라고 알려주는 것이다.

자동 시작 등록:

```bash
sudo systemctl enable hotspot-backend.service
```

의미:

```text
"Jetson이 부팅될 때 이 서비스를 자동으로 실행해라."
```

현재 바로 실행:

```bash
sudo systemctl start hotspot-backend.service
```

현재 상태 확인:

```bash
sudo systemctl status hotspot-backend.service
```

정상:

```text
Active: active (running)
```

---

# 30. FastAPI systemd 실행 중 발생한 문제

처음에는:

```text
Active: activating (auto-restart)
Result: exit-code
status=3
```

이 발생했다.

원인은 FastAPI 코드 자체가 아니라 **이미 수동으로 실행한 uvicorn이 8000번 포트를 사용하고 있었기 때문**이었다.

즉:

```text
수동 Uvicorn
   │
   └─ port 8000 사용 중

systemd Uvicorn
   │
   └─ port 8000 사용 시도
          ↓
        충돌
```

기존 수동 서버를 종료한 뒤 systemd 서비스를 다시 실행하자:

```text
Active: active (running)
```

으로 정상화되었다.

실제 로그:

```text
Started server process
Waiting for application startup.
Application startup complete.
Uvicorn running on http://0.0.0.0:8000
```

---

# 31. FLIR Collector systemd 서비스

Collector 역시 systemd로 등록했다.

개념:

```text
hotspot-backend.service
        │
        ▼
FastAPI :8000
        ▲
        │
hotspot-flir-collector.service
        │
        ▼
flir_collector.py
        │
        ▼
grab_flir_temperature.py
        │
        ▼
FLIR A50
```

Collector에서는 시스템 Python을 사용한다.

```text
/usr/bin/python3
```

이유는 Aravis/GI가 Ubuntu 시스템 Python에 설치되어 있기 때문이다.

---

# 32. journalctl이란?

systemd로 실행한 프로그램은 일반 터미널에 로그가 보이지 않는다.

그래서 `journalctl`을 사용한다.

실시간 로그:

```bash
journalctl -u hotspot-flir-collector.service -f
```

최근 50줄:

```bash
journalctl -u hotspot-flir-collector.service -n 50 --no-pager
```

오늘 이 로그를 통해 다음을 실제로 확인했다.

```text
FLIR 연결
RAW 수신
전체 온도 계산
ROI 계산
FastAPI POST
DB 생성
30초 대기
```

---

# 33. 오늘 최종 실제 로그

대표 결과:

```text
수신 이미지: 464 x 348

RAW:
min 29504
max 30829
mean 29900.3169

전체 화면:
최저 21.89°C
평균 25.85°C
95% 33.00°C
최고 35.14°C

ROI-01:
최저 23.46°C
평균 27.26°C
95% 33.88°C
최고 35.14°C

Delta:
9.29°C
```

FastAPI:

```text
status = created
capture_id = 14
analysis_id = 14
measurement_id = 14
temperature_status = normal
warning_temp = 47.0
critical_temp = 55.0
alert_id = None
```

Collector:

```text
FLIR 측정 및 FastAPI 전송 성공
다음 측정까지 30초 대기
```

즉 오늘 목표였던 핵심 데이터 파이프라인이 실제 장비 기준으로 성공했다.

---

# 34. 최종 운영 흐름

현재 전체 구조를 가장 간단하게 정리하면:

```text
[1] FLIR A50
     │
     │ 실제 Radiometric 열화상
     ▼

[2] grab_flir_temperature.py
     │
     ├─ RAW 수신
     ├─ Celsius 변환
     ├─ ROI 추출
     └─ 온도 통계 계산
     │
     ▼

[3] FastAPI
     │
     ├─ JSON 수신
     ├─ Threshold 조회 (참조용)
     ├─ 전달받은 status, do_alarm 그대로 DB 저장
     └─ DB 저장
     │
     ▼

[4] MariaDB
     │
     ├─ captures
     ├─ analysis_runs
     ├─ roi_measurements
     └─ alert_events
     │
     ▼

[5] Dashboard / 향후 알림
```

그리고 이 전체 측정을:

```text
flir_collector.py
```

가 반복 수행한다.

---

# 35. 재부팅 후 최종 확인 방법

systemd 등록까지 끝난 뒤 Jetson을 재부팅한다.

```bash
sudo reboot
```

재부팅 후에는 **직접 uvicorn이나 collector를 실행하지 않는다.**

FastAPI 확인:

```bash
systemctl status hotspot-backend.service
```

Collector 확인:

```bash
systemctl status hotspot-flir-collector.service
```

둘 다:

```text
Active: active (running)
```

이면 정상이다.

Collector 로그:

```bash
journalctl -u hotspot-flir-collector.service -n 50 --no-pager
```

재부팅 이후 새로운 측정 로그가 계속 생성된다면 자동 부팅까지 완성된 것이다.

---

# 36. DB에서 최종 확인

MariaDB:

```sql
USE hotspot_guard;

SELECT
    measurement_id,
    roi_id,
    min_temp,
    max_temp,
    mean_temp,
    percentile_95_temp,
    ambient_temp,
    delta_temp,
    status,
    measured_at
FROM roi_measurements
ORDER BY measurement_id DESC
LIMIT 10;
```

약 30초 간격으로 새로운 행이 계속 추가된다면:

```text
FLIR → FastAPI → MariaDB 자동 수집
```

이 완전히 동작하고 있다는 뜻이다.

---

# 37. 현재 시스템에서 남아 있는 개선사항

오늘 핵심 기능은 완성되었지만, 실제 운영 시스템으로 발전시키려면 몇 가지 개선점이 남아 있다.

## 37.1 ROI 좌표 자동 조회

현재 `grab_flir_temperature.py`에:

```python
X1 = 100
Y1 = 80
X2 = 350
Y2 = 280
```

처럼 좌표가 들어 있다.

최종적으로는:

```text
FastAPI GET /api/rois
        │
        ▼
DB roi_definitions
        │
        ▼
현재 활성 ROI 자동 적용
```

구조가 더 좋다.

그러면 Dashboard에서 ROI를 변경해도 Python 코드를 수정할 필요가 없다.

---

## 37.2 여러 ROI 지원

현재는:

```text
ROI-01
```

한 개만 분석한다.

향후:

```text
ROI-01 = 모터
ROI-02 = 베어링
ROI-03 = 배전반
```

처럼 한 카메라에서 여러 설비 영역을 동시에 분석할 수 있다.

---

## 37.3 `ambient_temp`

현재:

```text
ambient_temp = 전체 영상 평균
```

을 임시로 사용한다.

실제 산업 환경에서는 주변 기준 영역을 별도 ROI로 지정하거나, 설비별 baseline과 결합하는 방법이 더 정확할 수 있다.

---

## 37.4 Hotspot 크기 분석

현재:

```json
"over_temp_pixels": 0,
"max_hotspot_size": 0
```

은 임시값이다.

향후 임계온도를 초과한 픽셀을 찾아:

```text
몇 개의 픽셀이 과열되었는가?
가장 큰 고온 영역의 크기는 얼마인가?
```

를 계산할 수 있다.

이 데이터는 단순 최고온도보다 오탐 감소에 도움이 될 수 있다.

---

## 37.5 상태별 측정주기

현재 약:

```text
30초
```

주기다.

향후:

```text
Normal
→ 30초

Warning
→ 5초

Critical
→ 1~5초 + 즉시 알림
```

형태로 바꿀 수 있다.

DB의 `cameras` 테이블에도 normal/warning interval 개념을 둘 수 있다.

---

## 37.6 알림 연결

현재 Warning / Critical 발생 시 `alert_events`를 생성하는 구조가 있다.

향후:

```text
alert_events
   │
   ├─ Telegram
   ├─ Web Dashboard
   ├─ Email
   └─ 기타 알림
```

으로 확장할 수 있다.

---

## 37.7 UFW 재설정

현재 FLIR GigE Vision 테스트를 위해 UFW를 비활성화했다.

실제 배포 시에는:

```text
카메라 NIC
FLIR IP
FastAPI 접근 네트워크
GigE Vision discovery/stream
```

범위를 확인하고 필요한 트래픽만 허용하는 방식으로 다시 설정하는 것이 좋다.

---

# 38. 오늘 겪은 핵심 트러블슈팅 요약

## 문제 1. FLIR 검색 안 됨

원인 후보:

```text
UFW / GigE Vision 네트워크
```

UFW 비활성 후:

```bash
arv-tool-0.8
```

에서 FLIR A50 발견 성공.

---

## 문제 2. Python에서 `gi` 설치 실패

잘못된 접근:

```bash
pip install gi
```

해결:

```bash
sudo apt install python3-gi python3-gi-cairo python3-numpy python3-requests -y
```

Collector는:

```text
/usr/bin/python3
```

사용.

---

## 문제 3. 처음 온도가 -273.15°C

이는 RAW 데이터가 정상적으로 들어오지 않은 프레임에서 0 값이 섭씨 변환된 결과였다.

이후 RAW를 직접 출력하여:

```text
raw min
raw max
raw mean
non-zero pixel count
```

를 확인했고 정상 RAW 데이터 수신을 검증했다.

이후 실제 20~35°C 수준의 온도를 정상적으로 획득했다.

---

## 문제 4. `grab` / `grap` 중복 파일

오타 파일과 작업 중복이 발생했다.

최종적으로:

```text
grab_flir_temperature.py
```

하나를 실제 사용 파일로 정리했다.

---

## 문제 5. 코드 조각이 파일명으로 생성

Python 코드를 nano/vim 편집기 안이 아니라 터미널 셸에 잘못 붙여넣으면서:

```text
CAMERA_ID
API_URL
try:
except
...
```

같은 이상한 파일들이 생성되었다.

삭제 대신:

```text
junk_backup/
```

으로 먼저 격리하여 안전하게 정리했다.

---

## 문제 6. systemd FastAPI가 재시작 반복

증상:

```text
Active: activating (auto-restart)
status=3
```

원인:

```text
기존 수동 uvicorn이 8000 포트 사용 중
```

해결:

```text
기존 수동 서버 종료
→ systemd FastAPI 실행
```

결과:

```text
Active: active (running)
```

---

# 39. 앞으로 다시 구축할 때의 추천 순서

처음부터 다시 한다면 아래 순서가 가장 안전하다.

```text
1. MariaDB 정상 확인
2. FastAPI 단독 실행
3. /docs 확인
4. DB API 테스트
5. FLIR 네트워크 확인
6. Aravis 장치 검색
7. RAW 1프레임 수신
8. TemperatureLinear10mK 확인
9. 섭씨 변환
10. ROI 추출
11. ROI 통계 계산
12. FastAPI POST 연결
13. DB 저장 확인
14. Warning/Critical 판정 확인
15. flir_collector.py 반복 실행
16. backend systemd 등록
17. collector systemd 등록
18. 재부팅 테스트
```

한 번에 전부 자동화하려고 하면 어느 단계에서 문제가 생겼는지 찾기 어렵다.

따라서 오늘처럼:

```text
카메라
→ 온도
→ ROI
→ API
→ DB
→ 반복
→ 자동부팅
```

순서로 하나씩 검증하는 것이 가장 좋다.

---

# 40. 초심자가 반드시 이해해야 하는 전체 개념

## 데이터베이스

데이터를 저장하는 곳.

```text
MariaDB
```

---

## DBMS

DB를 생성하고 관리하는 프로그램.

이 프로젝트에서는 MariaDB가 DBMS 역할을 한다.

---

## Backend

센서, 웹페이지, DB 사이에서 실제 로직을 처리하는 프로그램.

```text
FastAPI
```

---

## API

프로그램끼리 어떤 주소와 데이터 형식으로 통신할지 정한 약속.

```text
POST /api/measurements
```

---

## Uvicorn

FastAPI Python 코드를 실제 HTTP 서버로 실행하는 프로그램.

```text
FastAPI 코드
     │
     ▼
Uvicorn
     │
     ▼
Port 8000
```

---

## Aravis

GigE Vision 카메라인 FLIR A50와 통신하여 실제 프레임을 가져오는 라이브러리.

---

## ROI

열화상 전체 영상 중 실제로 분석하려는 관심 영역.

---

## Collector

센서 데이터를 주기적으로 읽고 서버로 전송하는 프로그램.

---

## systemd

Jetson이 부팅될 때 FastAPI와 Collector를 자동 실행하고, 프로그램이 죽으면 재실행할 수 있도록 관리하는 Linux 서비스 관리자.

---

# 41. 현재 완성 상태

오늘 기준으로 확인된 핵심 항목:

```text
[완료] MariaDB 구축
[완료] FastAPI 구축
[완료] FastAPI ↔ MariaDB
[완료] Threshold 자동 판정 → thermal_monitoring 연동으로 변경
[완료] Warning/Critical Alert 구조
[완료] FLIR A50 네트워크 연결
[완료] Aravis 장치 발견
[완료] Mono16 프레임 수신
[완료] TemperatureLinear10mK
[완료] RAW → Celsius
[완료] 전체 온도 분석
[완료] ROI-01 분석
[완료] FLIR → FastAPI POST
[완료] FastAPI → DB 저장
[완료] 30초 Collector 반복
[완료] Backend systemd 실행
[완료] Collector systemd 실행
[확인 단계] 재부팅 후 두 서비스 자동실행 최종 검증
```

---

# 42. 한 문장으로 프로젝트를 설명하면

> **Jetson AGX Orin을 로컬 서버로 사용하고 FLIR A50 Radiometric 열화상 카메라의 실제 온도 데이터를 GigE Vision으로 수집하여 ROI 기반으로 분석한 뒤, FastAPI를 통해 MariaDB에 저장하며, thermal_monitoring 엔진의 이중 경로 판정(95th percentile + 클러스터 분석) 결과를 연동받아 systemd와 Collector를 이용해 무인 상태에서도 지속적으로 모니터링하는 산업 설비 과열 감시 시스템을 구축하였다.**

---

# 43. 오늘 작업의 핵심 성과

처음에는 각각 떨어져 있던:

```text
FLIR
Python
FastAPI
MariaDB
Linux
```

를 오늘 하나의 실제 시스템으로 연결했다.

최종적으로:

```text
실제 센서 데이터
→ 실제 서버
→ 실제 DB
→ thermal_monitoring 판정 결과 그대로 DB 저장
→ 자동 반복
→ 자동 서비스 실행
```

이 가능한 상태까지 만들었다.

즉 단순한 코드 예제가 아니라 **Jetson AGX Orin에서 실제 장비가 연결되어 동작하는 Hotspot_Guard 백엔드 데이터 파이프라인**을 완성한 것이다.
