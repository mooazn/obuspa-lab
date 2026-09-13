# Include from a plug-in Makefile to compile the virtual HAL client in:
#
#   VHAL_SRC ?= ../../plugin/vhal
#   include $(VHAL_SRC)/Makefile.inc      # adds -I$(VHAL_SRC) -DHAVE_VHAL and vhal.c
#
# scripts/flash.sh sets VHAL_SRC when it runs your Makefile.
ifneq ($(wildcard $(VHAL_SRC)/vhal.c),)
CFLAGS  += -I$(VHAL_SRC) -DHAVE_VHAL
SOURCES += $(VHAL_SRC)/vhal.c
endif
