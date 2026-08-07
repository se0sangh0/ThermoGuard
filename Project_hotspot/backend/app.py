from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.orm import Session
from datetime import datetime, timedelta
import json

from database import engine
from pydantic import BaseModel, Field

app = FastAPI(
    title="Hotspot Guard API",
    description="Jetson AGX Orin Local Backend Server",
    version="1.0.0",
)

# =============================
# POST 요청 데이터 형식 정의
# =============================

class FactoryCreate(BaseModel):
    factory_name: str
    timezone: str = "Asia/Seoul"


class ProductionLineCreate(BaseModel):
    factory_id: int
    line_name: str
    description: str | None = None


class RobotCreate(BaseModel):
    line_id: int
    robot_code: str
    robot_name: str | None = None
    location_x: float | None = None
    location_y: float | None = None
    location_label: str | None = None
    enabled: bool = True


class CameraCreate(BaseModel):
    robot_id: int
    camera_code: str
    ip_address: str
    model_name: str | None = None
    capture_mode: str = "both"
    normal_interval_sec: float = 30.0
    warning_interval_sec: float = 5.0
    enabled: bool = True

class ROICreate(BaseModel):
    camera_id: int
    roi_name: str
    x1: int
    y1: int
    x2: int
    y2: int
    version: int = 1
    enabled: bool = True

class ThresholdCreate(BaseModel):
    camera_id: int
    roi_id: int | None = None

    baseline_temp: float = 35.0
    warning_delta: float = 15.0
    critical_delta: float = 25.0

    min_hotspot_size: int = 3
    min_hotspot_size_max: int = 10

    alarm_cooldown_sec: int = 600

class MeasurementCreate(BaseModel):
    camera_id: int
    roi_id: int

    min_temp: float | None = None
    max_temp: float
    mean_temp: float
    percentile_95_temp: float

    ambient_temp: float | None = None
    delta_temp: float | None = None

    over_temp_pixels: int = 0
    max_hotspot_size: int = 0

    # thermal_monitoring에서 전달하는 판정 결과
    status: str = "normal"
    algorithm_version: str = "v2.0"
    do_alarm: bool = False
    alarm_message: str | None = None

    captured_at: datetime | None = None
    capture_mode: str | None = None
    thermal_status: str = "success"
    visual_status: str = "skipped"
    pair_status: str = "complete"
    files: list["CaptureFileCreate"] = Field(default_factory=list)
    hotspots: list["HotspotCreate"] = Field(default_factory=list)
    image_quality: "ImageQualityCreate | None" = None


class CaptureFileCreate(BaseModel):
    file_type: str
    storage_path: str
    width: int | None = None
    height: int | None = None
    size_bytes: int | None = None
    checksum_sha256: str | None = None


class HotspotCreate(BaseModel):
    center_x: int
    center_y: int
    max_temp: float
    area_pixels: int | None = None


class ImageQualityCreate(BaseModel):
    is_valid: bool
    reason_code: str
    reason_message: str | None = None
    thermal_width: int | None = None
    thermal_height: int | None = None
    visual_width: int | None = None
    visual_height: int | None = None
    mean_difference: float | None = None


class OperationLogCreate(BaseModel):
    category: str
    action: str
    result: str
    detail: dict | list | str | int | float | bool | None = None
    user_id: int | None = None


class CalibrationCreate(BaseModel):
    camera_id: int
    thermal_points: list[list[float]]
    visual_points: list[list[float]]
    homography_matrix: list[list[float]]
    mean_error_px: float | None = None
    max_error_px: float | None = None
    scale_ratio: float | None = None
    result: str = "success"
    active: bool = True
    performed_by: int | None = None


MeasurementCreate.model_rebuild()


class CameraStatusUpdate(BaseModel):
    connection_status: str
    error_message: str | None = None

class ThresholdUpdate(BaseModel):
    baseline_temp: float | None = None
    warning_delta: float | None = None
    critical_delta: float | None = None
    min_hotspot_size: int | None = None
    min_hotspot_size_max: int | None = None
    alarm_cooldown_sec: int | None = None

class AlertUpdate(BaseModel):
    event_status: str

class NotificationDeliveryCreate(BaseModel):
    alert_id: int
    delivery_status: str
    http_status: int | None = None
    retry_count: int = 0
    error_message: str | None = None   
# =============================
# 기존 GET API들
# =============================

@app.get("/")
def home():
    return {
        "system": "Hotspot Guard",
        "server": "Jetson AGX Orin",
        "status": "running"
    }


@app.get("/api/health")
def health():
    return {
        "server": "running",
        "device": "Jetson AGX Orin"
    }

