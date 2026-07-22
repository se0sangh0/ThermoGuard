
---
# 1. 프로젝트 목적

본 시스템은 산업용 다관절 로봇(3축~5축)의 이상 발열을 조기에 감지하여 예방 정비(Predictive Maintenance)를 지원하는 것을 목적으로 한다.

시스템의 주요 목표는 다음과 같다.

- 로봇의 이상 발열 발생 여부 탐지
- 발열 위치의 시각적 표시
- 고객사 내부 시스템과의 최소 연동
- 카메라 단독 설치만으로 서비스 제공 가능

---
# 2. 사용 장비

## 카메라 : FLIR A50 Bi-spectrum

	구성:
	- Thermal Camera
	- Visible Camera
	- 듀얼 센서 구조

	해상도
	| 구분     | 해상도      |
	| ------- | ----------- |
	| Thermal | 640 × 480   |
	| RGB     | 2592 × 1944 |

	카메라 치수

![카메라도면|693](./카메라_도면.png)

---
# 3. 시스템 아키텍처

```
FLIR A50
      ↓
REST Snapshot
      ↓
Temperature Matrix (.npy)
      ↓
ROI 설정
      ↓
온도 분석
      ↓
Threshold 판단
      ↓
Overlay
      ↓
Telegram 알림
```

## 실시간 감시 시퀀스 (monitor.py)

캡처, 무결성 검사, 분석, 알림을 하나의 연속 시퀀스로 자동화합니다.
다중 신규 쌍 적체 시 ThreadPoolExecutor로 병렬 처리합니다.

```
┌──────────────────────────────────────────────────────┐
│                  monitor.py (시퀀서)                    │
│                                                        │
│  백그라운드 스레드:                                      │
│    CaptureSession → 논스톱 이미지 수집 (JPG)              │
│                                                        │
│  메인 루프 (2초 주기):                                   │
│    1. 신규 이미지쌍 스캔                                 │
│    2. NPY 누락 시 JPEG에서 자동 추출 (병렬)               │
│    3. ROI 온도 통계 추출 (max/mean/95th + 클러스터)      │
│    4. 이중 경로 Threshold 판정 (95th + max)              │
│    5. 상태 머신 (Normal → Warning → Critical)            │
│    6. 과열 시 Overlay 생성 + Telegram 알림               │
│    7. 주기적: 무결성 검사(60s) + CSV 메타데이터(120s)     │
│       + 오래된 데이터 정리(3600s)                         │
│                                                        │
│  다중 쌍 적체 시 ThreadPoolExecutor 병렬 처리              │
│  모든 단계 예외 처리 → 로그만 남기고 시퀀스 계속           │
└──────────────────────────────────────────────────────┘
```

## 통합 모니터링 대시보드 (tools.py)

GUI 기반의 통합 대시보드로, 캡처부터 분석·알림까지 하나의 화면에서 운영할 수 있습니다.

