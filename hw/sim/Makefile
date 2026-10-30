# Збирається в контейнері co2vent-hw (Debian: simavr, libsimavr-dev, libelf-dev)
CFLAGS ?= -O2 -Wall -Wextra -Wno-unused-parameter
CFLAGS += $(shell pkg-config --cflags simavr)
LDLIBS = -lsimavrparts -lsimavr -lelf -lpthread -lutil

uno_sim: uno_sim.c
	$(CC) $(CFLAGS) -o $@ $< $(LDLIBS)

clean:
	rm -f uno_sim