@app.get("/api/db-test")
def db_test():
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text("SELECT DATABASE(), NOW()")
            ).fetchone()

        return {
            "status": "connected",
            "database": result[0],
            "database_time": str(result[1])
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


@app.get("/api/tables")
def get_tables():
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text("SHOW TABLES")
            )

            tables = [row[0] for row in result]

        return {
            "database": "hotspot_guard",
            "count": len(tables),
            "tables": tables
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

@app.get("/api/cameras")
def get_cameras():
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text("""
                    SELECT
                        camera_id,
                        robot_id,
                        camera_code,
                        ip_address,
                        model_name,
                        capture_mode,
                        normal_interval_sec,
                        warning_interval_sec,
                        connection_status,
                        last_connected_at,
                        last_failed_at,
                        enabled
                    FROM cameras
                    ORDER BY camera_id
                """)
            )

            cameras = []

            for row in result.mappings():
                cameras.append({
                    "camera_id": row["camera_id"],
                    "robot_id": row["robot_id"],
                    "camera_code": row["camera_code"],
                    "ip_address": row["ip_address"],
                    "model_name": row["model_name"],
                    "capture_mode": row["capture_mode"],
                    "normal_interval_sec": float(row["normal_interval_sec"]),
                    "warning_interval_sec": float(row["warning_interval_sec"]),
                    "connection_status": row["connection_status"],
                    "last_connected_at": (
                        str(row["last_connected_at"])
                        if row["last_connected_at"]
                        else None
                    ),
                    "last_failed_at": (
                        str(row["last_failed_at"])
                        if row["last_failed_at"]
                        else None
                    ),
                    "enabled": bool(row["enabled"])
                })

        return {
            "count": len(cameras),
            "cameras": cameras
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


@app.get("/api/measurements")
def get_measurements(limit: int = 100):
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text("""
                    SELECT
                        rm.measurement_id,
                        rm.analysis_id,
                        rm.capture_id,
                        rm.roi_id,
                        rd.roi_name,
                        c.camera_id,
                        c.camera_code,
                        rm.measured_at,
                        rm.min_temp,
                        rm.max_temp,
                        rm.mean_temp,
                        rm.percentile_95_temp,
                        rm.ambient_temp,
                        rm.delta_temp,
                        rm.over_temp_pixels,
                        rm.max_hotspot_size,
                        rm.status
                    FROM roi_measurements rm

                    JOIN roi_definitions rd
                        ON rm.roi_id = rd.roi_id

                    JOIN cameras c
                        ON rd.camera_id = c.camera_id

                    ORDER BY rm.measured_at DESC
                    LIMIT :limit
                """),
                {
                    "limit": limit
                }
            )

            measurements = []

            for row in result.mappings():

                measurements.append({
                    "measurement_id": row["measurement_id"],
                    "analysis_id": row["analysis_id"],
                    "capture_id": row["capture_id"],

                    "camera_id": row["camera_id"],
                    "camera_code": row["camera_code"],

                    "roi_id": row["roi_id"],
                    "roi_name": row["roi_name"],

                    "measured_at": str(row["measured_at"]),

                    "min_temp": (
                        float(row["min_temp"])
                        if row["min_temp"] is not None else None
                    ),

                    "max_temp": float(row["max_temp"]),
                    "mean_temp": float(row["mean_temp"]),
                    "percentile_95_temp": float(
                        row["percentile_95_temp"]
                    ),

                    "ambient_temp": (
                        float(row["ambient_temp"])
                        if row["ambient_temp"] is not None else None
                    ),

                    "delta_temp": (
                        float(row["delta_temp"])
                        if row["delta_temp"] is not None else None
                    ),

                    "over_temp_pixels": row["over_temp_pixels"],
                    "max_hotspot_size": row["max_hotspot_size"],

                    "status": row["status"]
                })

        return {
            "count": len(measurements),
            "measurements": measurements
        }

    except Exception as e:

        return {
            "status": "error",
            "error": str(e)
        }


