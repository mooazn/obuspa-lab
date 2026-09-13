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
  `-lpthread`, plus the vhal client (see §3).

The directory's name becomes the plug-in's name. Plug-ins are compiled inside
the platform's build container, **against the obuspa tree the card will
boot** — the built-in one, or the one you flashed with `SRC`/`REF`. That is
deliberate: obuspa's vendor API moves between releases, and this catches it
before it reaches a device (see §5, logging).

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

**Order.** Plug-ins initialise in `-x` order. The platform's proxy comes
first, so the TR-181 data model it serves (`Device.WiFi.*`, `Device.Hosts.*`,
`Device.Cellular.*`, …) exists by the time yours runs. Register your own
objects under a vendor prefix (`Device.X_<VENDOR>_Thing.`); registering paths
the proxy already owns fails.

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

## 3. Stimulating your code

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

### Through the virtual HAL (opt-in, for code that reads hardware)

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

## 4. Observing what it did

- **USP tab** / `GET /api/usp/timeline` / `WS /ws/usp` — every record between
  agent and controller, decoded: direction, message type, a one-line summary,
  the full body on click. `GET /api/usp/notifications` filters to Notify. This
  is where you watch your alarm leave the device.
- **Console tab** / `GET /api/console` / `WS /ws/console` — everything the
  agent container prints: bootloader lines, obuspa's log, your plug-in's
  output, the fault daemon. Rotated per boot; boot boundaries are marked.
- **From a test** — `controller.subscribe("Event", "Device.X_VDEV_Thing.Alarm!")`
  then `controller.wait_for_notification(...)`. See
  `tests/test_06_vendor_plugin.py` for the full disk-monitor scenario:
  flash → seat → reboot → subscribe → fill → assert → clear.

---

## 5. Things that bite

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

**The broker's name.** The agent connects to `mosquitto`. On this platform
that name resolves to the *device* container, whose WAN relay forwards to the
real broker (`broker`). That is how WAN faults reach the agent without any
change to its factory configuration.

---

## 6. Worked example

`examples/disk-monitor/` — ~200 lines of C: a vendor object with a live
getter, a persisted controller-writable threshold, a background thread doing
`statvfs()`, a `SpaceLow!` event with arguments, one optional `vhal_get`.
`README.md` there walks through running it; the tests above prove it.
