#!/usr/bin/env bash
# Точка входу контейнера hw: компіляція прошивки -> моделі I2C -> simavr (Uno) -> емулятор ESP8266.
# Змінні: PZIR_TIME_SCALE (швидкість симуляції), HW_SCENARIO, HW_CRC_FAULTS ("scd40-2:30 scd40-1:55"),
#         HW_WIFI_DROP ("<через с>:<на с>"), HW_LOG_DIR (журнали; за замовчуванням logs/hw-<час>)
set -euo pipefail
cd /app
SCALE="${PZIR_TIME_SCALE:-1}"
LOGS="${HW_LOG_DIR:-logs/hw-$(date -u +%Y%m%dT%H%M%SZ)-x${SCALE}}"
mkdir -p "$LOGS" hw/build
echo "hw: logs in $LOGS"

arduino-cli compile --fqbn arduino:avr:uno --output-dir hw/build hw/firmware/uno_node >"$LOGS/firmware-build.log" 2>&1 \
  || { cat "$LOGS/firmware-build.log"; exit 1; }
grep -E "^(Sketch|Global)" "$LOGS/firmware-build.log"
make -s -C hw/sim

FAULTS=()
for f in ${HW_CRC_FAULTS:-}; do FAULTS+=(--crc-fault "$f"); done
python -m hw.i2c_server --scenario "${HW_SCENARIO:-scenarios/class_day.csv}" "${FAULTS[@]}" >"$LOGS/i2c.log" 2>&1 &
P_I2C=$!
python -m hw.esp_bridge ${HW_WIFI_DROP:+--wifi-drop "$HW_WIFI_DROP"} >"$LOGS/esp.log" 2>&1 &
P_ESP=$!
trap 'kill $P_I2C $P_ESP ${P_SIM:-} 2>/dev/null || true' EXIT TERM INT
sleep 0.5
hw/sim/uno_sim hw/build/uno_node.ino.elf --speed "$SCALE" >"$LOGS/uno_sim.log" 2>&1 &
P_SIM=$!
echo "$P_SIM" >/tmp/uno_sim.pid
echo "hw: running (uno_sim pid $P_SIM; RESET Uno: kill -USR1 \$(cat /tmp/uno_sim.pid))"
wait -n $P_I2C $P_ESP $P_SIM
echo "hw: a component exited"; tail -5 "$LOGS"/*.log
