import time
from gpiozero import Servo
from gpiozero.pins.lgpio import LGPIOFactory

# 라즈베리 파이 5 전용 핀 팩토리 설정
factory = LGPIOFactory()

# 상하 서보모터 (12번 핀) 설정 
# (펄스 폭 여유 주기: 0.6/2.4)
servo_tilt = Servo(12, pin_factory=factory, min_pulse_width=0.6/1000, max_pulse_width=2.4/1000)

# 초기화 직후 쓸데없이 들어가는 신호를 바로 차단하여 초기 움찔거림 방지
servo_tilt.detach()

def set_angle(angle):
    # 0~180도를 -1.0 ~ 1.0 스케일로 변환
    mapped_value = (angle - 90) / 90.0
    servo_tilt.value = mapped_value
    
    # 모터가 목적지에 도달할 충분한 시간 주기
    time.sleep(0.6)
    
    # 목적지 도착 후 신호 끊기 (지터링 방지)
    servo_tilt.detach()

print("=== 상하 서보모터(12번 핀) 튜닝 버전 ===")
print("종료하려면 Ctrl+C를 누르세요.\n")

try:
    while True:
        # 상하 모터는 구조물 충돌 방지를 위해 0~120도로 범위를 제한
        val = input("이동할 각도 입력 (0~120, 중심 75): ")
        angle = int(val)
        
        # 0~120 사이일 때만 움직이도록 안전장치
        if 0 <= angle <= 120:
            set_angle(angle)
        else:
            print("안전 범위를 벗어났습니다. 0에서 120 사이의 값을 입력해주세요.")

except KeyboardInterrupt:
    print("\n테스트를 종료합니다.")
except ValueError:
    print("\n숫자만 입력해주세요.")