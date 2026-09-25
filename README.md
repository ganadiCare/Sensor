# Sensor

- 루트: 라즈베리파이 카메라·추적 및 하드웨어 PWM 코드
- [wemos](wemos/README.md): ESP8266 급식·급수 펌웨어
- [tests/manual](tests/manual/README.md): 라즈베리파이 수동 하드웨어 테스트

## 하드웨어 PWM 부팅 자동 설정

GPIO12/13 서보 PWM의 export와 쓰기 권한은 재부팅하면 초기화됩니다.
다음 명령을 한 번 실행하면 systemd가 부팅할 때마다 자동으로 설정합니다.

```bash
sudo bash install_hardware_pwm_service.sh
```

상태 및 부팅 로그 확인:

```bash
systemctl status hardware-pwm-setup.service
journalctl -u hardware-pwm-setup.service -b
```
