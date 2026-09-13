# Builds the disk-monitor example as an obuspa plug-in.
#
# OBUSPA_SRC  the obuspa tree to compile against (headers only)
# VHAL_SRC    where plugin/vhal lives; optional - the HAL call compiles out
#             without it
#
# scripts/flash.sh runs this inside the build container with both set.

OBUSPA_SRC ?= /usr/local/src/obuspa
VHAL_SRC   ?=

CFLAGS  += -fPIC -Wall -O2 -DENABLE_UDS \
           -I$(OBUSPA_SRC)/src/include \
           -I$(OBUSPA_SRC)/src/vendor \
           -I$(OBUSPA_SRC)/src/core
SOURCES := disk_monitor.c

ifneq ($(wildcard $(VHAL_SRC)/vhal.c),)
CFLAGS  += -I$(VHAL_SRC) -DHAVE_VHAL
SOURCES += $(VHAL_SRC)/vhal.c
endif

disk_monitor.so: $(SOURCES)
	$(CC) $(CFLAGS) -shared -o $@ $^ -lpthread

clean:
	rm -f disk_monitor.so
