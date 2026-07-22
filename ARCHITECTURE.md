# Thermal Monitoring System — Architecture

## 1. System Overview

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
│  Daemon Thread: _run()                                           │
│  ┌─────────────────────────────────────────────────────────────┐ │
│  │  while _running:                                            │ │
│  │    ┌──────────────────────────────────────┐                 │ │
│  │    │  1. CAPTURE                          │                 │ │
│  │    │     requests.get(thermal_jpg_url)     │ → JPG to disk  │ │
│  │    │     requests.get(visual_jpg_url) ────┘  (normal only) │ │
│  │    │     ThreadPoolExecutor(max_workers=2)                  │ │
│  │    │     → parallel fetch, write to thermal_dataset/        │ │
│  │    └──────────────────────────────────────┘                 │ │
│  │                          │                                  │ │
│  │    ┌──────────────────────────────────────┐                 │ │
│  │    │  2. PROBE LOOP  (normal mode only)   │                 │ │
│  │    │     while waiting:                   │                 │ │
│  │    │       sleep(3.0s)                    │                 │ │
│  │    │       probe_thermal_from_url() ──────┤                 │ │
│  │    │       if over_threshold:             │                 │ │
│  │    │         set_warning_mode(True)        │                 │ │
│  │    │         break → immediate recapture   │                 │ │
│  │    └──────────────────────────────────────┘                 │ │
│  └─────────────────────────────────────────────────────────────┘ │
│                                                                  │
│  Public API:                                                     │
│    start() / stop() / request_stop()                             │
│    running (property) / last_saved_pair (property)               │
│    set_warning_mode(bool) → changes self.interval (thread-locked) │
│    capture_both_once()   → alarm fresh capture (thermal+visual)  │
│                                                                  │
│  Callbacks:                                                      │
│    log_callback(msg)  → GUI or print log                         │
│    probe_callback(temp) → returns True if over threshold         │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. Run Modes (entry points)

### 2A. CLI Mode — MonitorSequencer

```
python -m thermal_monitoring.pipeline.monitor

Process (single Python process):
├── Main thread:
│   └── MonitorSequencer.start()
│       ├── load_roi_config()
│       ├── _prime_processed_cache()
│       ├── CaptureSession.start() → daemon thread
│       └── _monitoring_loop()       ← main thread blocks here
│           ┌─ _scan_new_pairs()
│           │  → scans thermal_dataset/ for new JPG pairs
│           │  → extracts missing NPY via extract_from_jpeg()
│           │  → ThreadPoolExecutor: parallel _process_one()
│           ├─ integrity check  (every INTEGRITY_INTERVAL seconds)
│           ├─ metadata update  (every METADATA_INTERVAL seconds)
│           └─ sleep(process_interval) in 1s chunks
│
├── CaptureSession daemon thread:
│   └── _run() — see Section 4
│
└── ThreadPoolExecutor workers (ephemeral):
    └── _process_one() per pair
        → roi analysis → threshold → overlay → alarm
```

### 2B. GUI Mode — ProductDashboard

```
python -m thermal_monitoring.tools.product_dashboard

Process (single Python process):
├── Main thread = tkinter root.mainloop()
│   ├── ProductDashboard.__init__()
│   │   └── builds tkinter UI widgets
│   ├── _check_connection_async()
│   │   └── spawns daemon thread → requests.get(camera)
│   ├── start_monitoring()
│   │   └── CaptureSession.start() → daemon thread
│   ├── root.after() callbacks:
│   │   └── refresh_now() → _schedule_analysis()
│   │       → _analysis_executor.submit(_run_analysis_worker)
│   │       → root.after(0, _apply_analysis_result)  ← back on main thread
│   └── stop_monitoring()
│
│   refresh_now()는 디스크에 저장된 최신 파일을 재분석할 뿐, 새로 카메라를 찍지
│   않는다. 카메라 캡처는 CaptureSession daemon thread가 별도로 담당하며,
│   capture_and_refresh() 버튼으로 수동 즉시 촬영이 가능하다.
│
├── CaptureSession daemon thread:
│   └── _run() — see Section 4
│
├── ThreadPoolExecutor (_analysis_executor, max_workers=1):
│   └── _run_analysis_worker()
│       → _latest_pair() → _process_pair_to_dict()
│       → root.after(0, _apply_analysis_result)  ← Tk-safe handoff
│
└── Ad-hoc daemon threads:
    ├── _check_connection_async() worker
    └── settings save connection check
```