```
┌──────────────────────────────────────────────────────────────────┐
│               Robot Thermal Monitoring Dashboard                 │
├──────────────────────────────────────────────────────────────────┤
│ ┌─ 환경 설정 ────────────────────────────────────────────────────┐
│ │ Camera IP: [192.168.0.51      ]  ● Connected  [Check Conn...]  │
│ │ Dataset :  [thermal_dataset    ]  [Browse...]                  │
│ │ Interval : [1.0] s   Mode: [both ▼]   [Start Monitoring]      │
│ │ ExifTool : [Auto-detect from PATH ]  [Browse...]               │
│ │ System Status: ● Ready  All required checks passed             │
│ └────────────────────────────────────────────────────────────────┘
│
│ ┌─ 감지 화면 ───────────────────────────────┬──────────────────────┐
│ │                                            │ ○ Thermal ● Visual  │
│ │          Overlay Image                     │ Status: Normal      │
│ │         (640x480 scaled)                   │ Max: 32.1°C         │
│ │                                            │ Mean: 28.5°C        │
│ │                                            │ 95th: 29.8°C        │
│ │                                            │ Hotspots: 2         │
│ │                                            │                      │
│ │                                            │ [Check Dataset]     │
│ │                                            │ [Generate Metadata] │
│ │                                            │ [Cleanup Dataset]   │
│ │                                            │ [Set ROI]           │
│ │                                            │ [Calibrate]         │
│ └────────────────────────────────────────────┴──────────────────────┘
│
│ ┌─ 로그 화면 ──────────────────────────────────────────────────────┐
│ │ [Detection Log]  [Activity Log]                                │
│ │ Detection Time │ Location  │ Temperature │ Alert   │ Notified │
│ │────────────────┼───────────┼─────────────┼─────────┼────────────│
│ │ 14:32:15       │ (320,240) │ 55.3°C      │ Warning │ Yes      │
│ │ 14:32:13       │ (310,235) │ 42.1°C      │ Normal  │ —        │
│ └──────────────────────────────────────────────────────────────────┘
└──────────────────────────────────────────────────────────────────┘
```

**대시보드 주요 기능:**
- **환경 설정**: 카메라 연결 상태(●/○) 비동기 확인, Flow 설정(ExifTool 경로/Mode), System Status(Not Ready/Ready/Monitoring/Error) 실시간 표시
- **감지 화면**: 최신 오버레이 이미지 표시 (핫스팟 마커 + 온도 정보), Thermal/Visual 전환
- **데이터 운영**: Check Dataset(무결성 검사·복구), Generate Metadata(CSV 생성), Cleanup Dataset(오래된 데이터 정리) 버튼 — 백그라운드 실행
- **도구 연동**: Set ROI(ROI 영역 설정), Calibrate(Thermal↔RGB 매핑) — 메인 스레드에서 OpenCV GUI 실행
- **로그 화면**: Detection Log(감지 시간/위치/온도/경고/알림) + Activity Log(운영·오류 메시지) 탭 분리

## 통합 설정 (config.json)

모든 설정을 단일 파일로 관리합니다. 구버전 설정 파일(`roi_config.json`, `experiment_config.json`)은 최초 실행 시 자동 이관(migration)되며 `.bak`으로 백업됩니다.

```
config.json
├── camera        IP, 캡처 주기
├── identity      카메라ID, 로봇ID
├── roi           좌표(x1,y1,x2,y2), baseline, warning/critical delta
├── monitoring    처리 주기, 쿨다운 타이머
├── hotspot       최소 클러스터 크기 (95th 경로 / max 경로)
├── paths         dataset_dir, overlay_dir, homography_path
├── display       표시 해상도
└── tools         exiftool 경로, 캡처 모드
```

## 1차 개발 범위(MVP)

```
Single Camera MVP
```
---
# 3. 감시 시퀀스 전체 흐름

`python monitor.py` 실행 시점부터 정상 복귀까지의 전체 흐름입니다.

## 3-1. 기동 (0~2초)

```
main() → MonitorSequencer.start()
  ├── load_roi_config() — config.json에서 ROI/임계값 로드
  ├── _prime_processed_cache() — 기존 파일은 재분석 제외
  ├── CaptureSession 생성 (interval=30s, mode=both, probe_callback 등록)
  └── CaptureSession.start() — 백그라운드 스레드에서 캡처 시작
```

이 시점부터 캡처 스레드와 분석 루프가 병렬로 동작합니다.

## 3-2. 평상시 캡처 사이클 (2-트랙)

```
[캡처 스레드]
t=0s   HTTP 병렬 요청 (thermal + visual 동시) → JPG 저장
       → 30초 대기 시작

       ┌─ 대기 중 프로브 루프 (3초 간격) ─────────────────┐
t=1s   │ probe: HTTP로 thermal JPEG 다운로드               │
       │   → exiftool stdin 파이프로 XMP 메타데이터 추출   │
       │   → Main:MaxValue 읽기 (디스크 I/O 없음)          │
       │   → max_temp=36°C, threshold=38°C → 통과          │
 ...   │ ...                                               │
       └──────────────────────────────────────────────────┘
t=30s  → 다음 풀캡처
```