@app.get("/api/temperature-trend")
def get_temperature_trend(
    hours: int | None = None,
    days: int = 7,
    limit: int = 150000
):
    """Return one maximum temperature per capture for the recent trend graph."""
    try:
        if hours is None:
            if days < 1 or days > 7:
                return {
                    "status": "error",
                    "error": "days는 1일부터 7일까지 지정할 수 있습니다."
                }
            selected_hours = days * 24
        else:
            selected_hours = hours
            if selected_hours not in {1, 24, 72, 168}:
                return {
                    "status": "error",
                    "error": "hours는 1, 24, 72, 168 중 하나여야 합니다."
                }
        safe_limit = max(1, min(limit, 150000))
        cutoff = datetime.now() - timedelta(hours=selected_hours)

        with engine.connect() as connection:
            result = connection.execute(
                text("""
                    SELECT
                        rm.capture_id,
                        rm.measured_at,
                        MAX(rm.max_temp) AS max_temp
                    FROM roi_measurements rm
                    WHERE rm.measured_at >= :cutoff
                    GROUP BY rm.capture_id, rm.measured_at
                    ORDER BY rm.measured_at DESC
                    LIMIT :limit
                """),
                {
                    "cutoff": cutoff,
                    "limit": safe_limit
                }
            )
            points = [
                {
                    "capture_id": row["capture_id"],
                    "measured_at": str(row["measured_at"]),
                    "max_temp": float(row["max_temp"])
                }
                for row in result.mappings()
            ]

        return {
            "count": len(points),
            "hours": selected_hours,
            "points": points
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


@app.get("/api/alerts")
def get_alerts(
    limit: int = 100,
    hours: int | None = None,
    days: int = 7
):
    try:
        if hours is None:
            if days < 1 or days > 7:
                return {
                    "status": "error",
                    "error": "days는 1일부터 7일까지 지정할 수 있습니다."
                }
            selected_hours = days * 24
        else:
            selected_hours = hours
            if selected_hours not in {1, 24, 72, 168}:
                return {
                    "status": "error",
                    "error": "hours는 1, 24, 72, 168 중 하나여야 합니다."
                }
        safe_limit = max(1, min(limit, 5000))
        cutoff = datetime.now() - timedelta(hours=selected_hours)

        with engine.connect() as connection:

            result = connection.execute(
                text("""
                    SELECT
                        ae.alert_id,
                        ae.capture_id,
                        ae.measurement_id,
                        ae.robot_id,
                        r.robot_code,
                        ae.roi_id,
                        rd.roi_name,
                        ae.occurred_at,
                        ae.severity,
                        ae.max_temp,
                        ae.message,
                        ae.event_status,
                        ae.acknowledged_at,
                        ae.resolved_at
                    FROM alert_events ae

                    JOIN robots r
                        ON ae.robot_id = r.robot_id

                    LEFT JOIN roi_definitions rd
                        ON ae.roi_id = rd.roi_id

                    WHERE ae.occurred_at >= :cutoff

                    ORDER BY ae.occurred_at DESC

                    LIMIT :limit
                """),
                {
                    "limit": safe_limit,
                    "cutoff": cutoff
                }
            )

            alerts = []

            for row in result.mappings():

                alerts.append({
                    "alert_id": row["alert_id"],
                    "capture_id": row["capture_id"],
                    "measurement_id": row["measurement_id"],

                    "robot_id": row["robot_id"],
                    "robot_code": row["robot_code"],

                    "roi_id": row["roi_id"],
                    "roi_name": row["roi_name"],

                    "occurred_at": str(row["occurred_at"]),

                    "severity": row["severity"],
                    "max_temp": float(row["max_temp"]),

                    "message": row["message"],
                    "event_status": row["event_status"],

                    "acknowledged_at": (
                        str(row["acknowledged_at"])
                        if row["acknowledged_at"] else None
                    ),

                    "resolved_at": (
                        str(row["resolved_at"])
                        if row["resolved_at"] else None
                    )
                })

        return {
            "count": len(alerts),
            "hours": selected_hours,
            "alerts": alerts
        }

    except Exception as e:

        return {
            "status": "error",
            "error": str(e)
        }

@app.get("/api/notification-deliveries")
def get_notification_deliveries(limit: int = 100):
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text("""
                    SELECT
                        delivery_id,
                        alert_id,
                        attempted_at,
                        delivery_status,
                        http_status,
                        retry_count,
                        sent_at,
                        error_message
                    FROM notification_deliveries
                    ORDER BY delivery_id DESC
                    LIMIT :limit
                """),
                {
                    "limit": limit
                }
            )

            deliveries = []

            for row in result.mappings():
                deliveries.append({
                    "delivery_id": row["delivery_id"],
                    "alert_id": row["alert_id"],
                    "attempted_at": str(row["attempted_at"]),
                    "delivery_status": row["delivery_status"],
                    "http_status": row["http_status"],
                    "retry_count": row["retry_count"],
                    "sent_at": (
                        str(row["sent_at"])
                        if row["sent_at"] else None
                    ),
                    "error_message": row["error_message"]
                })

        return {
            "count": len(deliveries),
            "deliveries": deliveries
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

@app.get("/api/dashboard/summary")
def get_dashboard_summary():
    try:
        with engine.connect() as connection:

            # 1. 가장 최근 측정값 조회
            latest = connection.execute(
                text("""
                    SELECT
                        rm.measurement_id,
                        rm.roi_id,
                        rd.roi_name,
                        c.camera_id,
                        c.camera_code,
                        rm.max_temp,
                        rm.status,
                        rm.measured_at
                    FROM roi_measurements rm
                    JOIN roi_definitions rd
                        ON rm.roi_id = rd.roi_id
                    JOIN cameras c
                        ON rd.camera_id = c.camera_id
                    ORDER BY rm.measured_at DESC
                    LIMIT 1
                """)
            ).mappings().fetchone()

            if latest is None:
                return {
                    "status": "empty",
                    "message": "측정 데이터가 없습니다."
                }

            # 2. 현재 ROI의 threshold 조회
            threshold = connection.execute(
                text("""
                    SELECT
                        baseline_temp,
                        warning_delta,
                        critical_delta
                    FROM threshold_profiles
                    WHERE camera_id = :camera_id
                      AND (roi_id = :roi_id OR roi_id IS NULL)
                      AND valid_to IS NULL
                    ORDER BY
                        CASE WHEN roi_id = :roi_id THEN 0 ELSE 1 END,
                        valid_from DESC
                    LIMIT 1
                """),
                {
                    "camera_id": latest["camera_id"],
                    "roi_id": latest["roi_id"]
                }
            ).mappings().fetchone()

            warning_temp = None
            critical_temp = None

            if threshold is not None:
                baseline = float(threshold["baseline_temp"])
                warning_temp = (
                    baseline + float(threshold["warning_delta"])
                )
                critical_temp = (
                    baseline + float(threshold["critical_delta"])
                )

            # 3. 현재 미해결 alert 개수 조회
            open_alerts = connection.execute(
                text("""
                    SELECT COUNT(*)
                    FROM alert_events
                    WHERE event_status = 'open'
                """)
            ).scalar()

        return {
            "camera": latest["camera_code"],
            "roi": latest["roi_name"],
            "current_temp": float(latest["max_temp"]),
            "status": latest["status"],
            "warning_temp": warning_temp,
            "critical_temp": critical_temp,
            "open_alerts": open_alerts,
            "measured_at": str(latest["measured_at"])
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

@app.get("/api/rois")
def get_rois():
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text("""
                    SELECT
                        roi_id,
                        camera_id,
                        roi_name,
                        x1,
                        y1,
                        x2,
                        y2,
                        version,
                        enabled,
                        created_at,
                        updated_at
                    FROM roi_definitions
                    ORDER BY roi_id
                """)
            )

            rois = []

            for row in result.mappings():
                rois.append({
                    "roi_id": row["roi_id"],
                    "camera_id": row["camera_id"],
                    "roi_name": row["roi_name"],
                    "x1": row["x1"],
                    "y1": row["y1"],
                    "x2": row["x2"],
                    "y2": row["y2"],
                    "version": row["version"],
                    "enabled": bool(row["enabled"]),
                    "created_at": str(row["created_at"]),
                    "updated_at": str(row["updated_at"])
                })

        return {
            "count": len(rois),
            "rois": rois
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


@app.get("/api/thresholds")
def get_thresholds():
    try:
        with engine.connect() as connection:
            result = connection.execute(
                text("""
                    SELECT
                        threshold_id,
                        camera_id,
                        roi_id,
                        baseline_temp,
                        warning_delta,
                        critical_delta,
                        min_hotspot_size,
                        min_hotspot_size_max,
                        alarm_cooldown_sec,
                        valid_from,
                        valid_to
                    FROM threshold_profiles
                    ORDER BY threshold_id
                """)
            )

            thresholds = []

            for row in result.mappings():
                thresholds.append({
                    "threshold_id": row["threshold_id"],
                    "camera_id": row["camera_id"],
                    "roi_id": row["roi_id"],
                    "baseline_temp": float(row["baseline_temp"]),
                    "warning_delta": float(row["warning_delta"]),
                    "critical_delta": float(row["critical_delta"]),
                    "min_hotspot_size": row["min_hotspot_size"],
                    "min_hotspot_size_max": row["min_hotspot_size_max"],
                    "alarm_cooldown_sec": row["alarm_cooldown_sec"],
                    "valid_from": str(row["valid_from"]),
                    "valid_to": (
                        str(row["valid_to"])
                        if row["valid_to"] else None
                    )
                })

        return {
            "count": len(thresholds),
            "thresholds": thresholds
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

        
#위는 get, 아래는 post

@app.post("/api/notification-deliveries")
def create_notification_delivery(
    data: NotificationDeliveryCreate
):
    try:
        allowed_statuses = {
            "pending",
            "success",
            "failed"
        }

        if data.delivery_status not in allowed_statuses:
            return {
                "status": "error",
                "error": (
                    "delivery_status는 "
                    "pending, success, failed 중 하나여야 합니다."
                )
            }

        with engine.begin() as connection:

            # 1. 연결할 alert_id가 실제로 존재하는지 확인
            alert = connection.execute(
                text("""
                    SELECT alert_id
                    FROM alert_events
                    WHERE alert_id = :alert_id
                """),
                {
                    "alert_id": data.alert_id
                }
            ).fetchone()

            if alert is None:
                return {
                    "status": "error",
                    "error": (
                        f"alert_id={data.alert_id}인 "
                        "경고 이벤트가 없습니다."
                    )
                }

            # 2. Telegram 전송 결과 저장
            result = connection.execute(
                text("""
                    INSERT INTO notification_deliveries (
                        alert_id,
                        attempted_at,
                        delivery_status,
                        http_status,
                        retry_count,
                        sent_at,
                        error_message
                    )
                    VALUES (
                        :alert_id,
                        NOW(6),
                        :delivery_status,
                        :http_status,
                        :retry_count,
                        CASE
                            WHEN :delivery_status = 'success'
                            THEN NOW(6)
                            ELSE NULL
                        END,
                        :error_message
                    )
                """),
                {
                    "alert_id": data.alert_id,
                    "delivery_status": data.delivery_status,
                    "http_status": data.http_status,
                    "retry_count": data.retry_count,
                    "error_message": data.error_message
                }
            )

            delivery_id = result.lastrowid

        return {
            "status": "created",
            "delivery_id": delivery_id,
            "alert_id": data.alert_id,
            "delivery_status": data.delivery_status
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

@app.post("/api/factories")
def create_factory(factory: FactoryCreate):
    try:
        with engine.begin() as connection:
            existing = connection.execute(
                text("""
                    SELECT factory_id FROM factories
                    WHERE factory_name = :factory_name AND timezone = :timezone
                    ORDER BY factory_id LIMIT 1
                """),
                {"factory_name": factory.factory_name, "timezone": factory.timezone},
            ).fetchone()
            if existing is not None:
                return {
                    "status": "created", "factory_id": existing[0],
                    "factory_name": factory.factory_name, "existing": True,
                }
            result = connection.execute(
                text("""
                    INSERT INTO factories (
                        factory_name,
                        timezone
                    )
                    VALUES (
                        :factory_name,
                        :timezone
                    )
                """),
                {
                    "factory_name": factory.factory_name,
                    "timezone": factory.timezone
                }
            )

            factory_id = result.lastrowid

        return {
            "status": "created",
            "factory_id": factory_id,
            "factory_name": factory.factory_name
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


@app.post("/api/production-lines")
def create_production_line(line: ProductionLineCreate):
    try:
        with engine.begin() as connection:
            existing = connection.execute(
                text("""
                    SELECT line_id FROM production_lines
                    WHERE factory_id = :factory_id AND line_name = :line_name
                    ORDER BY line_id LIMIT 1
                """),
                {"factory_id": line.factory_id, "line_name": line.line_name},
            ).fetchone()
            if existing is not None:
                return {"status": "created", "line_id": existing[0], "existing": True}
            result = connection.execute(
                text("""
                    INSERT INTO production_lines (
                        factory_id,
                        line_name,
                        description
                    )
                    VALUES (
                        :factory_id,
                        :line_name,
                        :description
                    )
                """),
                {
                    "factory_id": line.factory_id,
                    "line_name": line.line_name,
                    "description": line.description
                }
            )

            line_id = result.lastrowid

        return {
            "status": "created",
            "line_id": line_id
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


@app.post("/api/robots")
def create_robot(robot: RobotCreate):
    try:
        with engine.begin() as connection:
            existing = connection.execute(
                text("""
                    SELECT robot_id FROM robots
                    WHERE line_id = :line_id AND robot_code = :robot_code
                    ORDER BY robot_id LIMIT 1
                """),
                {"line_id": robot.line_id, "robot_code": robot.robot_code},
            ).fetchone()
            if existing is not None:
                return {"status": "created", "robot_id": existing[0], "existing": True}
            result = connection.execute(
                text("""
                    INSERT INTO robots (
                        line_id,
                        robot_code,
                        robot_name,
                        location_x,
                        location_y,
                        location_label,
                        enabled
                    )
                    VALUES (
                        :line_id,
                        :robot_code,
                        :robot_name,
                        :location_x,
                        :location_y,
                        :location_label,
                        :enabled
                    )
                """),
                {
                    "line_id": robot.line_id,
                    "robot_code": robot.robot_code,
                    "robot_name": robot.robot_name,
                    "location_x": robot.location_x,
                    "location_y": robot.location_y,
                    "location_label": robot.location_label,
                    "enabled": 1 if robot.enabled else 0
                }
            )

            robot_id = result.lastrowid

        return {
            "status": "created",
            "robot_id": robot_id
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


@app.post("/api/cameras")
def create_camera(camera: CameraCreate):
    try:
        with engine.begin() as connection:
            existing = connection.execute(
                text("""
                    SELECT camera_id FROM cameras
                    WHERE robot_id = :robot_id
                      AND (camera_code = :camera_code OR ip_address = :ip_address)
                    ORDER BY camera_id LIMIT 1
                """),
                {
                    "robot_id": camera.robot_id,
                    "camera_code": camera.camera_code,
                    "ip_address": camera.ip_address,
                },
            ).fetchone()
            if existing is not None:
                return {
                    "status": "created", "camera_id": existing[0],
                    "camera_code": camera.camera_code, "existing": True,
                }
            result = connection.execute(
                text("""
                    INSERT INTO cameras (
                        robot_id,
                        camera_code,
                        ip_address,
                        model_name,
                        capture_mode,
                        normal_interval_sec,
                        warning_interval_sec,
                        enabled
                    )
                    VALUES (
                        :robot_id,
                        :camera_code,
                        :ip_address,
                        :model_name,
                        :capture_mode,
                        :normal_interval_sec,
                        :warning_interval_sec,
                        :enabled
                    )
                """),
                {
                    "robot_id": camera.robot_id,
                    "camera_code": camera.camera_code,
                    "ip_address": camera.ip_address,
                    "model_name": camera.model_name,
                    "capture_mode": camera.capture_mode,
                    "normal_interval_sec": camera.normal_interval_sec,
                    "warning_interval_sec": camera.warning_interval_sec,
                    "enabled": 1 if camera.enabled else 0
                }
            )

            camera_id = result.lastrowid

        return {
            "status": "created",
            "camera_id": camera_id,
            "camera_code": camera.camera_code
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

@app.post("/api/rois")
def create_roi(roi: ROICreate):
    try:
        with engine.begin() as connection:
            result = connection.execute(
                text("""
                    INSERT INTO roi_definitions (
                        camera_id,
                        roi_name,
                        x1,
                        y1,
                        x2,
                        y2,
                        version,
                        enabled
                    )
                    VALUES (
                        :camera_id,
                        :roi_name,
                        :x1,
                        :y1,
                        :x2,
                        :y2,
                        :version,
                        :enabled
                    )
                """),
                {
                    "camera_id": roi.camera_id,
                    "roi_name": roi.roi_name,
                    "x1": roi.x1,
                    "y1": roi.y1,
                    "x2": roi.x2,
                    "y2": roi.y2,
                    "version": roi.version,
                    "enabled": 1 if roi.enabled else 0
                }
            )

            roi_id = result.lastrowid

        return {
            "status": "created",
            "roi_id": roi_id
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

@app.post("/api/thresholds")
def create_threshold(threshold: ThresholdCreate):
    try:
        with engine.begin() as connection:
            result = connection.execute(
                text("""
                    INSERT INTO threshold_profiles (
                        camera_id,
                        roi_id,
                        baseline_temp,
                        warning_delta,
                        critical_delta,
                        min_hotspot_size,
                        min_hotspot_size_max,
                        alarm_cooldown_sec
                    )
                    VALUES (
                        :camera_id,
                        :roi_id,
                        :baseline_temp,
                        :warning_delta,
                        :critical_delta,
                        :min_hotspot_size,
                        :min_hotspot_size_max,
                        :alarm_cooldown_sec
                    )
                """),
                {
                    "camera_id": threshold.camera_id,
                    "roi_id": threshold.roi_id,
                    "baseline_temp": threshold.baseline_temp,
                    "warning_delta": threshold.warning_delta,
                    "critical_delta": threshold.critical_delta,
                    "min_hotspot_size": threshold.min_hotspot_size,
                    "min_hotspot_size_max": threshold.min_hotspot_size_max,
                    "alarm_cooldown_sec": threshold.alarm_cooldown_sec
                }
            )

            threshold_id = result.lastrowid

        return {
            "status": "created",
            "threshold_id": threshold_id
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }


@app.post("/api/operation-logs")
def create_operation_log(data: OperationLogCreate):
    try:
        detail = data.detail
        if detail is not None and not isinstance(detail, (dict, list)):
            detail = {"message": detail}
        with engine.begin() as connection:
            result = connection.execute(
                text("""
                    INSERT INTO operation_logs (
                        occurred_at, user_id, category, action, result, detail
                    ) VALUES (
                        NOW(6), :user_id, :category, :action, :result, :detail
                    )
                """),
                {
                    "user_id": data.user_id,
                    "category": data.category,
                    "action": data.action,
                    "result": data.result,
                    "detail": json.dumps(detail, ensure_ascii=False) if detail is not None else None,
                }
            )
        return {"status": "created", "operation_id": result.lastrowid}
    except Exception as e:
        return {"status": "error", "error": str(e)}


@app.post("/api/calibrations")
def create_calibration(data: CalibrationCreate):
    try:
        with engine.begin() as connection:
            camera = connection.execute(
                text("SELECT camera_id FROM cameras WHERE camera_id = :camera_id"),
                {"camera_id": data.camera_id},
            ).fetchone()
            if camera is None:
                return {"status": "error", "error": "해당 camera_id가 없습니다."}
            if data.active:
                connection.execute(
                    text("UPDATE calibrations SET active = 0 WHERE camera_id = :camera_id"),
                    {"camera_id": data.camera_id},
                )
            result = connection.execute(
                text("""
                    INSERT INTO calibrations (
                        camera_id, performed_at, performed_by, thermal_points,
                        visual_points, homography_matrix, mean_error_px,
                        max_error_px, scale_ratio, result, active
                    ) VALUES (
                        :camera_id, NOW(6), :performed_by, :thermal_points,
                        :visual_points, :homography_matrix, :mean_error_px,
                        :max_error_px, :scale_ratio, :result, :active
                    )
                """),
                {
                    "camera_id": data.camera_id,
                    "performed_by": data.performed_by,
                    "thermal_points": json.dumps(data.thermal_points),
                    "visual_points": json.dumps(data.visual_points),
                    "homography_matrix": json.dumps(data.homography_matrix),
                    "mean_error_px": data.mean_error_px,
                    "max_error_px": data.max_error_px,
                    "scale_ratio": data.scale_ratio,
                    "result": data.result,
                    "active": 1 if data.active else 0,
                },
            )
        return {"status": "created", "calibration_id": result.lastrowid}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.post("/api/measurements")
def create_measurement(data: MeasurementCreate):
    try:
        with engine.begin() as connection:

            # 1. ROI가 실제로 요청 카메라에 속하는지 확인
            roi_owner = connection.execute(
                text("""
                    SELECT roi_id
                    FROM roi_definitions
                    WHERE roi_id = :roi_id
                      AND camera_id = :camera_id
                      AND enabled = 1
                    LIMIT 1
                """),
                {
                    "camera_id": data.camera_id,
                    "roi_id": data.roi_id
                }
            ).fetchone()

            if roi_owner is None:
                return {
                    "status": "error",
                    "error": "ROI가 요청한 카메라에 속하지 않거나 비활성 상태입니다."
                }

            # 2. 해당 ROI의 현재 임계값 설정 조회
            threshold = connection.execute(
                text("""
                    SELECT
                        baseline_temp,
                        warning_delta,
                        critical_delta,
                        min_hotspot_size,
                        min_hotspot_size_max
                    FROM threshold_profiles
                    WHERE camera_id = :camera_id
                      AND (roi_id = :roi_id OR roi_id IS NULL)
                      AND valid_to IS NULL
                    ORDER BY
                        CASE WHEN roi_id = :roi_id THEN 0 ELSE 1 END,
                        valid_from DESC
                    LIMIT 1
                """),
                {
                    "camera_id": data.camera_id,
                    "roi_id": data.roi_id
                }
            ).mappings().fetchone()

            if threshold is None:
                return {
                    "status": "error",
                    "error": "적용 가능한 threshold profile이 없습니다."
                }

            baseline = float(threshold["baseline_temp"])
            warning_delta = float(threshold["warning_delta"])
            critical_delta = float(threshold["critical_delta"])

            warning_temp = baseline + warning_delta
            critical_temp = baseline + critical_delta

            # 3. 상태 및 캡처 모드는 thermal_monitoring에서 전달한 값 사용
            status = data.status

            capture_mode = data.capture_mode or (
                "warning" if status in ("warning", "critical") else "normal"
            )
            captured_at = data.captured_at or datetime.now()

            # 4. 촬영 기록 생성
            capture_result = connection.execute(
                text("""
                    INSERT INTO captures (
                        camera_id,
                        requested_at,
                        captured_at,
                        completed_at,
                        capture_mode,
                        thermal_status,
                        visual_status,
                        pair_status
                    )
                    VALUES (
                        :camera_id,
                        :captured_at,
                        :captured_at,
                        NOW(6),
                        :capture_mode,
                        :thermal_status,
                        :visual_status,
                        :pair_status
                    )
                """),
                {
                    "camera_id": data.camera_id,
                    "captured_at": captured_at,
                    "capture_mode": capture_mode,
                    "thermal_status": data.thermal_status,
                    "visual_status": data.visual_status,
                    "pair_status": data.pair_status,
                }
            )

            capture_id = capture_result.lastrowid

            overlay_file_id = None
            for capture_file in data.files:
                file_result = connection.execute(
                    text("""
                        INSERT INTO capture_files (
                            capture_id, file_type, storage_path, width, height,
                            size_bytes, checksum_sha256
                        ) VALUES (
                            :capture_id, :file_type, :storage_path, :width, :height,
                            :size_bytes, :checksum_sha256
                        )
                    """),
                    {
                        "capture_id": capture_id,
                        **capture_file.model_dump(),
                    }
                )
                if capture_file.file_type == "overlay":
                    overlay_file_id = file_result.lastrowid

            # 5. 분석 실행 기록 생성 (thermal_monitoring의 algorithm_version 사용)
            analysis_result = connection.execute(
                text("""
                    INSERT INTO analysis_runs (
                        capture_id,
                        started_at,
                        completed_at,
                        result,
                        algorithm_version
                    )
                    VALUES (
                        :capture_id,
                        NOW(6),
                        NOW(6),
                        'success',
                        :algorithm_version
                    )
                """),
                {
                    "capture_id": capture_id,
                    "algorithm_version": data.algorithm_version,
                }
            )

            analysis_id = analysis_result.lastrowid

            # 6. 측정 데이터 저장 (thermal_monitoring의 status 사용)
            measurement_result = connection.execute(
                text("""
                    INSERT INTO roi_measurements (
                        analysis_id,
                        capture_id,
                        roi_id,
                        measured_at,
                        min_temp,
                        max_temp,
                        mean_temp,
                        percentile_95_temp,
                        ambient_temp,
                        delta_temp,
                        over_temp_pixels,
                        max_hotspot_size,
                        status
                    )
                    VALUES (
                        :analysis_id,
                        :capture_id,
                        :roi_id,
                        NOW(6),
                        :min_temp,
                        :max_temp,
                        :mean_temp,
                        :percentile_95_temp,
                        :ambient_temp,
                        :delta_temp,
                        :over_temp_pixels,
                        :max_hotspot_size,
                        :status
                    )
                """),
                {
                    "analysis_id": analysis_id,
                    "capture_id": capture_id,
                    "roi_id": data.roi_id,
                    "min_temp": data.min_temp,
                    "max_temp": data.max_temp,
                    "mean_temp": data.mean_temp,
                    "percentile_95_temp": data.percentile_95_temp,
                    "ambient_temp": data.ambient_temp,
                    "delta_temp": data.delta_temp,
                    "over_temp_pixels": data.over_temp_pixels,
                    "max_hotspot_size": data.max_hotspot_size,
                    "status": status,
                }
            )

            measurement_id = measurement_result.lastrowid

            for hotspot in data.hotspots:
                connection.execute(
                    text("""
                        INSERT INTO hotspots (
                            measurement_id, center_x, center_y, max_temp, area_pixels
                        ) VALUES (
                            :measurement_id, :center_x, :center_y, :max_temp, :area_pixels
                        )
                    """),
                    {"measurement_id": measurement_id, **hotspot.model_dump()}
                )

            if data.image_quality is not None:
                connection.execute(
                    text("""
                        INSERT INTO image_quality_results (
                            capture_id, checked_at, is_valid, reason_code,
                            reason_message, thermal_width, thermal_height,
                            visual_width, visual_height, mean_difference
                        ) VALUES (
                            :capture_id, NOW(6), :is_valid, :reason_code,
                            :reason_message, :thermal_width, :thermal_height,
                            :visual_width, :visual_height, :mean_difference
                        )
                    """),
                    {
                        "capture_id": capture_id,
                        **data.image_quality.model_dump(),
                    }
                )

            alert_id = None

            # 7. do_alarm이 True인 경우만 alert_events 생성
            #    (thermal_monitoring의 상태 머신 + 쿨다운을 통과한 알람만 기록)
            if data.do_alarm:

                robot_result = connection.execute(
                    text("""
                        SELECT robot_id
                        FROM cameras
                        WHERE camera_id = :camera_id
                    """),
                    {
                        "camera_id": data.camera_id
                    }
                ).fetchone()

                if robot_result is None:
                    return {
                        "status": "error",
                        "error": "카메라에 연결된 robot_id를 찾을 수 없습니다."
                    }

                robot_id = robot_result[0]

                alert_result = connection.execute(
                    text("""
                        INSERT INTO alert_events (
                            capture_id,
                            measurement_id,
                            robot_id,
                            roi_id,
                            occurred_at,
                            severity,
                            max_temp,
                            message,
                            event_status,
                            overlay_file_id
                        )
                        VALUES (
                            :capture_id,
                            :measurement_id,
                            :robot_id,
                            :roi_id,
                            NOW(6),
                            :severity,
                            :max_temp,
                            :message,
                            'open',
                            :overlay_file_id
                        )
                    """),
                    {
                        "capture_id": capture_id,
                        "measurement_id": measurement_id,
                        "robot_id": robot_id,
                        "roi_id": data.roi_id,
                        "severity": status,
                        "max_temp": data.max_temp,
                        "overlay_file_id": overlay_file_id,
                        "message": (
                            data.alarm_message or
                            f"ROI {data.roi_id} 온도 이상 감지: "
                            f"{data.max_temp}°C / 상태: {status}"
                        )
                    }
                )

                alert_id = alert_result.lastrowid

            connection.execute(
                text("""
                    INSERT INTO api_request_logs (
                        camera_id, requested_at, endpoint_type, result,
                        http_status, retry_count
                    ) VALUES (
                        :camera_id, NOW(6), 'measurements', 'success', 200, 0
                    )
                """),
                {"camera_id": data.camera_id}
            )

        return {
            "status": "created",
            "capture_id": capture_id,
            "analysis_id": analysis_id,
            "measurement_id": measurement_id,
            "temperature_status": status,
            "warning_temp": warning_temp,
            "critical_temp": critical_temp,
            "alert_id": alert_id,
            "do_alarm": data.do_alarm,
            "algorithm_version": data.algorithm_version
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

#=====여기서부터는 patch=====#

@app.patch("/api/cameras/{camera_id}/status")
def update_camera_status(camera_id: int, data: CameraStatusUpdate):
    try:
        allowed = {"connected", "disconnected", "error", "unknown"}
        if data.connection_status not in allowed:
            return {"status": "error", "error": "지원하지 않는 connection_status입니다."}
        with engine.begin() as connection:
            result = connection.execute(
                text("""
                    UPDATE cameras
                    SET connection_status = :connection_status,
                        last_connected_at = CASE
                            WHEN :connection_status = 'connected' THEN NOW(6)
                            ELSE last_connected_at
                        END,
                        last_failed_at = CASE
                            WHEN :connection_status IN ('disconnected', 'error') THEN NOW(6)
                            ELSE last_failed_at
                        END
                    WHERE camera_id = :camera_id
                """),
                {
                    "camera_id": camera_id,
                    "connection_status": data.connection_status,
                },
            )
        if result.rowcount == 0:
            return {"status": "error", "error": "해당 camera_id가 없습니다."}
        return {"status": "updated", "camera_id": camera_id}
    except Exception as e:
        return {"status": "error", "error": str(e)}

@app.patch("/api/thresholds/{threshold_id}")
def update_threshold(
    threshold_id: int,
    data: ThresholdUpdate
):
    try:
        fields = data.model_dump(exclude_none=True)

        if not fields:
            return {
                "status": "error",
                "error": "변경할 값이 없습니다."
            }

        allowed = {
            "baseline_temp",
            "warning_delta",
            "critical_delta",
            "min_hotspot_size",
            "min_hotspot_size_max",
            "alarm_cooldown_sec"
        }

        updates = []
        params = {"threshold_id": threshold_id}

        for key, value in fields.items():
            if key in allowed:
                updates.append(f"{key} = :{key}")
                params[key] = value

        sql = f"""
            UPDATE threshold_profiles
            SET {", ".join(updates)}
            WHERE threshold_id = :threshold_id
        """

        with engine.begin() as connection:
            result = connection.execute(
                text(sql),
                params
            )

        return {
            "status": "updated",
            "threshold_id": threshold_id,
            "updated_rows": result.rowcount
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }

@app.patch("/api/alerts/{alert_id}")
def update_alert(alert_id: int, data: AlertUpdate):
    try:
        if data.event_status not in (
            "open",
            "acknowledged",
            "resolved"
        ):
            return {
                "status": "error",
                "error": "event_status는 open, acknowledged, resolved 중 하나여야 합니다."
            }

        with engine.begin() as connection:

            # alert 존재 여부 확인
            alert = connection.execute(
                text("""
                    SELECT alert_id
                    FROM alert_events
                    WHERE alert_id = :alert_id
                """),
                {
                    "alert_id": alert_id
                }
            ).fetchone()

            if alert is None:
                return {
                    "status": "error",
                    "error": "해당 alert_id가 없습니다."
                }

            # 확인 처리
            if data.event_status == "acknowledged":

                connection.execute(
                    text("""
                        UPDATE alert_events
                        SET
                            event_status = 'acknowledged',
                            acknowledged_at = NOW(6)
                        WHERE alert_id = :alert_id
                    """),
                    {
                        "alert_id": alert_id
                    }
                )

            # 해결 처리
            elif data.event_status == "resolved":

                connection.execute(
                    text("""
                        UPDATE alert_events
                        SET
                            event_status = 'resolved',
                            resolved_at = NOW(6)
                        WHERE alert_id = :alert_id
                    """),
                    {
                        "alert_id": alert_id
                    }
                )

            # 다시 open 상태로 변경
            else:

                connection.execute(
                    text("""
                        UPDATE alert_events
                        SET
                            event_status = 'open',
                            acknowledged_at = NULL,
                            resolved_at = NULL
                        WHERE alert_id = :alert_id
                    """),
                    {
                        "alert_id": alert_id
                    }
                )

        return {
            "status": "updated",
            "alert_id": alert_id,
            "event_status": data.event_status
        }

    except Exception as e:
        return {
            "status": "error",
            "error": str(e)
        }
