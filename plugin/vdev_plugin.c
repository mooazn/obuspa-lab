/*
 * vdev_plugin.c
 *
 * A generic OB-USP-AGENT plug-in that proxies the whole of its registered data
 * model to an external "data model provider component" over a Unix domain
 * socket, using newline-delimited JSON.
 *
 * This file is deliberately dumb. It knows nothing about WiFi, firmware, or
 * any other device behaviour: at startup it asks the provider to describe its
 * data model, registers whatever it is told, and forwards every subsequent
 * get/set/add/delete to the provider. All device behaviour lives on the other
 * end of the socket (see ../device), so extending the simulated device never
 * requires touching or recompiling this file.
 *
 * The integration pattern is the one described in obuspa's QUICK_START_GUIDE.md
 * under "Extending the Data Model using grouped parameter and object API".
 *
 * Wire protocol (one JSON object per line, request then response):
 *
 *   -> {"op":"model"}
 *   <- {"ok":true,"objects":[{"path":"Device.WiFi.SSID.{i}.","writable":true}],
 *                 "params":[{"path":"Device.WiFi.SSID.{i}.SSID",
 *                            "type":"string","writable":true}]}
 *
 *   -> {"op":"instances"}
 *   <- {"ok":true,"instances":["Device.WiFi.SSID.1."]}
 *
 *   -> {"op":"get","paths":["Device.WiFi.SSID.1.SSID"]}
 *   <- {"ok":true,"values":{"Device.WiFi.SSID.1.SSID":"VirtualGateway-001"}}
 *
 *   -> {"op":"set","params":{"Device.WiFi.SSID.1.SSID":"NewSSID"}}
 *   <- {"ok":true}
 *   <- {"ok":false,"failure_index":0,"error":"invalid SSID length"}
 *
 *   -> {"op":"add","path":"Device.WiFi.SSID."}
 *   <- {"ok":true,"instance":2}
 *
 *   -> {"op":"delete","path":"Device.WiFi.SSID.1."}
 *   <- {"ok":true}
 *
 *   -> {"op":"operate","path":"Device.IP.Diagnostics.IPPing()",
 *       "command_key":"abc","async":true,"request_id":3,"input":{"Host":"1.1.1.1"}}
 *   <- {"ok":true}                                  (async: completion arrives later)
 *   <- {"ok":true,"output":{"Result":"..."}}        (sync)
 *
 *   -> {"op":"reboot"}
 *   <- {"ok":true}
 *
 *   -> {"op":"clock_sync"}                          (at init: the RTC read at boot)
 *   <- {"ok":true,"faketime":"+0"}
 *
 * Plus one long-lived connection carrying pushes from the device:
 *
 *   -> {"op":"events"}
 *   <- {"event":"operation_status","request_id":3,"status":"Requested"}
 *   <- {"event":"operation_complete","request_id":3,"err_code":0,"output":{...}}
 *   <- {"event":"dm_event","name":"Device.Custom!","args":{...}}
 *   <- {"event":"object_added","path":"Device.WiFi.SSID.2"}
 *   <- {"event":"clock","offset":3600,"rate":1}      (the lab moved the clock)
 */

#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdbool.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <pthread.h>
#include <sys/socket.h>
#include <sys/un.h>

#include <cjson/cJSON.h>

#include "usp_err_codes.h"
#include "vendor_defs.h"
#include "vendor_api.h"
#include "usp_api.h"

//-------------------------------------------------------------------------
// Logging. obuspa's USP_LOG_Error/Warning/Info macros are not stable across
// releases - v10's expand to globals a plug-in cannot see, v11's to an
// accessor - so this plug-in only depends on USP_LOG_Printf(), which has kept
// its signature. That matters because `make flash` compiles this file against
// whatever obuspa tree is being flashed.
#define VDEV_LOG_Error(...)     USP_LOG_Printf(kLogLevel_Error,   kLogType_Debug, __VA_ARGS__)
#define VDEV_LOG_Warning(...)   USP_LOG_Printf(kLogLevel_Warning, kLogType_Debug, __VA_ARGS__)
#define VDEV_LOG_Info(...)      USP_LOG_Printf(kLogLevel_Info,    kLogType_Debug, __VA_ARGS__)

//-------------------------------------------------------------------------
// All parameters and objects proxied by this plug-in belong to one group
#define VDEV_GROUP 1

// Where the provider component listens. Overridable so that several simulated
// devices can run side by side on one host.
#define DEFAULT_SOCK_PATH "/run/vdev/vdev.sock"

// How long VENDOR_Init() tolerates the provider being unreachable. The
// entrypoint only starts us once the device is serving, so this is a short
// grace for a socket mid-reopen - not a wait for the device to boot. If the
// device has gone away (it is rebooting under us), the right outcome is to
// exit and let the bootloader start over, not to connect late and run with a
// boot decision made before the reboot.
#define CONNECT_RETRY_COUNT 6
#define CONNECT_RETRY_USEC  500000      // 500ms

// Returned as *failure_index when we cannot attribute a set failure to one param
#define FAILURE_INDEX_UNKNOWN (-1)

