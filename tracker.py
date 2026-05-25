from flask import Flask, Response, render_template_string
from ultralytics import YOLO
import subprocess
import shlex
import cv2
import numpy as np
import threading
import time

from gpiozero import Servo
from gpiozero.pins.lgpio import LGPIOFactory

# ==========================================
# 1. Flask & YOLO11 초기화
# ==========================================
app = Flask(__name__)

TARGET_CLASSES = {15: "cat", 16: "dog"}
model = YOLO('yolo11n.pt')          # YOLOv8 → YOLO11 (첫 실행 시 자동 다운로드)
model.conf = 0.4
model.overrides['imgsz'] = 320      # 추론 해상도 낮춰서 파이5 속도 확보
model.overrides['verbose'] = False

# 전역 변수
exit_flag = False
latest_frame = None
output_frame = None
frame_lock = threading.Lock()

# ==========================================
# 2. 서보모터 초기화
# ==========================================
factory = LGPIOFactory()
servo_tilt = Servo(12, pin_factory=factory,
                   min_pulse_width=0.6/1000, max_pulse_width=2.4/1000)
servo_pan = Servo(13, pin_factory=factory,
                  min_pulse_width=0.5/1000, max_pulse_width=2.5/1000)

# 현재 각도 (중심값으로 시작)
current_tilt = 75   # 상하 (0~120, 중심 75)
current_pan = 105   # 좌우 (55~155, 중심 105)

def set_servo_angle(servo, angle):
    servo.value = (angle - 90) / 90.0

# 초기 위치로 이동 후 신호 차단 (지터 방지)
set_servo_angle(servo_tilt, current_tilt)
set_servo_angle(servo_pan, current_pan)
time.sleep(0.5)
servo_tilt.detach()
servo_pan.detach()


# ==========================================
# 스레드 1: 카메라 프레임 읽기
# ==========================================
def read_frames(process):
    global latest_frame, exit_flag
    buffer = b''

    while not exit_flag:
        chunk = process.stdout.read(4096)
        if not chunk:
            if process.poll() is not None:
                print("⚠️ 카메라 프로세스 종료됨")
                break
            time.sleep(0.01)
            continue
        buffer += chunk

        # 완성된 JPEG 프레임 모두 추출 (안정 버전)
        while True:
            start = buffer.find(b'\xff\xd8')
            if start == -1:
                break
            end = buffer.find(b'\xff\xd9', start + 2)
            if end == -1:
                break
            jpg = buffer[start:end + 2]
            buffer = buffer[end + 2:]
            image = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                with frame_lock:
                    latest_frame = image

        if len(buffer) > 10 * 1024 * 1024:
            buffer = b''


# ==========================================
# 스레드 2: YOLO11 추론 + 서보 트래킹 + 화면 그리기
# ==========================================
def run_inference_and_track():
    global exit_flag, current_pan, current_tilt, output_frame

    CENTER_X, CENTER_Y = 320, 320   # 640x640 화면의 중심
    DEADZONE = 50                   # 이 범위 안이면 안 움직임 (떨림 방지)
    STEP = 2                        # 한 번에 움직이는 각도
    last_processed = None
    last_move_time = time.time()

    while not exit_flag:
        with frame_lock:
            frame = latest_frame
        if frame is None or frame is last_processed:
            time.sleep(0.01)
            continue
        last_processed = frame
        frame = frame.copy()

        # YOLO11 추론
        results = model(frame, classes=list(TARGET_CLASSES.keys()))
        centers = []

        # 화면 중앙 십자선(에임)
        cv2.line(frame, (CENTER_X - 20, CENTER_Y), (CENTER_X + 20, CENTER_Y), (0, 255, 0), 2)
        cv2.line(frame, (CENTER_X, CENTER_Y - 20), (CENTER_X, CENTER_Y + 20), (0, 255, 0), 2)

        for box in results[0].boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            cx = (x1 + x2) // 2
            cy = (y1 + y2) // 2
            centers.append((TARGET_CLASSES[cls_id], cx, cy, conf))

            # 박스 + 라벨 + 중심점
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
            cv2.putText(frame, f"{TARGET_CLASSES[cls_id]} {conf:.2f}",
                        (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            cv2.circle(frame, (cx, cy), 5, (255, 0, 0), -1)

        # --- 서보 트래킹 ---
        if centers:
            _, cx, cy, _ = centers[0]   # 첫 번째 타겟 추적
            moved = False

            # 좌우(pan): 타겟이 왼쪽에 있으면 카메라를 왼쪽으로
            if cx < CENTER_X - DEADZONE:
                current_pan += STEP
                moved = True
            elif cx > CENTER_X + DEADZONE:
                current_pan -= STEP
                moved = True

            # 상하(tilt): 타겟이 위에 있으면 카메라를 위로
            if cy < CENTER_Y - DEADZONE:
                current_tilt -= STEP
                moved = True
            elif cy > CENTER_Y + DEADZONE:
                current_tilt += STEP
                moved = True

            # 안전 범위 제한
            current_pan = max(55, min(155, current_pan))
            current_tilt = max(0, min(120, current_tilt))

            if moved:
                set_servo_angle(servo_pan, current_pan)
                set_servo_angle(servo_tilt, current_tilt)
                last_move_time = time.time()
        else:
            # 타겟 없고 1초 지나면 신호 끊어서 떨림/소음 방지
            if time.time() - last_move_time > 1.0:
                servo_pan.detach()
                servo_tilt.detach()

        with frame_lock:
            output_frame = frame


# ==========================================
# 웹 스트리밍
# ==========================================
def generate_video_stream():
    last_sent = None
    while True:
        with frame_lock:
            frame = output_frame
        if frame is None or frame is last_sent:
            time.sleep(0.005)
            continue
        last_sent = frame

        ret, buffer = cv2.imencode('.jpg', frame)
        if not ret:
            continue
        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')


@app.route('/')
def index():
    html = """
    <html>
      <head>
        <title>HomeCam AI Tracker</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
      </head>
      <body style="background-color:#222; color:white; text-align:center; font-family:sans-serif; margin:0; padding:20px;">
        <h2>🎯 HomeCam: Dog & Cat Auto Tracker (YOLO11)</h2>
        <img src="/video_feed" style="border:3px solid #555; border-radius:10px; max-width:100%; height:auto;" />
      </body>
    </html>
    """
    return render_template_string(html)


@app.route('/video_feed')
def video_feed():
    return Response(generate_video_stream(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


if __name__ == "__main__":
    cmd = ('rpicam-vid --camera 1 --inline --nopreview -t 0 '
           '--codec mjpeg --width 640 --height 640 --framerate 30 -o -')
    process = subprocess.Popen(shlex.split(cmd),
                               stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)

    threading.Thread(target=read_frames, args=(process,), daemon=True).start()
    threading.Thread(target=run_inference_and_track, daemon=True).start()

    print("\n" + "=" * 50)
    print("🚀 AI 추적 서버 시작!")
    print("👉 http://[라즈베리파이_IP]:5000")
    print("=" * 50 + "\n")

    try:
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        exit_flag = True
        process.terminate()