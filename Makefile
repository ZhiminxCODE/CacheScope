CC ?= cc
CPPFLAGS += -D_FILE_OFFSET_BITS=64
CFLAGS ?= -O2 -g
CFLAGS += -std=c11 -Wall -Wextra -Wpedantic -Werror
PREFIX ?= /usr/local

.PHONY: all clean test test-unit install
all: cachescope

cachescope: src/cachescope.c src/cache_map.h
	$(CC) $(CPPFLAGS) $(CFLAGS) src/cachescope.c -o $@

test-map: tests/test_map.c src/cache_map.h
	$(CC) $(CPPFLAGS) $(CFLAGS) tests/test_map.c -o $@

test-unit: test-map
	./test-map
	python3 -m unittest discover -s tests -v

test: cachescope test-unit
	CACHESCOPE_BIN=$(CURDIR)/cachescope python3 tests/integration.py

install: cachescope
	install -d $(DESTDIR)$(PREFIX)/bin
	install -m 755 cachescope $(DESTDIR)$(PREFIX)/bin/cachescope

clean:
	rm -f cachescope test-map
	rm -rf cachescope.dSYM test-map.dSYM
