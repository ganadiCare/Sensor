#pragma once

// 이 파일을 secrets.h로 복사한 뒤 실제 값을 입력하세요.
// secrets.h는 .gitignore에 포함되어 GitHub에 올라가지 않습니다.
#define WIFI_SSID       "YOUR_WIFI_SSID"
#define WIFI_PASSWORD   "YOUR_WIFI_PASSWORD"

#define MQTT_HOST       "YOUR_MQTT_HOST"
#define MQTT_PORT       1883
#define MQTT_USERNAME   "YOUR_MQTT_USERNAME"
#define MQTT_PASSWORD   "YOUR_MQTT_PASSWORD"

// 로드셀 교정값. 실제 추를 올려 각각 보정해야 합니다.
#define FOOD_SCALE_FACTOR   2280.0f
#define WATER_SCALE_FACTOR  638.4f
