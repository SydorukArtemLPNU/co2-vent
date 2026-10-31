#!/usr/bin/env bash
# Налаштування AWS IoT Core для co2-vent (одноразово; повторний запуск пропускає наявне).
#   AWS_PROFILE=lpnu scripts/aws_setup.sh
# Створює: Thing class-20; сертифікати X.509 вузла і сервера (certs/, не в git); IoT Policy з
# мінімальними правами; роль і правила IoT -> CloudWatch Metrics (графіки) і CloudWatch Logs (журнал);
# панель CloudWatch "co2vent". Записує в .env лише шляхи до сертифікатів і адресу брокера.
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_PROFILE="${AWS_PROFILE:-lpnu}"
REGION="$(aws configure get region)"
ACCOUNT="$(aws sts get-caller-identity --query Account --output text)"
DEVICE="${PZIR_DEVICE_ID:-class-20}"
ENDPOINT="$(aws iot describe-endpoint --endpoint-type iot:Data-ATS --query endpointAddress --output text)"
ARN="arn:aws:iot:${REGION}:${ACCOUNT}"
mkdir -p certs && chmod 700 certs
echo "account=$ACCOUNT region=$REGION endpoint=$ENDPOINT"

# --- кореневий CA Amazon (публічний) --------------------------------------------
[ -s certs/AmazonRootCA1.pem ] || curl -fsS -o certs/AmazonRootCA1.pem https://www.amazontrust.com/repository/AmazonRootCA1.pem

# --- Thing -------------------------------------------------------------------
aws iot describe-thing --thing-name "$DEVICE" >/dev/null 2>&1 || aws iot create-thing --thing-name "$DEVICE" >/dev/null

# --- політики: вузол лише свої теми, сервер - усі класи ---------------------------
node_policy=$(cat <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":"iot:Connect","Resource":[
   "${ARN}:client/esp8266-${DEVICE}","${ARN}:client/co2vent-agent","${ARN}:client/co2vent-simulator"]},
 {"Effect":"Allow","Action":"iot:Publish","Resource":[
   "${ARN}:topic/pzir/course/${DEVICE}/telemetry","${ARN}:topic/pzir/course/${DEVICE}/ack",
   "${ARN}:topic/pzir/course/${DEVICE}/state"]},
 {"Effect":"Allow","Action":"iot:Subscribe","Resource":["${ARN}:topicfilter/pzir/course/${DEVICE}/command"]},
 {"Effect":"Allow","Action":"iot:Receive","Resource":["${ARN}:topic/pzir/course/${DEVICE}/command"]}]}
EOF
)
server_policy=$(cat <<EOF
{"Version":"2012-10-17","Statement":[
 {"Effect":"Allow","Action":"iot:Connect","Resource":["${ARN}:client/co2vent-processor","${ARN}:client/co2vent-check-*"]},
 {"Effect":"Allow","Action":["iot:Publish","iot:Receive"],"Resource":["${ARN}:topic/pzir/course/*"]},
 {"Effect":"Allow","Action":"iot:Subscribe","Resource":["${ARN}:topicfilter/pzir/course/*"]}]}
EOF
)
for name in co2vent-node co2vent-server; do
  doc=$([ "$name" = co2vent-node ] && echo "$node_policy" || echo "$server_policy")
  if ! aws iot get-policy --policy-name "$name" >/dev/null 2>&1; then
    aws iot create-policy --policy-name "$name" --policy-document "$doc" >/dev/null
  fi
done

# --- сертифікати (ключ лишається тільки локально) --------------------------------
for role in node server; do
  if [ ! -s "certs/$role.pem.crt" ]; then
    arn=$(aws iot create-keys-and-certificate --set-as-active \
          --certificate-pem-outfile "certs/$role.pem.crt" --private-key-outfile "certs/$role.private.pem.key" \
          --query certificateArn --output text)
    echo "$arn" > "certs/$role.arn"
    aws iot attach-policy --policy-name "co2vent-$role" --target "$arn"
    [ "$role" = node ] && aws iot attach-thing-principal --thing-name "$DEVICE" --principal "$arn"
  fi
