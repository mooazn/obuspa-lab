# Builds the data-model proxy plug-in as a shared object for obuspa's -x option.
#
# OBUSPA_SRC must point at a checked-out obuspa source tree (headers only are
# needed - the plug-in links against nothing in obuspa itself, since obuspa
# resolves the USP_* symbols when it dlopen()s us).

OBUSPA_SRC ?= /usr/local/src/obuspa

# ENABLE_UDS must match how obuspa itself was configured, otherwise the
# consistency checks in vendor_defs.h fail. obuspa's own example plug-in does
# the same thing (AC_DEFINE(ENABLE_UDS) in examples/plugin/configure.ac).
CFLAGS  += -fPIC -Wall -Wextra -Wno-unused-parameter -O2 \
           -DENABLE_UDS \
           -I$(OBUSPA_SRC)/src/include \
           -I$(OBUSPA_SRC)/src/vendor \
           -I$(OBUSPA_SRC)/src/core
LDFLAGS += -shared -export-dynamic
LIBS    += -lcjson -lpthread

TARGET  := vdev_plugin.so
SOURCES := vdev_plugin.c

.PHONY: all clean

all: $(TARGET)

$(TARGET): $(SOURCES)
	$(CC) $(CFLAGS) $(LDFLAGS) -o $@ $(SOURCES) $(LIBS)

clean:
	rm -f $(TARGET)
