# Thermal Monitoring System — Architecture

## 1. 시스템 개요

```
┌──────────────────────────────────────────────────────────────────┐
│                        config.json                               │
│  camera IP / intervals / ROI / paths / thresholds / hotspot      │
└────────────────────────────────┬─────────────────────────────────┘
                                 │ load_config()
                                 ▼
┌──────────────────────┐  ┌──────────────────────────┐
│   MonitorSequencer   │  │    ProductDashboard       │
│   (monitor.py, CLI)  │  │  (product_dashboard.py,   │
│                      │  │       tkinter GUI)        │
│  Main thread loop    │  │  Main thread = tk.mainloop│
│                      │  │                           │
│  • _monitoring_loop  │  │  • root.after() callback  │
│    → scan → process  │  │    → _schedule_analysis   │
│    → sleep           │  │  • _thread_pool executor  │
└──────────┬───────────┘  └────────────┬──────────────┘
           │                           │
           │  owns CaptureSession       │  owns CaptureSession
           │                           │
           ▼                           ▼
┌──────────────────────────────────────────────────────────────────┐
│                      CaptureSession                              │
│                      (capture.py)                                │
│                                                                  │
│  데몬 스레드: _run()                                              │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  while _running:                                            │ │
│  │    ┌──────────────────────────────────────┐                 │ │
│  │    │  1. 캡처                               │                 │ │
│  │    │     requests.get(thermal_jpg_url)     │ → JPG 저장      │ │
│  │    │     requests.get(visual_jpg_url) ────┘  (정상 모드만)  │ │
│  │    │     ThreadPoolExecutor(max_workers=2)                  │ │
│  │    │     → 병렬 요청, thermal_dataset/에 저장                │ │
│  │    └──────────────────────────────────────┘                 │ │
│  │                          │                                  │ │
│  │    ┌──────────────────────────────────────┐                 │ │
│  │    │  2. 프로브 루프  (정상 모드만)         │                 │ │
│  │    │     대기 중:                          │                 │ │
│  │    │       sleep(3.0s)                    │                 │ │
│  │    │       probe_thermal_from_url() ──────┤                 │ │
│  │    │       if 임계값 초과:                  │                 │ │
│  │    │         set_warning_mode(True)        │                 │ │
│  │    │         break → 즉시 재캡처            │                 │ │
│  │    └──────────────────────────────────────┘                 │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  공개 API:                                                       │
│    start() / stop() / request_stop()                             │
│    running (property) / last_saved_pair (property)               │
│    set_warning_mode(bool) → self.interval 변경 (스레드 안전)      │
│    capture_both_once()   → 알람용 신규 캡처 (thermal+visual)     │
│                                                                  │
│  콜백:                                                           │
│    log_callback(msg)  → GUI 또는 print 로그                      │
│    probe_callback(temp) → 임계값 초과 시 True 반환               │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. 실행 모드 (진입점)

### 2A. CLI 모드 — MonitorSequencer

```
python -m thermal_monitoring.pipeline.monitor

프로세스 (단일 Python 프로세스):
├── 메인 스레드:
│   └── MonitorSequencer.start()
│       ├── load_roi_config()
│       ├── _prime_processed_cache()
│       ├── CaptureSession.start() → 데몬 스레드
│       └── _monitoring_loop()       ← 메인 스레드가 여기서 블로킹
│           ┌─ _scan_new_pairs()
│           │  → thermal_dataset/에서 새 JPG 쌍 스캔
│           │  → extract_from_jpeg()로 누락 NPY 추출
│           │  → ThreadPoolExecutor: _process_one() 병렬 처리
│           ├─ 무결성 검사 (INTEGRITY_INTERVAL 초마다)
│           ├─ 메타데이터 갱신 (METADATA_INTERVAL 초마다)
│           └─ sleep(process_interval) → 1초 단위 분할
│
├── CaptureSession 데몬 스레드:
│   └── _run() — 4절 참조
│
└── ThreadPoolExecutor 워커 (임시):
    └── _process_one() per pair
        → ROI 분석 → Threshold → 오버레이 → 알림
```

### 2B. GUI 모드 — ProductDashboard

```
python -m thermal_monitoring.tools.product_dashboard

