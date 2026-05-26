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
model = YOLO('yolo11n.pt')
model.conf = 0.4
model.overrides['imgsz'] = 320
model.overrides['verbose'] = False

exit_flag = False
latest_frame = None
output_frame = None
frame_lock = threading.Lock()

# ==========================================
# 2. 서보모터 초기화
# ==========================================
factory = LGPIOFactory()
servo_tilt = Servo(12, pin_factory=factory, min_pulse_width=0.6/1000, max_pulse_width=2.4/1000)
servo_pan  = Servo(13, pin_factory=factory, min_pulse_width=0.5/1000, max_pulse_width=2.5/1000)

current_pan  = 105.0   # 좌우 (55~155, 중심 105)
current_tilt = 75.0    # 상하 (0~120, 중심 75)

next_pan  = current_pan    # 서보 스레드가 이동할 목표
next_tilt = current_tilt
servo_event = threading.Event()   # YOLO → 서보 스레드 이동 신호
servo_busy  = False               # 이동 중이면 True → YOLO 신호 차단

def set_servo_angle(servo, angle):
    servo.value = (angle - 90) / 90.0

# 초기 위치
set_servo_angle(servo_pan,  current_pan)
set_servo_angle(servo_tilt, current_tilt)
time.sleep(0.5)
servo_pan.detach()
servo_tilt.detach()

# ==========================================
# 3. 서보 워커 스레드 — 이벤트 받으면 한 번만 이동
# ==========================================
# 속도 조절: STEP_DEG ÷ STEP_SLEEP = 도/초
#   예) 1.5 / 0.04 = 37도/초  |  1.0 / 0.05 = 20도/초
# 속도 = STEP_DEG / STEP_SLEEP 도/초  예) 2.0/0.04 = 50도/초
# 느리게 하려면 STEP_SLEEP을 높이세요 (STEP_DEG는 건드리지 마세요 — 낮추면 이동 시간이 길어져 피드백 루프에 걸립니다)
STEP_DEG   = 2.0
STEP_SLEEP = 0.04
SETTLE     = 0.2

def run_servo_worker():
    global current_pan, current_tilt, servo_busy
    while not exit_flag:
        triggered = servo_event.wait(timeout=0.5)
        if not triggered:
            continue
        servo_event.clear()
        servo_busy = True   # ← 이동 시작, YOLO 신호 차단

        tp = next_pan
        tt = next_tilt

        while True:
            dp = tp - current_pan
            dt = tt - current_tilt
            if abs(dp) < 0.3 and abs(dt) < 0.3:
                break
            if abs(dp) >= 0.3:
                current_pan  += max(-STEP_DEG, min(STEP_DEG, dp))
            if abs(dt) >= 0.3:
                current_tilt += max(-STEP_DEG, min(STEP_DEG, dt))
            set_servo_angle(servo_pan,  current_pan)
            set_servo_angle(servo_tilt, current_tilt)
            time.sleep(STEP_SLEEP)

        current_pan  = tp
        current_tilt = tt
        set_servo_angle(servo_pan,  current_pan)
        set_servo_angle(servo_tilt, current_tilt)
        time.sleep(SETTLE)
        servo_pan.detach()
        servo_tilt.detach()
        servo_busy = False  # ← 이동 완료, YOLO 신호 허용

# ==========================================
# 4. 카메라 프레임 읽기 스레드
# ==========================================
def read_frames(process):
    global latest_frame, exit_flag
    buffer = b''
    while not exit_flag:
        chunk = process.stdout.read(4096)
        if not chunk:
            if process.poll() is not None:
                break
            time.sleep(0.01)
            continue
        buffer += chunk

        while True:
            start = buffer.find(b'\xff\xd8')
            if start == -1: break
            end = buffer.find(b'\xff\xd9', start + 2)
            if end == -1: break
            jpg = buffer[start:end + 2]
            buffer = buffer[end + 2:]
            image = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if image is not None:
                with frame_lock:
                    latest_frame = image

        if len(buffer) > 10 * 1024 * 1024:
            buffer = b''

