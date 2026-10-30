// Вузол «Вентиляція класу» (ПЗІР, курсова, варіант 20) - прошивка Arduino Uno Rev3.
//
// I2C: TCA9548A (0x70), канали 0..2 -> три SCD40 (кожен на 0x62 - адреса фіксована).
// UART0 9600 8N1 -> ESP8266 (міст Wi-Fi/MQTT). На реальній платі UART0 спільний з USB:
// під час прошивання від'єднати RX/TX ESP.
// Реле вентилятора - D7 (через модуль реле/транзистор), дубль стану - вбудований LED D13.
//
// Протокол рядків (до 48 символів, '\n'):
//   Uno -> ESP: B,uno_node,<версія>                    плата (пере)запустилась і готова до команд
//               I,<канал>,<серійний номер hex>|fail    ініціалізація давача
//               S,<seq>,<канал>,<co2 ppm>,<t*100>,<rh*100>
//               E,<канал>,nack|crc|mux                  перехід давача у стан помилки
//               A,<command_id>,applied,<0|1>            застосовано (або повтор id)
//               X,badline|overflow                      відхилений вхідний рядок
//               H,<uptime с>,<0|1>                       heartbeat раз на 10 с: живий + стан реле
//   ESP -> Uno: C,<command_id>,<0|1>
// Та сама прошивка виконується в емуляторі simavr і на реальній платі.

#include <Wire.h>
#include <avr/sleep.h>

const char FW_VERSION[] = "3";
const uint8_t MUX_ADDR = 0x70;
const uint8_t SCD40_ADDR = 0x62;
const uint8_t SENSORS = 3;
const uint8_t RELAY_PIN = 7;
const uint8_t LED_PIN = LED_BUILTIN;
const unsigned long POLL_MS = 1000;        // опитування get_data_ready
const unsigned long RECOVER_MS = 10000;    // повторна ініціалізація давача з помилкою
const unsigned long HEARTBEAT_MS = 10000;  // ESP може пропустити рядок B (стартує пізніше Uno)
const uint8_t LINE_MAX = 48;
const uint8_t ID_MAX = 24;

// Команди SCD4x (даташит Sensirion)
const uint16_t CMD_START_PERIODIC = 0x21B1;
const uint16_t CMD_STOP_PERIODIC = 0x3F86;
const uint16_t CMD_READ_MEASUREMENT = 0xEC05;
const uint16_t CMD_GET_DATA_READY = 0xE4B8;
const uint16_t CMD_GET_SERIAL = 0x3682;

enum ReadResult : uint8_t { READ_OK = 0, READ_NACK = 1, READ_CRC = 2 };

uint32_t seq = 0;
bool sensorOk[SENSORS];
unsigned long lastPoll = 0;
unsigned long lastHeartbeat = 0;
unsigned long failedAt[SENSORS];
char line[LINE_MAX + 1];
uint8_t lineLen = 0;
bool overflow = false;
char lastId[ID_MAX + 1] = "";
uint8_t ventilation = 0;

uint8_t crc8(const uint8_t *data, uint8_t len) {
  uint8_t crc = 0xFF;                       // CRC-8, поліном 0x31, як у SCD4x
  for (uint8_t i = 0; i < len; i++) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; bit++) {
      crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x31) : (uint8_t)(crc << 1);
    }
  }
  return crc;
}

bool selectChannel(uint8_t channel) {
  Wire.beginTransmission(MUX_ADDR);
  Wire.write((uint8_t)(1 << channel));
  return Wire.endTransmission() == 0;
}

bool sendCommand(uint16_t command) {
  Wire.beginTransmission(SCD40_ADDR);
  Wire.write((uint8_t)(command >> 8));
  Wire.write((uint8_t)(command & 0xFF));
  return Wire.endTransmission() == 0;
}

// n слів по 3 байти (2 дані + CRC)
ReadResult readWords(uint16_t *out, uint8_t n) {
  uint8_t want = n * 3;
  uint8_t got = Wire.requestFrom(SCD40_ADDR, want);
  if (got != want) {
    while (Wire.available()) Wire.read();
    return READ_NACK;
  }
  ReadResult result = READ_OK;
  for (uint8_t i = 0; i < n; i++) {
    uint8_t b[3];
    for (uint8_t j = 0; j < 3; j++) b[j] = Wire.read();
    if (crc8(b, 2) != b[2]) result = READ_CRC;
    out[i] = ((uint16_t)b[0] << 8) | b[1];
  }
  return result;
}

void markFailed(uint8_t channel, const char *reason) {
  if (sensorOk[channel]) {                  // повідомляємо лише про перехід у помилку
    Serial.print(F("E,")); Serial.print(channel); Serial.print(','); Serial.println(reason);
  }
  sensorOk[channel] = false;
  failedAt[channel] = millis();
}