done
chmod 600 certs/*

# --- CloudWatch: роль для правил, журнал, правила ---------------------------------
# (value - зарезервоване слово SQL AWS IoT, тому значення береться через get(*, 'value'))
ROLE=co2vent-iot-rules
if ! aws iam get-role --role-name "$ROLE" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ROLE" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"iot.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam put-role-policy --role-name "$ROLE" --policy-name co2vent-cloudwatch --policy-document \
    "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"cloudwatch:PutMetricData\",\"Resource\":\"*\"},
      {\"Effect\":\"Allow\",\"Action\":[\"logs:CreateLogStream\",\"logs:PutLogEvents\",\"logs:DescribeLogStreams\"],
       \"Resource\":\"arn:aws:logs:${REGION}:${ACCOUNT}:log-group:/co2vent/mqtt:*\"}]}"
  sleep 10   # IAM поширюється з затримкою
fi
ROLE_ARN="arn:aws:iam::${ACCOUNT}:role/${ROLE}"
aws logs create-log-group --log-group-name /co2vent/mqtt 2>/dev/null || true
aws logs put-retention-policy --log-group-name /co2vent/mqtt --retention-in-days 30

rule() {   # rule <name> <sql> <actions-json>
  local err="{\"cloudwatchLogs\":{\"roleArn\":\"$ROLE_ARN\",\"logGroupName\":\"/co2vent/mqtt\"}}"
  local payload="{\"sql\":\"$2\",\"awsIotSqlVersion\":\"2016-03-23\",\"ruleDisabled\":false,\"actions\":$3,\"errorAction\":$err}"
  if aws iot get-topic-rule --rule-name "$1" >/dev/null 2>&1; then
    aws iot replace-topic-rule --rule-name "$1" --topic-rule-payload "$payload"
  else
    aws iot create-topic-rule --rule-name "$1" --topic-rule-payload "$payload"
  fi
}
rule co2vent_co2_metric \
  "SELECT * FROM 'pzir/course/+/telemetry' WHERE metric = 'co2'" \
  "[{\"cloudwatchMetric\":{\"roleArn\":\"$ROLE_ARN\",\"metricNamespace\":\"CO2Vent\",\"metricName\":\"\${sensor_id}\",\"metricValue\":\"\${get(*, 'value')}\",\"metricUnit\":\"None\"}}]"
# вентиляція 1/0 за ACK: шаблони дій не підтримують CASE, тож два правила з константою
for st in on off; do
  rule "co2vent_ventilation_$st" \
    "SELECT * FROM 'pzir/course/+/ack' WHERE status = 'applied' AND ventilation = '$st'" \
    "[{\"cloudwatchMetric\":{\"roleArn\":\"$ROLE_ARN\",\"metricNamespace\":\"CO2Vent\",\"metricName\":\"ventilation\",\"metricValue\":\"$([ $st = on ] && echo 1 || echo 0)\",\"metricUnit\":\"None\"}}]"
done
aws iot delete-topic-rule --rule-name co2vent_ventilation_metric 2>/dev/null || true
rule co2vent_log_all \
  "SELECT *, topic() AS topic FROM 'pzir/course/#'" \
  "[{\"cloudwatchLogs\":{\"roleArn\":\"$ROLE_ARN\",\"logGroupName\":\"/co2vent/mqtt\"}}]"

# --- панель CloudWatch -------------------------------------------------------------
dash=$(cat <<EOF
{"widgets":[
 {"type":"metric","x":0,"y":0,"width":16,"height":8,"properties":{"title":"CO₂ за давачами, ppm (змодельовані дані)",
  "region":"$REGION","view":"timeSeries","stat":"Average","period":10,
  "metrics":[["CO2Vent","scd40-1"],["CO2Vent","scd40-2"],["CO2Vent","scd40-3"]],
  "annotations":{"horizontal":[{"label":"1000 ppm – увімкнення (180 с)","value":1000},{"label":"800 ppm – вимкнення","value":800}]}}},
 {"type":"metric","x":16,"y":0,"width":8,"height":8,"properties":{"title":"Вентиляція за ACK (1 – увімкнено)",
  "region":"$REGION","view":"timeSeries","stat":"Maximum","period":10,"metrics":[["CO2Vent","ventilation"]],
  "yAxis":{"left":{"min":0,"max":1}}}},
 {"type":"log","x":0,"y":8,"width":24,"height":8,"properties":{"title":"Журнал: команди, ACK і звіти стану",
  "region":"$REGION","view":"table",
  "query":"SOURCE '/co2vent/mqtt' | fields @timestamp, topic, command_id, action, status, ventilation, reason | filter ispresent(command_id) or ispresent(reason) | sort @timestamp desc | limit 50"}}]}
EOF
)
aws cloudwatch put-dashboard --dashboard-name co2vent --dashboard-body "$dash" >/dev/null

# --- .env: адреса й шляхи (секретів-рядків немає, ключі лише у файлах certs/) ------
set_env() { if grep -q "^$1=" .env; then sed -i '' "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi; }
touch .env && chmod 600 .env
set_env PZIR_BROKER_HOST "$ENDPOINT"
set_env PZIR_BROKER_PORT 8883
set_env PZIR_BROKER_TLS 1
set_env PZIR_BROKER_USER ""
set_env PZIR_BROKER_PASSWORD ""
set_env PZIR_BROKER_CA certs/AmazonRootCA1.pem
set_env PZIR_BROKER_CERT certs/server.pem.crt
set_env PZIR_BROKER_KEY certs/server.private.pem.key
set_env PZIR_NODE_CERT certs/node.pem.crt
set_env PZIR_NODE_KEY certs/node.private.pem.key
echo "done: dashboard https://${REGION}.console.aws.amazon.com/cloudwatch/home?region=${REGION}#dashboards/dashboard/co2vent"
