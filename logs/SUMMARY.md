# Зведення прогонів

Затримки, мс: медіана / p95 / максимум. Утримання — у сценарних секундах.

| Прогін | ×k | Подій у БД | Телеметрія, мс | Команда → ACK, мс | Команди за статусом | Відхилено | Утримання до ventilation_on, с |
|---|---|---|---|---|---|---|---|
| `run-20261030T152507Z-x10` | 10 | 323 | 2.0 / 5.0 / 41.0 (n=323) | 2.0 / 4.0 / 4.0 (n=3) | applied 3 | device_id mismatch with topic 1, duplicate event_id 1, stale event 1, unknown unit 1, value is not a number 1, value out of range 1 | 180.0 |
| `run-20261030T152629Z-x10-noagent` | 10 | 60 | 2.0 / 40.0 / 43.0 (n=60) | — | expired 7 | — | — |
| `run-20261030T152701Z-x10-brokerrestart` | 10 | 323 | 2.0 / 4.0 / 706.0 (n=323) | 3.0 / 3.0 / 3.0 (n=3) | applied 3 | ack: unknown command_id 7, device_id mismatch with topic 1, duplicate event_id 1, stale event 1, unknown unit 1, value is not a number 1, value out of range 1 | 180.0 |
| `run-20261030T155256Z-x10-hw` | 10 | 320 | 5.0 / 7.0 / 12.0 (n=320) | 16.0 / 63.0 / 63.0 (n=3) | applied 3 | — | 183.9 |
| `run-20261030T155552Z-x10-hw-unoreset-wifidrop-crc` | 10 | 296 | 6.0 / 1477.0 / 2864.0 (n=296) | 51.0 / 60.0 / 60.0 (n=3) | applied 3 | — | 180.9 |
| `run-20261030T161007Z-x1` | 1 | 323 | 3.0 / 6.0 / 18.0 (n=323) | 4.0 / 5.0 / 5.0 (n=3) | applied 3 | device_id mismatch with topic 1, duplicate event_id 1, stale event 1, unknown unit 1, value is not a number 1, value out of range 1 | 180.1 |
| `run-20261030T161936Z-x1-hw` | 1 | 326 | 4.0 / 12.0 / 42.0 (n=326) | 91.5 / 104.0 / 104.0 (n=2) | applied 2 | — | 180.0 |
| `run-20261030T170749Z-x10-noagent` | 10 | 60 | 2.0 / 38.0 / 43.0 (n=60) | — | expired 8 | — | — |
| `run-20261030T170809Z-x1-noagent` | 1 | 36 | 3.0 / 3.0 / 5.0 (n=36) | — | expired 5, sent 1 | — | — |
| `run-20261031T055522Z-x10` | 10 | 320 | 114.0 / 145.0 / 1364.0 (n=320) | 73.0 / 77.0 / 77.0 (n=3) | applied 3 | device_id mismatch with topic 1, duplicate event_id 1, out of order for sensor 3, stale event 1, unknown unit 1, value is not a number 1, value out of range 1 | 180.1 |
| `run-20261031T055724Z-x10-hw` | 10 | 302 | 80.0 / 146.0 / 241.0 (n=302) | 95.5 / 96.0 / 96.0 (n=2) | applied 2 | — | 180.1 |
| `run-20261031T060130Z-x1-hw` | 1 | 326 | 80.0 / 129.0 / 367.0 (n=326) | 138.0 / 142.0 / 142.0 (n=2) | applied 2 | ack: unknown command_id 5 | 180.0 |

## Критерій К1: утримання 180 с

Очікувано: 180 с ≤ утримання < 185 с (рішення на першій події після 180 с; період давачів 5 с).

- ✅ `run-20261030T152507Z-x10`: 180.00 с
- ✅ `run-20261030T152701Z-x10-brokerrestart`: 180.00 с
- ✅ `run-20261030T155256Z-x10-hw`: 183.92 с
- ✅ `run-20261030T155552Z-x10-hw-unoreset-wifidrop-crc`: 180.92 с
- ✅ `run-20261030T161007Z-x1`: 180.10 с
- ✅ `run-20261030T161936Z-x1-hw`: 180.01 с
- ✅ `run-20261031T055522Z-x10`: 180.11 с
- ✅ `run-20261031T055724Z-x10-hw`: 180.13 с
- ✅ `run-20261031T060130Z-x1-hw`: 180.01 с

## Критерій К4: позначка expired не пізніше TTL + 0,5 с

Запізнення = closed_at − expires_at, мс (лише прогони з v1.1, де є closed_at).

- — `run-20261030T152629Z-x10-noagent`: closed_at не записувався (до v1.1)
- ✅ `run-20261030T170749Z-x10-noagent`: 8 команд expired, запізнення 0…91 мс
- ✅ `run-20261030T170809Z-x1-noagent`: 5 команд expired, запізнення 13…87 мс
