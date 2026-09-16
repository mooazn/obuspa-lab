/*
 * parental_controls.c - vendor logic that acts on the simulated hardware.
 *
 * A controller manages a vendor object, Device.X_VDEV_ParentalControls.Rule.{i},
 * each rule naming a client's MAC. A background thread keeps the device's
 * firewall in step: every enabled rule has a matching Drop rule in
 * Device.Firewall.Chain.1, and the device derives the consequence - the
 * client's Hosts.Host and AssociatedDevice rows go inactive.
 *
 * Nothing here knows it is running on a simulator. The plug-in reaches the
 * hardware the same three ways real firmware does:
 *
 *   read    USP_DM_GetInstances / USP_DM_GetParameterValue, on the data
 *           model thread via USP_PROCESS_DoWorkSync
 *   change  USP_PROCESS_DM_SetParameterValue, from the worker thread
 *   create  vhal_dm_add / vhal_dm_set  - the hardware layer
 *   remove  vhal_dm_delete             - the hardware layer
 *
 * Values belong to the data model; rows belong to the hardware. obuspa's API
 * cannot create instances, and USP_DM_DeleteInstance may only run inside a
 * transaction obuspa itself opened (a Set or Operate callback), not from a
 * vendor thread. On a real device both are calls into the firewall engine,
 * which then informs obuspa.
 *
 * Exposes:
 *   Device.X_VDEV_ParentalControls.Enable            (bool, read-write, persisted)
 *   Device.X_VDEV_ParentalControls.Rule.{i}.Enable   (bool, read-write, persisted)
 *   Device.X_VDEV_ParentalControls.Rule.{i}.MACAddress
 *   Device.X_VDEV_ParentalControls.Rule.{i}.Description
 *   Device.X_VDEV_ParentalControls.Rule.{i}.Status   ("Blocking" | "Idle", live)
 *   Device.X_VDEV_ParentalControls.RuleApplied!      (Rule, MACAddress, FirewallRule)
 *
 * Firewall rules created here carry Description "X_VDEV_ParentalControls:<n>"
 * so that ownership survives a reboot and the map can be rebuilt.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <unistd.h>
#include <pthread.h>

#include "usp_err_codes.h"
#include "vendor_defs.h"
#include "vendor_api.h"
#include "usp_api.h"
#include "vhal.h"

#define LOG(...)  USP_LOG_Printf(kLogLevel_Info, kLogType_Debug, __VA_ARGS__)

#define OBJ         "Device.X_VDEV_ParentalControls."
#define RULE        OBJ "Rule.{i}."
#define EVENT       OBJ "RuleApplied!"
#define CHAIN       "Device.Firewall.Chain.1."
#define FW_RULE     CHAIN "Rule."
#define TAG         "X_VDEV_ParentalControls:"

#define MAX_RULES   64
#define POLL_SECS   2

// ---------------------------------------------------------------------------
// State shared between the data model thread and the worker

typedef struct {
    int  instance;              // vendor rule instance
    bool enabled;
    char mac[32];
    int  firewall_rule;         // 0 = none yet
} rule_t;

static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;
static rule_t rules[MAX_RULES];
static int    num_rules = 0;
static bool   global_enable = true;
static volatile bool dirty = true;      // something changed: reconcile soon

static rule_t *FindRule(int instance)
{
    for (int i = 0; i < num_rules; i++)
        if (rules[i].instance == instance)
            return &rules[i];
    return NULL;
}

// ---------------------------------------------------------------------------
// Data model callbacks (data model thread)

static int Get_Status(dm_req_t *req, char *buf, int len)
{
    const char *status = "Idle";
    pthread_mutex_lock(&lock);
    rule_t *r = FindRule(req->inst->instances[0]);
    if (r != NULL && r->firewall_rule > 0 && r->enabled && global_enable)
        status = "Blocking";
    pthread_mutex_unlock(&lock);
    strncpy(buf, status, len - 1);
    buf[len - 1] = '\0';
    return USP_ERR_OK;
}

// Any change to the vendor object: mark for reconciliation
static int Notify_Changed(dm_req_t *req, char *value)   { dirty = true; return USP_ERR_OK; }
static int Notify_Instance(dm_req_t *req)               { dirty = true; return USP_ERR_OK; }

// ---------------------------------------------------------------------------
// Snapshot: read the vendor rules and the firewall on the data model thread

typedef struct {
    rule_t  desired[MAX_RULES];
    int     num_desired;
    bool    global;
    int     fw_instances[MAX_RULES];
    int     fw_owner[MAX_RULES];     // vendor rule each firewall rule is tagged with
    int     num_fw;
} snapshot_t;

static void Snapshot(void *arg1, void *arg2)
{
    snapshot_t *snap = arg1;
    int instances[MAX_RULES];
    int n = 0;
    char value[256];
    char path[256];

    snap->num_desired = 0;
    snap->num_fw = 0;

    snap->global = true;
    if (USP_DM_GetParameterValue(OBJ "Enable", value, sizeof value) == USP_ERR_OK)
        snap->global = (strcmp(value, "true") == 0 || strcmp(value, "1") == 0);

    if (USP_DM_GetInstances(OBJ "Rule.", instances, MAX_RULES, &n) == USP_ERR_OK) {
        for (int i = 0; i < n; i++) {
            rule_t *d = &snap->desired[snap->num_desired++];
            memset(d, 0, sizeof *d);
            d->instance = instances[i];
            snprintf(path, sizeof path, OBJ "Rule.%d.Enable", instances[i]);
            d->enabled = USP_DM_GetParameterValue(path, value, sizeof value) == USP_ERR_OK
                         && (strcmp(value, "true") == 0 || strcmp(value, "1") == 0);
            snprintf(path, sizeof path, OBJ "Rule.%d.MACAddress", instances[i]);
            if (USP_DM_GetParameterValue(path, value, sizeof value) == USP_ERR_OK)
                strncpy(d->mac, value, sizeof d->mac - 1);
        }
    }

    if (USP_DM_GetInstances(FW_RULE, instances, MAX_RULES, &n) == USP_ERR_OK) {
        for (int i = 0; i < n; i++) {
            snprintf(path, sizeof path, FW_RULE "%d.Description", instances[i]);
            if (USP_DM_GetParameterValue(path, value, sizeof value) != USP_ERR_OK)
                continue;
            if (strncmp(value, TAG, strlen(TAG)) != 0)
                continue;               // not ours
            snap->fw_instances[snap->num_fw] = instances[i];
            snap->fw_owner[snap->num_fw] = atoi(value + strlen(TAG));
            snap->num_fw++;
        }
    }
}

// ---------------------------------------------------------------------------
// Reconcile: make the firewall match the vendor rules (worker thread)

static void RaiseApplied(int rule, const char *mac, int fw)
{
    char rule_s[16], fw_s[16];
    snprintf(rule_s, sizeof rule_s, "%d", rule);
    snprintf(fw_s, sizeof fw_s, "%d", fw);
    kv_vector_t *args = USP_ARG_Create();       // ownership passes to obuspa
    USP_ARG_Add(args, "Rule", rule_s);
    USP_ARG_Add(args, "MACAddress", (char *)mac);
    USP_ARG_Add(args, "FirewallRule", fw_s);
    USP_SIGNAL_DataModelEvent(EVENT, args);
}

static int CreateFirewallRule(const rule_t *d)
{
    char path[256], tag[64], err[256];
    int fw = 0;

    if (vhal_dm_add(FW_RULE, &fw) != 0) {
        LOG("parental-controls: could not create a firewall rule for rule %d", d->instance);
        return 0;
    }
    snprintf(tag, sizeof tag, TAG "%d", d->instance);
    snprintf(path, sizeof path, FW_RULE "%d.Description", fw);  vhal_dm_set(path, tag, err, sizeof err);
    snprintf(path, sizeof path, FW_RULE "%d.Target", fw);       vhal_dm_set(path, "Drop", err, sizeof err);
    snprintf(path, sizeof path, FW_RULE "%d.SourceMAC", fw);    vhal_dm_set(path, d->mac, err, sizeof err);
    snprintf(path, sizeof path, FW_RULE "%d.Enable", fw);       vhal_dm_set(path, "true", err, sizeof err);
    LOG("parental-controls: rule %d (%s) -> firewall rule %d", d->instance, d->mac, fw);
    RaiseApplied(d->instance, d->mac, fw);
    return fw;
}

static void SetFirewallParam(int fw, const char *param, const char *value)
{
    char path[256], err[256] = "";
    snprintf(path, sizeof path, FW_RULE "%d.%s", fw, param);
    if (USP_PROCESS_DM_SetParameterValue(path, (char *)value, err, sizeof err) != USP_ERR_OK)
        LOG("parental-controls: set %s=%s failed: %s", path, value, err);
}

static void Reconcile(void)
{
    snapshot_t snap;
    char path[256];

    memset(&snap, 0, sizeof snap);
    USP_PROCESS_DoWorkSync(Snapshot, &snap, NULL);

    pthread_mutex_lock(&lock);
    global_enable = snap.global;
    num_rules = snap.num_desired;
    memcpy(rules, snap.desired, sizeof(rule_t) * (size_t)snap.num_desired);

    // Attach existing firewall rules to their owners (survives a reboot)
    for (int i = 0; i < snap.num_fw; i++) {
        rule_t *r = FindRule(snap.fw_owner[i]);
        if (r != NULL)
            r->firewall_rule = snap.fw_instances[i];
    }
    pthread_mutex_unlock(&lock);

    for (int i = 0; i < num_rules; i++) {
        rule_t *r = &rules[i];
        bool want = snap.global && r->enabled && r->mac[0] != '\0';

        if (want && r->firewall_rule == 0) {
            int fw = CreateFirewallRule(r);
            pthread_mutex_lock(&lock);
            r->firewall_rule = fw;
            pthread_mutex_unlock(&lock);
        } else if (r->firewall_rule > 0) {
            // Keep the existing row in step: MAC and on/off through obuspa
            SetFirewallParam(r->firewall_rule, "SourceMAC", r->mac);
            SetFirewallParam(r->firewall_rule, "Enable", want ? "true" : "false");
        }
    }

    // Firewall rules tagged for vendor rules that no longer exist
    for (int i = 0; i < snap.num_fw; i++) {
        bool owned = false;
        for (int j = 0; j < num_rules; j++)
            if (rules[j].instance == snap.fw_owner[i])
                owned = true;
        if (!owned) {
            snprintf(path, sizeof path, FW_RULE "%d", snap.fw_instances[i]);
            LOG("parental-controls: removing orphaned firewall rule %d", snap.fw_instances[i]);
            vhal_dm_delete(path);
        }
    }
}

static void *Worker(void *arg)
{
    for (;;) {
        if (dirty) {
            dirty = false;
            Reconcile();
        }
        sleep(POLL_SECS);
    }
    return NULL;
}

// ---------------------------------------------------------------------------
// Plug-in entry points

int VENDOR_Init(void)
{
    int err = USP_ERR_OK;
    char *event_args[] = { "Rule", "MACAddress", "FirewallRule" };

    err |= USP_REGISTER_DBParam_ReadWrite(OBJ "Enable", "true", NULL, Notify_Changed, DM_BOOL);
    err |= USP_REGISTER_Object(OBJ "Rule.{i}", NULL, NULL, Notify_Instance, NULL, NULL, Notify_Instance);
    err |= USP_REGISTER_DBParam_ReadWrite(RULE "Enable", "true", NULL, Notify_Changed, DM_BOOL);
    err |= USP_REGISTER_DBParam_ReadWrite(RULE "MACAddress", "", NULL, Notify_Changed, DM_STRING);
    err |= USP_REGISTER_DBParam_ReadWrite(RULE "Description", "", NULL, Notify_Changed, DM_STRING);
    err |= USP_REGISTER_VendorParam_ReadOnly(RULE "Status", Get_Status, DM_STRING);
    err |= USP_REGISTER_Event(EVENT);
    err |= USP_REGISTER_EventArguments(EVENT, event_args, 3);

    return (err == USP_ERR_OK) ? USP_ERR_OK : USP_ERR_INTERNAL_ERROR;
}

int VENDOR_Start(void)
{
    pthread_t thread;
    if (pthread_create(&thread, NULL, Worker, NULL) != 0)
        return USP_ERR_INTERNAL_ERROR;
    pthread_detach(thread);
    LOG("parental-controls: reconciling rules against " CHAIN " every %ds", POLL_SECS);
    return USP_ERR_OK;
}

int VENDOR_Stop(void)
{
    return USP_ERR_OK;
}
