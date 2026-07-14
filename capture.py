import requests
import time
import os
from datetime import datetime

CAM_IP = "192.168.0.51"  # 카메라 IP 주소 192.168. 사설 대역 사용 권장
SAVE_DIR = "thermal_dataset"
os.makedirs(SAVE_DIR, exist_ok=True)

# 수집할 이미지 종류: "thermal" 만 또는 "both" (thermal + visual)
MODE = "both"
INTERVAL = 10.0

URLS = {
    "thermal": f"http://{CAM_IP}/api/image/current?imgformat=JPEG",
    "visual": f"http://{CAM_IP}/api/image/current?imgformat=JPEG_visual"
}

try:
    while True:
        try:
            filenametime = datetime.now().strftime("%Y%m%d%H%M%S_%f")

            for img_type in (["thermal", "visual"] if MODE == "both" else ["thermal"]):
                r = requests.get(URLS[img_type], timeout=10)

                if r.status_code == 200:
                    content_type = r.headers.get("Content-Type", "")
                    if "image" not in content_type.lower() and content_type != "octet-stream":
                        print(f"[{img_type}] 이미지 응답이 아닙니다. Content-Type: {content_type}")
                        continue

                    suffix = "_visual" if img_type == "visual" else ""
                    jpg_path = os.path.join(SAVE_DIR, f"{filenametime}{suffix}.jpg")

                    with open(jpg_path, "wb") as f:
                        f.write(r.content)

                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [{img_type}] 저장 완료: {jpg_path} "
                          f"({len(r.content)} bytes)")

                else:
                    print(f"[{img_type}] 요청 실패: HTTP {r.status_code}")

        except requests.exceptions.Timeout:
            print("요청 시간 초과: 카메라 응답이 없습니다.")

        except requests.exceptions.ConnectionError:
            print("연결 오류: 카메라 IP 또는 네트워크 연결을 확인하세요.")

        except Exception as e:
            print(f"오류 발생: {e}")

        time.sleep(INTERVAL)

except KeyboardInterrupt:
    print("이미지 수집을 종료합니다.")
