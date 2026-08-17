import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException


BACKEND_APP = (
    Path(__file__).resolve().parents[1]
    / "Project_hotspot"
    / "backend"
    / "app.py"
)


class _Result:
    def fetchone(self):
        return None


class _Connection:
    def __init__(self):
        self.calls = []

    def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return _Result()


class _Begin:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, _exc_type, _exc, _traceback):
        return False


class _Engine:
    def __init__(self, connection):
        self.connection = connection

    def begin(self):
        return _Begin(self.connection)

    def connect(self):
        return _Begin(self.connection)


def _load_backend_app(monkeypatch, engine):
    database = ModuleType("database")
    database.engine = engine
    monkeypatch.setitem(sys.modules, "database", database)
    spec = importlib.util.spec_from_file_location(
        "thermoguard_backend_contract_app",
        BACKEND_APP,
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_measurement_rejects_roi_from_another_camera(monkeypatch):
    connection = _Connection()
    backend = _load_backend_app(monkeypatch, _Engine(connection))
    measurement = backend.MeasurementCreate(
        camera_id=7,
        roi_id=42,
        max_temp=61.0,
        mean_temp=40.0,
        percentile_95_temp=55.0,
        status="warning",
    )

    result = backend.create_measurement(measurement)

    assert result["status"] == "error"
    assert "카메라에 속하지" in result["error"]
    assert len(connection.calls) == 1
    statement, params = connection.calls[0]
    assert "FROM roi_definitions" in statement
    assert "camera_id = :camera_id" in statement
    assert params == {"camera_id": 7, "roi_id": 42}


def test_dashboard_summary_uses_exact_then_camera_wide_threshold():
    source = BACKEND_APP.read_text(encoding="utf-8")
    summary_start = source.index("def get_dashboard_summary")
    measurement_start = source.index("def create_measurement")
    summary_source = source[summary_start:measurement_start]

    assert "AND (roi_id = :roi_id OR roi_id IS NULL)" in summary_source
    assert "CASE WHEN roi_id = :roi_id THEN 0 ELSE 1 END" in summary_source


def test_measurement_persists_files_hotspots_quality_and_overlay_link():
    source = BACKEND_APP.read_text(encoding="utf-8")
    measurement_start = source.index("def create_measurement")
    measurement_source = source[measurement_start:]

    assert "INSERT INTO capture_files" in measurement_source
    assert "INSERT INTO hotspots" in measurement_source
    assert "INSERT INTO image_quality_results" in measurement_source
    assert "overlay_file_id" in measurement_source
    assert "INSERT INTO api_request_logs" in measurement_source


def test_readiness_checks_database_without_changing_liveness(monkeypatch):
    connection = _Connection()
    backend = _load_backend_app(monkeypatch, _Engine(connection))

    assert backend.health() == {
        "server": "running",
        "device": "Jetson AGX Orin",
    }
    assert backend.readiness() == {"status": "ready", "database": "connected"}

    assert len(connection.calls) == 1
    statement, params = connection.calls[0]
    assert "SELECT 1" in statement
    assert params is None


def test_readiness_returns_503_without_database_error_details(monkeypatch):
    class _UnavailableEngine:
        def connect(self):
            raise RuntimeError("database driver connection failure")

    backend = _load_backend_app(monkeypatch, _UnavailableEngine())

    with pytest.raises(HTTPException) as exc_info:
        backend.readiness()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "status": "not_ready",
        "database": "unavailable",
    }


def test_database_diagnostic_routes_are_disabled_by_default(monkeypatch):
    backend = _load_backend_app(monkeypatch, _Engine(_Connection()))
    monkeypatch.delenv("THERMOGUARD_DIAGNOSTIC_ENDPOINTS", raising=False)

    with pytest.raises(HTTPException) as db_exc:
        backend.db_test()
    with pytest.raises(HTTPException) as tables_exc:
        backend.get_tables()

    assert db_exc.value.status_code == 404
    assert tables_exc.value.status_code == 404
    assert db_exc.value.detail == "Not found"
