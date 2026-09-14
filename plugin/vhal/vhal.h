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

/*
 * Data model operations from the hardware side.
 *
 * obuspa's own API (USP_DM_SetParameterValue, USP_DM_DeleteInstance and the
 * USP_PROCESS_DM_* variants) is the way firmware changes and removes state in
 * the data model, and it should be preferred. It cannot create instances:
 * on a real device the vendor's hardware layer creates the row and informs
 * obuspa afterwards. These calls are that hardware layer on the platform.
 * They act with hardware privileges - they may create, set and delete what a
 * controller may not - and the device signals obuspa about the result.
 */

/* Creates an instance of `object_path` (e.g. "Device.Firewall.Chain.1.Rule.").
 * Returns 0 and stores the new instance number, or -1. */
int vhal_dm_add(const char *object_path, int *instance);

/* Sets one parameter by full path, bypassing controller-facing access rules.
 * Values are the textual form used on the wire ("true", "42", ...).
 * Returns 0, or -1 (the device's reason is written to `err` if given). */
int vhal_dm_set(const char *path, const char *value, char *err, size_t err_size);

/* Deletes an instance by full path (e.g. "Device.Firewall.Chain.1.Rule.3").
 * Returns 0, or -1. */
int vhal_dm_delete(const char *instance_path);

#ifdef __cplusplus
}
#endif

#endif
