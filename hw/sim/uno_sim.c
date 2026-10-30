/*
 * uno_sim - виконання прошивки Arduino Uno (ATmega328P, 16 МГц) у simavr.
 *
 *  - UART0 -> pseudo-terminal (simavr parts/uart_pty), symlink /tmp/simavr-uart0;
 *    міст ESP8266 відкриває його як звичайний послідовний порт.
 *  - TWI (I2C) -> TCP-міст до Python-моделей шини (TCA9548A + 3 x SCD40).
 *    Кожна подія шини - один рядок запиту і рядок відповіді:
 *      "S <addr8> <t_us>" -> "1"|"0"   START + адреса (з бітом R/W): ACK або NACK
 *      "W <byte>"         -> "1"|"0"   байт від master: ACK або NACK
 *      "R"                -> "<byte>"  байт для master
 *      "P <t_us>"         -> "1"       STOP
 *    t_us - симульований час (такти / 16), тож моделі не залежать від швидкості ПК.
 *  - Синхронізація: симульований час іде зі швидкістю speed x реальний.
 *  - Вихід D7 (реле) і D13 (LED) журналюються; SIGUSR1 - скидання МК (кнопка RESET).
 *
 * Використання: uno_sim <firmware.elf> [--speed k] [--i2c host:port]
 */
#include <errno.h>
#include <netdb.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <time.h>
#include <unistd.h>

#include "avr_ioport.h"
#include "avr_twi.h"
#include "sim_avr.h"
#include "sim_elf.h"
#include "parts/uart_pty.h"

#define F_CPU 16000000UL

static avr_t *avr;
static uart_pty_t uart_pty;
static FILE *bus_in, *bus_out;
static uint8_t selected;              /* адреса (8-біт) поточної транзакції, 0 - не наша */
static volatile sig_atomic_t reset_requested;
static double speed = 1.0;
static uint64_t cycle_base;           /* такти до останнього скидання: час не йде назад */

static uint64_t cycles(void) { return cycle_base + avr->cycle; }
static uint64_t sim_us(void) { return cycles() / (F_CPU / 1000000UL); }

static uint64_t wall_ns(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000000000ULL + ts.tv_nsec;
}

/* один запит до моделей шини; повертає числову відповідь */
static long bus_rpc(const char *fmt, ...) {
    char reply[32];
    va_list ap;
    va_start(ap, fmt);
    vfprintf(bus_out, fmt, ap);
    va_end(ap);
    fputc('\n', bus_out);
    fflush(bus_out);
    if (!fgets(reply, sizeof reply, bus_in)) {
        fprintf(stderr, "uno_sim: I2C bridge closed\n");
        exit(3);
    }
    return strtol(reply, NULL, 10);
}

static void twi_hook(struct avr_irq_t *irq, uint32_t value, void *param) {
    avr_irq_t *bus = param;
    avr_twi_msg_irq_t v;
    v.u.v = value;
    if (v.u.twi.msg & TWI_COND_STOP) {
        if (selected) bus_rpc("P %llu", (unsigned long long)sim_us());
        selected = 0;
    }
    if (v.u.twi.msg & TWI_COND_START) {
        if (selected) bus_rpc("P %llu", (unsigned long long)sim_us());   /* repeated START */
        selected = 0;
        if (bus_rpc("S %u %llu", v.u.twi.addr, (unsigned long long)sim_us()) == 1) {
            selected = v.u.twi.addr;
            avr_raise_irq(bus + TWI_IRQ_INPUT, avr_twi_irq_msg(TWI_COND_ACK, selected, 1));
        }
    }
    if (!selected) return;
    if (v.u.twi.msg & TWI_COND_WRITE) {
        if (bus_rpc("W %u", v.u.twi.data) == 1)
            avr_raise_irq(bus + TWI_IRQ_INPUT, avr_twi_irq_msg(TWI_COND_ACK, selected, 1));
    }
    if (v.u.twi.msg & TWI_COND_READ) {
        uint8_t data = (uint8_t)bus_rpc("R");
        avr_raise_irq(bus + TWI_IRQ_INPUT, avr_twi_irq_msg(TWI_COND_READ, selected, data));
    }
}

static void pin_hook(struct avr_irq_t *irq, uint32_t value, void *param) {
    printf("PIN %s=%u t_us=%llu\n", (const char *)param, value, (unsigned long long)sim_us());
    fflush(stdout);
}

/* сон МК (SLEEP_MODE_IDLE) не чекаємо в реальному часі - темп задає основний цикл */
static void no_sleep(avr_t *a, avr_cycle_count_t how_long) { (void)a; (void)how_long; }

static void on_sigusr1(int sig) { (void)sig; reset_requested = 1; }