# ==========================================
# 5. YOLO 추론 + 트래킹 스레드
# ==========================================
def run_inference_and_track():
    global exit_flag, output_frame, next_pan, next_tilt

    CENTER_X, CENTER_Y = 320, 240
    DEADZONE_X = 120   # 픽셀 기준 데드존 (이 안이면 이동 안 함)
    DEADZONE_Y = 90
    MAX_STEP_PAN  = 4.0    # 한 번 이동에 최대 각도 (낮을수록 조금씩 이동)
    MAX_STEP_TILT = 3.0
    PAN_GAIN  = 0.03       # 픽셀 오차 → 각도 변환 비율 (낮을수록 조금씩 이동)
    TILT_GAIN = 0.025

    # 이동 명령 사이 최소 대기(초) — 서보가 이동 완료 후 카메라 안정될 시간
    MOVE_COOLDOWN = 1.5

    smooth_cx = float(CENTER_X)
    smooth_cy = float(CENTER_Y)
    EMA_ALPHA = 0.3
    last_move_time = 0.0
    last_processed = None

    while not exit_flag:
        with frame_lock:
            frame = latest_frame
        if frame is None or frame is last_processed:
            time.sleep(0.01)
            continue
        last_processed = frame
        frame = frame.copy()

        results = model(frame, classes=list(TARGET_CLASSES.keys()))
        centers = []

        cv2.line(frame, (CENTER_X - 20, CENTER_Y), (CENTER_X + 20, CENTER_Y), (0, 255, 0), 2)
        cv2.line(frame, (CENTER_X, CENTER_Y - 20), (CENTER_X, CENTER_Y + 20), (0, 255, 0), 2)

        for box in results[0].boxes:
            cls_id = int(box.cls[0])
            conf   = float(box.conf[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
            centers.append((TARGET_CLASSES[cls_id], cx, cy, conf))
            cv2.rectangle(frame, (x1, y1), (x2, y2), (0, 0, 255), 2)
            cv2.putText(frame, f"{TARGET_CLASSES[cls_id]} {conf:.2f}",
                        (x1, y1 - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 0, 255), 2)
            cv2.circle(frame, (cx, cy), 5, (255, 0, 0), -1)

        if centers:
            centers.sort(key=lambda x: x[3], reverse=True)
            _, cx, cy, _ = centers[0]

            # 쿨다운 중에도 EMA 누적 → 평균 위치로 수렴
            smooth_cx = EMA_ALPHA * cx + (1 - EMA_ALPHA) * smooth_cx
            smooth_cy = EMA_ALPHA * cy + (1 - EMA_ALPHA) * smooth_cy

            now = time.time()
            # 쿨다운 경과 & 서보가 현재 이동 중이 아닐 때만 신호 발사
            if now - last_move_time >= MOVE_COOLDOWN and not servo_busy:
                error_x = CENTER_X - smooth_cx
                error_y = CENTER_Y - smooth_cy

                new_pan  = current_pan
                new_tilt = current_tilt
                moved = False

                if abs(error_x) > DEADZONE_X:
                    delta = max(-MAX_STEP_PAN, min(MAX_STEP_PAN, error_x * PAN_GAIN))
                    new_pan = max(55.0, min(155.0, current_pan + delta))
                    moved = True
                if abs(error_y) > DEADZONE_Y:
                    delta = max(-MAX_STEP_TILT, min(MAX_STEP_TILT, -error_y * TILT_GAIN))
                    new_tilt = max(0.0, min(120.0, current_tilt + delta))
                    moved = True

                if moved:
                    next_pan  = new_pan
                    next_tilt = new_tilt
                    servo_event.set()    # 서보 워커에 이동 신호 — 딱 한 번
                    last_move_time = now
        else:
            smooth_cx = float(CENTER_X)
            smooth_cy = float(CENTER_Y)

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
        if not ret: continue
        yield (b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + buffer.tobytes() + b'\r\n')

@app.route('/')
def index():
    html = """<html><head><title>HomeCam AI Tracker</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
    <body style="background-color:#222;color:white;text-align:center;font-family:sans-serif;margin:0;padding:20px;">
    <h2>HomeCam: Dog &amp; Cat Auto Tracker</h2>
    <img src="/video_feed" style="border:3px solid #555;border-radius:10px;max-width:100%;height:auto;"/>
    </body></html>"""
    return render_template_string(html)

@app.route('/video_feed')
def video_feed():
    return Response(generate_video_stream(), mimetype='multipart/x-mixed-replace; boundary=frame')


if __name__ == "__main__":
    cmd = ('rpicam-vid --camera 1 --inline --nopreview -t 0 '
           '--codec mjpeg --width 640 --height 480 --framerate 30 -o -')
    process = subprocess.Popen(shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    threading.Thread(target=read_frames,           args=(process,), daemon=True).start()
    threading.Thread(target=run_inference_and_track,               daemon=True).start()
    threading.Thread(target=run_servo_worker,                      daemon=True).start()

    print("\n" + "=" * 50)
    print("AI 추적 서버 시작!")
    print("http://[라즈베리파이_IP]:5000")
    print("=" * 50 + "\n")

    try:
        app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
    except KeyboardInterrupt:
        exit_flag = True
        process.terminate()
