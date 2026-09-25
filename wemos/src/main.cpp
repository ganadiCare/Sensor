#include <Arduino.h>
#include <ArduinoJson.h>
#include <ESP8266WiFi.h>
#include <HX711.h>
#include <LittleFS.h>
#include <PubSubClient.h>
#include <Servo.h>
#include <time.h>
#include "secrets.h"

#define LOADCELL1_DT   12  // D6 - 물 그릇(실제 배선 기준)
#define LOADCELL1_SCK  13  // D7
#define LOADCELL2_DT   14  // D5 - 사료 그릇(실제 배선 기준)
#define LOADCELL2_SCK  16  // D0
#define SERVO_PIN       2  // D4
#define MOTOR_IA        5  // D1
#define MOTOR_IB        4  // D2

constexpr int MAX_SCHEDULES = 10;
constexpr int WEIGHT_TOLERANCE = 2;
constexpr unsigned long FEED_TIMEOUT_MS = 45UL * 1000UL;
constexpr unsigned long WATER_TIMEOUT_MS = 60UL * 1000UL;
constexpr unsigned long WATER_CHECK_MS = 60UL * 1000UL;
constexpr unsigned long WATER_REFILL_COOLDOWN_MS = 5UL * 60UL * 1000UL;
constexpr unsigned long WATER_STABILITY_MS = 5UL * 1000UL;
constexpr unsigned long WATER_LOW_RECHECK_MS = 10UL * 1000UL;
constexpr unsigned long WATER_FLOW_CHECK_MS = 5UL * 1000UL;
constexpr int WATER_STABLE_RANGE = 5;
constexpr int WATER_MIN_FLOW_INCREASE = 3;
constexpr int WATER_SUDDEN_DROP = 10;
constexpr unsigned long WATER_SUDDEN_DROP_CONFIRM_MS = 2UL * 1000UL;
constexpr unsigned long WATER_SENSOR_RETRY_MS = 2UL * 1000UL;
constexpr unsigned long WATER_PUMP_PULSE_MS = 1UL * 1000UL;
constexpr unsigned long WATER_SENSOR_SETTLE_MS = 500UL;
constexpr unsigned long MANUAL_PUMP_MAX_MS = 10UL * 1000UL;
constexpr unsigned long FOOD_STATUS_INTERVAL_MS = 60UL * 60UL * 1000UL;

struct FeedingSchedule {
  long id = 0;
  int hour = 0;
  int minute = 0;
  int targetWeight = 0;
  int lastRunDay = -1;
};

struct FeedingRunState {
  long scheduleId = 0;
  int dayKey = -1;
};

HX711 foodScale;
HX711 waterScale;
Servo foodServo;
WiFiClient wifiClient;
PubSubClient mqtt(wifiClient);

FeedingSchedule schedules[MAX_SCHEDULES];
FeedingRunState runStates[MAX_SCHEDULES];
int scheduleCount = 0;
int runStateCount = 0;
bool autoFeed = false;
bool autoWater = false;
int minWater = 0;
int maxWater = 0;
unsigned long lastMqttAttempt = 0;
unsigned long lastWaterCheck = 0;
unsigned long lastWaterRefill = 0;
unsigned long manualPumpStartedAt = 0;
bool manualPumpRunning = false;
unsigned long lastFoodStatusPublish = 0;
unsigned long lastWaterStatusPublish = 0;

String configTopic;
String eventTopic;
String statusTopic;

int lastRunDayFor(long scheduleId) {
  for (int i = 0; i < runStateCount; i++) {
    if (runStates[i].scheduleId == scheduleId) return runStates[i].dayKey;
  }
  return -1;
}

void saveRunStates() {
  if (!LittleFS.begin()) return;
  JsonDocument state;
  JsonArray items = state["items"].to<JsonArray>();
  for (int i = 0; i < runStateCount; i++) {
    JsonObject item = items.add<JsonObject>();
    item["scheduleId"] = runStates[i].scheduleId;
    item["dayKey"] = runStates[i].dayKey;
  }
  File file = LittleFS.open("/feeding-state.json", "w");
  if (file) {
    serializeJson(state, file);
    file.close();
  }
}

void rememberRunDay(long scheduleId, int dayKey) {
  for (int i = 0; i < runStateCount; i++) {
    if (runStates[i].scheduleId == scheduleId) {
      runStates[i].dayKey = dayKey;
      saveRunStates();
      return;
    }
  }
  if (runStateCount < MAX_SCHEDULES) {
    runStates[runStateCount].scheduleId = scheduleId;
    runStates[runStateCount].dayKey = dayKey;
    runStateCount++;
    saveRunStates();
  }
}