//-------------------------------------------------------------------------
// Forward references
static int VdevGetGroup(int group_id, kv_vector_t *params);
static int VdevSetGroup(int group_id, kv_vector_t *params, unsigned *types, int *failure_index);
static int VdevAddGroup(int group_id, char *path, int *instance);
static int VdevDelGroup(int group_id, char *path);

static int VdevSyncOper(dm_req_t *req, char *command_key, kv_vector_t *input_args, kv_vector_t *output_args);
static int VdevAsyncOper(dm_req_t *req, kv_vector_t *input_args, int instance);
static int VdevAsyncRestart(dm_req_t *req, int instance, bool *is_restart, int *err_code,
                            char *err_msg, int err_msg_len, kv_vector_t *output_args);
static int VdevReboot(void);
static int VdevFactoryReset(void);
static int VdevGetSoftwareVersion(char *buf, int len);

static const char *SockPath(void);
static cJSON *Rpc(cJSON *request);
static cJSON *RpcWithRetry(cJSON *request, int retries);
static unsigned TypeFlagsFromName(const char *name);
static int RegisterCommands(cJSON *commands);
static int RegisterEvents(cJSON *events);
static cJSON *KvToJson(kv_vector_t *kvv);
static void JsonToKv(cJSON *object, kv_vector_t *kvv);
static void *EventThread(void *arg);
static void SyncClock(void);
static void ClockChanged(void *arg1, void *arg2);

/*********************************************************************//**
**
** SockPath
**
** Returns the path of the provider's Unix domain socket
**
**************************************************************************/
static const char *SockPath(void)
{
    const char *env = getenv("VDEV_SOCK");
    return (env != NULL && *env != '\0') ? env : DEFAULT_SOCK_PATH;
}

/*********************************************************************//**
**
** Rpc
**
** Sends one JSON request to the provider and reads one JSON response.
**
** A fresh connection is made per call. This keeps the plug-in stateless -
** there is no reconnect logic to get wrong, and the provider restarting is
** transparent. On a Unix socket the connect cost is negligible relative to
** everything else in a USP transaction.
**
** \param   request - request object. Ownership stays with the caller.
**
** \return  response object (caller must free with cJSON_Delete), or NULL on
**          transport failure
**
**************************************************************************/
static cJSON *Rpc(cJSON *request)
{
    int sock = -1;
    char *tx = NULL;
    char *rx = NULL;
    size_t rx_len = 0;
    size_t rx_size = 0;
    cJSON *response = NULL;
    struct sockaddr_un addr;
    const char *path = SockPath();
    size_t tx_len;
    ssize_t sent;
    size_t offset = 0;

    // Serialise the request and terminate it with the newline the provider frames on
    tx = cJSON_PrintUnformatted(request);
    if (tx == NULL)
    {
        return NULL;
    }
    tx_len = strlen(tx);

    sock = socket(AF_UNIX, SOCK_STREAM, 0);
    if (sock == -1)
    {
        goto exit;
    }

    memset(&addr, 0, sizeof(addr));
    addr.sun_family = AF_UNIX;
    strncpy(addr.sun_path, path, sizeof(addr.sun_path) - 1);

    if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) == -1)
    {
        goto exit;
    }

    while (offset < tx_len)
    {
        sent = send(sock, &tx[offset], tx_len - offset, MSG_NOSIGNAL);
        if (sent <= 0)
        {
            goto exit;
        }
        offset += (size_t)sent;
    }

    if (send(sock, "\n", 1, MSG_NOSIGNAL) != 1)
    {
        goto exit;
    }

    // Read until the newline terminating the response
    for (;;)
    {
        char buf[2048];
        ssize_t got = recv(sock, buf, sizeof(buf), 0);
        if (got < 0)
        {
            goto exit;
        }

        if (got > 0)
        {
            if (rx_len + (size_t)got + 1 > rx_size)
            {
                size_t new_size = (rx_size == 0) ? 4096 : rx_size * 2;
                while (new_size < rx_len + (size_t)got + 1)
                {
                    new_size *= 2;
                }
                char *grown = realloc(rx, new_size);
                if (grown == NULL)
                {
                    goto exit;
                }
                rx = grown;
                rx_size = new_size;
            }
            memcpy(&rx[rx_len], buf, (size_t)got);
            rx_len += (size_t)got;
            rx[rx_len] = '\0';

            if (memchr(rx, '\n', rx_len) != NULL)
            {
                break;      // complete response
            }
        }

        if (got == 0)
        {
            break;          // provider closed; parse whatever arrived
        }
    }

    if (rx != NULL)
    {
        response = cJSON_Parse(rx);
    }

exit:
    if (sock != -1)
    {
        close(sock);
    }
    free(tx);
    free(rx);
    return response;
}

/*********************************************************************//**
**
** RpcWithRetry
**
** As Rpc(), but tolerates the provider not being up yet. Used only at
** startup, where obuspa and the provider may race.
**
**************************************************************************/
static cJSON *RpcWithRetry(cJSON *request, int retries)
{
    int i;

    for (i = 0; i < retries; i++)
    {
        cJSON *response = Rpc(request);
        if (response != NULL)
        {
            return response;
        }

        VDEV_LOG_Warning("%s: provider at %s not ready (attempt %d/%d)", __FUNCTION__, SockPath(), i + 1, retries);
        usleep(CONNECT_RETRY_USEC);
    }

    return NULL;
}

