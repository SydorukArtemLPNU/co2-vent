#!/usr/bin/env bash
# Один хмарний прогін (ECS Fargate): сертифікати з SSM -> обробник + вебпанель :8000 ->
# прошивка Uno у simavr + моделі давачів + модель ESP8266 -> AWS IoT Core.
# Після сценарію панель ще PANEL_GRACE_S секунд доступна, далі результати йдуть у S3 і контейнер зупиняється.
# Змінні: PZIR_TIME_SCALE (1 | 10), RUN_ID, FAULTS (1 - RESET Uno, обрив Wi-Fi, збій CRC),
#         PZIR_BROKER_HOST, RESULTS_BUCKET, PANEL_GRACE_S, MAX_RUN_S (жорсткий ліміт).
set -euo pipefail
cd /app
if [ -z "${INNER:-}" ]; then              # жорсткий ліміт часу на весь прогін - захист кредитів
  INNER=1 exec timeout --signal=TERM "${MAX_RUN_S:-1500}" "$0" "$@"
fi

SCALE="${PZIR_TIME_SCALE:-10}"
RUN_ID="${RUN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
TAG="-hw-cloud${FAULTS:+-faults}"
RUN="logs/run-${RUN_ID}-x${SCALE}${TAG}"
mkdir -p "$RUN/hw"
echo "co2vent cloud run $RUN_ID scale=x$SCALE faults=${FAULTS:-0}"

[ "${CERTS_FROM_SSM:-1}" = 1 ] && python cloud/cloud_io.py fetch-certs   # локальний тест: CERTS_FROM_SSM=0 + -v certs
export PZIR_TIME_SCALE="$SCALE" PZIR_BROKER_PORT=8883 PZIR_BROKER_TLS=1 \
       PZIR_BROKER_CA=certs/AmazonRootCA1.pem \
       PZIR_BROKER_CERT=certs/server.pem.crt PZIR_BROKER_KEY=certs/server.private.pem.key \
       PZIR_NODE_CERT=certs/node.pem.crt PZIR_NODE_KEY=certs/node.private.pem.key \
       PZIR_DB="$RUN/co2vent.db" PZIR_AGENT_DB="$RUN/agent-state.db" PZIR_API_HOST=0.0.0.0

FAULT_ARGS=()
if [ -n "${FAULTS:-}" ]; then
  FAULT_ARGS+=(--crc-fault scd40-2:20)
  WIFI_DROP="$(python3 -c "print(f'{15*10/$SCALE:g}:{6*10/$SCALE:g}')")"
  RESET_AT="$(python3 -c "print(f'{42*10/$SCALE:g}')")"
fi

python processor.py >"$RUN/processor.log" 2>&1 &
python api.py >"$RUN/api.log" 2>&1 &
python -m hw.i2c_server --scenario scenarios/class_day.csv "${FAULT_ARGS[@]}" >"$RUN/hw/i2c.log" 2>&1 &
I2C_PID=$!
python -m hw.esp_bridge ${WIFI_DROP:+--wifi-drop "$WIFI_DROP"} >"$RUN/hw/esp.log" 2>&1 &
ESP_PID=$!
sleep 1
hw/sim/uno_sim hw/build/uno_node.ino.elf --speed "$SCALE" >"$RUN/hw/uno_sim.log" 2>&1 &
SIM_PID=$!
tail -n +1 -F "$RUN/processor.log" 2>/dev/null | grep --line-buffered -E "RULE|COMMAND sent|ACK|STATE|TIMEOUT" &
if [ -n "${RESET_AT:-}" ]; then
  ( sleep "$RESET_AT"; echo "RESET Uno"; kill -USR1 "$SIM_PID" ) &
fi

# кінець сценарію - за часом симуляції (uno_sim друкує "SIM t=<с>" щохвилини симульованого часу):
# якщо CPU не встигає за ×k, вузол не зупиниться передчасно; верхня межа - 3x номінальної тривалості
SIM_END="$(python3 -c "import csv; r=list(csv.DictReader(open('scenarios/class_day.csv'))); print(int(float(r[-1]['offset_s']))+15)")"
LIMIT=$(( SIM_END * 3 / SCALE + 30 ))
echo "scenario ${SIM_END}s of simulated time (≈ $(( SIM_END / SCALE ))s at x$SCALE), panel :8000 stays ${PANEL_GRACE_S:-180}s after"
for (( waited = 0; waited < LIMIT; waited += 2 )); do
  sim_t="$( { grep -o 'SIM t=[0-9]*' "$RUN/hw/uno_sim.log" 2>/dev/null || true; } | tail -1 | cut -d= -f2)"
  [ "${sim_t:-0}" -ge "$SIM_END" ] && break
  sleep 2
done
grep -E "SIM t=" "$RUN/hw/uno_sim.log" | tail -1 || true
echo "scenario done - node stopped, panel stays with results"
kill "$SIM_PID" "$ESP_PID" "$I2C_PID" 2>/dev/null || true   # вузол «вимкнено»: історія = сценарій
sleep "${PANEL_GRACE_S:-180}"

python scripts/report_runs.py logs >"$RUN/SUMMARY.md" || true
if [ -n "${RESULTS_BUCKET:-}" ]; then
  python cloud/cloud_io.py upload "$RUN" "runs/$RUN_ID"
  echo "uploaded s3://$RESULTS_BUCKET/runs/$RUN_ID/"
fi
