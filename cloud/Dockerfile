# Хмарний прогін co2-vent: прошивка Uno (arduino-cli) у simavr + моделі SCD40/TCA9548A і ESP8266
# + обробник і вебпанель. Секретів в образі немає: сертифікати береться з SSM під час старту.
FROM debian:bookworm-slim

ARG ARDUINO_CLI_VERSION=1.1.1
ARG ARDUINO_AVR_CORE=1.8.6

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl build-essential pkg-config \
        simavr libsimavr-dev libelf-dev \
        python3 python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN arch="$(dpkg --print-architecture)"; \
    case "$arch" in amd64) a=64bit;; arm64) a=ARM64;; *) echo "unsupported $arch"; exit 1;; esac; \
    curl -fsSL "https://github.com/arduino/arduino-cli/releases/download/v${ARDUINO_CLI_VERSION}/arduino-cli_${ARDUINO_CLI_VERSION}_Linux_${a}.tar.gz" \
      | tar -xz -C /usr/local/bin arduino-cli \
    && arduino-cli core update-index \
    && arduino-cli core install "arduino:avr@${ARDUINO_AVR_CORE}"

RUN python3 -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir \
        paho-mqtt==2.1.0 pyserial==3.5 boto3
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONPATH=/app

WORKDIR /app
COPY . /app
# прошивка й обв'язка збираються під час збирання образу - прогін стартує за секунди
RUN arduino-cli compile --fqbn arduino:avr:uno --output-dir hw/build hw/firmware/uno_node \
    && make -s -C hw/sim \
    && chmod +x cloud/run_cloud.sh

EXPOSE 8000
ENTRYPOINT ["cloud/run_cloud.sh"]
