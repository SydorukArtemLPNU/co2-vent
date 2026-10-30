"""Моделі I2C-пристроїв вузла: TCA9548A і SCD40 (байтовий рівень, симульований час).

Що відтворено з даташиту SCD4x (Sensirion):
  - адреса 0x62; команди 16-біт старшим байтом уперед; відповіді - слова по 2 байти + CRC-8
    (поліном 0x31, початкове 0xFF; CRC(0xBEEF) = 0x92);
  - start_periodic_measurement 0x21B1 - нове вимірювання кожні 5 с;
    stop_periodic_measurement 0x3F86 - 500 мс, поки давач не приймає команд;
    get_data_ready_status 0xE4B8 (1 мс) - молодші 11 біт != 0 означають «є дані»;
    read_measurement 0xEC05 (1 мс) - CO2 ppm, T = -45 + 175*w/2^16, RH = 100*w/2^16;
    get_serial_number 0x3682 (1 мс, лише в режимі idle);
  - у periodic-режимі давач приймає лише read/get_data_ready/stop - інші команди NACK.
Припущення моделі (позначено, перевірити на реальному давачі):
  - читання до завершення часу виконання команди або без нової команди -> NACK адреси;
  - read_measurement без нових даних -> NACK адреси.
Значення CO2 не вимірюються: їх задає сценарій (один такт на вимірювання), тому source=simulated.
"""
import csv
import logging

log = logging.getLogger("i2c")

MUX_ADDR, SCD40_ADDR = 0x70, 0x62
PERIOD_US = 5_000_000
STOP_BUSY_US = 500_000
EXEC_US = 1_000

CMD_START_PERIODIC = 0x21B1
CMD_STOP_PERIODIC = 0x3F86
CMD_READ_MEASUREMENT = 0xEC05
CMD_GET_DATA_READY = 0xE4B8
CMD_GET_SERIAL = 0x3682
PERIODIC_ALLOWED = {CMD_READ_MEASUREMENT, CMD_GET_DATA_READY, CMD_STOP_PERIODIC}
KNOWN = PERIODIC_ALLOWED | {CMD_START_PERIODIC, CMD_GET_SERIAL}


def crc8(data):
    crc = 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x31) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def words(*values):
    out = []
    for value in values:
        pair = [(value >> 8) & 0xFF, value & 0xFF]
        out += pair + [crc8(pair)]
    return out


class Truth:
    """Значення сценарію для давача: k-те вимірювання = k-тий рядок цього давача."""

    def __init__(self, rows):
        self.rows = rows            # [(value, quality)]

    @classmethod
    def from_csv(cls, path, sensor_id):
        with open(path, encoding="utf-8") as fh:
            rows = [(float(r["value"]), r["quality"]) for r in csv.DictReader(fh)
                    if r["sensor_id"] == sensor_id and r["quality"] != "duplicate"]
        return cls(rows)

    def at(self, index):
        if not self.rows:
            return 600.0, "ok"
        return self.rows[min(index, len(self.rows) - 1)]