/*********************************************************************//**
**
** TypeFlagsFromName
**
** Maps a type name from the provider's model description onto obuspa's DM_ flags
**
**************************************************************************/
static unsigned TypeFlagsFromName(const char *name)
{
    if (name == NULL)                       return DM_STRING;
    if (strcmp(name, "string") == 0)        return DM_STRING;
    if (strcmp(name, "bool") == 0)          return DM_BOOL;
    if (strcmp(name, "int") == 0)           return DM_INT;
    if (strcmp(name, "uint") == 0)          return DM_UINT;
    if (strcmp(name, "ulong") == 0)         return DM_ULONG;
    if (strcmp(name, "long") == 0)          return DM_LONG;
    if (strcmp(name, "datetime") == 0)      return DM_DATETIME;
    if (strcmp(name, "decimal") == 0)       return DM_DECIMAL;
    if (strcmp(name, "base64") == 0)        return DM_BASE64;
    if (strcmp(name, "hexbin") == 0)        return DM_HEXBIN;

    VDEV_LOG_Warning("%s: unknown type '%s', defaulting to string", __FUNCTION__, name);
    return DM_STRING;
}

/*********************************************************************//**
**
** KvToJson
**
** Converts a key-value vector into a JSON object
**
**************************************************************************/
static cJSON *KvToJson(kv_vector_t *kvv)
{
    cJSON *object = cJSON_CreateObject();
    int i;

    if (kvv != NULL)
    {
        for (i = 0; i < kvv->num_entries; i++)
        {
            cJSON_AddStringToObject(object, kvv->vector[i].key, kvv->vector[i].value);
        }
    }

    return object;
}

/*********************************************************************//**
**
** JsonToKv
**
** Copies the members of a JSON object into a key-value vector
**
**************************************************************************/
static void JsonToKv(cJSON *object, kv_vector_t *kvv)
{
    cJSON *item;

    if ((object == NULL) || (kvv == NULL))
    {
        return;
    }

    cJSON_ArrayForEach(item, object)
    {
        if (cJSON_IsString(item))
        {
            USP_ARG_Add(kvv, item->string, item->valuestring);
        }
    }
}

/*********************************************************************//**
**
** RegisterCommands
**
** Registers the USP commands described by the provider.
**
** Sync commands block the data model thread until the provider answers, so
** the provider must only use them for things that complete immediately.
** Anything with duration (firmware, diagnostics) should be declared async.
**
**************************************************************************/
static int RegisterCommands(cJSON *commands)
{
    int err = USP_ERR_OK;
    cJSON *item;

    cJSON_ArrayForEach(item, commands)
    {
        cJSON *path = cJSON_GetObjectItem(item, "path");
        cJSON *is_async = cJSON_GetObjectItem(item, "async");
        cJSON *max_concurrency = cJSON_GetObjectItem(item, "max_concurrency");
        cJSON *inputs = cJSON_GetObjectItem(item, "input");
        cJSON *outputs = cJSON_GetObjectItem(item, "output");

        if (cJSON_IsString(path) == false)
        {
            continue;
        }

        if (cJSON_IsTrue(is_async))
        {
            err |= USP_REGISTER_AsyncOperation(path->valuestring, VdevAsyncOper, VdevAsyncRestart);
            if (cJSON_IsNumber(max_concurrency))
            {
                err |= USP_REGISTER_AsyncOperation_MaxConcurrency(path->valuestring,
                                                                  max_concurrency->valueint);
            }
        }
        else
        {
            err |= USP_REGISTER_SyncOperation(path->valuestring, VdevSyncOper);
        }

        // Argument names have to be handed over as C arrays of char*
        if (cJSON_IsArray(inputs) || cJSON_IsArray(outputs))
        {
            int num_in = cJSON_IsArray(inputs) ? cJSON_GetArraySize(inputs) : 0;
            int num_out = cJSON_IsArray(outputs) ? cJSON_GetArraySize(outputs) : 0;
            char **in_names = (num_in > 0) ? calloc((size_t)num_in, sizeof(char *)) : NULL;
            char **out_names = (num_out > 0) ? calloc((size_t)num_out, sizeof(char *)) : NULL;
            int i;

            for (i = 0; i < num_in; i++)
            {
                in_names[i] = cJSON_GetArrayItem(inputs, i)->valuestring;
            }
            for (i = 0; i < num_out; i++)
            {
                out_names[i] = cJSON_GetArrayItem(outputs, i)->valuestring;
            }

            err |= USP_REGISTER_OperationArguments(path->valuestring, in_names, num_in,
                                                   out_names, num_out);
            free(in_names);
            free(out_names);
        }
    }

    return err;
}

