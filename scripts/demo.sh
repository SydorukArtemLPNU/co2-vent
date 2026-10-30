#!/usr/bin/env bash
# Наскрізний прогін сценарію: обробник + агент + API + симулятор, журнали в logs/<run>/.
# Брокер має вже працювати: docker compose up -d
# Використання: scripts/demo.sh [time_scale=10] [scenario=scenarios/class_day.csv]
# Відмовні режими (змінні середовища):
#   NO_AGENT=1            - агент не запускається: команди мають перейти в expired (T7)
#   BROKER_RESTART_AT=20  - через N с після старту симулятора перезапустити брокер (T8)
#   LIMIT=60              - відтворити лише перші N рядків сценарію
# Емульоване залізо (Uno у simavr + 3 x SCD40 + ESP8266, контейнер hw) замість agent.py/simulator.py:
#   HW=1                  - увімкнути; журнали заліза в logs/<run>/hw/
#   HW_RESET_AT=30        - через N с натиснути RESET на Uno
#   HW_WIFI_DROP=20:8     - через 20 с обрив Wi-Fi ESP на 8 с
#   HW_CRC_FAULTS="scd40-2:30" - зіпсувати CRC 30-го вимірювання давача
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python
export PZIR_TIME_SCALE="${1:-10}"
SCENARIO="${2:-scenarios/class_day.csv}"
TAG="${HW:+-hw}${NO_AGENT:+-noagent}${BROKER_RESTART_AT:+-brokerrestart}${HW_RESET_AT:+-unoreset}${HW_WIFI_DROP:+-wifidrop}${HW_CRC_FAULTS:+-crc}"
RUN="logs/run-$(date -u +%Y%m%dT%H%M%SZ)-x${PZIR_TIME_SCALE}${TAG}"
mkdir -p "$RUN"
export PZIR_DB="$RUN/co2vent.db" PZIR_AGENT_DB="$RUN/agent-state.db" PYTHONUNBUFFERED=1

$PY processor.py >"$RUN/processor.log" 2>&1 & P1=$!
if [ -z "${NO_AGENT:-}" ] && [ -z "${HW:-}" ]; then
  $PY agent.py   >"$RUN/agent.log"     2>&1 & P2=$!
else
  P2=""; echo "agent disabled" >"$RUN/agent.log"
fi
$PY api.py       >"$RUN/api.log"       2>&1 & P3=$!
trap 'kill $P1 $P2 $P3 2>/dev/null || true; [ -n "${HW:-}" ] && docker compose --profile hw rm -sf hw >/dev/null 2>&1 || true' EXIT
sleep 1.5
echo "run=$RUN  panel: http://127.0.0.1:${PZIR_API_PORT:-8000}/"
if [ -n "${BROKER_RESTART_AT:-}" ]; then
  ( sleep "$BROKER_RESTART_AT"; echo "$(date -u +%FT%T.%3NZ 2>/dev/null || date -u) broker restart" >"$RUN/broker.log"
    docker compose restart mqtt >>"$RUN/broker.log" 2>&1 ) &
fi
if [ -n "${HW:-}" ]; then
  export HW_SCENARIO="$SCENARIO" HW_LOG_DIR="$RUN/hw" HW_CRC_FAULTS="${HW_CRC_FAULTS:-}" HW_WIFI_DROP="${HW_WIFI_DROP:-}"
  docker compose --profile hw up -d --build hw >"$RUN/hw-compose.log" 2>&1
  if [ -n "${HW_RESET_AT:-}" ]; then
    ( sleep "$HW_RESET_AT"; echo "$(date -u) RESET Uno" >>"$RUN/hw-compose.log"
      docker compose exec -T hw sh -c 'kill -USR1 $(cat /tmp/uno_sim.pid)' >>"$RUN/hw-compose.log" 2>&1 ) &
  fi
  # тривалість сценарію + старт прошивки і давачів (~2 с) + перший такт 5 с
  sleep "$(python3 -c "import csv,sys; r=list(csv.DictReader(open('$SCENARIO'))); print(round((float(r[-1]['offset_s'])+10)/float(sys.argv[1]),1))" "$PZIR_TIME_SCALE")"
  docker compose --profile hw stop hw >>"$RUN/hw-compose.log" 2>&1
else
  $PY simulator.py --scenario "$SCENARIO" --limit "${LIMIT:-0}" >"$RUN/simulator.jsonl"
fi
sleep "${DEMO_TAIL_S:-3}"                    # дочекатися останніх ACK і тайм-аутів
curl -s "http://127.0.0.1:${PZIR_API_PORT:-8000}/api/devices/${PZIR_DEVICE_ID:-class-20}/state" >"$RUN/state.json" || true
curl -s "http://127.0.0.1:${PZIR_API_PORT:-8000}/api/devices/${PZIR_DEVICE_ID:-class-20}/commands?limit=50" >"$RUN/commands.json" || true
echo "done: $RUN"
