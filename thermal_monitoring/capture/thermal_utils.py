"""thermal_utils.py - FLIR A50 공통 유틸 (exiftool, Planck 변환, 온도 추출)"""

import os
import json
import subprocess
import re
import numpy as np
from PIL import Image
import io
import threading
import time

from ..logger import get_logger

_log = get_logger("capture.thermal_utils")

# exiftool 경로 — config.json에서 설정하거나, 없으면 DJI SDK 경로 → PATH fallback
def _get_default_exiftool() -> str:
    from ..config import load_config
    cfg = load_config()
    if cfg.tools.exiftool_path:
        return cfg.tools.exiftool_path
    bundled = os.path.abspath(os.path.join(
        os.environ.get("CONDA_PREFIX", ""),
        "Lib", "site-packages", "dji_executables",
        "dji_thermal_sdk_v1.7", "exiftool-12.35.exe"
    ))
    if os.path.exists(bundled):
        return bundled
    return "exiftool"

EXIFTOOL = _get_default_exiftool()

ABSOLUTE_ZERO = 273.15


def extract_float(val):
    if isinstance(val, (int, float)):
        return float(val)
    m = re.findall(r"[-+]?\d*\.\d+|\d+", str(val))
    return float(m[0]) if m else 0.0


def raw2temp(raw, E=1.0, OD=1.0, RTemp=20.0, ATemp=20.0, IRWTemp=20.0,
             IRT=1.0, RH=50.0, PR1=21106.77, PB=1501.0, PF=1.0,
             PO=-7340.0, PR2=0.012545258,
             ATA1=0.006569, ATA2=0.01262, ATB1=-0.002276, ATB2=-0.00667, ATX=1.9):
    emiss_wind = 1.0 - IRT
    refl_wind = 0.0

    h2o = (RH / 100.0) * np.exp(
        1.5587 + 0.06939 * ATemp - 0.00027816 * ATemp**2 + 0.00000068455 * ATemp**3
    )
    tau1 = ATX * np.exp(-np.sqrt(OD / 2) * (ATA1 + ATB1 * np.sqrt(h2o))) + \
           (1 - ATX) * np.exp(-np.sqrt(OD / 2) * (ATA2 + ATB2 * np.sqrt(h2o)))
    tau2 = tau1

    raw_refl1 = PR1 / (PR2 * (np.exp(PB / (RTemp + ABSOLUTE_ZERO)) - PF)) - PO
    raw_refl1_attn = (1 - E) / E * raw_refl1

    raw_atm1 = PR1 / (PR2 * (np.exp(PB / (ATemp + ABSOLUTE_ZERO)) - PF)) - PO
    raw_atm1_attn = (1 - tau1) / E / tau1 * raw_atm1

    raw_wind = PR1 / (PR2 * (np.exp(PB / (IRWTemp + ABSOLUTE_ZERO)) - PF)) - PO
    raw_wind_attn = emiss_wind / E / tau1 / IRT * raw_wind

    raw_refl2 = PR1 / (PR2 * (np.exp(PB / (RTemp + ABSOLUTE_ZERO)) - PF)) - PO
    raw_refl2_attn = refl_wind / E / tau1 / IRT * raw_refl2

    raw_atm2 = PR1 / (PR2 * (np.exp(PB / (ATemp + ABSOLUTE_ZERO)) - PF)) - PO
    raw_atm2_attn = (1 - tau2) / E / tau1 / IRT / tau2 * raw_atm2

    raw_obj = (raw / E / tau1 / IRT / tau2
               - raw_atm1_attn - raw_atm2_attn
               - raw_wind_attn - raw_refl1_attn - raw_refl2_attn)

    val = PR1 / (PR2 * (raw_obj + PO)) + PF
    return PB / np.log(val) - ABSOLUTE_ZERO


# ---- probe cache ----
_planck_cache: dict | None = None
_planck_cache_ts: float = 0.0
_planck_cache_lock = threading.Lock()
_PLANCK_CACHE_TTL = 300.0  # 5분