/*********************************************************************//**
**
** RegisterEvents
**
** Registers the USP events described by the provider.
**
**************************************************************************/
static int RegisterEvents(cJSON *events)
{
    int err = USP_ERR_OK;
    cJSON *item;

    cJSON_ArrayForEach(item, events)
    {
        cJSON *path = cJSON_GetObjectItem(item, "path");
        cJSON *args = cJSON_GetObjectItem(item, "args");

        if (cJSON_IsString(path) == false)
        {
            continue;
        }

        err |= USP_REGISTER_Event(path->valuestring);

        if (cJSON_IsArray(args) && (cJSON_GetArraySize(args) > 0))
        {
            int num_args = cJSON_GetArraySize(args);
            char **names = calloc((size_t)num_args, sizeof(char *));
            int i;

            for (i = 0; i < num_args; i++)
            {
                names[i] = cJSON_GetArrayItem(args, i)->valuestring;
            }

            err |= USP_REGISTER_EventArguments(path->valuestring, names, num_args);
            free(names);
        }
    }

    return err;
}

/*********************************************************************//**
**
** VdevSyncOper
**
** Runs a synchronous USP command in the provider.
**
**************************************************************************/
static int VdevSyncOper(dm_req_t *req, char *command_key, kv_vector_t *input_args,
                        kv_vector_t *output_args)
{
    cJSON *request = NULL;
    cJSON *response = NULL;
    cJSON *ok = NULL;
    int result = USP_ERR_OK;

    request = cJSON_CreateObject();
    cJSON_AddStringToObject(request, "op", "operate");
    cJSON_AddStringToObject(request, "path", req->path);
    cJSON_AddStringToObject(request, "command_key", (command_key != NULL) ? command_key : "");
    cJSON_AddBoolToObject(request, "async", false);
    cJSON_AddItemToObject(request, "input", KvToJson(input_args));

    response = Rpc(request);
    cJSON_Delete(request);

    if (response == NULL)
    {
        USP_ERR_SetMessage("%s: no response from provider", __FUNCTION__);
        return USP_ERR_COMMAND_FAILURE;
    }

    ok = cJSON_GetObjectItem(response, "ok");
    if (cJSON_IsTrue(ok) == false)
    {
        cJSON *error = cJSON_GetObjectItem(response, "error");
        USP_ERR_SetMessage("%s", cJSON_IsString(error) ? error->valuestring : "command failed");
        result = USP_ERR_COMMAND_FAILURE;
    }
    else
    {
        JsonToKv(cJSON_GetObjectItem(response, "output"), output_args);
    }

    cJSON_Delete(response);
    return result;
}

/*********************************************************************//**
**
** VdevAsyncOper
**
** Starts an asynchronous USP command in the provider and returns immediately.
**
** The provider signals progress and completion over the event connection,
** quoting the request instance number passed in here.
**
**************************************************************************/
static int VdevAsyncOper(dm_req_t *req, kv_vector_t *input_args, int instance)
{
    cJSON *request = NULL;
    cJSON *response = NULL;
    cJSON *ok = NULL;
    int result = USP_ERR_OK;

    request = cJSON_CreateObject();
    cJSON_AddStringToObject(request, "op", "operate");
    cJSON_AddStringToObject(request, "path", req->path);
    cJSON_AddBoolToObject(request, "async", true);
    cJSON_AddNumberToObject(request, "request_id", instance);
    cJSON_AddItemToObject(request, "input", KvToJson(input_args));

    response = Rpc(request);
    cJSON_Delete(request);

    if (response == NULL)
    {
        USP_ERR_SetMessage("%s: no response from provider", __FUNCTION__);
        return USP_ERR_COMMAND_FAILURE;
    }

    ok = cJSON_GetObjectItem(response, "ok");
    if (cJSON_IsTrue(ok) == false)
    {
        cJSON *error = cJSON_GetObjectItem(response, "error");
        USP_ERR_SetMessage("%s", cJSON_IsString(error) ? error->valuestring : "command failed");
        result = USP_ERR_COMMAND_FAILURE;
    }

    cJSON_Delete(response);
    return result;
}

/*********************************************************************//**
**
** VdevAsyncRestart
**
** Called for operations that were still in progress when the agent restarted.
**
** A simulated device loses in-flight operations across a reboot, exactly as
** real hardware does, so these are never restarted.
**
**************************************************************************/
static int VdevAsyncRestart(dm_req_t *req, int instance, bool *is_restart, int *err_code,
                            char *err_msg, int err_msg_len, kv_vector_t *output_args)
{
    *is_restart = false;
    *err_code = USP_ERR_COMMAND_FAILURE;
    snprintf(err_msg, err_msg_len, "operation did not survive the device reboot");
    return USP_ERR_OK;
}