void loadRunStates() {
  if (!LittleFS.begin() || !LittleFS.exists("/feeding-state.json")) return;
  File file = LittleFS.open("/feeding-state.json", "r");
  if (!file) return;
  JsonDocument state;
  if (deserializeJson(state, file)) {
    file.close();
    return;
  }
  file.close();
  runStateCount = 0;
  for (JsonObject item : state["items"].as<JsonArray>()) {
    if (runStateCount >= MAX_SCHEDULES) break;
    runStates[runStateCount].scheduleId = item["scheduleId"] | 0L;
    runStates[runStateCount].dayKey = item["dayKey"] | -1;
    runStateCount++;
  }
}

int readFoodWeight() {
  if (!foodScale.is_ready()) return 0;
  return max(0, static_cast<int>(round(foodScale.get_units(5))));
}

bool readWaterWeightSafe(int& weight, bool allowNegativeNoise = false) {
  if (!waterScale.is_ready()) return false;
  float units = waterScale.get_units(5);
  if (isnan(units) || isinf(units)) return false;

  int measured = static_cast<int>(round(units));
  (void)allowNegativeNoise;
  weight = max(0, measured);
  return true;
}

int readWaterWeight() {
  int weight = 0;
  // 물 1g을 약 1ml로 취급한다.
  return readWaterWeightSafe(weight) ? weight : 0;
}

void motorRun() {
  digitalWrite(MOTOR_IA, HIGH);
  digitalWrite(MOTOR_IB, LOW);
}

void motorStop() {
  digitalWrite(MOTOR_IA, LOW);
  digitalWrite(MOTOR_IB, LOW);
}

// USB 시리얼에서만 실행하는 하드웨어 점검용 명령이다.
// o: 서보 열기, c: 서보 닫기, g: 로드셀 측정값 출력, t: 로드셀 영점 보정,
// p: 펌프 켜기, s: 펌프 끄기, ?: 도움말
void runManualHardwareTest(char command) {
  if (command == '\r' || command == '\n') return;

  if (command == 'o' || command == 'O') {
    Serial.println("[TEST] Feeding servo: open");
    foodServo.attach(SERVO_PIN, 500, 2500);
    foodServo.write(90);
  } else if (command == 'c' || command == 'C') {
    Serial.println("[TEST] Feeding servo: close");
    foodServo.attach(SERVO_PIN, 500, 2500);
    foodServo.write(0);
    delay(300);
    foodServo.detach();
  } else if (command == 'g' || command == 'G') {
    Serial.printf("[TEST] Food=%dg, Water=%dml\n", readFoodWeight(), readWaterWeight());
  } else if (command == 't' || command == 'T') {
    Serial.println("[TEST] Taring food and water load cells...");
    foodScale.tare(10);
    waterScale.tare(10);
    Serial.println("[TEST] Load cells zeroed");
  } else if (command == 'p' || command == 'P') {
    Serial.println("[TEST] Water pump: on (press s to stop)");
    motorRun();
    manualPumpRunning = true;
    manualPumpStartedAt = millis();
  } else if (command == 's' || command == 'S') {
    motorStop();
    manualPumpRunning = false;
    Serial.println("[TEST] Water pump: off");
  } else if (command == '?') {
    Serial.println("[TEST] o=open, c=close, g=weights, t=tare, p=pump on, s=pump off");
  }
}

void keepNetworkAlive() {
  mqtt.loop();
  yield();
}

bool readWaterWeightWithRetry(int& weight, bool allowNegativeNoise,
                              unsigned long timeoutMs) {
  unsigned long startedAt = millis();
  do {
    if (readWaterWeightSafe(weight, allowNegativeNoise)) return true;
    keepNetworkAlive();
    delay(50);
  } while (millis() - startedAt < timeoutMs);
  return false;
}

void waitWithNetwork(unsigned long durationMs) {
  unsigned long startedAt = millis();
  while (millis() - startedAt < durationMs) {
    keepNetworkAlive();
    delay(100);
  }
}

