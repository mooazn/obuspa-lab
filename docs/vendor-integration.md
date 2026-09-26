# Running your own code on the virtual platform

This platform runs an unmodified OB-USP-AGENT against simulated hardware. If
you have code of your own that lives inside an obuspa build — a vendor data
model, a background thread that watches something and raises alarms, a HAL —
you can run it here, stimulate it, and watch what it does over USP, without a
board.

The one idea everything below follows from:

> **The agent container is firmware. The device container is hardware.**
> Your code is firmware. It reaches the world through the environment (files,
> syscalls, sockets), which the lab can perturb, or — only if it needs to —
> through a tiny hardware abstraction the lab can drive. It never talks to
> the simulator directly, and the simulator never learns what your code does.

That is what keeps the two agnostic of each other, and what makes the same
code run unchanged on real hardware.

---

## 1. Getting your code onto the device

Two routes. Both end with "seat the card, press reset".

### As a plug-in (recommended)

obuspa loads shared objects with `-x`, and a plug-in is exactly the shape
"my logic, initialised at boot" takes. Build yours as its own `.so` and it
goes onto the SD card next to the platform's data-model proxy:

```bash
make flash PLUGINS="path/to/my-plugin"            # against the built-in obuspa (v11)
make flash PLUGINS="a/ b/" SRC=~/obuspa           # against your own tree, booted too
```

No fork of obuspa, no rebuild of the platform, your code isolated from ours.

A plug-in directory is one of:

- **A directory with a `Makefile`.** It is run as
  `make OBUSPA_SRC=<tree> VHAL_SRC=<vhal dir>` and must leave exactly one
  `*.so` behind. The Makefile owns its flags; `examples/disk-monitor/Makefile`
  is a good template.
- **A directory of `.c` files, no Makefile.** Everything is compiled into one
  `.so` with `-fPIC -DENABLE_UDS -I<tree>/src/{include,vendor,core}` and
  `-lpthread`, plus the vhal client (see §4).

The directory's name becomes the plug-in's name. Plug-ins are compiled inside
the platform's build container, **against the obuspa tree the card will
boot** — the built-in one, or the one you flashed with `SRC`/`REF`. That is
deliberate: obuspa's vendor API moves between releases, and this catches it
before it reaches a device (see §7, logging).

### As a whole tree

If your logic is tangled into obuspa itself (`src/vendor/vendor.c`, patches to
core), flash the tree:

```bash
make flash SRC=~/obuspa
```

Everything compiled in comes along.

### What the card holds