def _planck_params_from_meta(meta: dict) -> dict:
    """meta JSON에서 Planck 변환 파라미터 dict 추출."""
    return {
        "E": extract_float(meta.get("Emissivity", 1.0)),
        "OD": extract_float(meta.get("SubjectDistance", 1.0)),
        "RTemp": extract_float(meta.get("ReflectedApparentTemperature", 20.0)),
        "ATemp": extract_float(meta.get("AtmosphericTemperature", 20.0)),
        "IRWTemp": extract_float(meta.get("IRWindowTemperature", 20.0)),
        "IRT": extract_float(meta.get("IRWindowTransmission", 1.0)),
        "RH": extract_float(meta.get("RelativeHumidity", 50.0)),
        "PR1": extract_float(meta.get("PlanckR1", 21106.77)),
        "PB": extract_float(meta.get("PlanckB", 1501.0)),
        "PF": extract_float(meta.get("PlanckF", 1.0)),
        "PO": extract_float(meta.get("PlanckO", -7340.0)),
        "PR2": extract_float(meta.get("PlanckR2", 0.012545258)),
        "ATA1": extract_float(meta.get("AtmosphericTransAlpha1", 0.006569)),
        "ATA2": extract_float(meta.get("AtmosphericTransAlpha2", 0.01262)),
        "ATB1": extract_float(meta.get("AtmosphericTransBeta1", -0.002276)),
        "ATB2": extract_float(meta.get("AtmosphericTransBeta2", -0.00667)),
        "ATX": extract_float(meta.get("AtmosphericTransX", 1.9)),
    }


def _raw_bytes_to_max_temp(raw_bytes: bytes, planck_params: dict) -> float:
    """RawThermalImage 바이너리 → Planck 변환 → 최고 온도(°C).

    raw2temp는 raw에 대해 단조 증가이므로, 전체 배열을 변환할 필요 없이
    최대 raw 값 하나만 변환하여 최고 온도를 얻는다.
    """
    raw_stream = io.BytesIO(raw_bytes)
    thermal_img = Image.open(raw_stream)
    raw_np = np.array(thermal_img, dtype=np.uint16)
    max_raw = np.max(raw_np)
    max_temp = raw2temp(np.float32(max_raw), **planck_params)
    if np.isnan(max_temp):
        thermal = raw2temp(raw_np.astype(np.float32), **planck_params)
        return float(np.nanmax(thermal))
    return float(max_temp)


def extract_from_jpeg(jpg_path, exiftool=None):
    """Extract temperature matrix + metadata from FLIR A50 radiometric JPEG.

    Returns: (thermal: np.float32 ndarray, meta: dict)
        meta keys: timestamp, distance_cm, ambient_temp
    """
    if exiftool is None:
        exiftool = _get_default_exiftool()

    meta_args = [
        exiftool, "-j",
        "-Emissivity", "-SubjectDistance",
        "-AtmosphericTemperature", "-ReflectedApparentTemperature",
        "-IRWindowTemperature", "-IRWindowTransmission",
        "-RelativeHumidity",
        "-PlanckR1", "-PlanckB", "-PlanckF", "-PlanckO", "-PlanckR2",
        "-AtmosphericTransAlpha1", "-AtmosphericTransAlpha2",
        "-AtmosphericTransBeta1", "-AtmosphericTransBeta2",
        "-AtmosphericTransX",
        "-DateTimeOriginal", "-FocusDistance",
        jpg_path
    ]
    try:
        meta_json = subprocess.check_output(meta_args, timeout=30).decode()
    except subprocess.TimeoutExpired:
        _log.error("ExifTool metadata timeout for %s", jpg_path)
        raise
    except Exception as e:
        _log.error("ExifTool metadata failed for %s: %s", jpg_path, e)
        raise

    meta = json.loads(meta_json)[0]

    try:
        raw_bytes = subprocess.check_output(
            [exiftool, "-RawThermalImage", "-b", jpg_path], timeout=30
        )
    except subprocess.TimeoutExpired:
        _log.error("ExifTool RawThermalImage timeout for %s", jpg_path)
        raise
    except Exception as e:
        _log.error("ExifTool RawThermalImage failed for %s: %s", jpg_path, e)
        raise
    raw_stream = io.BytesIO(raw_bytes)
    thermal_img = Image.open(raw_stream)
    raw_np = np.array(thermal_img, dtype=np.uint16).astype(np.float32)

    params = _planck_params_from_meta(meta)

    thermal = raw2temp(raw_np, **params)

    capture_meta = {
        "timestamp": meta.get("DateTimeOriginal", ""),
        "distance_cm": int(round(extract_float(meta.get("SubjectDistance", 1.0)) * 100)),
        "ambient_temp": extract_float(meta.get("AtmosphericTemperature", 20.0)),
    }

    return thermal.astype(np.float32), capture_meta


def _fetch_jpeg(url: str, timeout: float) -> bytes | None:
    """카메라에서 Thermal JPEG 바이트를 가져옵니다. 실패 시 None."""
    import requests
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code != 200:
            _log.warning("probe: HTTP %d", r.status_code)
            return None
        content_type = r.headers.get("Content-Type", "")
        if "image" not in content_type.lower() and content_type != "octet-stream":
            _log.warning("probe: unexpected Content-Type: %s", content_type)
            return None
        return r.content
    except Exception as e:
        _log.warning("probe: HTTP GET failed: %s", e)
        return None