프로세스 (단일 Python 프로세스):
├── 메인 스레드 = tkinter root.mainloop()
│   ├── ProductDashboard.__init__()
│   │   └── tkinter UI 위젯 구성
│   ├── _check_connection_async()
│   │   └── 데몬 스레드 생성 → requests.get(camera)
│   ├── start_monitoring()
│   │   └── CaptureSession.start() → 데몬 스레드
│   ├── root.after() 콜백:
│   │   └── refresh_now() → _schedule_analysis()
│   │       → _analysis_executor.submit(_run_analysis_worker)
│   │       → root.after(0, _apply_analysis_result)  ← 메인 스레드로 복귀
│   └── stop_monitoring()
│
│   refresh_now()는 디스크에 저장된 최신 파일을 재분석할 뿐, 새로 카메라를 찍지
│   않는다. 카메라 캡처는 CaptureSession 데몬 스레드가 별도로 담당하며,
│   capture_and_refresh() 버튼으로 수동 즉시 촬영이 가능하다.
│
│   Thermal(640×480)과 Visual(2592×1944) 모두 원본으로 디스크에 저장된다.
│   GUI 표시 시에는 pil.thumbnail((650, 340))으로 축소해서 보여준다.
│
├── CaptureSession 데몬 스레드:
│   └── _run() — 4절 참조
│
├── ThreadPoolExecutor (_analysis_executor, max_workers=1):
│   └── _run_analysis_worker()
│       → _latest_pair() → _process_pair_to_dict()
│       → root.after(0, _apply_analysis_result)  ← Tk 안전 핸드오프
│
└── 임시 데몬 스레드:
    ├── _check_connection_async() 워커
    └── 설정 저장 연결 확인
```

---

## 3. 카메라 통신 (HTTP)

```
FLIR A50 카메라  (192.168.0.51)
│
├── GET /api/image/current?imgformat=JPEG        → Thermal JPEG (~500KB-1MB)
├── GET /api/image/current?imgformat=JPEG_visual → Visual JPEG (~1-2MB)
│
└── 호출자 요약:

    ┌──────────────────────┬─────────────────┬──────────────────────────┐
    │ 호출자                │ 빈도             │ 용도                      │
    ├──────────────────────┼─────────────────┼──────────────────────────┤
    │ CaptureSession._run  │ 정상 30초        │ 풀캡처 (both 모드이면     │
    │ (capture.py)         │ 과열 5초         │  thermal + visual)       │
    ├──────────────────────┼─────────────────┼──────────────────────────┤
    │ capture_both_once()  │ 알람 발생 시      │ 오버레이 생성을 위한       │
    │                      │                 │ 새 thermal+visual 쌍      │
    ├──────────────────────┼─────────────────┼──────────────────────────┤
    │ probe_thermal_from_  │ 정상 시 3초      │ 경량 온도 체크             │
    │ url() (프로브)        │                 │ Planck 캐시 사용           │
    ├──────────────────────┼─────────────────┼──────────────────────────┤
    │ 대시보드 연결 확인     │ 시작 시 1회      │ 카메라 연결 상태 확인       │
    └──────────────────────┴─────────────────┴──────────────────────────┘

    참고: 과열 모드 → visual 캡처 건너뜀 (thermal only).
          알람 → capture_both_once()가 thermal+visual 새 쌍을 가져옴.
```


---

## 4. CaptureSession 스레드 상세

```
CaptureSession._run()  [데몬 스레드]

    스레드-로컬 상태:
      - self._running: bool           (start/stop으로 설정)
      - self.interval: float           (_interval_lock으로 보호)
      - self._consecutive_failures: int
      - self._was_connected: bool

    스레드 간 공유 상태 (스레드 안전):
      - self.interval          : threading.Lock (probe_callback에서 읽기/쓰기)
      - _planck_cache (모듈)   : threading.Lock (_full_probe에서 읽기/쓰기)

    ┌─ 캡처 ────────────────────────────────────────────────────┐
    │  mode=="both" AND 정상 모드:                               │
    │    ThreadPoolExecutor(2) → thermal + visual 병렬 요청      │
    │  아니면:                                                   │
    │    thermal만 순차 요청                                      │
    │                                                             │
    │  성공 시 → thermal_dataset/에 JPG 저장                      │
    │  파일명: YYYYMMDDHHMMSS_FFFFFF.jpg                          │
    │          YYYYMMDDHHMMSS_FFFFFF_visual.jpg  (visual 있는 경우)│
    │                                                             │
    │  _fetch_image()는 일시적 HTTP 오류(502/503/504/429) 발생 시  │
    │  0.5초/1.0초 백오프로 재시도 (최대 2회, 상한 2초).           │
    │  이로 인해 한쪽만 재시도에 빠지면 thermal-visual 시간차가     │
    │  최대 3초까지 발생할 수 있다.                                │
    └─────────────────────────────────────────────────────────────┘

    ┌─ 프로브 루프 ─────────────────────────────────────────────┐
    │  활성 조건: 정상 모드 + 연결됨 + probe_callback 등록됨      │
    │                                                             │
    │  약 3초마다:                                                │
    │    temp = probe_thermal_from_url(thermal_url)               │
    │    if probe_callback(temp):                                 │
    │      → set_warning_mode(True) → interval=5s → break         │
    │      → 루프 시작점에서 즉시 재캡처                            │
    │                                                             │
    │  프로브 실패 시: ~6초 백오프 (3초 sleep 2사이클)              │
    └─────────────────────────────────────────────────────────────┘