bool initSensor(uint8_t channel) {
  uint16_t serial[3];
  bool ok = selectChannel(channel)
            && sendCommand(CMD_STOP_PERIODIC);  // давач міг лишитися в periodic після скидання Uno
  if (ok) {
    delay(500);                             // stop_periodic_measurement: 500 мс
    ok = sendCommand(CMD_GET_SERIAL);
  }
  if (ok) {
    delay(1);
    ok = readWords(serial, 3) == READ_OK;
  }
  if (ok) ok = sendCommand(CMD_START_PERIODIC);
  Serial.print(F("I,")); Serial.print(channel); Serial.print(',');
  if (ok) {
    for (uint8_t i = 0; i < 3; i++) {
      if (serial[i] < 0x1000) Serial.print('0');
      if (serial[i] < 0x100) Serial.print('0');
      if (serial[i] < 0x10) Serial.print('0');
      Serial.print(serial[i], HEX);
    }
    Serial.println();
  } else {
    Serial.println(F("fail"));
  }
  sensorOk[channel] = ok;
  if (!ok) failedAt[channel] = millis();
  return ok;
}

void pollSensor(uint8_t channel) {
  if (!selectChannel(channel)) { markFailed(channel, "mux"); return; }
  uint16_t status;
  if (!sendCommand(CMD_GET_DATA_READY)) { markFailed(channel, "nack"); return; }
  delay(1);
  ReadResult r = readWords(&status, 1);
  if (r != READ_OK) { markFailed(channel, r == READ_CRC ? "crc" : "nack"); return; }
  if ((status & 0x07FF) == 0) return;       // нового вимірювання ще немає
  uint16_t w[3];
  if (!sendCommand(CMD_READ_MEASUREMENT)) { markFailed(channel, "nack"); return; }
  delay(1);
  r = readWords(w, 3);
  if (r == READ_CRC) {                      // поодинокий збій CRC: значення відкидаємо,
    Serial.print(F("E,")); Serial.print(channel); Serial.println(F(",crc"));
    return;                                 // давач лишається робочим
  }
  if (r != READ_OK) { markFailed(channel, "nack"); return; }
  long t100 = -4500L + (long)((17500UL * w[1]) >> 16);
  long rh100 = (long)((10000UL * w[2]) >> 16);
  Serial.print(F("S,")); Serial.print(++seq); Serial.print(',');
  Serial.print(channel); Serial.print(','); Serial.print(w[0]); Serial.print(',');
  Serial.print(t100); Serial.print(','); Serial.println(rh100);
}

bool validId(const char *id, uint8_t len) {
  if (len == 0 || len > ID_MAX) return false;
  for (uint8_t i = 0; i < len; i++) {
    char c = id[i];
    if (!(isalnum(c) || c == '-' || c == '_')) return false;
  }
  return true;
}

void handleLine() {
  // C,<id>,<0|1>
  char *comma = (lineLen > 2 && line[0] == 'C' && line[1] == ',') ? strchr(line + 2, ',') : NULL;
  if (comma == NULL || !validId(line + 2, comma - (line + 2)) || strlen(comma + 1) != 1
      || (comma[1] != '0' && comma[1] != '1')) {
    Serial.println(F("X,badline"));
    return;
  }
  *comma = '\0';
  const char *id = line + 2;
  if (strcmp(id, lastId) != 0) {            // повтор останнього id не виконуємо вдруге
    ventilation = comma[1] == '1';
    digitalWrite(RELAY_PIN, ventilation ? HIGH : LOW);
    digitalWrite(LED_PIN, ventilation ? HIGH : LOW);
    strncpy(lastId, id, ID_MAX);
    lastId[ID_MAX] = '\0';
  }
  Serial.print(F("A,")); Serial.print(id); Serial.print(F(",applied,")); Serial.println(ventilation);
}

void readSerial() {
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n') {
      line[lineLen] = '\0';
      if (overflow) Serial.println(F("X,overflow"));
      else if (lineLen > 0) handleLine();
      lineLen = 0;
      overflow = false;
    } else if (ch != '\r') {
      if (lineLen < LINE_MAX) line[lineLen++] = ch;
      else overflow = true;                 // довгий кадр відкидається до '\n'
    }
  }
}

void setup() {
  pinMode(RELAY_PIN, OUTPUT);
  pinMode(LED_PIN, OUTPUT);
  digitalWrite(RELAY_PIN, LOW);             // безпечний стан після скидання - вимкнено
  digitalWrite(LED_PIN, LOW);
  Serial.begin(9600);
  Wire.begin();
  Wire.setClock(100000);
  Wire.setWireTimeout(25000, true);         // не зависати на шині, що "залипла"
  for (uint8_t ch = 0; ch < SENSORS; ch++) initSensor(ch);   // ~1,5 с: stop_periodic по 500 мс
  // B - наприкінці: команда, надіслана до готовності, чекала б у буфері UART довше за тайм-аут ESP
  Serial.print(F("B,uno_node,")); Serial.println(FW_VERSION);
  lastPoll = millis();
}

void loop() {
  readSerial();
  unsigned long now = millis();
  if (now - lastPoll >= POLL_MS) {
    lastPoll = now;
    for (uint8_t ch = 0; ch < SENSORS; ch++) {
      if (sensorOk[ch]) pollSensor(ch);
      else if (now - failedAt[ch] >= RECOVER_MS) initSensor(ch);
    }
  }
  if (now - lastHeartbeat >= HEARTBEAT_MS) {
    lastHeartbeat = now;
    Serial.print(F("H,")); Serial.print(now / 1000); Serial.print(','); Serial.println(ventilation);
  }
  set_sleep_mode(SLEEP_MODE_IDLE);          // до наступного переривання (Timer0 / UART)
  sleep_mode();
}