def _extract_planck_params_from_bytes(jpeg_bytes: bytes) -> dict | None:
    """JPEG 바이트 → exiftool로 Planck 파라미터 추출 (메타데이터만). 캐시에 저장하지 않음."""
    exiftool = _get_default_exiftool()
    meta_proc = subprocess.run(
        [exiftool, "-j",
         "-Emissivity", "-SubjectDistance",
         "-AtmosphericTemperature", "-ReflectedApparentTemperature",
         "-IRWindowTemperature", "-IRWindowTransmission",
         "-RelativeHumidity",
         "-PlanckR1", "-PlanckB", "-PlanckF", "-PlanckO", "-PlanckR2",
         "-AtmosphericTransAlpha1", "-AtmosphericTransAlpha2",
         "-AtmosphericTransBeta1", "-AtmosphericTransBeta2", "-AtmosphericTransX",
         "-"],
        input=jpeg_bytes,
        capture_output=True,
        timeout=15,
    )
    if meta_proc.returncode != 0:
        _log.warning("probe: metadata extraction failed (rc=%d)", meta_proc.returncode)
        return None
    try:
        meta = json.loads(meta_proc.stdout.decode())[0]
    except (json.JSONDecodeError, IndexError) as e:
        _log.warning("probe: metadata parse failed: %s", e)
        return None
    return _planck_params_from_meta(meta)


def _extract_raw_bytes(jpeg_bytes: bytes) -> bytes | None:
    """JPEG 바이트 → exiftool로 RawThermalImage 바이너리 추출."""
    exiftool = _get_default_exiftool()
    raw_proc = subprocess.run(
        [exiftool, "-RawThermalImage", "-b", "-"],
        input=jpeg_bytes,
        capture_output=True,
        timeout=15,
    )
    if raw_proc.returncode != 0 or not raw_proc.stdout:
        _log.warning("probe: raw thermal extraction failed (rc=%d, size=%d)",
                     raw_proc.returncode, len(raw_proc.stdout))
        return None
    return raw_proc.stdout


def _full_probe(url: str, timeout: float) -> float | None:
    """전체 프로브: JPEG 다운로드 → Planck 파라미터 추출 → 캐시 갱신 → Raw 추출 → Planck 변환 → 최고 온도 반환."""
    global _planck_cache, _planck_cache_ts

    jpeg_bytes = _fetch_jpeg(url, timeout)
    if jpeg_bytes is None:
        return None

    params = _extract_planck_params_from_bytes(jpeg_bytes)
    if params is None:
        return None

    raw_bytes = _extract_raw_bytes(jpeg_bytes)
    if raw_bytes is None:
        return None

    with _planck_cache_lock:
        _planck_cache = params
        _planck_cache_ts = time.monotonic()
        _log.debug("planck cache updated (TTL=%.0fs, keys=%d)", _PLANCK_CACHE_TTL, len(params))

    return _raw_bytes_to_max_temp(raw_bytes, params)


def _fast_probe(url: str, timeout: float) -> float | None:
    """고속 프로브: JPEG 다운로드 → RawThermalImage만 추출 → 캐싱된 Planck 파라미터로 변환 → 최고 온도 반환."""
    with _planck_cache_lock:
        params = _planck_cache

    if params is None:
        _log.debug("fast probe: no cache, falling back to full probe")
        return _full_probe(url, timeout)

    jpeg_bytes = _fetch_jpeg(url, timeout)
    if jpeg_bytes is None:
        return None

    raw_bytes = _extract_raw_bytes(jpeg_bytes)
    if raw_bytes is None:
        return None

    return _raw_bytes_to_max_temp(raw_bytes, params)


def probe_thermal_from_url(url: str, timeout: float = 10.0) -> float | None:
    """
    전체 프로브 또는 고속 프로브로 프레임 최고 온도(°C)를 반환합니다.

    Planck 캐시가 유효(TTL 이내)하면 고속 프로브 (exiftool 1회),
    그렇지 않으면 전체 프로브 (exiftool 2회 + 캐시 갱신).
    디스크에 JPEG를 저장하지 않습니다 (subprocess stdin 파이프 사용).
    실패 시 None 반환.

    Args:
        url: 카메라 Thermal JPEG API URL
        timeout: HTTP 타임아웃

    Returns:
        최고 온도(°C) 또는 None
    """
    with _planck_cache_lock:
        cached = _planck_cache
        age = time.monotonic() - _planck_cache_ts

    if cached is not None and age < _PLANCK_CACHE_TTL:
        return _fast_probe(url, timeout)
    else:
        if cached is not None:
            _log.debug("planck cache expired (age=%.0fs), refreshing", age)
        return _full_probe(url, timeout)