```

---

## 5. Planck 캐시 시스템

```
thermal_utils.py — 모듈 레벨 캐시

    공유 상태 (threading.Lock으로 보호):
    ┌──────────────────────────────────────────────┐
    │  _planck_cache: dict | None                  │
    │  _planck_cache_ts: float                     │
    │  _PLANCK_CACHE_TTL = 300.0  (5분)            │
    │  _planck_cache_lock = threading.Lock()       │
    └──────────────────────────────────────────────┘

    probe_thermal_from_url(url, timeout):
    │
    ├─ 캐시 유효? → _fast_probe(url, timeout)
    │   └─ _fetch_jpeg()  → HTTP GET thermal JPEG
    │   └─ _extract_raw_bytes()  → exiftool -RawThermalImage -b  (1회 호출)
    │   └─ _raw_bytes_to_max_temp(cached_params)  → Planck 변환
    │
    └─ 캐시 만료/없음? → _full_probe(url, timeout)
        └─ _fetch_jpeg()  → HTTP GET thermal JPEG
        └─ _extract_planck_params_from_bytes()  → exiftool JSON  (1회 호출)
        └─ _extract_raw_bytes()  → exiftool -RawThermalImage -b  (1회 호출)
        └─ Lock으로 캐시 갱신
        └─ _raw_bytes_to_max_temp(fresh_params)  → Planck 변환

    참고: Planck 파라미터(R1, B, F, O, R2, Alpha, Beta, X)는 카메라 교정 상수로
          거의 변하지 않는다. 환경 파라미터(ATemp, RH)는 서서히 변하며 5분마다 갱신된다.
```

---

## 6. 분석 파이프라인 (양쪽 모드 공통)

```
Thermal JPG (디스크) + Visual JPG (디스크, 선택)
          │
          ▼
    extract_from_jpeg(jpg_path)     ← exiftool 서브프로세스 (메타데이터 + raw)
          │  → thermal.astype(np.float32) — Planck 변환, 메모리 절반
          ▼
    NPY 파일 (디스크, float32)
          │  → np.load().astype(np.float32)
          ▼
    roi.py: extract_roi_from_npy() / extract_all_rois_from_npy()
    ├── _scale_roi_to_npy()  — 640×480 → npy 해상도
    ├── valid = roi[~isnan]  — NaN 제거
    ├── 95th percentile, max, mean
    ├── cv2.connectedComponentsWithStats()  — 핫스팟 클러스터링
    └── return RoiResult
          │
          ▼
    threshold.py: evaluate_with_state()
    ├── evaluate_threshold(): 이중 경로 판정
    │   ├─ 경로 1: 95th >= baseline + delta AND cluster >= 3px
    │   └─ 경로 2: max >= baseline + critical_delta AND cluster >= 10px
    ├── should_alarm(): Critical + 쿨다운 체크
    └── return (Status, do_alarm)
          │
          ▼
    overlay.py: create_overlay() + save_overlay()
    ├── _load_homography()  — thermal_to_rgb.npy
    ├── _prepare_canvas()   — visual 또는 thermal-only fallback
    └── overlay/ 디렉토리에 저장
          │
          ▼
    notifier.py: send_alarm()
    ├── Telegram sendPhoto (이미지 + 캡션)  ← HTTP POST
    └── 폴백: sendMessage (텍스트만)        ← HTTP POST
