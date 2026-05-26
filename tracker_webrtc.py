import asyncio
import json
import subprocess
import shlex
import threading
import time
import uuid

import cv2
import numpy as np
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.mediastreams import VideoStreamTrack
from av import VideoFrame
from ultralytics import YOLO
from gpiozero import Servo
from gpiozero.pins.lgpio import LGPIOFactory

# ==========================================
# 설정 — 서버 주소만 바꾸면 됩니다
# ==========================================
SIGNAL_URL = "ws://20.189.241.58:8080/ws/signal"

# ==========================================
# 1. YOLO 초기화
# ==========================================
TARGET_CLASSES = {15: "cat", 16: "dog"}
model = YOLO('yolo11n.pt')
model.conf = 0.4
model.overrides['imgsz'] = 320
model.overrides['verbose'] = False

exit_flag    = False
latest_frame = None
output_frame = None
frame_lock   = threading.Lock()

# ==========================================
# 2. 서보모터 초기화
# ==========================================
factory    = LGPIOFactory()
servo_tilt = Servo(12, pin_factory=factory, min_pulse_width=0.6/1000, max_pulse_width=2.4/1000)
servo_pan  = Servo(13, pin_factory=factory, min_pulse_width=0.5/1000, max_pulse_width=2.5/1000)

current_pan  = 105.0
current_tilt = 75.0
next_pan     = current_pan
next_tilt    = current_tilt
servo_event  = threading.Event()
servo_busy   = False
manual_mode  = False

def set_servo_angle(servo, angle):
    servo.value = (angle - 90) / 90.0

set_servo_angle(servo_pan,  current_pan)
set_servo_angle(servo_tilt, current_tilt)
time.sleep(0.5)
servo_pan.detach()
servo_tilt.detach()

# ==========================================
# 3. 서보 워커 스레드 (AUTO/MANUAL 공용)
# ==========================================
STEP_DEG   = 2.0
STEP_SLEEP = 0.04
SETTLE     = 0.1   # 수동 조작감 위해 0.2 → 0.1

def run_servo_worker():
    global current_pan, current_tilt, servo_busy
    while not exit_flag:
        if not servo_event.wait(timeout=0.5):
            continue
        servo_event.clear()
        servo_busy = True

        tp, tt = next_pan, next_tilt
        while True:
            dp, dt = tp - current_pan, tt - current_tilt
            if abs(dp) < 0.3 and abs(dt) < 0.3:
                break
            if abs(dp) >= 0.3:
                current_pan  += max(-STEP_DEG, min(STEP_DEG, dp))
            if abs(dt) >= 0.3:
                current_tilt += max(-STEP_DEG, min(STEP_DEG, dt))
            set_servo_angle(servo_pan,  current_pan)
            set_servo_angle(servo_tilt, current_tilt)
            time.sleep(STEP_SLEEP)

        current_pan, current_tilt = tp, tt
        set_servo_angle(servo_pan,  current_pan)
        set_servo_angle(servo_tilt, current_tilt)
        time.sleep(SETTLE)
        servo_pan.detach()
        servo_tilt.detach()
        servo_busy = False

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
            s = buffer.find(b'\xff\xd8')
            if s == -1: break
            e = buffer.find(b'\xff\xd9', s + 2)
            if e == -1: break
            img = cv2.imdecode(np.frombuffer(buffer[s:e+2], np.uint8), cv2.IMREAD_COLOR)
            buffer = buffer[e+2:]
            if img is not None:
                with frame_lock:
                    latest_frame = img
        if len(buffer) > 10 * 1024 * 1024:
            buffer = b''

