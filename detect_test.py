from ultralytics import YOLO
import subprocess
import shlex
import cv2
import numpy as np
import threading
import time

TARGET_CLASSES = {15: "cat", 16: "dog"}

model = YOLO('yolov8n.pt')
model.conf = 0.4
model.overrides['imgsz'] = 640
model.overrides['verbose'] = False

exit_flag = False
latest_frame = None
latest_frame_lock = threading.Lock()
latest_centers = []
latest_centers_lock = threading.Lock()

def read_frames(process):
    global latest_frame, exit_flag
    buffer = b''

    while not exit_flag:
        chunk = process.stdout.read(4096)
        if not chunk:
            continue
        buffer += chunk

        # 최신 프레임만 유지
        last_start = buffer.rfind(b'\xff\xd8')
        end = buffer.find(b'\xff\xd9', last_start) if last_start != -1 else -1
        if last_start != -1 and end != -1:
            jpg = buffer[last_start:end+2]
            buffer = buffer[end+2:]
            image = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                with latest_frame_lock:
                    latest_frame = image

        if len(buffer) > 5 * 1024 * 1024:
            buffer = b''

def run_inference():
    global exit_flag
    last_processed = None

    while not exit_flag:
        with latest_frame_lock:
            frame = latest_frame

        if frame is None or frame is last_processed:
            time.sleep(0.01)
            continue

        last_processed = frame
        results = model(frame, classes=list(TARGET_CLASSES.keys()))
        centers = []
        for box in results[0].boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            centers.append((TARGET_CLASSES[cls_id], cx, cy, conf))
            print(f"Detected: {TARGET_CLASSES[cls_id]} ({conf:.2f}) - center: ({cx}, {cy})")

        with latest_centers_lock:
            latest_centers[:] = centers

if __name__ == "__main__":
    cmd = 'rpicam-vid --inline --nopreview -t 0 --codec mjpeg --width 640 --height 640 --framerate 30 -o -'
    process = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    threading.Thread(target=read_frames, args=(process,), daemon=True).start()
    threading.Thread(target=run_inference, daemon=True).start()

    print("Detector started... (Ctrl+C to quit)")

    try:
        while not exit_flag:
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("Stopped")
        exit_flag = True
        process.terminate()