/*********************************************************************//**
**
** VdevReboot
**
** Core vendor hook invoked when a controller calls Device.Reboot().
**
** Tells the provider to simulate the reboot, then returns. obuspa exits after
** this, the container restarts it, and the agent emits Boot! once the provider
** is serving again - which is a real reconnect against real agent code.
**
**************************************************************************/
static int VdevReboot(void)
{
    cJSON *request = NULL;
    cJSON *response = NULL;

    VDEV_LOG_Info("%s: telling provider to reboot", __FUNCTION__);

    request = cJSON_CreateObject();
    cJSON_AddStringToObject(request, "op", "reboot");

    response = Rpc(request);
    cJSON_Delete(request);

    if (response == NULL)
    {
        // The device may already have torn its socket down; not fatal
        VDEV_LOG_Warning("%s: provider did not acknowledge the reboot", __FUNCTION__);
    }
    else
    {
        cJSON_Delete(response);
    }

    // Terminate the agent ourselves rather than returning.
    //
    // obuspa allows either ("The vendor hook may return or may exit the
    // executable itself"), but returning here deadlocks: obuspa's exit(0) runs
    // atexit handlers while this plug-in's event thread is still blocked
    // reading the device socket. _exit() skips all of that, and is also a
    // better model of a real device losing power - nothing gets tidied up.
    VDEV_LOG_Info("%s: agent exiting for reboot", __FUNCTION__);
    _exit(0);

    return USP_ERR_OK;      // not reached
}

/*********************************************************************//**
**
** VdevFactoryReset
**
** Core vendor hook invoked when a controller calls Device.FactoryReset().
**
** By the time this runs obuspa has already reset its own database. The device
** is told to wipe its side and reboot; we then exit exactly as for a reboot.
**
**************************************************************************/
static int VdevFactoryReset(void)
{
    cJSON *request = NULL;
    cJSON *response = NULL;

    VDEV_LOG_Info("%s: telling provider to factory reset", __FUNCTION__);

    request = cJSON_CreateObject();
    cJSON_AddStringToObject(request, "op", "factory_reset");
    cJSON_AddStringToObject(request, "cause", "FactoryReset");

    response = Rpc(request);
    cJSON_Delete(request);

    if (response == NULL)
    {
        VDEV_LOG_Warning("%s: provider did not acknowledge the factory reset", __FUNCTION__);
    }
    else
    {
        cJSON_Delete(response);
    }

    VDEV_LOG_Info("%s: agent exiting for factory reset", __FUNCTION__);
    _exit(0);

    return USP_ERR_OK;      // not reached
}

/*********************************************************************//**
**
** EventThread
**
** Holds a long-lived connection to the provider and turns pushed events into
** USP signals.
**
** Runs on its own thread: only the USP_SIGNAL_XXX() functions are safe to
** call from here, which is exactly what this needs.
**
**************************************************************************/
static void *EventThread(void *arg)
{
    bool was_connected = false;

    for (;;)
    {
        int sock;
        struct sockaddr_un addr;
        FILE *stream = NULL;
        char *line = NULL;
        size_t line_size = 0;

        sock = socket(AF_UNIX, SOCK_STREAM, 0);
        if (sock == -1)
        {
            sleep(1);
            continue;
        }

        memset(&addr, 0, sizeof(addr));
        addr.sun_family = AF_UNIX;
        strncpy(addr.sun_path, SockPath(), sizeof(addr.sun_path) - 1);

        if (connect(sock, (struct sockaddr *)&addr, sizeof(addr)) == -1)
        {
            close(sock);
            sleep(1);        // provider is down (rebooting, most likely)
            continue;
        }

        if (send(sock, "{\"op\":\"events\"}\n", 16, MSG_NOSIGNAL) != 16)
        {
            close(sock);
            sleep(1);
            continue;
        }

        // Reading line-wise is much simpler with stdio than by hand
        stream = fdopen(sock, "r");
        if (stream == NULL)
        {
            close(sock);
            sleep(1);
            continue;
        }

        VDEV_LOG_Info("%s: subscribed to device events", __FUNCTION__);
        was_connected = true;

        while (getline(&line, &line_size, stream) > 0)
        {
            cJSON *event = cJSON_Parse(line);
            cJSON *name;

            if (event == NULL)
            {
                continue;
            }

            name = cJSON_GetObjectItem(event, "event");
            if (cJSON_IsString(name) == false)
            {
                cJSON_Delete(event);
                continue;
            }

            if (strcmp(name->valuestring, "operation_complete") == 0)
            {
                cJSON *request_id = cJSON_GetObjectItem(event, "request_id");
                cJSON *err_code_item = cJSON_GetObjectItem(event, "err_code");
                cJSON *err_msg_item = cJSON_GetObjectItem(event, "err_msg");
                int err_code = cJSON_IsNumber(err_code_item) ? err_code_item->valueint
                                                             : USP_ERR_OK;
                char *err_msg = NULL;

                // obuspa rejects the call unless err_msg is NULL exactly when
                // err_code is USP_ERR_OK, so an empty string will not do.
                if (err_code != USP_ERR_OK)
                {
                    err_msg = (cJSON_IsString(err_msg_item) &&
                               (err_msg_item->valuestring[0] != '\0'))
                                  ? err_msg_item->valuestring
                                  : "command failed";
                }

                // USP_SIGNAL_OperationComplete takes ownership of the args
                kv_vector_t *output = USP_ARG_Create();
                JsonToKv(cJSON_GetObjectItem(event, "output"), output);

                USP_SIGNAL_OperationComplete(
                    cJSON_IsNumber(request_id) ? request_id->valueint : 0,
                    err_code, err_msg, output);
            }
            else if (strcmp(name->valuestring, "operation_status") == 0)
            {
                cJSON *request_id = cJSON_GetObjectItem(event, "request_id");
                cJSON *status = cJSON_GetObjectItem(event, "status");

                if (cJSON_IsNumber(request_id) && cJSON_IsString(status))
                {
                    USP_SIGNAL_OperationStatus(request_id->valueint, status->valuestring);
                }
            }
            else if (strcmp(name->valuestring, "dm_event") == 0)
            {
                cJSON *event_name = cJSON_GetObjectItem(event, "name");

                if (cJSON_IsString(event_name))
                {
                    kv_vector_t *args = USP_ARG_Create();
                    JsonToKv(cJSON_GetObjectItem(event, "args"), args);
                    USP_SIGNAL_DataModelEvent(event_name->valuestring, args);
                }
            }
            else if (strcmp(name->valuestring, "object_added") == 0)
            {
                cJSON *path = cJSON_GetObjectItem(event, "path");
                if (cJSON_IsString(path))
                {
                    USP_SIGNAL_ObjectAdded(path->valuestring);
                }
            }
            else if (strcmp(name->valuestring, "object_deleted") == 0)
            {
                cJSON *path = cJSON_GetObjectItem(event, "path");
                if (cJSON_IsString(path))
                {
                    USP_SIGNAL_ObjectDeleted(path->valuestring);
                }
            }
            else if (strcmp(name->valuestring, "clock") == 0)
            {
                // The data model thread sleeps until its next timer deadline
                // in real time. Posting work to it makes it re-evaluate its
                // timers against the moved clock, so anything now due fires.
                USP_PROCESS_DoWork(ClockChanged, NULL, NULL);
            }

            cJSON_Delete(event);
        }

        free(line);
        fclose(stream);     // also closes sock

        // The agent is the device's firmware: it does not outlive the device.
        // Once the provider has been up and then goes away, whatever caused
        // that - a reset button, a factory reset, a firmware activation - has
        // to restart the agent too, so that it reconnects and emits Boot! like
        // real hardware would. The container restarts us.
        if (was_connected)
        {
            VDEV_LOG_Warning("%s: device went away - agent exiting as if power-cycled", __FUNCTION__);
            _exit(0);
        }

        sleep(1);
    }

    return NULL;
}

