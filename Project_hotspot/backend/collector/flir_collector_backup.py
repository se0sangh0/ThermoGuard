import time
import requests

API_URL = "http://127.0.0.1:8000/api/measurements"

CAMERA_ID = 1
ROI_ID = 4


def send_measurement():
    payload = {
        "camera_id": CAMERA_ID,
        "roi_id": ROI_ID,
        "min_temp": 32.0,
        "max_temp": 42.0,
        "mean_temp": 36.0,
        "percentile_95_temp": 40.0,
        "ambient_temp": 33.0,
        "delta_temp": 9.0,
        "over_temp_pixels": 0,
        "max_hotspot_size": 0
    }

    response = requests.post(
        API_URL,
        json=payload,
        timeout=10
    )

    print(response.json())


while True:
    send_measurement()
    time.sleep(30)
