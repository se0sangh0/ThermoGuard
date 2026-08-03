# ThermoGuard UI 시퀀스 다이어그램

## 1. UI 명세서 검토 및 교정 결과

첨부된 `UI명세서.xls`과 현재 코드를 대조한 결과다.

| 항목 | 기존 내용 | 교정 내용 |
|---|---|---|
| 설정 파일 | `config.json` | 현재 코드와 일치하므로 유지 |
| 화면 갱신 주기 | 정상 30초 / 경고 5초 | 카메라 촬영은 정상 30초·과열 5초, GUI 분석·DB 동기화는 30초로 구분 |
| 미확인 알림 | 최근 선택 기간 | 1시간·1일·3일·7일 중 선택 |
| 온도 추이 | 선택 기간 | DB의 1시간·1일·3일·7일 중 선택 + 실시간 측정값 병합 |
| 온도 그래프 표시 | 온도 이력 표시 | 원본은 최대 7일치를 유지하고 화면은 최고점을 보존해 최대 1,000개 표시점으로 축약 |
| Telegram 로그인 | 텔레그램 계정 접속 | 사용자 계정 로그인이 아니라 Bot Token·Chat ID 검증 |
| 환경설정 저장 | 설정값 저장 | `config.json` 저장, 설비·ROI·Threshold DB 동기화, 그래프·게이지 갱신, 최신 데이터 재분석 |
| 운영 로그 | 명세 누락 | DB가 아닌 `self.operating_logs` 런타임 메모리에 최근 1,000건 보관, 앱 재시작 시 초기화 |
| 카메라 연결 표시 | 별도 연결 라벨로 표현 | 현재는 헤더 시스템 상태·API 안정성 표시에 통합 |
| 온도 화면 형태 | 팝업으로 오해 가능 | 현재 메인 화면 하단에서 펼침·접힘되는 패널 |

## 2. 전체 모니터링 시퀀스

```mermaid
sequenceDiagram
    autonumber
    actor Operator as 운영자
    participant UI as ProductDashboard
    participant Camera as FLIR 카메라
    participant Capture as CaptureSession
    participant Analyzer as ROI 분석
    participant API as FastAPI
    participant DB as MySQL

    Operator->>UI: 앱 실행
    UI->>Camera: 연결 확인 HTTP GET
    Camera-->>UI: 연결 결과
    UI->>Capture: 자동 촬영 시작

    loop 정상 30초 / 과열 5초 촬영
        Capture->>Camera: Thermal 촬영
        Camera-->>Capture: Radiometric JPEG
        alt 정상 모드
            Capture->>Camera: Visual 촬영
            Camera-->>Capture: Visual JPEG
        else 과열 모드
            Capture->>Capture: Visual 촬영 생략 가능
        end
        Capture->>Capture: JPG·NPY 로컬 저장
    end

    loop GUI 분석 30초
        UI->>Analyzer: 최신 완성 촬영 쌍 분석
        Analyzer-->>UI: ROI 온도·상태·오버레이
        UI->>UI: 이미지·상태·게이지·그래프 갱신
        UI->>API: POST /api/measurements
        API->>DB: captures·analysis_runs·roi_measurements 저장
        opt 알람 생성 조건 충족
            API->>DB: alert_events 저장
        end
        API-->>UI: capture_id·measurement_id·alert_id
    end
```

## 3. 환경설정 저장 및 즉시 동기화