bool readStableWaterWeight(int& stableWeight) {
  int minimum = 32767;
  int maximum = -32768;
  long total = 0;
  int samples = 0;
  unsigned long startedAt = millis();

  while (millis() - startedAt < WATER_STABILITY_MS) {
    int weight = 0;
    if (!readWaterWeightWithRetry(weight, false, WATER_SENSOR_RETRY_MS)) {
      Serial.println("[WATER] Sensor unavailable for 2 seconds; refill cancelled");
      return false;
    }
    minimum = min(minimum, weight);
    maximum = max(maximum, weight);
    total += weight;
    samples++;
    keepNetworkAlive();
    delay(250);
  }

  if (samples == 0 || maximum - minimum > WATER_STABLE_RANGE) {
    Serial.printf("[WATER] Unstable level: range=%dml; refill postponed\n",
                  samples == 0 ? 0 : maximum - minimum);
    return false;
  }

  stableWeight = static_cast<int>(round(static_cast<float>(total) / samples));
  return true;
}

void publishResult(const char* type, const String& eventId, long scheduleId,
                   int target, int before, int after, int requestedAmount) {
  JsonDocument event;
  event["eventId"] = eventId;
  event["type"] = type;
  if (scheduleId > 0) event["scheduleId"] = scheduleId;
  event["target"] = target;
  event["before"] = before;
  event["after"] = after;
  // 사용자가 원한 목표량 - 기존 잔량. 실제 변화량도 진단용으로 함께 보낸다.
  event["amount"] = max(0, requestedAmount);
  event["actualAmount"] = max(0, after - before);

  char payload[384];
  size_t length = serializeJson(event, payload, sizeof(payload));
  mqtt.publish(eventTopic.c_str(), reinterpret_cast<const uint8_t*>(payload), length, false);
}

void executeFeeding(FeedingSchedule& schedule, int dayKey) {
  // 재부팅이나 설정 재수신에도 중복 급식되지 않도록 플래시에 먼저 기록한다.
  schedule.lastRunDay = dayKey;
  rememberRunDay(schedule.id, dayKey);
  int before = readFoodWeight();
  int needed = max(0, schedule.targetWeight - before);

  Serial.printf("[FEED] target=%dg, before=%dg, needed=%dg\n",
                schedule.targetWeight, before, needed);

  if (needed > WEIGHT_TOLERANCE) {
    foodServo.attach(SERVO_PIN, 500, 2500);
    foodServo.write(90);
    unsigned long startedAt = millis();

    while (millis() - startedAt < FEED_TIMEOUT_MS) {
      keepNetworkAlive();
      if (readFoodWeight() >= schedule.targetWeight - WEIGHT_TOLERANCE) break;
      delay(250);
    }

    foodServo.write(0);
    delay(500);
    foodServo.detach();
  }

  delay(1200); // 사료가 그릇에 안착할 시간
  int after = readFoodWeight();
  String eventId = String("feed-") + String(schedule.id) + "-" + String(dayKey);
  publishResult("feeding", eventId, schedule.id, schedule.targetWeight,
                before, after, needed);
}

void executeWatering() {
  if (maxWater <= minWater) {
    Serial.println("[WATER] Invalid config: maxWater must be greater than minWater");
    return;
  }

  int firstLow = 0;
  if (!readStableWaterWeight(firstLow) || firstLow >= minWater) return;

  Serial.printf("[WATER] Low level confirmed once: %dml; rechecking in 10 seconds\n",
                firstLow);
  waitWithNetwork(WATER_LOW_RECHECK_MS);

  int before = 0;
  if (!readStableWaterWeight(before) || before >= minWater) {
    Serial.println("[WATER] Low level was not stable; refill postponed");
    return;
  }

  int needed = max(0, maxWater - before);
  Serial.printf("[WATER] target=%dml, before=%dml, needed=%dml\n",
                maxWater, before, needed);

  unsigned long startedAt = millis();
  unsigned long lastFlowCheck = startedAt;
  int flowBaseline = before;
  int highestMeasured = before;
  unsigned long suddenDropStartedAt = 0;
  int latest = before;
  const char* stopReason = "timeout";

  while (millis() - startedAt < WATER_TIMEOUT_MS) {
    // Motor noise can temporarily block the HX711. Pump in short pulses and
    // measure only while the motor is stopped.
    motorRun();
    waitWithNetwork(WATER_PUMP_PULSE_MS);
    motorStop();
    waitWithNetwork(WATER_SENSOR_SETTLE_MS);

    int current = 0;
    if (!readWaterWeightWithRetry(current, true, WATER_SENSOR_RETRY_MS)) {
      stopReason = "sensor unavailable for 2 seconds";
      break;
    }
    latest = current;

    if (current >= maxWater - WEIGHT_TOLERANCE) {
      stopReason = "target reached";
      break;
    }
    highestMeasured = max(highestMeasured, current);
    if (highestMeasured - current >= WATER_SUDDEN_DROP) {
      if (suddenDropStartedAt == 0) suddenDropStartedAt = millis();
      if (millis() - suddenDropStartedAt >= WATER_SUDDEN_DROP_CONFIRM_MS) {
        stopReason = "sudden level drop sustained for 2 seconds";
        break;
      }
    } else {
      suddenDropStartedAt = 0;
    }

    unsigned long nowMs = millis();
    if (nowMs - lastFlowCheck >= WATER_FLOW_CHECK_MS) {
      if (current - flowBaseline < WATER_MIN_FLOW_INCREASE) {
        stopReason = "less than 3ml increase in 5 seconds";
        break;
      }
      flowBaseline = current;
      lastFlowCheck = nowMs;
    }

    delay(250);
  }
  motorStop();
  lastWaterRefill = millis();
  Serial.printf("[WATER] Pump stopped: %s\n", stopReason);

  waitWithNetwork(1200);
  int after = latest;
  int settled = 0;
  if (readWaterWeightSafe(settled)) after = settled;

  time_t now = time(nullptr);
  String eventId = String("water-") + String(static_cast<unsigned long>(now));
  publishResult("watering", eventId, 0, maxWater, before, after, needed);
}

