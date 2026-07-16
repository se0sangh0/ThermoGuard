
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

```
┌──────────────────────────────────────────────────────┐
│                  monitor.py (시퀀서)                    │
│                                                        │
│  백그라운드 스레드:                                      │
│    CaptureSession → 논스톱 이미지 수집 (JPG)              │
│                                                        │
│  메인 루프 (2초 주기):                                   │
│    1. 신규 이미지쌍 스캔                                 │
│    2. NPY 누락 시 JPEG에서 자동 추출                      │
│    3. ROI 온도 통계 추출 (max/mean/95th + 클러스터)      │
│    4. 이중 경로 Threshold 판정 (95th + max)              │
│    5. 상태 머신 (Normal → Warning → Critical)            │
│    6. 과열 시 Overlay 생성 + Telegram 알림               │
│    7. 주기적: 무결성 검사(60s) + CSV 메타데이터(120s)     │
│                                                        │
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
│ │ Camera IP: [192.168.0.51          ]  ● Connected / ○ Disconn.  │
│ │ Dataset :  [thermal_dataset        ]  [Browse...]              │
│ │ Interval : [1.0] s                    [Start Monitoring]       │
│ └────────────────────────────────────────────────────────────────┘
│
│ ┌─ 감지 화면 ───────────────────────────────┬──────────────────────┐
│ │                                            │ ○ Thermal ● Visual  │
│ │          Overlay Image                     │ Status: Normal      │
│ │         (640x480 scaled)                   │ Max: 32.1°C         │
│ │                                            │ 95th: 29.8°C        │
│ │                                            │ Hotspots: 2         │
│ │                                            │                      │
│ │                                            │ [Set ROI]           │
│ │                                            │ [Calibrate]         │
│ └────────────────────────────────────────────┴──────────────────────┘
│
│ ┌─ 로그 화면 ──────────────────────────────────────────────────────┐
│ │ Detection Time │ Location    │ Temperature │ Alert   │ Notified │
│ │────────────────┼─────────────┼─────────────┼─────────┼──────────│
│ │ 14:32:15       │ (320, 240)  │ 55.3°C      │ Warning │ Yes      │
│ │ 14:32:13       │ (310, 235)  │ 42.1°C      │ Normal  │ —        │
│ └──────────────────────────────────────────────────────────────────┘
└──────────────────────────────────────────────────────────────────┘
```

**대시보드 주요 기능:**
- **환경 설정**: 카메라 연결 상태(●/○) 실시간 확인, 데이터셋 디렉토리 변경, 저장 주기 설정
- **감지 화면**: 최신 오버레이 이미지 표시 (핫스팟 마커 + 온도 정보), Thermal/Visual 전환, ROI 설정·캘리브레이션 연동
- **로그 화면**: 감지 시간, 위치, 온도, 경고 단계(Critical=빨강, Warning=주황), 알림 전송 여부를 테이블로 표시

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
# 3. 데이터 수집 방식

```
1초 주기 Snapshot 요청
```
#### 수집 데이터:

```
RGB 이미지
Thermal 이미지
Temperature Matrix (.npy)
```
##### ~~*실시간 스트리밍 미사용 이유*
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
# ~~5. AI 분석 절차
## Step 1
### RGB 영상에서 로봇 검출
```
Robot Detection
```
출력:
```
robot_bbox
```
## Step 2
### Thermal 영역 매핑
	RGB ROI를 Thermal 좌표계로 변환.
## Step 3
### Temperature ROI 추출
```
roi = thermal[
    y1:y2,
    x1:x2
]
```
## Step 4

### 온도 통계 계산

```
max_temp
mean_temp
95_percentile_temp
```
### 권장 지표

```
temperature = np.percentile(
    roi,
    95
    # 최대 온도가 노이즈로 인한 피크치 일 수 있기에 95 기준으로
)
```

---
# 5. ROI 분석

	1. 최초 설치

```
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
│        🔴        │
│      83.5℃       │
│                  │
│ Status : Warning │
└──────────────────┘
```

---
# 10. 알림 시스템

| 상태 값 | 메세지 전송 유무 | 전송 정보                             |
| ---- | --------- | --------------------------------- |
| 평시   | 없음        |                                   |
| 과열   | 전송        | 로봇ID, 상태, 최고 온도, 발생 시간, 발생 범위 이미지 |
| 경보   | 전송        | 로봇ID, 상태, 최고 온도, 발생 시간, 발생 범위 이미지 |

```
# 전송 규칙
# 상태 변화 시에만 메세지 전송하기

Normal → Warning
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