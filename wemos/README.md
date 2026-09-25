# Wemos D1 mini 급식·급수 펌웨어

ESP8266에서 HX711 로드셀 2개, 급식 서보, 급수 펌프를 제어합니다.
MQTT 설정으로 예약 급식과 자동 급수를 실행하며, 급식 실행 날짜는 LittleFS에 저장합니다.

## 설정 및 빌드

1. `include/secrets.example.h`를 `include/secrets.h`로 복사합니다.
2. Wi-Fi 및 MQTT 접속 정보와 로드셀 교정값을 설정합니다. 실제 비밀번호가 들어가는 `secrets.h`는 Git에서 제외됩니다.
3. PlatformIO가 설치된 환경에서 아래 명령을 실행합니다.

```sh
cd wemos
pio run
pio run --target upload
pio device monitor
```

보드는 `d1_mini`, 시리얼 속도는 115200입니다. 부팅할 때 로드셀 영점을 잡으므로 그릇의 초기 상태를 확인하세요.

## 현재 배선

| 장치 | Wemos 핀 (GPIO) |
| --- | --- |
| 물 로드셀 DT / SCK | D6 (12) / D7 (13) |
| 사료 로드셀 DT / SCK | D5 (14) / D0 (16) |
| 급식 서보 | D4 (2) |
| 펌프 드라이버 IA / IB | D1 (5) / D2 (4) |

## MQTT

- `homecam/config`: `autoFeed`, `autoWater`, `minWater`, `maxWater`, `schedules` 설정 수신
- 예약 항목: `id`, `time` (`HH:MM`, 한국 시간), `targetWeight` (g)
- `homecam/events`: 급식·급수 결과 및 시간별 잔량 발행
- `homecam/status`: `online` / `offline` 상태

물 무게 1g은 약 1ml로 취급합니다. 자동 급수에는 수위 안정성 확인, 펄스 구동, 유량 확인, 시간 제한 및 재급수 대기 시간이 적용됩니다.

## USB 시리얼 수동 점검

실제 서보와 펌프가 작동하므로 장치를 보면서 실행하세요. 네트워크 연결 대기 중에는 명령 처리가 지연될 수 있습니다.

| 입력 | 동작 |
| --- | --- |
| `o` / `c` | 급식 서보 열기 / 닫기 |
| `g` | 사료·물 무게 출력 |
| `t` | 로드셀 영점 보정 |
| `p` / `s` | 펌프 켜기 / 끄기 |
| `?` | 도움말 |

수동 펌프는 메인 루프에서 10초 제한을 확인합니다. 실제 하드웨어 검증은 별도로 필요합니다.