---

## 3. Camera Communication (HTTP)

```
FLIR A50 Camera  (192.168.0.51)
│
├── GET /api/image/current?imgformat=JPEG        → Thermal JPEG (~500KB-1MB)
├── GET /api/image/current?imgformat=JPEG_visual → Visual JPEG (~1-2MB)
│
└── Caller summary:

    ┌──────────────────────┬─────────────────┬──────────────────────────┐
    │ Caller               │ Frequency       │ Purpose                  │
    ├──────────────────────┼─────────────────┼──────────────────────────┤
    │ CaptureSession._run  │ 30s (normal)    │ Full capture (thermal    │
    │ (capture)            │  5s (warning)   │  + visual if both mode)  │
    ├──────────────────────┼─────────────────┼──────────────────────────┤
    │ capture_both_once()  │ On alarm        │ Fresh thermal+visual     │
    │                      │                 │ for overlay generation   │
    ├──────────────────────┼─────────────────┼──────────────────────────┤
    │ probe_thermal_from_  │ 3s (normal only)│ Lightweight temp check   │
    │ url() (probe)        │                 │ uses Planck cache        │
    ├──────────────────────┼─────────────────┼──────────────────────────┤
    │ Dashboard connection │ Once at startup │ Connection health check  │
    │ check                │                 │                          │
    └──────────────────────┴─────────────────┴──────────────────────────┘

    Note: Warning mode → visual capture SKIPPED (thermal only).
          Alarm → capture_both_once() fetches fresh thermal+visual pair.
```

---

## 4. CaptureSession Thread Detail

```
CaptureSession._run()  [daemon thread]

    Thread-local state:
      - self._running: bool           (set by start/stop)
      - self.interval: float           (protected by self._interval_lock)
      - self._consecutive_failures: int
      - self._was_connected: bool

    Shared mutable state (thread-safe):
      - self.interval          : threading.Lock (read/write by probe_callback)
      - _planck_cache (module) : threading.Lock (read/write by _full_probe)

    ┌─ CAPTURE ──────────────────────────────────────────────────┐
    │  if mode=="both" AND normal:                                │
    │    ThreadPoolExecutor(2) → parallel thermal + visual fetch  │
    │  else:                                                      │
    │    sequential thermal only                                  │
    │                                                             │
    │  Each successful fetch → write JPG to thermal_dataset/      │
    │  Filename: YYYYMMDDHHMMSS_FFFFFF.jpg                        │
    │           YYYYMMDDHHMMSS_FFFFFF_visual.jpg  (if visual)     │
    └─────────────────────────────────────────────────────────────┘

    ┌─ PROBE LOOP ───────────────────────────────────────────────┐
    │  Only active when: normal mode + connected + probe_callback │
    │                                                             │
    │  Every ~3 seconds:                                          │
    │    temp = probe_thermal_from_url(thermal_url)               │
    │    if probe_callback(temp):                                 │
    │      → set_warning_mode(True) → interval=5s → break         │
    │      → immediately recapture at top of while loop           │
    │                                                             │
    │  On probe failure: backoff ~6s (2 cycles of 3s sleep)       │
    └─────────────────────────────────────────────────────────────┘
```

---

## 5. Planck Cache System