## 3-3. 과열 감지 — 프로브 트리거

```
t=15s  probe: max_temp=52°C → threshold 38°C 초과!
         ├── probe_callback(52.0) → True 반환
         ├── capture.set_warning_mode(True) → interval 5초로 전환
         └── break → 대기 루프 즉시 탈출

t=15.1s  풀캡처 (thermal + visual 동시 요청 → JPG 저장)
         [로그] "Probe detected elevated temp — triggering immediate capture"
         [로그] "Capture interval changed: 30.0s → 1.0s (warning mode)"
```

## 3-4. 분석 파이프라인 (2초 주기)

```
[분석 루프]
t=17s  _scan_new_pairs() → 방금 저장된 쌍 발견
         ├── extract_roi_from_npy() → ROI max/mean/95th 추출
         ├── connectedComponentsWithStats → 핫스팟 클러스터 (3px↑)
         ├── evaluate_with_state() → 이중 경로 판정
         │     경로1: 95th=50°C ≥ 38°C + cluster≥3px → Warning
         │     경로2: max=52°C ≥ 48°C + cluster≥10px → Warning
         │     → Status: Warning
         ├── create_overlay() → Thermal에 ROI박스+핫스팟 마커+온도 정보
         ├── save_overlay() → overlay/ 저장
         └── send_alarm() → Telegram 이미지+캡션 전송
```

## 3-5. 과열 모드 — 5초 고속 캡처

```
t=16.1s  풀캡처 (thermal-only, 5초 주기) → 분석 → Warning 지속
t=21.1s  풀캡처 → 분석 → Warning 지속 (쿨다운 중 → 알림 없음)
t=26.1s  풀캡처 → 분석 → Critical 감지
           ├── prev=Warning, new=Critical → 알림 전송
           └── [Telegram] "🚨 Overheat Alarm — Status: Critical"
t=31.1s  풀캡처 → Critical (쿨다운 중)
```

## 3-6. 정상 복귀

```
t=45s   풀캡처 → 분석 → max_temp=35°C, 95th=34°C → Normal 판정
          ├── capture.set_warning_mode(False) → interval 30초로 복귀
          └── [로그] "Capture interval restored to normal (30.0s)"

t=46s   → 30초 대기 + 1초 프로브 루프 재개
```

## 3-7. 백그라운드 유지보수

분석 루프 내에서 주기적으로 실행됩니다.

```
60초마다   run_check() → NPY 누락 복구 (병렬), 고아 정리
120초마다  run_metadata() → metadata.csv 생성/갱신
3600초마다 run_cleanup_if_due() → 2일 지난 Normal 쌍 삭제 (Warning/Critical 보존)
```

## 3-8. 데이터 수집 방식

#### 2-트랙 캡처 구조

평상시와 과열 시 캡처 주기를 분리하여 디스크 사용량과 응답성을 모두 확보합니다.

```
평상시 (30초 주기)
  ├── 풀캡처: 30초마다 Thermal + Visual JPEG 저장
  └── 프로브: 대기 시간 동안 매 3초마다 경량 Thermal 체크
                (JPEG 바이트 → exiftool stdin 파이프 → XMP Main:MaxValue 추출)
                │
                ▼ threshold 초과 감지 시
과열 모드 (5초 주기)
  └── 풀캡처: 5초마다 Thermal JPEG만 저장 (visual 생략, 정밀 추적)
                │
                ▼ Normal 복귀 시
평상시 (30초 주기) ← 복귀
```