# ==========================================
# 5. YOLO 추론 + AI 트래킹 스레드
# ==========================================
def run_inference_and_track():
    global exit_flag, output_frame, next_pan, next_tilt

    CENTER_X, CENTER_Y = 320, 240
    DEADZONE_X, DEADZONE_Y = 120, 90
    MAX_STEP_PAN, MAX_STEP_TILT = 4.0, 3.0
    PAN_GAIN, TILT_GAIN = 0.03, 0.025
    MOVE_COOLDOWN = 1.5

    smooth_cx, smooth_cy = float(CENTER_X), float(CENTER_Y)
    EMA_ALPHA = 0.3
    last_move_time = 0.0
    last_processed = None

    while not exit_flag:
        # 수동 모드면 YOLO 추론 완전 스킵 — 서보 경쟁 없음
        if manual_mode:
            with frame_lock:
                frame = latest_frame
            if frame is not None:
                f = frame.copy()
                cv2.putText(f, "MANUAL", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 165, 255), 2)
                with frame_lock:
                    output_frame = f
            time.sleep(0.05)
            last_processed = None
            smooth_cx, smooth_cy = float(CENTER_X), float(CENTER_Y)
            continue

        with frame_lock:
            frame = latest_frame
        if frame is None or frame is last_processed:
            time.sleep(0.01)
            continue
        last_processed = frame
        frame = frame.copy()

        results = model(frame, classes=list(TARGET_CLASSES.keys()))
        cv2.line(frame, (CENTER_X-20, CENTER_Y), (CENTER_X+20, CENTER_Y), (0,255,0), 2)
        cv2.line(frame, (CENTER_X, CENTER_Y-20), (CENTER_X, CENTER_Y+20), (0,255,0), 2)

        centers = []
        for box in results[0].boxes:
            cls_id = int(box.cls[0])
            conf   = float(box.conf[0])
            x1, y1, x2, y2 = map(int, box.xyxy[0])
            cx, cy = (x1+x2)//2, (y1+y2)//2
            centers.append((TARGET_CLASSES[cls_id], cx, cy, conf))
            cv2.rectangle(frame, (x1,y1), (x2,y2), (0,0,255), 2)
            cv2.putText(frame, f"{TARGET_CLASSES[cls_id]} {conf:.2f}",
                        (x1, y1-10), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,0,255), 2)
            cv2.circle(frame, (cx,cy), 5, (255,0,0), -1)

        if centers:
            centers.sort(key=lambda x: x[3], reverse=True)
            _, cx, cy, _ = centers[0]
            smooth_cx = EMA_ALPHA*cx + (1-EMA_ALPHA)*smooth_cx
            smooth_cy = EMA_ALPHA*cy + (1-EMA_ALPHA)*smooth_cy
            now = time.time()
            if now - last_move_time >= MOVE_COOLDOWN and not servo_busy:
                ex, ey = CENTER_X - smooth_cx, CENTER_Y - smooth_cy
                np_, nt_, moved = current_pan, current_tilt, False
                if abs(ex) > DEADZONE_X:
                    np_ = max(55.0, min(155.0, current_pan + max(-MAX_STEP_PAN, min(MAX_STEP_PAN, ex*PAN_GAIN))))
                    moved = True
                if abs(ey) > DEADZONE_Y:
                    nt_ = max(0.0, min(120.0, current_tilt + max(-MAX_STEP_TILT, min(MAX_STEP_TILT, -ey*TILT_GAIN))))
                    moved = True
                if moved:
                    next_pan, next_tilt = np_, nt_
                    servo_event.set()
                    last_move_time = now
        else:
            smooth_cx, smooth_cy = float(CENTER_X), float(CENTER_Y)

        cv2.putText(frame, "AUTO", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0,255,0), 2)

        with frame_lock:
            output_frame = frame

# ==========================================
# 6. 수동 조작 처리
# ==========================================
MANUAL_STEP = 5.0

def handle_control(msg):
    global manual_mode, next_pan, next_tilt
    cmd = msg.get("cmd")
    if cmd == "mode":
        manual_mode = (msg.get("val") == "manual")
        print(f"모드 변경: {'MANUAL' if manual_mode else 'AUTO'}")
    elif cmd == "move" and manual_mode:
        # 브라우저 부호 기준:
        #   pan  +1 = 오른쪽(▶),  -1 = 왼쪽(◀)
        #   tilt +1 = 위(▲),      -1 = 아래(▼)
        # 실제 서보 방향이 반대면 아래 부호 반전
        new_pan  = max(55.0,  min(155.0, current_pan  + msg.get("pan",  0) * MANUAL_STEP))
        new_tilt = max(0.0,   min(120.0, current_tilt - msg.get("tilt", 0) * MANUAL_STEP))  # tilt 부호 반전
        if new_pan != current_pan or new_tilt != current_tilt:
            next_pan, next_tilt = new_pan, new_tilt
            servo_event.set()   # 워커 스레드에 위임 — detach/settle 포함