```
thermal_utils.py — module-level cache

    Shared state (protected by threading.Lock):
    ┌──────────────────────────────────────────────┐
    │  _planck_cache: dict | None                  │
    │  _planck_cache_ts: float                     │
    │  _PLANCK_CACHE_TTL = 300.0  (5 minutes)      │
    │  _planck_cache_lock = threading.Lock()       │
    └──────────────────────────────────────────────┘

    probe_thermal_from_url(url, timeout):
    │
    ├─ cache valid? → _fast_probe(url, timeout)
    │   └─ _fetch_jpeg()  → HTTP GET thermal JPEG
    │   └─ _extract_raw_bytes()  → exiftool -RawThermalImage -b  (1 call)
    │   └─ _raw_bytes_to_max_temp(cached_params)  → Planck transform
    │
    └─ cache expired/missing? → _full_probe(url, timeout)
        └─ _fetch_jpeg()  → HTTP GET thermal JPEG
        └─ _extract_planck_params_from_bytes()  → exiftool JSON  (1 call)
        └─ _extract_raw_bytes()  → exiftool -RawThermalImage -b  (1 call)
        └─ cache update with lock
        └─ _raw_bytes_to_max_temp(fresh_params)  → Planck transform

    Note: Planck params (R1, B, F, O, R2, Alpha, Beta, X) are camera
          calibration constants that rarely change. Environmental params
          (ATemp, RH) change slowly and are refreshed every 5 min.
```

---

## 6. Analysis Pipeline (shared by both modes)

```
Thermal JPG (on disk) + Visual JPG (on disk, optional)
          │
          ▼
    extract_from_jpeg(jpg_path)     ← exiftool subprocess (metadata + raw)
          │  → thermal.astype(np.float32) — Planck 변환, 메모리 절반
          ▼
    NPY file (on disk, float32)
          │  → np.load().astype(np.float32)
          ▼
    roi.py: extract_roi_from_npy() / extract_all_rois_from_npy()
    ├── _scale_roi_to_npy()  — 640×480 → npy resolution
    ├── valid = roi[~isnan]  — NaN removal
    ├── 95th percentile, max, mean
    ├── cv2.connectedComponentsWithStats()  — hotspot clustering
    └── return RoiResult
          │
          ▼
    threshold.py: evaluate_with_state()
    ├── evaluate_threshold(): dual-path
    │   ├─ Path 1: 95th >= baseline + delta AND cluster >= 3px
    │   └─ Path 2: max >= baseline + critical_delta AND cluster >= 10px
    ├── should_alarm(): Critical + cooldown check
    └── return (Status, do_alarm)
          │
          ▼
    overlay.py: create_overlay() + save_overlay()
    ├── _load_homography()  — thermal_to_rgb.npy
    ├── _prepare_canvas()   — visual or thermal-only fallback
    └── save to overlay/ directory
          │
          ▼
    notifier.py: send_alarm()
    ├── Telegram sendPhoto (image + caption)  ← HTTP POST
    └── fallback: sendMessage (text only)     ← HTTP POST
```

---

## 7. Data Lifecycle (filesystem communication)

```
thermal_dataset/
├── 20260721120000_123456.jpg          ← thermal capture
├── 20260721120000_123456_visual.jpg   ← visual capture (normal mode only)
├── 20260721120000_123456_thermal.npy  ← extracted from JPEG (lazy/cached)
├── overlay/
│   └── 20260721120000_123456_overlay.jpg  ← alarm overlay
├── snapshots/ (optional, for debugging)
└── metadata.csv                          ← periodic batch update

    Component           │ Reads                │ Writes
    ────────────────────┼──────────────────────┼──────────────────────
    CaptureSession._run │ —                    │ thermal JPG, visual JPG
    capture_both_once() │ —                    │ thermal JPG, visual JPG
    MonitorSequencer    │ JPG + NPY            │ NPY (extract), overlay
    ProductDashboard    │ JPG + NPY            │ NPY (extract), overlay
    data/checking.py    │ JPG + NPY            │ NPY (recovery), JPG (cleanup)
    data/metadata.py    │ JPG + NPY            │ metadata.csv
    data/cleanup.py     │ all files            │ deletes old files
```

---

## 8. Thread Safety Summary