static void connect_bus(const char *spec) {
    char host[128];
    const char *colon = strrchr(spec, ':');
    if (!colon || colon - spec >= (long)sizeof host) { fprintf(stderr, "bad --i2c %s\n", spec); exit(2); }
    memcpy(host, spec, colon - spec);
    host[colon - spec] = '\0';
    struct addrinfo hints = {.ai_socktype = SOCK_STREAM}, *res;
    for (int attempt = 0; attempt < 50; attempt++) {
        if (getaddrinfo(host, colon + 1, &hints, &res) == 0) {
            int s = socket(res->ai_family, res->ai_socktype, res->ai_protocol);
            if (s >= 0 && connect(s, res->ai_addr, res->ai_addrlen) == 0) {
                freeaddrinfo(res);
                bus_in = fdopen(s, "r");
                bus_out = fdopen(dup(s), "w");
                return;
            }
            if (s >= 0) close(s);
            freeaddrinfo(res);
        }
        usleep(200000);
    }
    fprintf(stderr, "uno_sim: cannot connect I2C bridge %s: %s\n", spec, strerror(errno));
    exit(2);
}

int main(int argc, char **argv) {
    const char *elf = NULL, *i2c = "127.0.0.1:7700";
    for (int i = 1; i < argc; i++) {
        if (!strcmp(argv[i], "--speed") && i + 1 < argc) speed = atof(argv[++i]);
        else if (!strcmp(argv[i], "--i2c") && i + 1 < argc) i2c = argv[++i];
        else elf = argv[i];
    }
    if (!elf || speed <= 0) {
        fprintf(stderr, "usage: uno_sim <firmware.elf> [--speed k] [--i2c host:port]\n");
        return 2;
    }
    elf_firmware_t fw;
    memset(&fw, 0, sizeof fw);
    if (elf_read_firmware(elf, &fw) != 0) { fprintf(stderr, "cannot read %s\n", elf); return 2; }
    strcpy(fw.mmcu, "atmega328p");     /* ELF від arduino-cli не містить секції .mmcu */
    fw.frequency = F_CPU;
    avr = avr_make_mcu_by_name(fw.mmcu);
    if (!avr) { fprintf(stderr, "atmega328p not supported\n"); return 2; }
    avr_init(avr);
    avr_load_firmware(avr, &fw);
    avr->sleep = no_sleep;

    connect_bus(i2c);
    static const char *names[] = {"twi.in", "twi.out"};
    avr_irq_t *bus = avr_alloc_irq(&avr->irq_pool, 0, 2, names);
    avr_irq_register_notify(bus + TWI_IRQ_OUTPUT, twi_hook, bus);
    avr_connect_irq(bus + TWI_IRQ_INPUT, avr_io_getirq(avr, AVR_IOCTL_TWI_GETIRQ(0), TWI_IRQ_INPUT));
    avr_connect_irq(avr_io_getirq(avr, AVR_IOCTL_TWI_GETIRQ(0), TWI_IRQ_OUTPUT), bus + TWI_IRQ_OUTPUT);

    uart_pty_init(avr, &uart_pty);
    uart_pty_connect(&uart_pty, '0');

    avr_irq_register_notify(avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('D'), 7), pin_hook, "relay_D7");
    avr_irq_register_notify(avr_io_getirq(avr, AVR_IOCTL_IOPORT_GETIRQ('B'), 5), pin_hook, "led_D13");
    signal(SIGUSR1, on_sigusr1);

    printf("SIM start mcu=%s f=%lu speed=%.2f i2c=%s\n", fw.mmcu, F_CPU, speed, i2c);
    fflush(stdout);
    uint64_t wall0 = wall_ns(), last_report = 0;
    for (unsigned long n = 0;; n++) {
        int state = avr_run(avr);
        if (state == cpu_Done || state == cpu_Crashed) {
            printf("SIM stopped state=%d\n", state);
            return state == cpu_Done ? 0 : 1;
        }
        if (reset_requested) {
            reset_requested = 0;
            printf("SIM reset t_us=%llu\n", (unsigned long long)sim_us());
            fflush(stdout);
            uint64_t before = avr->cycle;
            avr_reset(avr);
            if (avr->cycle < before) cycle_base += before - avr->cycle;
        }
        if ((n & 0x3FF) == 0) {
            uint64_t sim_ns = cycles() * 1000ULL / (F_CPU / 1000000UL);
            uint64_t target = (uint64_t)((wall_ns() - wall0) * speed);
            if (sim_ns > target + 2000000ULL) {          /* випередили більш ніж на 2 мс */
                uint64_t ahead = (uint64_t)((sim_ns - target) / speed);
                struct timespec ts = {ahead / 1000000000ULL, ahead % 1000000000ULL};
                nanosleep(&ts, NULL);
            }
            uint64_t now_s = sim_us() / 1000000ULL;
            if (now_s >= last_report + 60) {               /* раз на хвилину симульованого часу */
                last_report = now_s;
                double real = (wall_ns() - wall0) / 1e9;
                printf("SIM t=%llus real=%.1fs ratio=%.2f\n", (unsigned long long)now_s, real,
                       real > 0 ? now_s / real : 0.0);
                fflush(stdout);
            }
        }
    }
}
