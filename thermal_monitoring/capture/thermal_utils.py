"""thermal_utils.py - FLIR A50 공통 유틸 (exiftool, Planck 변환, 온도 추출)"""

import os
import json
import subprocess
import re
import numpy as np
from PIL import Image
import io

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


def extract_from_jpeg(jpg_path, exiftool=None):
    """Extract temperature matrix + metadata from FLIR A50 radiometric JPEG.

    Returns: (thermal: np.float32 ndarray, meta: dict)
        meta keys: timestamp, distance_cm, ambient_temp
    """
    if exiftool is None:
        # GUI-UPDATE: GUI에서 경로를 변경한 경우를 반영하도록 호출 시점에 다시 확인한다.
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
    meta_json = subprocess.check_output(meta_args, timeout=30).decode()
    meta = json.loads(meta_json)[0]

    raw_bytes = subprocess.check_output(
        [exiftool, "-RawThermalImage", "-b", jpg_path], timeout=30
    )
    raw_stream = io.BytesIO(raw_bytes)
    thermal_img = Image.open(raw_stream)
    raw_np = np.array(thermal_img, dtype=np.uint16).astype(np.float64)

    E = extract_float(meta.get("Emissivity", 1.0))
    OD = extract_float(meta.get("SubjectDistance", 1.0))
    RTemp = extract_float(meta.get("ReflectedApparentTemperature", 20.0))
    ATemp = extract_float(meta.get("AtmosphericTemperature", 20.0))
    IRWTemp = extract_float(meta.get("IRWindowTemperature", 20.0))
    IRT = extract_float(meta.get("IRWindowTransmission", 1.0))
    RH = extract_float(meta.get("RelativeHumidity", 50.0))
    PR1 = extract_float(meta.get("PlanckR1", 21106.77))
    PB = extract_float(meta.get("PlanckB", 1501.0))
    PF = extract_float(meta.get("PlanckF", 1.0))
    PO = extract_float(meta.get("PlanckO", -7340.0))
    PR2 = extract_float(meta.get("PlanckR2", 0.012545258))
    ATA1 = extract_float(meta.get("AtmosphericTransAlpha1", 0.006569))
    ATA2 = extract_float(meta.get("AtmosphericTransAlpha2", 0.01262))
    ATB1 = extract_float(meta.get("AtmosphericTransBeta1", -0.002276))
    ATB2 = extract_float(meta.get("AtmosphericTransBeta2", -0.00667))
    ATX = extract_float(meta.get("AtmosphericTransX", 1.9))

    thermal = raw2temp(raw_np,
                       E=E, OD=OD, RTemp=RTemp, ATemp=ATemp,
                       IRWTemp=IRWTemp, IRT=IRT, RH=RH,
                       PR1=PR1, PB=PB, PF=PF, PO=PO, PR2=PR2,
                       ATA1=ATA1, ATA2=ATA2, ATB1=ATB1, ATB2=ATB2, ATX=ATX)

    capture_meta = {
        "timestamp": meta.get("DateTimeOriginal", ""),
        "distance_cm": int(round(extract_float(meta.get("SubjectDistance", 1.0)) * 100)),
        "ambient_temp": extract_float(meta.get("AtmosphericTemperature", 20.0)),
    }

    return thermal.astype(np.float32), capture_meta