void applyConfig(const byte* payload, unsigned int length) {
  JsonDocument config;
  DeserializationError error = deserializeJson(config, payload, length);
  if (error) {
    Serial.printf("Config JSON error: %s\n", error.c_str());
    return;
  }

  autoFeed = config["autoFeed"] | false;
  autoWater = config["autoWater"] | false;
  minWater = max(0, config["minWater"] | 0);
  maxWater = max(0, config["maxWater"] | 0);
  // Apply a newly received water configuration immediately.
  lastWaterCheck = 0;

  scheduleCount = 0;
  for (JsonObject item : config["schedules"].as<JsonArray>()) {
    if (scheduleCount >= MAX_SCHEDULES) break;
    const char* timeText = item["time"] | "";
    int hour = 0;
    int minute = 0;
    if (sscanf(timeText, "%d:%d", &hour, &minute) != 2) continue;

    schedules[scheduleCount].id = item["id"] | 0L;
    schedules[scheduleCount].hour = hour;
    schedules[scheduleCount].minute = minute;
    schedules[scheduleCount].targetWeight = max(0, item["targetWeight"] | 0);
    schedules[scheduleCount].lastRunDay =
        lastRunDayFor(schedules[scheduleCount].id);
    scheduleCount++;
  }

  Serial.printf("Config applied: feed=%s, water=%s, min=%dml, max=%dml, schedules=%d\n",
                autoFeed ? "on" : "off", autoWater ? "on" : "off",
                minWater, maxWater, scheduleCount);
}

void mqttCallback(char* topic, byte* payload, unsigned int length) {
  if (configTopic.equals(topic)) applyConfig(payload, length);
}

void connectWifi() {
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  Serial.print("Wi-Fi connecting");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.printf("\nWi-Fi connected: %s\n", WiFi.localIP().toString().c_str());
}

void connectMqttIfNeeded() {
  if (mqtt.connected() || millis() - lastMqttAttempt < 5000) return;
  lastMqttAttempt = millis();

  String clientId = String("homecam-") + String(ESP.getChipId(), HEX);
  bool connected = mqtt.connect(clientId.c_str(), MQTT_USERNAME, MQTT_PASSWORD,
                                statusTopic.c_str(), 1, true, "offline");
  if (!connected) {
    Serial.printf("MQTT failed, state=%d\n", mqtt.state());
    return;
  }

  mqtt.publish(statusTopic.c_str(), "online", true);
  mqtt.subscribe(configTopic.c_str(), 1);
  Serial.println("MQTT connected");
}

void checkSchedules() {
  if (!autoFeed) return;

  time_t now = time(nullptr);
  if (now < 1700000000) return; // NTP 동기화 전
  struct tm localTime;
  localtime_r(&now, &localTime);
  int dayKey = (localTime.tm_year + 1900) * 10000
             + (localTime.tm_mon + 1) * 100
             + localTime.tm_mday;

  for (int i = 0; i < scheduleCount; i++) {
    FeedingSchedule& schedule = schedules[i];
    if (schedule.hour == localTime.tm_hour
        && schedule.minute == localTime.tm_min
        && schedule.lastRunDay != dayKey) {
      executeFeeding(schedule, dayKey);
    }
  }
}

