import time
from gpiozero import Servo
from gpiozero.pins.lgpio import LGPIOFactory

# 라즈베리 파이 5 전용 핀 팩토리 설정
factory = LGPIOFactory()

# 좌우 서보모터 (13번 핀) 설정
servo_pan = Servo(13, pin_factory=factory, min_pulse_width=0.5/1000, max_pulse_width=2.5/1000)

servo_pan.detach()

def set_angle(angle):
    # 0~180도를 -1.0 ~ 1.0 스케일로 변환
    mapped_value = (angle - 90) / 90.0
    servo_pan.value = mapped_value
    time.sleep(0.3)
    servo_pan.detach() # 모터 떨림 방지

print("=== 좌우 서보모터(13번 핀) 제어 - gpiozero ===")
print("종료하려면 Ctrl+C를 누르세요.\n")

try:
    while True:
        val = input("이동할 각도 입력 (55~155, 중심 105): ")
        angle = int(val)
        
        if 0 <= angle <= 180:
            set_angle(angle)
        else:
            print("0에서 180 사이의 값을 입력해주세요.")

except KeyboardInterrupt:
    print("\n테스트를 종료합니다.")
except ValueError:
    print("\n숫자만 입력해주세요.")