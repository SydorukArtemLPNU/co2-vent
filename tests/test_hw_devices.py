"""Моделі шини I2C (без simavr): CRC, команди SCD40, тайминги, мультиплексор, несправності."""
import unittest

from hw.devices import (CMD_GET_DATA_READY, CMD_GET_SERIAL, CMD_READ_MEASUREMENT, CMD_START_PERIODIC,
                        CMD_STOP_PERIODIC, MUX_ADDR, PERIOD_US, SCD40, SCD40_ADDR, Bus, Truth, crc8, words)

W, R = SCD40_ADDR << 1, (SCD40_ADDR << 1) | 1
MS = 1000


class Master:
    """Послідовність транзакцій, як їх робить Wire у прошивці."""

    def __init__(self, bus):
        self.bus, self.t = bus, 0

    def select(self, channel):
        assert self.bus.start(MUX_ADDR << 1, self.t)
        self.bus.write(1 << channel, self.t)
        self.bus.stop(self.t)

    def command(self, code):
        if not self.bus.start(W, self.t):
            return False
        ok = self.bus.write(code >> 8, self.t) and self.bus.write(code & 0xFF, self.t)
        self.bus.stop(self.t)
        return ok

    def read(self, n_words):
        if not self.bus.start(R, self.t):
            return None
        data = [self.bus.read() for _ in range(n_words * 3)]
        self.bus.stop(self.t)
        return data

    def wait(self, us):
        self.t += us


def decode_words(data):
    return [(data[i] << 8) | data[i + 1] for i in range(0, len(data), 3)]


class Crc(unittest.TestCase):
    def test_datasheet_example(self):
        self.assertEqual(crc8([0xBE, 0xEF]), 0x92)        # приклад із даташиту SCD4x

    def test_words_append_crc(self):
        self.assertEqual(words(0xBEEF), [0xBE, 0xEF, 0x92])


class Scd40Model(unittest.TestCase):
    def setUp(self):
        # рядок i - інтервал [i*5 с, (i+1)*5 с): чи відповідає давач і що він виміряв
        truth = Truth([(650.4, "ok"), (1012.6, "ok"), (1050.0, "ok"), (999.0, "skip"), (1100.0, "ok")])
        self.sensor = SCD40("scd40-1", truth, serial=(0x1111, 0x2222, 0x3333), crc_faults={4})
        self.m = Master(Bus([self.sensor]))
        self.m.select(0)

    def start(self):
        self.assertTrue(self.m.command(CMD_START_PERIODIC))

    def ready(self):
        self.m.command(CMD_GET_DATA_READY)
        self.m.wait(MS)
        return decode_words(self.m.read(1))[0] & 0x07FF != 0

    def measure(self):
        self.m.command(CMD_READ_MEASUREMENT)
        self.m.wait(MS)
        return self.m.read(3)

    def test_serial_number_only_in_idle(self):
        self.assertTrue(self.m.command(CMD_GET_SERIAL))
        self.m.wait(MS)
        self.assertEqual(decode_words(self.m.read(3)), [0x1111, 0x2222, 0x3333])
        self.start()
        self.assertFalse(self.m.command(CMD_GET_SERIAL))   # periodic: команда -> NACK

    def test_read_before_exec_time_is_nack(self):
        self.m.command(CMD_GET_SERIAL)
        self.assertIsNone(self.m.read(3))                  # 1 мс ще не минула

    def test_data_ready_every_5_s_and_values(self):
        self.start()
        self.m.wait(PERIOD_US - 10 * MS)
        self.assertFalse(self.ready())
        self.m.wait(10 * MS)
        self.assertTrue(self.ready())
        data = self.measure()
        co2, t_raw, rh_raw = decode_words(data)
        self.assertEqual(co2, 650)                         # округлення сценарного значення
        self.assertAlmostEqual(-45 + 175 * t_raw / 65536, 22.5, places=2)
        self.assertAlmostEqual(100 * rh_raw / 65536, 45.0, places=2)
        self.assertTrue(all(crc8(data[i:i + 2]) == data[i + 2] for i in (0, 3, 6)))
        self.assertFalse(self.ready())                     # прочитане вже не «нове»
        self.assertIsNone(self.measure())                  # без нових даних -> NACK (припущення)

    def test_stop_busy_500_ms(self):
        self.start()
        self.assertTrue(self.m.command(CMD_STOP_PERIODIC))
        self.m.wait(400 * MS)
        self.assertFalse(self.m.command(CMD_GET_SERIAL))   # ще зайнятий
        self.m.wait(100 * MS)
        self.assertTrue(self.m.command(CMD_GET_SERIAL))

    def test_skip_row_means_no_response(self):
        self.start()
        self.m.wait(3 * PERIOD_US + MS)                    # такт 3 сценарію - skip
        self.assertFalse(self.m.command(CMD_GET_DATA_READY))

    def test_reinit_does_not_rewind_scenario(self):
        self.start()
        self.m.wait(PERIOD_US + MS)
        self.assertEqual(decode_words(self.measure())[0], 650)
        self.m.command(CMD_STOP_PERIODIC)                  # прошивка переініціалізує давач
        self.m.wait(500 * MS)
        self.start()
        self.m.wait(PERIOD_US + MS)
        self.assertEqual(decode_words(self.measure())[0], 1013)   # такт 1, а не знову такт 0

    def test_crc_fault_injection(self):
        self.start()
        for _ in range(4):
            self.m.wait(PERIOD_US)
        data = self.measure()                              # 4-те вимірювання - зіпсований CRC
        self.assertNotEqual(crc8(data[0:2]), data[2])


class Mux(unittest.TestCase):
    def test_same_address_selected_by_channel(self):
        sensors = [SCD40(f"s{i}", Truth([(500.0 + i, "ok")]), serial=(i, i, i)) for i in range(3)]
        m = Master(Bus(sensors))
        self.assertFalse(m.command(CMD_GET_SERIAL))        # жоден канал не відкрито -> NACK
        for channel in range(3):
            m.select(channel)
            m.command(CMD_GET_SERIAL)
            m.wait(MS)
            self.assertEqual(decode_words(m.read(3))[0], channel)


if __name__ == "__main__":
    unittest.main()