/*********************************************************************//**
**
** VENDOR_Init
**
** Asks the provider to describe its data model, and registers it.
**
**************************************************************************/
int VENDOR_Init(void)
{
    int err = USP_ERR_OK;
    cJSON *request = NULL;
    cJSON *response = NULL;
    cJSON *objects = NULL;
    cJSON *params = NULL;
    cJSON *commands = NULL;
    cJSON *events = NULL;
    cJSON *item = NULL;
    vendor_hook_cb_t core_callbacks;
    int count = 0;

    request = cJSON_CreateObject();
    cJSON_AddStringToObject(request, "op", "model");

    response = RpcWithRetry(request, CONNECT_RETRY_COUNT);
    cJSON_Delete(request);

    if (response == NULL)
    {
        VDEV_LOG_Error("%s: device at %s went away during init - exiting as if power-cycled", __FUNCTION__, SockPath());
        _exit(0);
    }

    // Register objects before the parameters that live inside them
    objects = cJSON_GetObjectItem(response, "objects");
    cJSON_ArrayForEach(item, objects)
    {
        cJSON *path = cJSON_GetObjectItem(item, "path");
        cJSON *writable = cJSON_GetObjectItem(item, "writable");
        if (cJSON_IsString(path))
        {
            err |= USP_REGISTER_GroupedObject(VDEV_GROUP, path->valuestring, cJSON_IsTrue(writable));
            count++;
        }
    }

    params = cJSON_GetObjectItem(response, "params");
    cJSON_ArrayForEach(item, params)
    {
        cJSON *path = cJSON_GetObjectItem(item, "path");
        cJSON *type = cJSON_GetObjectItem(item, "type");
        cJSON *writable = cJSON_GetObjectItem(item, "writable");

        if (cJSON_IsString(path) == false)
        {
            continue;
        }

        unsigned flags = TypeFlagsFromName(cJSON_IsString(type) ? type->valuestring : NULL);

        if (cJSON_IsTrue(writable))
        {
            err |= USP_REGISTER_GroupedVendorParam_ReadWrite(VDEV_GROUP, path->valuestring, flags);
        }
        else
        {
            err |= USP_REGISTER_GroupedVendorParam_ReadOnly(VDEV_GROUP, path->valuestring, flags);
        }
        count++;
    }

    err |= USP_REGISTER_GroupVendorHooks(VDEV_GROUP, VdevGetGroup, VdevSetGroup, VdevAddGroup, VdevDelGroup);

    // USP commands and events provided by the device
    commands = cJSON_GetObjectItem(response, "commands");
    err |= RegisterCommands(commands);
    count += cJSON_IsArray(commands) ? cJSON_GetArraySize(commands) : 0;

    events = cJSON_GetObjectItem(response, "events");
    err |= RegisterEvents(events);
    count += cJSON_IsArray(events) ? cJSON_GetArraySize(events) : 0;

    // Take over Device.Reboot() so that it reboots the simulated device
    memset(&core_callbacks, 0, sizeof(core_callbacks));
    core_callbacks.reboot_cb = VdevReboot;
    core_callbacks.factory_reset_cb = VdevFactoryReset;
    core_callbacks.get_active_software_version_cb = VdevGetSoftwareVersion;
    err |= USP_REGISTER_CoreVendorHooks(&core_callbacks);

    cJSON_Delete(response);

    if (err != USP_ERR_OK)
    {
        VDEV_LOG_Error("%s: failed to register proxied data model", __FUNCTION__);
        return USP_ERR_INTERNAL_ERROR;
    }

    VDEV_LOG_Info("%s: registered %d proxied data model entries from %s", __FUNCTION__, count, SockPath());

    SyncClock();
    return USP_ERR_OK;
}