```

---

## 7. 데이터 생명주기 (파일시스템 통신)

```
thermal_dataset/
├── 20260721120000_123456.jpg          ← thermal 캡처
├── 20260721120000_123456_visual.jpg   ← visual 캡처 (정상 모드만)
├── 20260721120000_123456_thermal.npy  ← JPEG에서 추출 (지연/캐시)
├── overlay/
│   └── 20260721120000_123456_overlay.jpg  ← 알람 오버레이
├── snapshots/ (선택, 디버깅용)
└── metadata.csv                          ← 주기적 배치 갱신

    컴포넌트               │ 읽기                │ 쓰기
    ────────────────────┼──────────────────────┼──────────────────────
    CaptureSession._run │ —                    │ thermal JPG, visual JPG
    capture_both_once() │ —                    │ thermal JPG, visual JPG
    MonitorSequencer    │ JPG + NPY            │ NPY (추출), overlay
    ProductDashboard    │ JPG + NPY            │ NPY (추출), overlay
    data/checking.py    │ JPG + NPY            │ NPY (복구), JPG (정리)
    data/metadata.py    │ JPG + NPY            │ metadata.csv
    data/cleanup.py     │ 모든 파일             │ 오래된 파일 삭제
```

---

## 8. 스레드 안전성 요약

```
┌──────────────────────────┬──────────────────────┬────────────────────┐
│ 공유 자원                 │ 보호 방식              │ 접근 패턴           │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ CaptureSession.interval  │ _interval_lock       │ R: 프로브 루프      │
│                          │ (threading.Lock)     │ W: probe_callback  │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ CaptureSession._last_pair│ _last_pair_lock      │ R: _apply_analysis │
│                          │ (threading.Lock)     │ W: _run() 캡처     │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ _planck_cache (모듈)     │ _planck_cache_lock   │ R: _fast_probe     │
│                          │ (threading.Lock)     │ W: _full_probe     │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ MonitorSequencer.        │ _lock (threading.Lock)│ R: 스캔, 로그      │
│ processed_bases,         │                      │ W: _mark_processed  │
│ _alarm_count,            │                      │   _process_one     │
│ _status_counts           │                      │                    │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ MonitorState.status      │ _lock (threading.Lock)│ R: threshold 체크  │
│                          │ (monitor.py)         │ W: 상태 변경        │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ ProductDashboard GUI     │ root.after() 콜백     │ W: tkinter 위젯     │
│ 위젯                      │ (tkinter 스레드 안전) │   (메인 스레드만)    │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ ProductDashboard.        │ _analysis_generation  │ W: after(0) 콜백   │
│ _analysis_generation     │ (정수, 원자적)         │ R: 워커 스레드      │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ Logger (전역)            │ logging 모듈           │ 모든 스레드         │
│                          │ (내장 스레드 안전)      │                    │
└──────────────────────────┴──────────────────────┴────────────────────┘
```

---

## 9. 서브프로세스 호출

```
exiftool  (외부 프로세스, config.json 또는 PATH에서 찾음)

    호출처:
    ├── extract_from_jpeg()         — 메타데이터 JSON + RawThermalImage (2회 호출)
    ├── _extract_planck_params_from_bytes()  — 메타데이터 JSON (1회 호출)
    └── _extract_raw_bytes()        — RawThermalImage 바이너리 (1회 호출)

    통신: subprocess.run() with stdin pipe (capture_output=True)
    임시 파일 없음 — JPEG 바이트는 stdin으로 전달.
    타임아웃: 호출당 15초.

    참고: exiftool 프로세스는 호출 스레드(캡처 데몬 또는 프로브 콜백)에서 생성된다.
          별도의 exiftool 프로세스 풀은 존재하지 않는다.
```

---

## 10. 캘리브레이션 & 수치 정밀도

### 10A. Homography 캘리브레이션 검증

`tools/calibration.py`의 `run_calibration()`은 thermal/visual 이미지 파일명 stem을
비교해 동일 캡처 사이클에서 온 쌍인지 검증한다. 불일치 시 경고를 출력하고
Enter 확인 없이 계속하거나 Ctrl+C로 중단할 수 있다.

```
run_calibration(thermal_path, rgb_path)
    ├── t_stem = os.path.basename(thermal)에서 확장자 제거
    ├── v_stem = os.path.basename(rgb)에서 확장자 제거, "_visual" 접미사 제거
    └── if t_stem != v_stem:
            print 경고 → input() 대기
```

product_dashboard.py의 `open_calibration()`은 호출 전에
`visual = dataset / f"{thermal.stem}_visual.jpg"` → `visual.exists()` 체크로
이미 검증을 수행한다.

---

## 11. 외부 네트워크

```
Telegram Bot API  (api.telegram.org:443)

    호출처: notifier.py

    엔드포인트:
    ├── POST /bot{TOKEN}/sendPhoto     — 이미지 + 캡션 (알람)
    └── POST /bot{TOKEN}/sendMessage   — 텍스트만 (폴백)

    호출 스레드:
    ├── MonitorSequencer._process_one()     — 메인 스레드 + ThreadPoolExecutor
    └── pipeline.py (배치)                   — ThreadPoolExecutor 워커

    설정: .env 파일 → BOT_TOKEN, CHAT_ID

    참고: send_alarm()은 호출 스레드에서 requests.post()를 실행한다.
          이는 ThreadPoolExecutor 워커 스레드이며, 캡처 데몬 스레드가 아니다.