class SCD40:
    def __init__(self, name, truth, serial=(0x0000, 0x0000, 0x0000), crc_faults=(),
                 temperature_c=22.5, humidity_pct=45.0):
        self.name = name
        self.truth = truth
        self.serial = serial
        self.crc_faults = set(crc_faults)      # такти (від подачі живлення) із зіпсованим CRC
        self.temperature_c = temperature_c
        self.humidity_pct = humidity_pct
        self.periodic_since = None
        self.epoch = None                       # перший start після подачі живлення - вісь сценарію
        self.busy_until = 0
        self.consumed = 0                       # скільки вимірювань уже прочитано
        self.response = None                    # (готове після, байти) для наступного читання
        self.rx = []
        self.reading = False

    # --- стан ---
    def available(self, t_us):
        """Кількість вимірювань, що вже з'явились від старту periodic-режиму."""
        if self.periodic_since is None:
            return 0
        return max(0, (t_us - self.periodic_since) // PERIOD_US)

    def tick(self, t_us):
        """Номер такту сценарію; рахується від першого старту, тож повторна ініціалізація
        давача прошивкою не «перемотує» сценарій."""
        return None if self.epoch is None else (t_us - self.epoch) // PERIOD_US

    def connected(self, t_us):
        """skip у сценарії = давач не відповідає (обрив, відсутнє живлення) на цьому такті."""
        index = self.tick(t_us)
        return index is None or self.truth.at(index)[1] != "skip"

    # --- шина ---
    def address(self, read, t_us):
        if not self.connected(t_us) or t_us < self.busy_until:
            return False
        if read:
            if self.response is None or t_us < self.response[0]:
                return False
            self.reading = True
            return True
        self.rx = []
        return True

    def write(self, byte, t_us):
        self.rx.append(byte)
        if len(self.rx) == 2:
            command = (self.rx[0] << 8) | self.rx[1]
            if command not in KNOWN:
                return False
            if self.periodic_since is not None and command not in PERIODIC_ALLOWED:
                return False
        return True

    def read(self):
        if not self.response or not self.response[1]:
            return 0xFF
        return self.response[1].pop(0)

    def stop(self, t_us):
        """Кінець транзакції: після читання відповідь вичерпано, після запису - виконати команду."""
        if self.reading:
            self.reading, self.response = False, None
            return
        if len(self.rx) < 2:
            self.rx = []
            return
        command = (self.rx[0] << 8) | self.rx[1]
        self.rx = []
        self.response = None
        if command == CMD_START_PERIODIC and self.periodic_since is None:
            self.periodic_since, self.consumed = t_us, 0
            if self.epoch is None:
                self.epoch = t_us
        elif command == CMD_STOP_PERIODIC:
            self.periodic_since = None
            self.busy_until = t_us + STOP_BUSY_US
        elif command == CMD_GET_SERIAL and self.periodic_since is None:
            self.response = (t_us + EXEC_US, words(*self.serial))
        elif command == CMD_GET_DATA_READY:
            ready = self.available(t_us) > self.consumed
            self.response = (t_us + EXEC_US, words(0x8006 if ready else 0x8000))
        elif command == CMD_READ_MEASUREMENT and self.available(t_us) > self.consumed:
            self.consumed = self.available(t_us)
            measured_at = self.periodic_since + self.consumed * PERIOD_US   # останнє готове
            value, _ = self.truth.at(self.tick(measured_at) - 1)
            co2 = max(0, min(0xFFFF, round(value)))
            t_raw = round((self.temperature_c + 45) * 65536 / 175)
            rh_raw = round(self.humidity_pct * 65536 / 100)
            data = words(co2, t_raw, rh_raw)
            if self.tick(measured_at) in self.crc_faults:   # номер такту від подачі живлення
                data[2] ^= 0x01                     # пошкоджений CRC першого слова
                log.info("FAULT %s crc corrupted on tick %d", self.name, self.tick(measured_at))
            self.response = (t_us + EXEC_US, data)


class TCA9548A:
    def __init__(self):
        self.mask = 0

    def address(self, read, t_us):
        return True

    def write(self, byte, t_us):
        self.mask = byte
        return True

    def read(self):
        return self.mask

    def stop(self, t_us):
        pass


class Bus:
    """Шина за мультиплексором: давач k підключено до каналу k."""

    def __init__(self, sensors):
        self.mux = TCA9548A()
        self.sensors = list(sensors)
        self.target = None

    def _device(self, addr7):
        if addr7 == MUX_ADDR:
            return self.mux
        if addr7 == SCD40_ADDR:
            # кілька відкритих каналів з однаковою адресою - конфлікт; модель бере перший
            for channel, sensor in enumerate(self.sensors):
                if self.mux.mask & (1 << channel):
                    return sensor
        return None

    def start(self, addr8, t_us):
        device = self._device(addr8 >> 1)
        ok = device is not None and device.address(bool(addr8 & 1), t_us)
        self.target = device if ok else None
        return ok

    def write(self, byte, t_us):
        return bool(self.target) and self.target.write(byte, t_us)

    def read(self):
        return self.target.read() if self.target else 0xFF

    def stop(self, t_us):
        if self.target:
            self.target.stop(t_us)
        self.target = None