# ==========================================
# 7. WebRTC 비디오 트랙
# ==========================================
class CameraTrack(VideoStreamTrack):
    kind = "video"

    async def recv(self):
        pts, time_base = await self.next_timestamp()
        with frame_lock:
            frame = output_frame
        img = frame.copy() if frame is not None else np.zeros((480, 640, 3), dtype=np.uint8)
        vf = VideoFrame.from_ndarray(img, format="bgr24")
        vf.pts, vf.time_base = pts, time_base
        return vf

# ==========================================
# 8. WebRTC 연결 처리
# ==========================================
ICE_CONFIG = RTCConfiguration(iceServers=[
    RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
    RTCIceServer(urls=["stun:stun1.l.google.com:19302"]),
    RTCIceServer(
        urls=["turn:openrelay.metered.ca:80"],
        username="openrelayproject",
        credential="openrelayproject"
    ),
    RTCIceServer(
        urls=["turn:openrelay.metered.ca:443"],
        username="openrelayproject",
        credential="openrelayproject"
    ),
    RTCIceServer(
        urls=["turn:openrelay.metered.ca:443?transport=tcp"],
        username="openrelayproject",
        credential="openrelayproject"
    ),
])

pcs = {}   # sessionId → RTCPeerConnection

async def handle_offer(session_id: str, sdp: str, ws):
    pc = RTCPeerConnection(configuration=ICE_CONFIG)
    pcs[session_id] = pc

    pc.addTrack(CameraTrack())

    @pc.on("datachannel")
    def on_datachannel(channel):
        @channel.on("message")
        def on_message(msg):
            try:
                handle_control(json.loads(msg))
            except Exception:
                pass

    @pc.on("connectionstatechange")
    async def on_state_change():
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            pcs.pop(session_id, None)

    await pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
    answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)

    ice_done = asyncio.Event()
    @pc.on("icegatheringstatechange")
    def on_ice_change():
        if pc.iceGatheringState == "complete":
            ice_done.set()
    if pc.iceGatheringState != "complete":
        await asyncio.wait_for(ice_done.wait(), timeout=10.0)

    await ws.send(json.dumps({
        "type":      "answer",
        "sdp":       pc.localDescription.sdp,
        "sessionId": session_id,
    }))
    print(f"Answer 전송 완료 (session: {session_id[:8]}...)")

# ==========================================
# 9. 시그널링 루프
# ==========================================
async def signaling_loop():
    while not exit_flag:
        try:
            print(f"시그널링 서버 연결 중... {SIGNAL_URL}")
            async with websockets.connect(SIGNAL_URL) as ws:
                await ws.send(json.dumps({"type": "register", "role": "pi"}))
                print("시그널링 서버 연결됨, 브라우저 대기 중...")

                async for raw in ws:
                    msg = json.loads(raw)
                    t   = msg.get("type")

                    if t == "offer":
                        sid = msg["sessionId"]
                        print(f"Offer 수신 (session: {sid[:8]}...)")
                        asyncio.create_task(handle_offer(sid, msg["sdp"], ws))

                    elif t == "bye":
                        sid = msg.get("sessionId")
                        if sid in pcs:
                            await pcs[sid].close()
                            pcs.pop(sid, None)
                            print(f"브라우저 연결 종료 (session: {sid[:8]}...)")

        except Exception as e:
            print(f"연결 실패: {e}  →  5초 후 재연결...")
            await asyncio.sleep(5)

# ==========================================
# 10. 메인
# ==========================================
if __name__ == "__main__":
    cmd = ('rpicam-vid --camera 1 --inline --nopreview -t 0 '
           '--codec mjpeg --width 640 --height 480 --framerate 30 -o -')
    process = subprocess.Popen(shlex.split(cmd),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    threading.Thread(target=read_frames,             args=(process,), daemon=True).start()
    threading.Thread(target=run_inference_and_track,                  daemon=True).start()
    threading.Thread(target=run_servo_worker,                         daemon=True).start()

    print("="*50)
    print("WebRTC 트래커 시작 (Spring Boot 시그널링)")
    print("="*50)

    try:
        asyncio.run(signaling_loop())
    except KeyboardInterrupt:
        exit_flag = True
        process.terminate()