| 모드 | 캡처 주기 | 설정 키 | 동작 |
|------|-----------|---------|------|
| Normal | 30초 | `camera.capture_interval_sec` | 프로브가 백그라운드에서 3초마다 온도 체크 |
| Warning/Critical | 5초 | `camera.warning_interval_sec` | 고속 풀캡처로 정밀 추적 (thermal-only), Normal 복귀 시 자동 해제 |

프로브는 FLIR JPEG의 XMP 메타데이터(Main:MaxValue)를 exiftool stdin 파이프로 추출하므로, Raw Thermal 변환이나 디스크 I/O가 발생하지 않습니다.

#### 수집 데이터:
```
RGB 이미지
Thermal 이미지
Temperature Matrix (.npy)
```
##### *실시간 스트리밍 미사용 이유*
- 네트워크 품질 불확실
- 방화벽 제약
- 대역폭 제한
- 유지보수 복잡도 증가


#### Temperature Matrix 활용

```
#.npy 파일은 픽셀별 실제 온도 정보를 포함하여 재분석 필요 없음
temperature[y, x] = 36.4
```
---
# 5. ROI 분석

```
#1회 진행 필요 
#1. GUI 대시보드(tools.py)에서 "Set ROI" 버튼 클릭
# → roi_selector.py (OpenCV GUI) 실행
# → Thermal 이미지에서 마우스 드래그로 ROI 영역 지정
# → S 키로 config.json에 자동 저장

#2. 열화상 캘리브레이션
# → GUI 대시보드에서 "Calibrate" 버튼 클릭
# → Thermal ↔ RGB 대응점 선택 후 Homography 계산
# → thermal_to_rgb.npy 저장

#3. 운영 중 좌표
roi = thermal[
    ty1:ty2,
    tx1:tx2
]
```
```
초기 버전은 사용자가 설정하는 ROI 기반으로 구현하며, 
향후 현장 데이터 확보 후 자동 객체 검출 및 추적 기능을 적용한다.
```
---
# 6. 온도 분석
```
# 최대 값
max_temp = roi.max()

# 평균 값
mean_temp = roi.mean()

# 노이즈 감안 시
hot_temp = np.percentile(
    roi,
    95
)
```
---
# 7. 임계값 판단(Threshold)

**이중 경로 판정 (Dual-Path)**
| 경로 | 기준 | 조건 | 목적 |
|------|------|------|------|
| 1 — 95th percentile | `95th >= baseline + delta` + cluster ≥ 3px | 넓은 영역이 서서히 과열될 때 감지 | 주 경로 |
| 2 — max 온도 | `max >= baseline + critical_delta` + cluster ≥ 10px | ROI 대비 소수 픽셀만 국소 과열될 때 보완 | 보조 경로 |

**클러스터 분석 (다중 핫스팟)**

```
# cv2.connectedComponentsWithStats로 모든 과열 클러스터 검출
mask = roi > (baseline + warning_delta)
num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(mask)

# 3px 이상의 클러스터만 발열로 인정 (1~2px = 센서 노이즈)
# 각 클러스터마다 무게중심 좌표 + 최고 온도 기록
for label_id in range(1, num_labels):
    if stats[label_id, cv2.CC_STAT_AREA] >= 3:
        centroids.append((cx, cy, cluster_max_temp))
```

```
if hot_temp > baseline + 15: warning
if hot_temp > baseline + 25: critical
```

---
# 8. 상태 머신
```
Normal
  ↓
Warning
  ↓
Critical
```
---
# 9. 시각화(Overlay)
```
#유저 알림에는 가시광 이미지에 과열부위 디택팅 된 이미지
#대시 보드에는 열화상 이미지에 과열부위 디택팅 된 이미지
┌──────────────────┐
│      Robot       │
│        🔴       │
│      83.5℃      │
│                  │
│ Status : Warning │
└──────────────────┘
```

---
# 10. 알림 시스템

| 상태 값 | 메세지 전송 유무 | 전송 정보                             |
| ---- | --------- | --------------------------------- |
| 평시   | 없음        |                                   |
| 과열   | **없음** (캡처 주기만 5초로 전환)  |                                   |
| 경보   | 전송        | 로봇ID, 상태, 최고 온도, 발생 시간, 발생 범위 이미지 |