/*********************************************************************//**
**
** VdevGetSoftwareVersion
**
** Serves Device.DeviceInfo.SoftwareVersion: the identity of the firmware image
** that booted, as named by the bootloader (agent/entrypoint.sh). obuspa also
** compares it across boots to set Boot!'s FirmwareUpdated argument.
**
**************************************************************************/
static int VdevGetSoftwareVersion(char *buf, int len)
{
    const char *image = getenv("VDEV_SOFTWARE_VERSION");

    snprintf(buf, len, "%s", (image != NULL) ? image : "");
    return USP_ERR_OK;
}

/*********************************************************************//**
**
** SyncClock
**
** Asks the device to rewrite the lab clock's control file, so that this
** process starts on the current lab time. Failure is not an error: a device
** without a lab clock leaves the firmware on real time.
**
**************************************************************************/
static void SyncClock(void)
{
    cJSON *request = cJSON_CreateObject();
    cJSON *response;
    cJSON *faketime;

    cJSON_AddStringToObject(request, "op", "clock_sync");
    response = Rpc(request);
    cJSON_Delete(request);

    if (response == NULL)
    {
        return;
    }

    faketime = cJSON_GetObjectItem(response, "faketime");
    if (cJSON_IsString(faketime) && strcmp(faketime->valuestring, "+0") != 0)
    {
        VDEV_LOG_Info("%s: lab clock is %s", __FUNCTION__, faketime->valuestring);
    }
    cJSON_Delete(response);
}

/*********************************************************************//**
**
** ClockChanged
**
** Runs on the data model thread after the lab moved the clock. Nothing to
** do: waking the thread is the point.
**
**************************************************************************/
static void ClockChanged(void *arg1, void *arg2)
{
    (void)arg1;
    (void)arg2;
}

/*********************************************************************//**
**
** VENDOR_Start
**
** Seeds obuspa with the object instances that already exist in the provider.
**
**************************************************************************/
int VENDOR_Start(void)
{
    cJSON *request = NULL;
    cJSON *response = NULL;
    cJSON *instances = NULL;
    cJSON *item = NULL;
    pthread_t event_thread;

    request = cJSON_CreateObject();
    cJSON_AddStringToObject(request, "op", "instances");

    response = Rpc(request);
    cJSON_Delete(request);

    if (response == NULL)
    {
        VDEV_LOG_Error("%s: could not read instances from provider", __FUNCTION__);
        return USP_ERR_INTERNAL_ERROR;
    }

    instances = cJSON_GetObjectItem(response, "instances");
    cJSON_ArrayForEach(item, instances)
    {
        if (cJSON_IsString(item))
        {
            USP_DM_InformInstance(item->valuestring);
        }
    }

    cJSON_Delete(response);

    // Start listening for pushes from the device (operation completion, events)
    if (pthread_create(&event_thread, NULL, EventThread, NULL) != 0)
    {
        VDEV_LOG_Error("%s: could not start the device event thread", __FUNCTION__);
        return USP_ERR_INTERNAL_ERROR;
    }
    pthread_detach(event_thread);

    return USP_ERR_OK;
}

/*********************************************************************//**
**
** VENDOR_Stop
**
**************************************************************************/
int VENDOR_Stop(void)
{
    return USP_ERR_OK;
}