`sdcard/` after a flash: optionally `obuspa` and `vdev_plugin.so` (the
platform's proxy, rebuilt against your tree), `plugins/<name>.so` for each
plug-in, and `manifest.json` describing all of it. Flashing rewrites the whole
card. `make eject` wipes it.

At boot, the agent's entrypoint (`agent/entrypoint.sh`, the "bootloader")
boots the card's `obuspa` if present, else the built-in; and loads every
`plugins/*.so` with its own `-x`, **after** the proxy plug-in, in name order.
The "booted from" tag in the 3D view and `GET /api/state` → `system.bootedFrom`
say what actually ran.

Over USP, `Device.DeviceInfo.SoftwareVersion` names the image that booted:
`<label>-<commit>` for a card's obuspa, `builtin-<release>` for the built-in
one, `builtin-<release>+<label>` for plug-ins on top of it.
`Device.LocalAgent.SoftwareVersion` stays the obuspa release. Booting a
different image than last time sets `FirmwareUpdated` in `Boot!`, as a
firmware update would on hardware.

---

## 2. The plug-in contract

What obuspa expects of a plug-in, and what this platform adds.

**Entry points.** Export `int VENDOR_Init(void)`, `int VENDOR_Start(void)`,
`int VENDOR_Stop(void)`. `Init` runs before the data model is live: register
things (`USP_REGISTER_*`). `Start` runs once the database is readable: read
persisted settings, spawn threads. Optionally export
`VENDOR_GetFactoryResetParams`.

**Loading is strict.** Plug-ins are `dlopen(RTLD_NOW)`ed: a single unresolved
symbol and obuspa exits before it ever connects to the device. On the
platform that looks like a crash loop — the agent container restarting every
few seconds and the Console tab showing the same boot over and over. The
dlopen error is in that console. (`system.agent.eventsConnected` stays
`false` the whole time; the 3D view's power LED never settles.)

**Order, and what the platform owns.** Plug-ins initialise in `-x` order.
The platform's proxy comes first, so the TR-181 data model it serves
(`Device.WiFi.*`, `Device.Hosts.*`, `Device.Firewall.*`, `Device.Cellular.*`,
…) exists by the time yours runs. Those standard, hardware-facing subtrees
are the platform's: they are the simulated hardware. Register your own
objects under a vendor prefix (`Device.X_<VENDOR>_Thing.`).

Registering a path the platform already provides fails. obuspa logs the
exact path and returns `USP_ERR_INTERNAL_ERROR` (7003) from the
`USP_REGISTER_*` call:

```
DM_PRIV_AddSchemaPath: Path Device.WiFi.SSID.{i}.SSID already exists in schema
```

What happens next depends on your `VENDOR_Init`. Return the error and obuspa
exits immediately - a crash loop, flagged in the Console tab and on the boot
tag, with that line showing on every pass. Swallow it and the agent comes up
with your registration silently missing, which is harder to notice; prefer
the loud failure. Either way the fix is the same: keep to a vendor prefix,
and act on the standard subtrees through the data model (§3).

**`VENDOR_Stop` never runs.** A simulated reboot is a power cycle — the agent
is `_exit()`ed, not shut down — exactly as firmware loses power. Do not rely
on `Stop` for anything.

**Threads.** obuspa has one data-model thread. The rules, from `usp_api.h`:

| From your thread | From the data-model thread only |
|---|---|
| `USP_SIGNAL_DataModelEvent`, `USP_SIGNAL_OperationComplete/Status`, `USP_SIGNAL_ObjectAdded/Deleted`, `USP_SIGNAL_Reboot` | every `USP_REGISTER_*`, every `USP_DM_*` |

To touch the data model from a thread, hop:
`USP_PROCESS_DoWork(callback, arg1, arg2)` runs `callback` on the data-model
thread. For a *live* value, do not write it into the model at all — register
a read-only vendor parameter with a getter that reads a cache your thread
updates under a mutex. Keep getters non-blocking; they run inside USP request
handling.

**Reboots.** Every reboot on this platform is real: the agent process exits
and restarts, and a subscribed controller gets `Boot!`. Your `VENDOR_Start`
runs again. State you did not persist is gone — same as hardware.

---

## 3. Acting on the hardware

Vendor logic changes things: it blocks a client, brings an interface down,
rewrites a firewall. On a real device that logic reaches the hardware through
the data model the vendor implemented on top of the drivers. On the platform
the standard TR-181 subtrees *are* the simulated hardware, so the same calls
reach it:

```c
/* on the data model thread (VENDOR_Start, a callback, a DoWork callback) */
USP_DM_GetParameterValue("Device.WiFi.SSID.1.SSID", buf, sizeof buf);
USP_DM_SetParameterValue("Device.WiFi.Radio.2.Enable", "false");

/* from your own thread */
USP_PROCESS_DM_SetParameterValue("Device.Firewall.Chain.1.Rule.3.Enable", "true", err, sizeof err);
USP_PROCESS_DoWorkSync(snapshot_callback, &snapshot, NULL);   /* runs on the data model thread */
```

The device reacts as hardware would: disable a radio and its SSIDs go down
and its clients drop; add a Drop rule naming a MAC and that client's
`Hosts.Host` and `AssociatedDevice` rows go inactive. Nothing in the plug-in
knows it is not talking to a driver.

**Values are the data model's; rows are the hardware's.** obuspa's API reads
and sets parameters. It cannot create an instance, and `USP_DM_DeleteInstance`
may only run inside a transaction obuspa itself opened (a Set or Operate
callback) - calling it from a vendor thread trips an assertion and exits the
agent. On a real device creating and deleting rows is the vendor's hardware
layer: the call into the firewall engine, which then informs obuspa. On the
platform that layer is the HAL:

```c
int fw;
vhal_dm_add("Device.Firewall.Chain.1.Rule.", &fw);                 /* -> instance number */
vhal_dm_set("Device.Firewall.Chain.1.Rule.3.SourceMAC", mac, err, sizeof err);
vhal_dm_delete("Device.Firewall.Chain.1.Rule.3");
```

These act with hardware privileges - they may create, set and delete what a
controller may not - and the device signals obuspa, so a subscribed
controller sees the ObjectCreation. Nothing may set a *derived* effect
directly (`Hosts.Host.Active`, `AssociatedDevice.Active`): obuspa refuses
with 7013 for firmware and controllers alike. Write the cause; the device
derives the effect.

`examples/parental-controls/` uses every path above.

## 4. Stimulating your code

### Through the environment (no changes to your code)

Vendor code that reads the world through POSIX gets real symptoms from lab
faults. The **Faults** tab, or `POST /api/faults {"kind": …, "params": …}`,
`DELETE /api/faults/<kind>`. One instance per kind; POSTing again updates it.

| Kind | Params | What your code sees |
|---|---|---|
| `disk_fill` | `percent` 1–100 | The data partition (`/data`, a 64 MiB tmpfs; `VDEV_DATA_PATH`/`VDEV_DATA_SIZE` in compose) fills with ballast. `statvfs()`, `df`, `/proc/mounts` all report it. |
| `wan_down` | — | The agent's broker connection is cut ~1 s later and refused until cleared. The agent reconnects on its own retry timer (5–10 s, doubling to ~30 s). |
| `wan_latency` | `ms` 0–2000 | Every byte through the WAN is delayed, each direction. |

Faults are **device state**: they persist, they survive reboots, and they are
re-applied as the agent comes up (a full disk stays full across a power
cycle). They survive a factory reset too — the lab's injector is not device
configuration. Applied state and the daemon's detail/error come back in
`GET /api/faults`.

Disabling the WAN over USP (`Device.IP.Interface.1.Enable = false`) is a real
link loss too: the Set is answered, then the link drops. Re-enable it through
the UI or `POST /api/set`, since the controller can no longer reach the agent.

### Through the clock

Anything scheduled hours or days out - periodic statistics windows,
time-referenced sample intervals, `Periodic!`, retry backoffs, your own
timers - can be exercised in seconds by moving the lab clock. The
**Faults & clock** tab, or:

```
POST   /api/clock {"jump": 3600}         # one hour ahead, for the device and the firmware
POST   /api/clock {"rate": 60}           # run at 60x; jump and rate may be combined
DELETE /api/clock                        # back to real time
GET    /api/clock                        # {"real", "offset", "rate", "now", "nowIso"}
```

The firmware runs under [libfaketime](https://github.com/wolfcw/libfaketime):
`time()`, `gettimeofday()`, `clock_gettime(CLOCK_REALTIME)` and the sleeping
calls (`poll`, `select`, `nanosleep`, …) all follow the lab clock, and a rate
above 1 shortens every wait. Your code needs no change to be affected, and
nothing in it can tell. Two things stay real: `CLOCK_MONOTONIC`, and the
kernel's uptime counter (`Device.DeviceInfo.UpTime`, `/proc/uptime`) - the
same things a wall-clock change leaves alone on hardware.

A jump takes effect at once: the agent's timer loop is woken so that
whatever the jump made due fires immediately, rather than at the next
unrelated activity. The clock persists across reboots the way an RTC keeps
its time, and a factory reset does not touch it. The rate is capped at 60:
the controller and the broker are on real time, so a fast firmware pings
and retries more often than they expect.

libfaketime's time functions hold a lock (the multithreaded build, which
the clock needs to stay correct under concurrent callers). A process that
forks while another of its threads is reading the time leaves the child
holding that lock, so a child that reads the time before `exec` hangs.
Children that go straight to `exec`, as obuspa's own do, are unaffected.

### Through the virtual HAL (opt-in)

The HAL is the escape hatch for what the data model does not cover: reading
hardware the model does not represent, and creating or deleting rows (§3).
Code that reads a temperature sensor, a modem's RSSI, a chipset SDK — things
a container does not have — has nothing to read here. If that code sits
behind a HAL of your own (`hal_get_temp()`, one backend per board), write a
simulator backend against `plugin/vhal/vhal.h`:

```c
#include "vhal.h"

char buf[16];
if (vhal_get("thermal.cpu", buf, sizeof buf) == 0)   /* 0 set, 1 unset, -1 no device */
    temp = atoi(buf);

vhal_set("modem.last_at_cmd", "AT+CSQ");
vhal_watch("wifi.", on_change, ctx);                  /* blocks; -1 when the device reboots */
```

- Keys are a free namespace. The platform defines none; you pick them. The
  **HAL** tab, `GET/PUT/DELETE /api/hal/<key>`, and tests set values.
- Values are strings, volatile, and — being hardware — survive reboots and
  factory resets.
- `vhal.c` is one dependency-free file (POSIX sockets, no obuspa headers,
  no cJSON), safe from any thread, and never exits the process: a lost
  socket is a `-1`, so your thread survives the device rebooting under it.
- `build-plugins.sh` compiles it in automatically and defines `HAVE_VHAL`;
  a Makefile can `include $(VHAL_SRC)/Makefile.inc`. Guard the call with
  `#ifdef HAVE_VHAL` and your production build never sees it.

The disk-monitor example uses exactly one call, to let the lab override its
threshold as if it came from a board's EEPROM.

---

## 5. Observing what it did

- **USP tab** / `GET /api/usp/timeline` / `WS /ws/usp` — every record between
  agent and controller, decoded: direction, message type, a one-line summary,
  the full body on click. `GET /api/usp/notifications` filters to Notify. This
  is where you watch your alarm leave the device.
- **USP tab → Browse** — the data model as a controller sees it, including
  everything the agent serves itself (`LocalAgent`, `PeriodicStatistics`, an
  object your plug-in registers without touching the device), which the Data
  model tab cannot show. Get a path to a chosen depth, click a value to Set
  it, add and delete instances, run a command. Errors are the agent's own,
  with their USP code. Requests go as the lab's controller, `self::vdev-lab`
  (`Controller.2`), a different identity from the test suite's
  (`self::usp-controller`, `Controller.1`), so what you create by hand is
  owned by, and attributed to, `Controller.2`. The same calls are
  `POST /api/usp/{get,set,add,delete,operate}`.
- **Console tab** / `GET /api/console` / `WS /ws/console` — everything the
  agent container prints: bootloader lines, obuspa's log, your plug-in's
  output, the fault daemon. Rotated per boot; boot boundaries are marked.
- **From a test** — `controller.subscribe("Event", "Device.X_VDEV_Thing.Alarm!")`
  then `controller.wait_for_notification(...)`. See
  `tests/test_06_vendor_plugin.py` for the full disk-monitor scenario:
  flash → seat → reboot → subscribe → fill → assert → clear.

---

## 6. Bringing your own controller

The lab's own controllers are test tools. To see your work through a real
controller - the one your operator runs, or an open source one - plug it
into the agent as another controller. The lab stays controller-agnostic:
nothing in it knows which controller is attached.

**Oktopus, ready made.** [Oktopus](https://github.com/OktopUSP/oktopus) is
an open source USP controller and device management platform.

```
make oktopus         # fetch the pinned release, start it, plug it into the agent
                     # UI: http://127.0.0.1:8090 - create an admin account on first visit
make oktopus-down    # unplug it and stop it (its data is kept in .oktopus/)
```

The device appears in Oktopus's inventory as `os::vdev-001`, and everything
Oktopus does - Get, Set, Add, Delete, Operate, reboots - reaches the
simulated hardware through the agent, exactly as the lab's own requests do.

**How a controller is attached.** A controller reached over its own MQTT
connection needs three rows in the agent: a `Device.MQTT.Client` (the
connection to its broker), a `Device.LocalAgent.MTP` (the agent listening on
it) and a `Device.LocalAgent.Controller`. The device creates them from a
small definition, `controllers/oktopus.json` being the example:

```
POST   /api/controllers {name, endpointId, broker: {address, port},
                         controllerTopic, agentTopic?, role?}
GET    /api/controllers
DELETE /api/controllers/<name>
```

Every row carries the name as its `Alias`. Plugging in again replaces the
rows, and the agent makes a fresh connection. To take a controller offline
without removing it, set `Device.MQTT.Client.[Alias=="<name>"].Enable` to
`false` in the Browse view.

**The second WAN route.** The agent reaches another controller's broker
through the device's WAN port, like its own: `mosquitto:1884` leads to
whatever is attached to the `vdev-lab` network as `controller-broker` (Oktopus's
broker is). WAN faults cut both routes and latency applies to both, so a
`wan_down` takes the device offline in the other controller too. To use your
own controller, attach its broker to `vdev-lab` with that alias and write a
definition for it; a broker reachable some other way needs only the right
`broker` address in the definition, but then bypasses the WAN faults.

**Things to know.**
- The rows live in the agent's database: they survive reboots and are
  removed by a factory reset - including the one the test suite performs.
  Plug in again afterwards (`make oktopus`).
- Oktopus lists a device when its broker sees the agent subscribe, and does
  not ask again. `make oktopus` waits for Oktopus to be ready before
  plugging in; if Oktopus ever shows the device offline while the lab says
  it is connected, plugging in again makes a fresh connection.
- A controller that tracks devices by endpoint ID sees one device per
  agent. Two connections from the agent to the same broker confuse it: when
  either closes, the device is marked offline.
- Oktopus's images are amd64 only; on Apple Silicon they run emulated.
- `tests/test_07_oktopus.py` checks the device is listed, that Oktopus reads
  and writes the hardware, and that WAN faults reach it. It runs only with
  Oktopus up and an account given in `VDEV_OKTOPUS_EMAIL` and
  `VDEV_OKTOPUS_PASSWORD`.

---

## 7. Things that bite

**Logging.** Use `USP_LOG_Printf(kLogLevel_Info, kLogType_Debug, fmt, …)`.
The convenience macros `USP_LOG_Error/Warning/Info` are **not stable across
obuspa releases** — v10's expand to globals a plug-in cannot see and fail to
link; v11's do not. Because plug-ins are compiled against the tree being
flashed, this shows up at flash time rather than on a device, which is the
point. `printf` to stdout works but is block-buffered under the console's
pipe; prefer `USP_LOG_Printf` or stderr.

**`-DENABLE_UDS`.** obuspa's `vendor_defs.h` has consistency checks that fail
unless the plug-in is compiled with the same feature defines as obuspa. The
bare-`.c` build sets it; a Makefile must too.

**Crash loop.** See §2. Eject the card (SD slot in the 3D view or
`POST /api/sdcard {"action":"eject"}`) and press reset to get back to the
built-in image; the console tells you what failed.

**"I updated my plug-in and nothing changed."** Two ways to boot without
your code and not notice, both now called out in amber on the boot tag in the
3D view:

- *Card in the slot but not seated.* The built-in image boots; it is stock
  firmware and comes up perfectly healthy. Your object is simply absent —
  `Get Device.X_VENDOR_Thing.` returns error 7026 "Path is invalid", the boot
  report lists no plug-ins, and the console has no plug-in lines after the
  boot marker. Seat the card and press reset.
- *Card seated after the boot.* Seating a card does nothing until the next
  reset; the tag says so. (Rebooting while the previous boot is still in its
  bootloader is fine: the bootloader waits for the device before deciding
  what to boot, so the decision reflects the card as it is then.)

`tests/test_06_vendor_plugin.py::test_vendor_logic_is_absent_without_the_card`
pins the first case.

**"Failed to determine controller trust role - No cert chain".** Logged on
every MQTT CONNACK. Benign here: the broker connection is plaintext, so there
is no TLS chain to derive a role from, and obuspa falls back to the role for
non-TLS connections (`ROLE_NON_SSL`, full access in `vendor_defs.h`). On a
real deployment with TLS it would mean the controller's certificate could not
be mapped to a role.

**A row you just created is not visible over USP instantly.** `vhal_dm_add`
returns when the device has the row; obuspa learns of it a few milliseconds
later, when the plug-in's event thread delivers the ObjectAdded signal. A
controller Get in that window sees the previous instance list. Real agents
behave the same way; tests should wait for visibility rather than assume it
(`tests/conftest.py::usp_visible`).

**The broker's name.** The agent connects to `mosquitto`. On this platform
that name resolves to the *device* container, whose WAN relay forwards to the
real broker (`broker`). That is how WAN faults reach the agent without any
change to its factory configuration.

---

## 8. Worked examples

`examples/disk-monitor/` — a vendor object with a live getter, a persisted
controller-writable threshold, a background thread doing `statvfs()`, a
`SpaceLow!` event, one optional `vhal_get`. Logic that *observes* and alarms.

`examples/parental-controls/` — a controller-managed vendor object
(`Rule.{i}` with a MAC), a thread that reconciles it against
`Device.Firewall`: reads via `USP_PROCESS_DoWorkSync`, changes via
`USP_PROCESS_DM_SetParameterValue`, creates and deletes rules via the HAL,
survives reboots by re-linking rules it tagged. Logic that *acts* on the
hardware.

Each has a `README.md`; `tests/test_06_vendor_plugin.py` proves both.