```
┌──────────────────────────┬──────────────────────┬────────────────────┐
│ Shared Resource          │ Protection           │ Access Pattern     │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ CaptureSession.interval  │ _interval_lock       │ R: probe loop      │
│                          │ (threading.Lock)     │ W: probe_callback  │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ _planck_cache (module)   │ _planck_cache_lock   │ R: _fast_probe     │
│                          │ (threading.Lock)     │ W: _full_probe     │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ MonitorSequencer.        │ _lock (threading.Lock)│ R: scan, logs      │
│ processed_bases,         │                      │ W: _mark_processed  │
│ _alarm_count,            │                      │   _process_one     │
│ _status_counts           │                      │                    │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ MonitorState.status      │ _lock (threading.Lock)│ R: threshold check │
│                          │ (monitor.py)         │ W: status change   │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ ProductDashboard GUI     │ root.after() callbacks│ W: tkinter widgets  │
│ widgets                  │ (tkinter thread-safe) │   (main thread only)│
├──────────────────────────┼──────────────────────┼────────────────────┤
│ ProductDashboard.        │ _analysis_generation  │ W: after(0) callback│
│ _analysis_generation     │ (integer, atomic)     │ R: worker thread   │
├──────────────────────────┼──────────────────────┼────────────────────┤
│ Logger (global)          │ logging module        │ All threads        │
│                          │ (built-in thread-safe)│                    │
└──────────────────────────┴──────────────────────┴────────────────────┘
```

---

## 9. Subprocess Calls

```
exiftool  (external process, found via config.json or PATH)

    Called by:
    ├── extract_from_jpeg()         — metadata JSON + RawThermalImage (2 calls)
    ├── _extract_planck_params_from_bytes()  — metadata JSON (1 call)
    └── _extract_raw_bytes()        — RawThermalImage binary  (1 call)

    Communication: subprocess.run() with stdin pipe (capture_output=True)
    No temp files written — JPEG bytes piped via stdin.
    Timeout: 15s per call.

    NOTE: exiftool process is spawned by the calling thread (capture daemon
          or probe callback). No separate exiftool process pool exists.
```

---

## 10. Calibration & Numeric Precision

### 10A. Homography Calibration Validation

`tools/calibration.py`의 `run_calibration()`은 thermal/visual 이미지 파일명 stem을
비교해 동일 캡처 사이클에서 온 쌍인지 검증한다. 불일치 시 경고를 출력하고
Enter 확인 없이 계속하거나 Ctrl+C로 중단할 수 있다.

```
run_calibration(thermal_path, rgb_path)
    ├── t_stem = os.path.basename(thermal) sans extension
    ├── v_stem = os.path.basename(rgb) sans extension, strip "_visual" suffix
    └── if t_stem != v_stem:
            print WARNING → input() 대기
```

product_dashboard.py의 `open_calibration()`은 호출 전에
`visual = dataset / f"{thermal.stem}_visual.jpg"` → `visual.exists()` 체크로
이미 검증을 수행한다.

### 10B. Float32 Precision

전체 온도 행렬 파이프라인이 `float32`로 통일되어 있다.

| 위치 | 이전 | 현재 |
|------|------|------|
| `extract_from_jpeg()` | `float64` 중간 변환 | `float32`로 일관 (반환 시 이미 .astype(np.float32)) |
| `_raw_bytes_to_max_temp()` | `np.float64(max_raw)` | `np.float32(max_raw)` |
| `roi.py: extract_roi_from_npy()` | `.astype(np.float64)` | `.astype(np.float32)` |
| NPY 디스크 저장 | `np.save(..., float32)` | `float32` 그대로 |

Planck 변환의 float64 ↔ float32 오차는 0°C (raw ≥ 10000 범위, 240×320 2D 프레임 기준)로
정밀도 손실이 전혀 없으며 메모리 사용량은 절반으로 감소한다.

---

## 11. External Network

