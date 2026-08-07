# ThermoGuard 데이터베이스 테이블 역할

## 설비 구성

| 테이블 | 역할 |
|---|---|
| `factories` | 공장 최상위 정보와 시간대 |
| `production_lines` | 공장에 속한 생산 라인 |
| `robots` | 생산 라인에 속한 감시 대상 로봇 |
| `cameras` | 로봇에 연결된 열화상 카메라와 현재 연결 상태 |
| `roi_definitions` | 카메라 영상에서 감시할 ROI 좌표와 버전 |
| `threshold_profiles` | 카메라/ROI별 정상·경고·위험 판정 임계값 |

## 캡처와 분석

| 테이블 | 역할 |
|---|---|
| `captures` | 한 번의 촬영 요청, 실제 촬영 시각 및 thermal/visual 쌍 상태 |
| `capture_files` | 촬영에 속한 thermal JPG, visual JPG, thermal NPY, overlay 파일 메타데이터 |
| `image_quality_results` | 한 캡처의 영상 유효성, 해상도, 영상 간 차이와 실패 사유 |
| `analysis_runs` | 캡처에 대해 실행한 분석 알고리즘과 성공/실패 상태 |
| `roi_measurements` | ROI별 최소·최대·평균·95백분위 온도와 판정 상태 |
| `hotspots` | 측정값에서 검출된 개별 hotspot 중심 좌표, 온도와 면적 |

## 알림과 운영

| 테이블 | 역할 |
|---|---|
| `alert_events` | 상태 머신과 쿨다운을 통과한 실제 경보 이벤트와 overlay 파일 연결 |
| `notification_deliveries` | Telegram 등 외부 알림의 전송 결과와 재시도 정보 |
| `api_request_logs` | 카메라별 백엔드 측정 API 처리 결과 |
| `operation_logs` | 대시보드에서 발생한 사용자·시스템 운영 행위 |
| `calibrations` | Thermal/RGB 대응점, Homography 행렬과 재투영 오차 |

## 측정 저장 트랜잭션

`POST /api/measurements`는 다음 데이터를 하나의 DB 트랜잭션으로 저장한다.

1. `captures`
2. `capture_files`
3. `analysis_runs`
4. `roi_measurements`
5. `hotspots`
6. `image_quality_results`
7. 알람 발생 시 `alert_events`와 overlay `file_id`
8. `api_request_logs`

어느 단계에서든 SQL 오류가 발생하면 전체 트랜잭션이 롤백되어 일부 테이블만 남지 않는다.

## 파일 보관 원칙

DB에는 이미지 바이너리를 넣지 않고 절대 경로, 크기, 해상도, SHA-256 체크섬을 저장한다.
Warning/Critical 오버레이는 `config.json`의 `paths.overlay_dir`에 영구 저장한다.
따라서 데이터 정리 작업은 디스크 파일과 `capture_files` 행을 같은 기준으로 처리해야 한다.
