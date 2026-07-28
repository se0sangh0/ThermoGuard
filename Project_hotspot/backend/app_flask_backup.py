from fastapi import FastAPI

app = FastAPI(
    title="Hotspot Guard API",
    description="Jetson AGX Orin Local Backend Server",
    version="1.0.0",
)


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