/*********************************************************************//**
**
** VdevGetGroup
**
** Gets a batch of parameters from the provider.
**
** Per the obuspa contract, parameters that could not be obtained are simply
** left untouched, and an error is only returned if the whole call failed.
**
**************************************************************************/
static int VdevGetGroup(int group_id, kv_vector_t *params)
{
    cJSON *request = NULL;
    cJSON *response = NULL;
    cJSON *paths = NULL;
    cJSON *values = NULL;
    cJSON *ok = NULL;
    int i;

    if (params->num_entries == 0)
    {
        return USP_ERR_OK;
    }

    request = cJSON_CreateObject();
    cJSON_AddStringToObject(request, "op", "get");
    paths = cJSON_AddArrayToObject(request, "paths");
    for (i = 0; i < params->num_entries; i++)
    {
        cJSON_AddItemToArray(paths, cJSON_CreateString(params->vector[i].key));
    }

    response = Rpc(request);
    cJSON_Delete(request);

    if (response == NULL)
    {
        VDEV_LOG_Error("%s: no response from provider", __FUNCTION__);
        return USP_ERR_INTERNAL_ERROR;
    }

    ok = cJSON_GetObjectItem(response, "ok");
    if (cJSON_IsTrue(ok) == false)
    {
        cJSON *error = cJSON_GetObjectItem(response, "error");
        VDEV_LOG_Error("%s: provider refused get: %s", __FUNCTION__,
                      cJSON_IsString(error) ? error->valuestring : "unknown");
        cJSON_Delete(response);
        return USP_ERR_INTERNAL_ERROR;
    }

    // Copy returned values back into the caller's vector. The hint is the index
    // we expect the key at, which is right in the common case of the provider
    // answering in request order.
    values = cJSON_GetObjectItem(response, "values");
    for (i = 0; i < params->num_entries; i++)
    {
        cJSON *value = cJSON_GetObjectItem(values, params->vector[i].key);
        if (cJSON_IsString(value))
        {
            USP_ARG_ReplaceWithHint(params, params->vector[i].key, value->valuestring, i);
        }
    }

    cJSON_Delete(response);
    return USP_ERR_OK;
}

/*********************************************************************//**
**
** VdevSetGroup
**
** Sets a batch of parameters in the provider.
**
**************************************************************************/
static int VdevSetGroup(int group_id, kv_vector_t *params, unsigned *types, int *failure_index)
{
    cJSON *request = NULL;
    cJSON *response = NULL;
    cJSON *values = NULL;
    cJSON *ok = NULL;
    int i;

    *failure_index = FAILURE_INDEX_UNKNOWN;

    if (params->num_entries == 0)
    {
        return USP_ERR_OK;
    }

    request = cJSON_CreateObject();
    cJSON_AddStringToObject(request, "op", "set");
    values = cJSON_AddObjectToObject(request, "params");
    for (i = 0; i < params->num_entries; i++)
    {
        cJSON_AddStringToObject(values, params->vector[i].key, params->vector[i].value);
    }

    response = Rpc(request);
    cJSON_Delete(request);

    if (response == NULL)
    {
        VDEV_LOG_Error("%s: no response from provider", __FUNCTION__);
        return USP_ERR_INTERNAL_ERROR;
    }

    ok = cJSON_GetObjectItem(response, "ok");
    if (cJSON_IsTrue(ok) == false)
    {
        cJSON *error = cJSON_GetObjectItem(response, "error");
        cJSON *index = cJSON_GetObjectItem(response, "failure_index");

        if (cJSON_IsNumber(index))
        {
            *failure_index = index->valueint;
        }

        VDEV_LOG_Error("%s: provider refused set: %s", __FUNCTION__,
                      cJSON_IsString(error) ? error->valuestring : "unknown");
        cJSON_Delete(response);
        return USP_ERR_SET_FAILURE;
    }

    cJSON_Delete(response);
    return USP_ERR_OK;
}

/*********************************************************************//**
**
** VdevAddGroup
**
** Asks the provider to create an object instance.
**
**************************************************************************/
static int VdevAddGroup(int group_id, char *path, int *instance)
{
    cJSON *request = NULL;
    cJSON *response = NULL;
    cJSON *ok = NULL;
    cJSON *created = NULL;

    request = cJSON_CreateObject();
    cJSON_AddStringToObject(request, "op", "add");
    cJSON_AddStringToObject(request, "path", path);

    response = Rpc(request);
    cJSON_Delete(request);

    if (response == NULL)
    {
        return USP_ERR_CREATION_FAILURE;
    }

    ok = cJSON_GetObjectItem(response, "ok");
    created = cJSON_GetObjectItem(response, "instance");

    if ((cJSON_IsTrue(ok) == false) || (cJSON_IsNumber(created) == false))
    {
        cJSON_Delete(response);
        return USP_ERR_CREATION_FAILURE;
    }

    *instance = created->valueint;
    cJSON_Delete(response);
    return USP_ERR_OK;
}

/*********************************************************************//**
**
** VdevDelGroup
**
** Asks the provider to delete an object instance.
**
**************************************************************************/
static int VdevDelGroup(int group_id, char *path)
{
    cJSON *request = NULL;
    cJSON *response = NULL;
    cJSON *ok = NULL;

    request = cJSON_CreateObject();
    cJSON_AddStringToObject(request, "op", "delete");
    cJSON_AddStringToObject(request, "path", path);

    response = Rpc(request);
    cJSON_Delete(request);

    if (response == NULL)
    {
        return USP_ERR_OBJECT_NOT_DELETABLE;
    }

    ok = cJSON_GetObjectItem(response, "ok");
    if (cJSON_IsTrue(ok) == false)
    {
        cJSON_Delete(response);
        return USP_ERR_OBJECT_NOT_DELETABLE;
    }

    cJSON_Delete(response);
    return USP_ERR_OK;
}
