/*
 * disk_monitor.c - a worked example of vendor logic on the virtual platform.
 *
 * A background thread, started when the agent boots, watches a data partition
 * and raises a USP event when it fills past a threshold. It stands in for any
 * "monitor something and alarm" logic a vendor might have; nothing here is
 * specific to disks except the statvfs() call.
 *
 * What it demonstrates:
 *   - a vendor object with a live read-only parameter (served by a getter
 *     over a mutex-protected cache the thread updates)
 *   - a persisted, controller-writable setting (a DB parameter)
 *   - a USP event raised from a background thread (USP_SIGNAL_* is the one
 *     family that is safe to call off the data model thread)
 *   - reading the environment through plain POSIX, so the platform can drive
 *     it with a fault ("disk_fill") and no simulator-specific code is needed
 *   - optionally, one hardware-style value read through the virtual HAL
 *
 * Exposes:
 *   Device.X_VDEV_DiskMonitor.UsedPercent   (uint, read-only, live)
 *   Device.X_VDEV_DiskMonitor.Path          (string, read-only)
 *   Device.X_VDEV_DiskMonitor.Threshold     (uint, read-write, persisted, default 90)
 *   Device.X_VDEV_DiskMonitor.SpaceLow!     (event: UsedPercent, Threshold, Path)
 *
 * Build: see Makefile. Loaded with obuspa's -x, so it has no main().
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <unistd.h>
#include <pthread.h>
#include <sys/statvfs.h>

#include "usp_err_codes.h"
#include "vendor_defs.h"
#include "vendor_api.h"
#include "usp_api.h"

#ifdef HAVE_VHAL
#include "vhal.h"
#endif

// Only USP_LOG_Printf is stable across obuspa releases; the USP_LOG_Error /
// Warning / Info macros are not (see plugin/vdev_plugin.c).
#define LOG(...)  USP_LOG_Printf(kLogLevel_Info, kLogType_Debug, __VA_ARGS__)

#define OBJ         "Device.X_VDEV_DiskMonitor."
#define EVENT       OBJ "SpaceLow!"
#define POLL_SECS   2

// ---------------------------------------------------------------------------
// State shared between the data model thread (getters, setters) and the
// monitor thread. Keep the critical sections tiny: getters run on the thread
// that answers USP requests.
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static unsigned used_percent = 0;
static unsigned threshold = 90;
static bool alarm_raised = false;
static char path[256] = "/data";

// ---------------------------------------------------------------------------
// Parameter callbacks (data model thread)

static int Get_UsedPercent(dm_req_t *req, char *buf, int len)
{
    pthread_mutex_lock(&lock);
    snprintf(buf, len, "%u", used_percent);
    pthread_mutex_unlock(&lock);
    return USP_ERR_OK;
}

static int Get_Path(dm_req_t *req, char *buf, int len)
{
    strncpy(buf, path, len - 1);
    buf[len - 1] = '\0';
    return USP_ERR_OK;
}

// Called after a controller's Set has been committed to the database
static int Notify_Threshold(dm_req_t *req, char *value)
{
    pthread_mutex_lock(&lock);
    threshold = (unsigned)atoi(value);
    pthread_mutex_unlock(&lock);
    LOG("disk-monitor: threshold now %s%%", value);
    return USP_ERR_OK;
}

// ---------------------------------------------------------------------------
// The monitor thread

static void RaiseSpaceLow(unsigned percent, unsigned limit)
{
    char percent_str[16], limit_str[16];
    snprintf(percent_str, sizeof(percent_str), "%u", percent);
    snprintf(limit_str, sizeof(limit_str), "%u", limit);

    // Ownership of the argument vector passes to obuspa
    kv_vector_t *args = USP_ARG_Create();
    USP_ARG_Add(args, "UsedPercent", percent_str);
    USP_ARG_Add(args, "Threshold", limit_str);
    USP_ARG_Add(args, "Path", path);

    LOG("disk-monitor: %s is %u%% full (threshold %u%%) - raising " EVENT, path, percent, limit);
    USP_SIGNAL_DataModelEvent(EVENT, args);
}

static void *Monitor(void *arg)
{
    for (;;)
    {
        struct statvfs st;
        if (statvfs(path, &st) == 0 && st.f_blocks > 0)
        {
            unsigned long long total = (unsigned long long)st.f_blocks * st.f_frsize;
            unsigned long long avail = (unsigned long long)st.f_bavail * st.f_frsize;
            unsigned percent = (unsigned)(100 - (avail * 100 / total));
            unsigned limit;

            pthread_mutex_lock(&lock);
            used_percent = percent;
            limit = threshold;
            pthread_mutex_unlock(&lock);

#ifdef HAVE_VHAL
            // The virtual HAL, opt-in: if the lab has set a threshold there,
            // it wins - the way a value read from a board's EEPROM might.
            char hal_value[16];
            if (vhal_get("diskmon.threshold", hal_value, sizeof(hal_value)) == 0)
            {
                limit = (unsigned)atoi(hal_value);
            }
#endif

            if (percent >= limit && !alarm_raised)
            {
                alarm_raised = true;
                RaiseSpaceLow(percent, limit);
            }
            else if (percent < limit && alarm_raised)
            {
                alarm_raised = false;
                LOG("disk-monitor: %s back to %u%% - alarm cleared", path, percent);
            }
        }
        else
        {
            LOG("disk-monitor: cannot statvfs %s", path);
        }

        sleep(POLL_SECS);
    }
    return NULL;
}

// ---------------------------------------------------------------------------
// Plug-in entry points, called by obuspa on the data model thread

int VENDOR_Init(void)
{
    int err = USP_ERR_OK;
    const char *env = getenv("DISKMON_PATH");
    char *event_args[] = { "UsedPercent", "Threshold", "Path" };

    if (env != NULL && *env != '\0')
    {
        strncpy(path, env, sizeof(path) - 1);
    }

    err |= USP_REGISTER_VendorParam_ReadOnly(OBJ "UsedPercent", Get_UsedPercent, DM_UINT);
    err |= USP_REGISTER_VendorParam_ReadOnly(OBJ "Path", Get_Path, DM_STRING);
    err |= USP_REGISTER_DBParam_ReadWrite(OBJ "Threshold", "90", NULL, Notify_Threshold, DM_UINT);
    err |= USP_REGISTER_Event(EVENT);
    err |= USP_REGISTER_EventArguments(EVENT, event_args, 3);

    return (err == USP_ERR_OK) ? USP_ERR_OK : USP_ERR_INTERNAL_ERROR;
}

int VENDOR_Start(void)
{
    pthread_t thread;
    char value[16];

    // The database is readable now (not in VENDOR_Init): pick up a threshold
    // a controller set on a previous boot.
    if (USP_DM_GetParameterValue(OBJ "Threshold", value, sizeof(value)) == USP_ERR_OK)
    {
        threshold = (unsigned)atoi(value);
    }

    if (pthread_create(&thread, NULL, Monitor, NULL) != 0)
    {
        LOG("disk-monitor: could not start the monitor thread");
        return USP_ERR_INTERNAL_ERROR;
    }
    pthread_detach(thread);

    LOG("disk-monitor: watching %s every %ds, threshold %u%%", path, POLL_SECS, threshold);
    return USP_ERR_OK;
}

int VENDOR_Stop(void)
{
    // Never reached on a simulated power-cycle: the agent is _exit()ed, the
    // way real firmware loses power. Do not rely on this for anything.
    return USP_ERR_OK;
}