void publishHourlyFoodStatus() {
  if (!mqtt.connected()) return;

  unsigned long nowMs = millis();
  if (lastFoodStatusPublish != 0
      && nowMs - lastFoodStatusPublish < FOOD_STATUS_INTERVAL_MS) return;

  time_t now = time(nullptr);
  if (now < 1700000000) return;

  int current = readFoodWeight();
  String eventId = String("food-status-")
      + String(static_cast<unsigned long>(now / 3600));
  publishResult("food-status", eventId, 0, current, current, current, 0);
  lastFoodStatusPublish = nowMs;
  Serial.printf("[FOOD] Hourly status published: %dg\n", current);
}

void publishHourlyWaterStatus() {
  if (!mqtt.connected()) return;

  unsigned long nowMs = millis();
  if (lastWaterStatusPublish != 0
      && nowMs - lastWaterStatusPublish < FOOD_STATUS_INTERVAL_MS) return;

  time_t now = time(nullptr);
  if (now < 1700000000) return;

  int current = 0;
  if (!readWaterWeightWithRetry(current, false, WATER_SENSOR_RETRY_MS)) {
    Serial.println("[WATER] Hourly status skipped: sensor unavailable");
    return;
  }

  String eventId = String("water-status-")
      + String(static_cast<unsigned long>(now / 3600));
  publishResult("water-status", eventId, 0, current, current, current, 0);
  lastWaterStatusPublish = nowMs;
  Serial.printf("[WATER] Hourly status published: %dml\n", current);
}

void checkWatering() {
  if (!autoWater || !mqtt.connected() || manualPumpRunning) return;

  unsigned long nowMs = millis();
  if (lastWaterCheck != 0 && nowMs - lastWaterCheck < WATER_CHECK_MS) return;
  lastWaterCheck = nowMs;

  Serial.printf("[WATER] Starting safety check: min=%dml, max=%dml\n",
                minWater, maxWater);

  if (lastWaterRefill != 0
      && nowMs - lastWaterRefill < WATER_REFILL_COOLDOWN_MS) {
    unsigned long remainingSeconds =
        (WATER_REFILL_COOLDOWN_MS - (nowMs - lastWaterRefill)) / 1000UL;
    Serial.printf("[WATER] Refill cooldown: %lus remaining\n", remainingSeconds);
    return;
  }

  executeWatering();
}

void setup() {
  Serial.begin(115200);
  Serial.println("[WATER] Safety firmware v4: pulse=1s, settle=0.5s, check=60s");
  loadRunStates();
  configTopic = "homecam/config";
  eventTopic = "homecam/events";
  statusTopic = "homecam/status";

  // 실제 배선은 기존 이름과 반대다. 음수 scale로 올려놓는 무게가 양수가 되게 반전한다.
  foodScale.begin(LOADCELL2_DT, LOADCELL2_SCK);
  waterScale.begin(LOADCELL1_DT, LOADCELL1_SCK);
  foodScale.set_scale(-FOOD_SCALE_FACTOR);
  waterScale.set_scale(-WATER_SCALE_FACTOR);
  delay(1000);
  foodScale.tare();
  waterScale.tare();

  foodServo.attach(SERVO_PIN, 500, 2500);
  foodServo.write(0);
  delay(500);
  foodServo.detach();
  pinMode(MOTOR_IA, OUTPUT);
  pinMode(MOTOR_IB, OUTPUT);
  motorStop();

  connectWifi();
  // KST(UTC+9), 일광절약시간 없음
  configTime(9 * 3600, 0, "pool.ntp.org", "time.google.com");

  mqtt.setServer(MQTT_HOST, MQTT_PORT);
  mqtt.setCallback(mqttCallback);
  mqtt.setBufferSize(2048);
  mqtt.setKeepAlive(30);
}

void loop() {
  if (Serial.available()) {
    runManualHardwareTest(static_cast<char>(Serial.read()));
  }

  if (manualPumpRunning && millis() - manualPumpStartedAt >= MANUAL_PUMP_MAX_MS) {
    motorStop();
    manualPumpRunning = false;
    Serial.println("[TEST] Water pump: auto-stopped after 10 seconds");
  }

  if (WiFi.status() != WL_CONNECTED) {
    connectWifi();
  }
  connectMqttIfNeeded();
  mqtt.loop();
  checkSchedules();
  publishHourlyFoodStatus();
  publishHourlyWaterStatus();

  checkWatering();

  delay(100);
}