```

---

## 12. 로깅 아키텍처

```
logger.py — 일자별 롤링 파일을 지원하는 중앙 로거

    포맷: 2026-07-20 14:32:15.123 [INFO ] [module.name] message

    로그 디렉토리: logs/  (config.json → monitoring.log_dir로 설정 가능)

    핸들러:
    ├── TimedRotatingFileHandler  — logs/YYYY-MM-DD.log, 자정 롤링
    └── StreamHandler → stderr    — WARNING+만 출력 (콘솔 스팸 방지)

    스레드 안전성: Python의 logging 모듈은 본질적으로 스레드 안전하다.
    여러 스레드/모듈이 get_logger("name") 호출 → 공유 로거 인스턴스.

    로깅 대상:
    ├── capture.py              → "capture"
    ├── thermal_utils.py        → "capture.thermal_utils"
    ├── monitor.py              → "pipeline.monitor"
    ├── threshold.py            → "analysis.threshold"
    ├── overlay.py              → "analysis.overlay"
    ├── notifier.py             → "analysis.notifier"
    ├── roi.py                  → "analysis.roi"
    └── data/*.py               → "data.*"
```

---

## 13. 기동 시퀀스

```
1. Python 프로세스 시작
2. config.json 로드 (load_config)
3. .env 로드 (notifier.py BOT_TOKEN, CHAT_ID)
4. Logger 초기화
5. 진입점 선택:

   CLI 모드 (monitor.py):
   5a. MonitorSequencer.__init__()
   5b. .start()
       ├── load_roi_config()
       ├── _prime_processed_cache()  — 기존 쌍을 처리済으로 표시
       ├── CaptureSession.start()    — 데몬 스레드에서 캡처 시작
       └── _monitoring_loop()        — 메인 스레드 블로킹, 신규 쌍 처리

   GUI 모드 (product_dashboard.py):
   5a. ProductDashboard.__init__()  — tkinter UI 구성
   5b. root.mainloop()
   5c. _check_connection_async()  — 일회성 상태 확인
   5d. start_monitoring()         — 사용자 동작
       ├── CaptureSession.start() — 데몬 스레드
       └── _schedule_analysis()   — root.after() 주기적 분석
```

---

## 14. 셧다운 / 시그널 처리

```
CLI 모드:
    KeyboardInterrupt (Ctrl+C) in _monitoring_loop()
    → self.stop()
      → self._running = False
      → self.capture.stop()
        → self._running = False
        → self._thread.join(timeout=interval+5)
      → 요약 로그 출력

GUI 모드:
    사용자가 창 닫기 / "촬영 정지" 버튼 클릭
    → stop_monitoring()
      → self.monitoring = False
      → capture.request_stop()  — 논블로킹 플래그 설정
      → capture = None
    → root.destroy()  — tkinter 정리
```

---

## 15. 크리티컬 패스 — 알람 흐름

```
프로브가 과열 감지  (3초 간격, 정상 모드)
    ▼
probe_thermal_from_url() → temp >= baseline + warning_delta
    ▼
probe_callback(True)  [CaptureSession 데몬 스레드]
    ├─ set_warning_mode(True)  → interval = 5s
    ├─ return True  → 프로브 루프 탈출
    ▼
즉시 재캡처  [_run() 루프 시작점]
    │  thermal only (과열 모드에서는 visual 생략)
    │  JPG를 디스크에 저장
    ▼
_monitoring_loop() / _schedule_analysis()  [메인 스레드 / executor]
    │  _scan_new_pairs()가 새 JPG 발견
    │  NPY 누락 시 추출
    ▼
extract_roi_from_npy()  [roi.py]
    ▼
evaluate_with_state()  [threshold.py]
    │  returns (Status.CRITICAL, do_alarm=True)
    ▼
capture_both_once()  [알람 발생 시 thermal+visual 새 촬영]
    ▼
create_overlay() + save_overlay()  [overlay.py]
    ▼
send_alarm()  [notifier.py]
    │  Telegram sendPhoto → 작업자에게 알림
    ▼
알람 쿨다운 시작 (기본 600초)
    쿨다운 만료 전까지 추가 Critical 감지 억제
```