```mermaid
sequenceDiagram
    autonumber
    actor Operator as 운영자
    participant Dialog as SettingsDialog
    participant Config as config.json
    participant Asset as asset_api_client
    participant Threshold as threshold_api_client
    participant API as FastAPI
    participant DB as MySQL
    participant UI as ProductDashboard

    Operator->>Dialog: 카메라 주소·저장 폴더·온도 기준 입력
    Operator->>Dialog: 저장 클릭
    Dialog->>Dialog: 경로·숫자 입력 검증
    Dialog->>Asset: register_asset_hierarchy()
    Asset->>API: GET /api/cameras
    API->>DB: 기존 카메라·상위 ID 조회
    DB-->>API: 설비 계층
    API-->>Asset: cameras JSON

    alt 필요한 DB ID가 없음
        Asset->>API: POST /api/factories
        API->>DB: factories INSERT
        Asset->>API: POST /api/production-lines
        API->>DB: production_lines INSERT
        Asset->>API: POST /api/robots
        API->>DB: robots INSERT
        Asset->>API: POST /api/cameras
        API->>DB: cameras INSERT
    end

    Dialog->>Config: AppConfig JSON 저장
    Dialog->>Threshold: sync_threshold_profiles()
    Threshold->>API: GET /api/thresholds
    API->>DB: threshold_profiles SELECT
    alt 활성 Threshold 존재
        Threshold->>API: PATCH /api/thresholds/{threshold_id}
        API->>DB: threshold_profiles UPDATE
    else Threshold 없음
        Threshold->>API: POST /api/thresholds
        API->>DB: threshold_profiles INSERT
    end

    Dialog->>UI: apply_saved_settings_immediately()
    UI->>UI: 기준선·게이지 재표시
    UI->>UI: 최신 촬영 데이터 즉시 재분석
    UI->>UI: 자동 갱신 타이머 재설정
    UI->>UI: 카메라 연결 재확인
```

## 4. ROI 설정·캘리브레이션 시퀀스

```mermaid
sequenceDiagram
    autonumber
    actor Operator as 운영자
    participant Dialog as SettingsDialog
    participant Tool as OpenCV 설정 도구
    participant Local as config.json / Homography NPY
    participant Client as roi_api_client
    participant API as FastAPI
    participant DB as MySQL

    alt 캘리브레이션
        Operator->>Dialog: 캘리브레이션 실행
        Dialog->>Tool: Thermal·Visual 최신 이미지 전달
        Operator->>Tool: 두 영상의 대응점 선택
        Tool->>Tool: Homography 계산·오차 검사
        Tool->>Local: 변환행렬 NPY 저장
    else ROI 설정
        Operator->>Dialog: ROI 설정
        Dialog->>Tool: 가시광 이미지·Homography 전달
        Operator->>Tool: 감시 영역 선택·저장
        Tool->>Local: ROI 좌표 config.json 저장
        Tool->>Client: sync_rois()
        Client->>API: GET /api/rois
        API->>DB: roi_definitions SELECT
        alt 좌표 변경 또는 신규 ROI
            Client->>API: POST /api/rois
            API->>DB: roi_definitions INSERT
        else 동일한 ROI
            Client->>Client: 기존 roi_id 재사용
        end
    end
```

## 5. Critical 알림·Telegram·전달 로그

```mermaid
sequenceDiagram
    autonumber
    participant UI as ProductDashboard
    participant Dispatcher as TelegramDispatcher
    participant API as FastAPI
    participant DB as MySQL
    participant Telegram as Telegram Bot API

    UI->>Dispatcher: post_measurement(ROI 분석 결과)
    Dispatcher->>API: POST /api/measurements
    API->>DB: 측정 및 alert_events 저장
    API-->>Dispatcher: alert_id 반환

    alt Critical + 알림 활성 + alert_id 존재
        UI->>Dispatcher: maybe_dispatch()
        Dispatcher->>Telegram: HTTPS sendPhoto
        Telegram-->>Dispatcher: HTTP 성공/실패
        Dispatcher->>API: POST /api/notification-deliveries
        API->>DB: notification_deliveries INSERT
        API-->>Dispatcher: delivery_id
    else alert_id 없음
        Dispatcher->>Dispatcher: 전달 로그 DB 저장 생략·오류 기록
    end
```

## 6. 기간 선택 알림 팝업

```mermaid
sequenceDiagram
    autonumber
    actor Operator as 운영자
    participant UI as ProductDashboard
    participant API as FastAPI
    participant DB as MySQL

    Operator->>UI: 미확인 알림 버튼 클릭
    UI->>UI: 알림 팝업 생성
    Operator->>UI: 1시간·1일·3일·7일 선택
    UI->>API: GET /api/alerts?hours={1|24|72|168}&limit=5000
    API->>DB: 선택 기간 alert_events SELECT
    DB-->>API: 선택 기간 알림
    API-->>UI: alerts JSON
    UI->>UI: DB 결과+로컬 이벤트 병합·상태 필터

    opt 운영자가 알림 확인 처리
        Operator->>UI: 알림 선택 후 확인 처리
        UI->>API: PATCH /api/alerts/{alert_id}
        API->>DB: event_status=acknowledged, acknowledged_at=NOW
        DB-->>API: 수정 완료
        API-->>UI: updated JSON
        UI->>UI: 확인 완료로 표시
    end
```

