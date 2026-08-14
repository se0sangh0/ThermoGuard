# Retired FLIR collector scripts

ThermoGuard now has one operational capture-and-analysis path:

```bash
python dashboard.py
```

The Product Dashboard owns camera capture, ROI analysis, state transitions,
database persistence, and Telegram delivery. The scripts in this directory
are intentional safe stubs retained to prevent old commands or an old
`hotspot-flir-collector.service` unit from starting a second camera loop.

Keep `hotspot-flir-collector.service` disabled. The separate
`hotspot-backend.service` remains required because it provides the dashboard's
FastAPI and MariaDB integration.
