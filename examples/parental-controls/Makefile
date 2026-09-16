# Builds the parental-controls example as an obuspa plug-in.
#
# OBUSPA_SRC  the obuspa tree to compile against (headers only)
# VHAL_SRC    plugin/vhal - required here: the example creates firewall rules
#             through the HAL

OBUSPA_SRC ?= /usr/local/src/obuspa
VHAL_SRC   ?= ../../plugin/vhal

CFLAGS  += -fPIC -Wall -O2 -DENABLE_UDS \
           -I$(OBUSPA_SRC)/src/include \
           -I$(OBUSPA_SRC)/src/vendor \
           -I$(OBUSPA_SRC)/src/core \
           -I$(VHAL_SRC)
SOURCES := parental_controls.c $(VHAL_SRC)/vhal.c

parental_controls.so: $(SOURCES)
	$(CC) $(CFLAGS) -shared -o $@ $^ -lpthread

clean:
	rm -f parental_controls.so
