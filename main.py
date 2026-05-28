import asyncio
import json
import subprocess
import shlex
import threading
import time
import uuid
import os

import cv2
import numpy as np
import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, RTCIceServer
from aiortc.mediastreams import VideoStreamTrack
from av import VideoFrame
from ultralytics import YOLO
from gpiozero import Servo, Device
from gpiozero.pins.lgpio import LGPIOFactory
from dotenv import load_dotenv

load_dotenv()

# ==========================================
# 주소 설정 
# ==========================================
SIGNAL_URL   = os.getenv("SIGNAL_URL", "ws://20.189.241.58:8080/ws/signal")
TURN_HOST    = os.getenv("TURN_HOST",  "20.189.241.58")
TURN_USER    = os.getenv("TURN_USER",  "ganadicare")
TURN_PASS    = os.getenv("TURN_PASS")

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
# 2. 서보모터 초기화 (lgpio 전역 설정 적용)
# ==========================================
# 🔥 시스템 전체의 기본 핀 팩토리를 강제로 lgpio로 고정! (경고 완벽 차단)
Device.pin_factory = LGPIOFactory()

# 이제 pin_factory 옵션을 안 적어도 알아서 적용됩니다.
servo_tilt = Servo(12, min_pulse_width=0.6/1000, max_pulse_width=2.4/1000)
servo_pan  = Servo(13, min_pulse_width=0.5/1000, max_pulse_width=2.5/1000)

current_pan  = 105.0
current_tilt = 75.0
manual_mode  = False

def set_servo_angle(servo, angle):
    servo.value = (angle - 90) / 90.0

# 초기 구동 후 지터링 방지를 위해 즉시 detach
set_servo_angle(servo_pan,  current_pan)
set_servo_angle(servo_tilt, current_tilt)
time.sleep(0.5)
servo_pan.detach()
servo_tilt.detach()

# ==========================================
# 3. 카메라 프레임 읽기 스레드
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
# 4. YOLO 추론 + AI 트래킹 스레드 (다이렉트 제어 및 간섭 차단)
# ==========================================
def run_inference_and_track():
    global exit_flag, output_frame, current_pan, current_tilt

    CENTER_X, CENTER_Y = 320, 240
    DEADZONE_X, DEADZONE_Y = 120, 90
    MAX_STEP_PAN, MAX_STEP_TILT = 4.0, 3.0
    PAN_GAIN, TILT_GAIN = 0.03, 0.025
    MOVE_COOLDOWN = 1.0

    smooth_cx, smooth_cy = float(CENTER_X), float(CENTER_Y)
    EMA_ALPHA = 0.3
    last_move_time = 0.0
    last_processed = None

    while not exit_flag:
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
            
            if now - last_move_time >= MOVE_COOLDOWN:
                ex, ey = CENTER_X - smooth_cx, CENTER_Y - smooth_cy
                pan_moved = False
                tilt_moved = False
                
                # 좌우로 움직여야 할 때만 값 갱신
                if abs(ex) > DEADZONE_X:
                    current_pan = max(55.0, min(155.0, current_pan + max(-MAX_STEP_PAN, min(MAX_STEP_PAN, ex*PAN_GAIN))))
                    pan_moved = True
                
                # 상하로 움직여야 할 때만 값 갱신
                if abs(ey) > DEADZONE_Y:
                    current_tilt = max(0.0, min(120.0, current_tilt + max(-MAX_STEP_TILT, min(MAX_STEP_TILT, -ey*TILT_GAIN))))
                    tilt_moved = True
                
                # 🔥 핵심 수정: 값이 변한(움직이는) 모터에만 전기를 쏴서 간섭/발작 차단
                if pan_moved:
                    set_servo_angle(servo_pan, current_pan)
                if tilt_moved:
                    set_servo_angle(servo_tilt, current_tilt)
                    
                if pan_moved or tilt_moved:
                    last_move_time = now
        else:
            smooth_cx, smooth_cy = float(CENTER_X), float(CENTER_Y)
            # 타겟이 화면에서 사라지고 2초가 지나면 모든 모터 전기 차단 (떨림 방지)
            if time.time() - last_move_time > 2.0:
                servo_pan.detach()
                servo_tilt.detach()

        cv2.putText(frame, "AUTO", (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0,255,0), 2)

        with frame_lock:
            output_frame = frame

# ==========================================
# 5. 수동 조작 처리 (다이렉트 이동 + Detach)
# ==========================================
def handle_control(msg):
    global manual_mode, current_pan, current_tilt
    cmd = msg.get("cmd")
    
    if cmd == "mode":
        manual_mode = (msg.get("val") == "manual")
        print(f"모드 변경: {'MANUAL' if manual_mode else 'AUTO'}")
        if not manual_mode:
            servo_pan.detach()
            servo_tilt.detach()
            
    elif cmd == "move" and manual_mode:
        MANUAL_STEP = 3.0  # 움직이는 각도
        
        pan_dir = msg.get("pan", 0)
        tilt_dir = msg.get("tilt", 0)
        
        if pan_dir == 0 and tilt_dir == 0:
            current_pan, current_tilt = 105.0, 75.0
            set_servo_angle(servo_pan, current_pan)
            set_servo_angle(servo_tilt, current_tilt)
            return

        # 🔥 수정된 부분: 방향값이 있는 모터(움직여야 하는 모터)만 신호를 보냄!
        if pan_dir != 0:
            current_pan  = max(55.0,  min(155.0, current_pan  + pan_dir * MANUAL_STEP))
            set_servo_angle(servo_pan, current_pan)
            
        if tilt_dir != 0:
            current_tilt = max(0.0,   min(120.0, current_tilt - tilt_dir * MANUAL_STEP))
            set_servo_angle(servo_tilt, current_tilt)
        
    elif cmd == "stop" and manual_mode:
        servo_pan.detach()
        servo_tilt.detach()

# ==========================================
# 6. WebRTC 비디오 트랙
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
# 7. WebRTC 연결 처리
# ==========================================
ICE_CONFIG = RTCConfiguration(iceServers=[
    RTCIceServer(
        urls=[
            f"turn:{TURN_HOST}:3478?transport=udp",
            f"turn:{TURN_HOST}:3478?transport=tcp"
        ],
        username=TURN_USER,
        credential=TURN_PASS
    ),
])

pcs = {}

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
        print(f"연결 상태: {pc.connectionState}")
        if pc.connectionState in ("failed", "closed"):
            await pc.close()
            pcs.pop(session_id, None)

    @pc.on("iceconnectionstatechange")
    async def on_ice_state():
        print(f"ICE 상태: {pc.iceConnectionState}")

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
# 8. 시그널링 루프
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
# 9. 메인
# ==========================================
if __name__ == "__main__":
    cmd = ('rpicam-vid --camera 1 --inline --nopreview -t 0 '
           '--codec mjpeg --width 640 --height 480 --framerate 30 -o -')
    process = subprocess.Popen(shlex.split(cmd),
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    threading.Thread(target=read_frames,             args=(process,), daemon=True).start()
    threading.Thread(target=run_inference_and_track,                  daemon=True).start()

    print("="*50)
    print("WebRTC 트래커 시작 (모터 튜닝 완벽 버전)")
    print("="*50)

    try:
        asyncio.run(signaling_loop())
    except KeyboardInterrupt:
        exit_flag = True
        process.terminate()