```
# 전송 규칙
# 상태 변화 시에만 메세지 전송하기 (Critical 진입 시에만)

Warning → Critical
Critical → Normal

# 쿨다운 걸어 연속 발송 필터링 하기
ALARM_INTERVAL = 10min

#텔레그램 봇 알림 구현 방법
def send_alarm(
    image_path,
    temp,
    status,
    robot_id="Robot-01"
):
    message = f"""
🚨 로봇 이상 발열 감지

🤖 Robot : {robot_id}
🌡 최고온도 : {temp:.1f}℃
⚠ 상태 : {status}
"""

    with open(image_path, "rb") as photo:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            data={
                "chat_id": CHAT_ID,
                "caption": message
            },
            files={
                "photo": photo
            }
        )
```
---
# 11. 이력 관리

모든 온도 데이터를 저장.

| 시간    | 온도  |
| ----- | --- |
| 13:00 | 42℃ |
| 13:10 | 43℃ |
| 13:20 | 44℃ |
| 14:00 | 57℃ |

	활용
	- 추세 분석
	- 예방 정비
	- 이상 징후 예측

---
# 12. 운영 로깅 시스템

모든 모듈에서 발생하는 이벤트를 파일과 콘솔에 이중 출력합니다.

**로그 포맷**
```
2026-07-20 14:32:15.123 [INFO ] [capture] Camera connection restored: 192.168.0.51
2026-07-20 14:32:20.456 [ERROR] [capture] [thermal] Timeout connecting to 192.168.0.51
2026-07-20 14:32:25.789 [WARN ] [pipeline.monitor] [20260720...] Unexpected error: Permission denied
  Traceback (most recent call last):
    ...
```

**파일 관리**
| 항목 | 설정 |
|------|------|
| 저장 위치 | `logs/app.log` |
| 롤링 주기 | 매일 자정 |
| 보존 기간 | 30일 |
| 콘솔 출력 | INFO 레벨 이상 |

**주요 감시 항목**
- 카메라 연결 끊김/복구 (`Connection refused`, `Connection restored`)
- 연속 실패 횟수 (5회 → WARNING, 30회 → ERROR)
- HTTP 오류 코드
- 파이프라인 처리 예외 (스택 트레이스 포함)
- Telegram 알림 전송 성공/실패
- 데이터 정리 결과 (제거된 파일, 확보된 디스크 용량)

---
# 13. 병렬 처리 최적화

AGX Orin(12코어) 환경에서 ThreadPoolExecutor로 CPU 자원을 최대한 활용합니다.

| 대상 | 방식 | 워커 수 | 기대 효과 |
|------|------|---------|-----------|
| 배치 파이프라인 (`pipeline.py`) | 전체 쌍 병렬 분석 | `cpu_count` (12) | 최대 10배 속도 향상 |
| 실시간 감시 (`monitor.py`) | 적체된 신규 쌍 동시 처리 | `cpu_count // 2` (6) | 적체 해소 시간 단축 |
| NPY 복구 (`checking.py`) | 누락 NPY 동시 추출 | `cpu_count // 2` (6) | 대량 복구 속도 향상 |
| 이미지 캡처 (`capture.py`) | Thermal + Visual 동시 요청 | 2 | 정렬 오차 제거 |

---
## 향후 발전 방안

```
Rule-based
      ↓
Robot Detection
      ↓
Joint Segmentation
      ↓
Anomaly Detection
      ↓
Predictive Maintenance
```
### Phase 2
- Robot Detection AI
- 이상탐지 모델
- 다중 카메라 지원
- RTSP 지원
- 다중 로봇 지원
- 카메라 제조사 확장
### Phase 3
- 부품 단위 진단
- 예지보전
- 클라우드 연동
- AI 기반 이상 패턴 분석