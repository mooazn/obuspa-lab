/*
 * vhal.h - the virtual hardware abstraction, for vendor code that reads
 * hardware the container does not have.
 *
 * Vendor code that reads the world through POSIX (files, statvfs, sockets)
 * needs nothing from the platform: the lab perturbs the environment and the
 * code sees real symptoms. This header is for the other kind - a temperature
 * sensor, a modem's RSSI, a chipset SDK call - where there is nothing real to
 * read inside a container. Put your HAL's simulator backend on top of it and
 * the lab (UI or tests) supplies the values.
 *
 * Keys are a free namespace: the platform defines none. Values are strings.
 *
 *   char rssi[16];
 *   if (vhal_get("wifi.radio1.rssi", rssi, sizeof rssi) == 0) ...
 *
 * Properties:
 *   - dependency-free: POSIX sockets only, no obuspa headers, no cJSON
 *   - thread-safe: no shared state; every call opens its own connection
 *   - never exits the process: a lost socket is an error return, so a vendor
 *     thread survives the device rebooting underneath it
 *
 * Socket: $VDEV_SOCK, or /run/vdev/vdev.sock. Wire format: one JSON object
 * per line, see device/vdev/shim.py.
 */

#ifndef VHAL_H
#define VHAL_H

#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Reads a key. Returns 0 and fills `value` (NUL-terminated, truncated to
 * `size`) if set; 1 if the key is not set; -1 on transport failure. */
int vhal_get(const char *key, char *value, size_t size);

/* Writes a key. `value` may be NULL to delete it. Returns 0, or -1. */
int vhal_set(const char *key, const char *value);

/* Called for the current value of every matching key, then for each change.
 * `value` is NULL when a key is deleted. */
typedef void (*vhal_watch_cb)(const char *key, const char *value, void *ctx);

/* Blocks, delivering changes to keys starting with `prefix` ("" for all).
 * Returns -1 when the connection drops - typically because the device is
 * rebooting - so callers loop with a short sleep. */
int vhal_watch(const char *prefix, vhal_watch_cb callback, void *ctx);

#ifdef __cplusplus
}
#endif

#endif