```
Telegram Bot API  (api.telegram.org:443)

    Called by: notifier.py

    ENDPOINTS:
    ├── POST /bot{TOKEN}/sendPhoto     — image + caption (alarm)
    └── POST /bot{TOKEN}/sendMessage   — text only (fallback)

    Called from:
    ├── MonitorSequencer._process_one()     — main thread + ThreadPoolExecutor
    └── pipeline.py (batch)                 — ThreadPoolExecutor workers

    Config: .env file → BOT_TOKEN, CHAT_ID

    Note: send_alarm() contains requests.post() on the calling thread.
          This is in ThreadPoolExecutor workers, NOT the capture daemon thread.
```

---

## 12. Logging Architecture

```
logger.py — Central logger with daily rotating files

    Format: 2026-07-20 14:32:15.123 [INFO ] [module.name] message

    Log directory: logs/  (configurable via config.json → monitoring.log_dir)

    Handlers:
    ├── TimedRotatingFileHandler  — logs/YYYY-MM-DD.log, rotate at midnight
    └── StreamHandler → stderr    — WARNING+ only (avoids console spam)

    Thread-safety: Python's logging module is inherently thread-safe.
    Multiple threads/modules call get_logger("name") → shared logger instance.

    Logged from:
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

## 13. Startup Sequence

```
1. Python process starts
2. config.json loaded (load_config)
3. .env loaded (notifier.py BOT_TOKEN, CHAT_ID)
4. Logger initialized
5. Entry point chosen:

   CLI mode (monitor.py):
   5a. MonitorSequencer.__init__()
   5b. .start()
       ├── load_roi_config()
       ├── _prime_processed_cache()  — mark existing pairs as processed
       ├── CaptureSession.start()    — daemon thread begins capture
       └── _monitoring_loop()        — main thread blocks, processes new pairs

   GUI mode (product_dashboard.py):
   5a. ProductDashboard.__init__()  — builds tkinter UI
   5b. root.mainloop()
   5c. _check_connection_async()  — one-shot health check
   5d. start_monitoring()         — user action
       ├── CaptureSession.start() — daemon thread
       └── _schedule_analysis()   — root.after() periodic analysis
```

---

## 14. Shutdown / Signal Handling

```
CLI mode:
    KeyboardInterrupt (Ctrl+C) in _monitoring_loop()
    → self.stop()
      → self._running = False
      → self.capture.stop()
        → self._running = False
        → self._thread.join(timeout=interval+5)
      → summary log printed

GUI mode:
    User clicks window close / "촬영 정지" button
    → stop_monitoring()
      → self.monitoring = False
      → capture.request_stop()  — non-blocking flag set
      → capture = None
    → root.destroy()  — tkinter cleanup
```

---

## 15. Critical Path — Alarm Flow

```
PROBE DETECTS OVERHEAT  (3s interval, normal mode)
    │
    ▼
probe_thermal_from_url() → temp >= baseline + warning_delta
    │
    ▼
probe_callback(True)  [CaptureSession daemon thread]
    │
    ├─ set_warning_mode(True)  → interval = 5s
    └─ return True  → break probe loop
    │
    ▼
IMMEDIATE RECAPTURE  [top of _run() loop]
    │  thermal only (visual skipped in warning mode)
    │  JPG written to disk
    │
    ▼
_monitoring_loop() / _schedule_analysis()  [main thread / executor]
    │  _scan_new_pairs() finds new JPG
    │  extract NPY if missing
    │
    ▼
extract_roi_from_npy()  [roi.py]
    │
    ▼
evaluate_with_state()  [threshold.py]
    │  returns (Status.CRITICAL, do_alarm=True)
    │
    ▼
capture_both_once()  [fresh thermal+visual, on alarm]
    │
    ▼
create_overlay() + save_overlay()  [overlay.py]
    │
    ▼
send_alarm()  [notifier.py]
    │  Telegram sendPhoto → factory workers notified
    │
    ▼
alarm_cooldown starts (600s default)
    future Critical detections suppressed until cooldown expires
```

