# 라즈베리파이 수동 테스트

자동 테스트가 아니라 연결된 센서·카메라·서보를 직접 동작시키는 점검 스크립트입니다.
기존 코드 내용은 유지하고 위치만 모았습니다. 서보 테스트는 기존 gpiozero 방식이며 현재 운영용 하드웨어 PWM 코드와 다릅니다.

저장소 루트에서 실행하세요. 객체 인식 테스트는 루트의 `yolov8n.pt`를 사용합니다.

```sh
python3 tests/manual/CDS_test.py
python3 tests/manual/detect_test.py
python3 tests/manual/servo_test.py
python3 tests/manual/servo_test2.py
```

- `CDS_test.py`: SPI 조도센서
- `detect_test.py`: 카메라 고양이·강아지 객체 인식
- `servo_test.py`: GPIO12 상하 서보
- `servo_test2.py`: GPIO13 좌우 서보

장치별 Python 의존성과 라즈베리파이 하드웨어가 필요합니다. 서보 동작 범위와 배선을 확인한 뒤 개별 실행하고, 운영 프로그램과 동시에 GPIO를 제어하지 마세요.
