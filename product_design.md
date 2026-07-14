
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
## 1차 개발 범위(MVP)

```
Single Camera MVP
```
---
# 3. 데이터 수집 방식

```
10초 주기 Snapshot 요청
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

---
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

---
## Step 4

### 온도 통계 계산

```
max_temp
mean_temp
95_percentile_temp
```

---
## 권장 지표

```
temperature = np.percentile(
    roi,
    95
)
```

최대 온도보다 노이즈에 강함.

---
# 5. ROI 분석

	1. 최초 설치

```
#1. 최초 설치
robot_roi = [
    x1, y1,
    x2, y2
]
#2.열화상 캘리브레이션 후
thermal_roi = [
    tx1, ty1,
    tx2, ty2
]
#3. 운영 중 좌표
roi = thermal[
    ty1:ty2,
    tx1:tx2
]
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

```
if hot_temp >
baseline + 15:
    warning
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