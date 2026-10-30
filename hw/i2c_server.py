"""TCP-сервер моделей шини I2C для uno_sim (протокол - див. hw/sim/uno_sim.c).

python -m hw.i2c_server --scenario scenarios/class_day.csv [--crc-fault scd40-2:30]
"""
import argparse
import logging
import socketserver

import config
from hw.devices import SCD40, Bus, Truth

log = logging.getLogger("i2c")

SERIALS = {"scd40-1": (0x9A3F, 0x07B2, 0x3B01), "scd40-2": (0x9A3F, 0x07B2, 0x3C14),
           "scd40-3": (0x9A3F, 0x07B2, 0x3D27)}   # умовні серійні номери моделей


def build_bus(scenario, crc_faults):
    sensors = [SCD40(name, Truth.from_csv(scenario, name), SERIALS[name], crc_faults.get(name, ()))
               for name in config.SENSORS]
    return Bus(sensors)


def serve_line(bus, state, line):
    parts = line.split()
    if not parts:
        return "0"
    op = parts[0]
    if op == "S":
        state["t"] = int(parts[2])
        return "1" if bus.start(int(parts[1]), state["t"]) else "0"
    if op == "W":
        return "1" if bus.write(int(parts[1]), state["t"]) else "0"
    if op == "R":
        return str(bus.read())
    if op == "P":
        state["t"] = int(parts[1])
        bus.stop(state["t"])
        return "1"
    return "0"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--scenario", default="scenarios/class_day.csv")
    parser.add_argument("--port", type=int, default=7700)
    parser.add_argument("--crc-fault", action="append", default=[],
                        help="<sensor>:<номер вимірювання> - зіпсувати CRC (можна кілька)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    faults = {}
    for spec in args.crc_fault:
        name, number = spec.split(":")
        faults.setdefault(name, set()).add(int(number))

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            bus = build_bus(args.scenario, faults)    # нове підключення = подача живлення
            state = {"t": 0}
            log.info("uno_sim connected from %s, scenario=%s faults=%s", self.client_address,
                     args.scenario, faults or "-")
            for raw in self.rfile:
                self.wfile.write((serve_line(bus, state, raw.decode().strip()) + "\n").encode())
                self.wfile.flush()
            log.info("uno_sim disconnected")

    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", args.port), Handler) as server:
        log.info("I2C bus models on 127.0.0.1:%d (TCA9548A 0x70, 3 x SCD40 0x62)", args.port)
        server.serve_forever()


if __name__ == "__main__":
    main()