## 7. 기간 선택 온도 추이

```mermaid
sequenceDiagram
    autonumber
    actor Operator as 운영자
    participant UI as ProductDashboard
    participant API as FastAPI
    participant DB as MySQL

    Operator->>UI: 온도 추이 버튼 클릭
    UI->>UI: 게이지·그래프 패널 펼침
    Operator->>UI: 1시간·1일·3일·7일 선택
    UI->>API: GET /api/temperature-trend?hours={1|24|72|168}&limit=150000
    API->>DB: roi_measurements 선택 기간 집계
    DB-->>API: 촬영별 전체 ROI 최대온도
    API-->>UI: points JSON
    UI->>UI: DB 과거 기록+실시간 메모리 병합
    UI->>UI: 촬영시각 중복 제거·7일 이전 제외
    UI->>UI: 최고점 보존 최대 1,000개 표시점 축약
    UI->>UI: 정상·경고·위험 게이지와 선 그래프 표시

    loop 30초마다
        UI->>API: 선택 기간 이력 재동기화
        API-->>UI: 최신 points
    end
```

## 8. 운영 로그

```mermaid
sequenceDiagram
    autonumber
    actor Operator as 운영자
    participant Feature as 카메라·분석·Backend·Telegram 기능
    participant Memory as self.operating_logs
    participant Popup as 운영 로그 팝업

    Feature->>Memory: _add_operating_log(구분, 결과, 상세)
    Memory->>Memory: 시간을 붙여 최신 위치에 추가
    Memory->>Memory: 1,000건 초과 시 가장 오래된 항목 제거
    Operator->>Popup: 운영 로그 버튼 클릭
    Popup->>Memory: 현재 메모리 목록 조회
    Memory-->>Popup: 최근 운영 로그
    Popup-->>Operator: 시각·구분·결과·상세 표시

    Note over Memory: DB 조회 없음<br/>앱 재시작 시 초기화
```

## 9. 동기·비동기 기준

| 기능 | 방식 | 주기 |
|---|---|---|
| 환경설정 입력 검증·`config.json` 저장 | 동기 | 사용자 저장 요청 시 |
| 설비 계층·ROI·Threshold API 동기화 | 동기 | 설정 저장 시 |
| 카메라 연결 확인 | 비동기 | 앱 시작·설정 저장·재시작 시 |
| 자동 카메라 촬영 | 비동기 | 정상 30초, 과열 5초 |
| GUI 분석·화면 갱신 | 비동기 | 30초 |
| 알림 이력 DB 동기화 | 비동기 | 팝업 열기·새로고침·30초 유지보수 |
| 온도 이력 DB 동기화 | 비동기 | 패널 열기·30초 |
| Telegram 전송 | 비동기 | Critical 알람 발생 시 |
| 운영 로그 표시 | 동기 | 팝업 열기 시 메모리에서 즉시 표시 |

## 10. 발견된 추가 개선 후보

1. 운영 로그는 재시작 시 사라지므로 장기 이력이 필요하면 DB 테이블·저장 API·조회 API가 필요하다.
2. 알림·온도 조회는 `hours=1|24|72|168`로 제한하여 임의의 과도한 범위 조회를 방지한다.
3. 설정 저장은 여러 DB API를 순차 호출하므로 중간 실패 시 로컬 설정과 DB 상태가 다를 수 있다. 완전한 원자적 적용이 필요하면 Backend 통합 설정 API가 필요하다.
4. 헤더의 카메라·Backend 상태는 일부 통합되어 있어, 어느 연결이 실패했는지 별도 표시하면 운영자가 원인을 더 빨리 판단할 수 있다.
