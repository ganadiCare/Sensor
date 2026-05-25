from flask import Flask, Response, render_template_string
import subprocess
import shlex
import threading
import time

app = Flask(__name__)

# 전역 변수
latest_frame = None
frame_lock = threading.Lock()
process = None
exit_flag = False


# ==========================================
# 카메라 실행 명령어 (주간 카메라: imx708 = camera 1)
# ==========================================
def build_camera_cmd():
    return (
        'rpicam-vid --camera 1 --inline --nopreview -t 0 '
        '--codec mjpeg --width 640 --height 480 --framerate 15 '
        '--quality 50 '
        '-o -'
    )


# ==========================================
# 카메라 프로세스 시작
# ==========================================
def start_camera():
    global process
    cmd = build_camera_cmd()
    process = subprocess.Popen(
        shlex.split(cmd),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL  #디버깅용
    )
    return process


# ==========================================
# 스레드: 카메라에서 영상 읽어오기
# ==========================================
def read_frames():
    global latest_frame, exit_flag
    buffer = b''

    while not exit_flag:
        if process is None:
            time.sleep(0.1)
            continue

        chunk = process.stdout.read(4096)
        if not chunk:
            # 프로세스가 죽었는지 체크 후 재시작
            if process.poll() is not None:
                print("⚠️ 카메라 프로세스가 종료됨! 재시작합니다.")
                start_camera()
                buffer = b''
            time.sleep(0.01)
            continue
        buffer += chunk

        # 버퍼 안에서 완성된 JPEG 프레임을 순서대로 모두 추출
        while True:
            start = buffer.find(b'\xff\xd8')          # JPEG 시작 마커
            if start == -1:
                break
            end = buffer.find(b'\xff\xd9', start + 2)  # JPEG 끝 마커
            if end == -1:
                break  # 아직 프레임이 다 안 들어옴

            jpg = buffer[start:end + 2]
            buffer = buffer[end + 2:]

            with frame_lock:
                latest_frame = jpg

        # 버퍼 비대화 방지
        if len(buffer) > 10 * 1024 * 1024:
            buffer = b''


# ==========================================
# 웹 스트리밍
# ==========================================
def generate_video_stream():
    last_sent = None
    while True:
        with frame_lock:
            frame = latest_frame
        # 새 프레임이 없으면 잠깐 대기 (중복 전송 방지)
        if frame is None or frame is last_sent:
            time.sleep(0.005)
            continue
        last_sent = frame

        yield (b'--frame\r\n'
               b'Content-Type: image/jpeg\r\n\r\n' + frame + b'\r\n')


@app.route('/')
def index():
    html = """
    <html>
      <head>
        <title>HomeCam</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
      </head>
      <body style="background-color:#222; color:white; text-align:center; font-family:sans-serif; margin:0; padding:20px;">
        <h2>🏠 HomeCam</h2>
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
    start_camera()
    threading.Thread(target=read_frames, daemon=True).start()

    print("\n" + "=" * 50)
    print("🚀 카메라 서버 시작!")
    print("🌐 브라우저에서 접속:")
    print("👉 http://[라즈베리파이_IP]:5000")
    print("=" * 50 + "\n")

    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)