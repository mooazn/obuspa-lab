# disk-monitor — a worked example of vendor logic

A background thread that watches a data partition and raises a USP event when
it fills past a threshold. It stands in for any "monitor something and alarm"
logic you might have in your own obuspa build; nothing here is specific to
disks except one `statvfs()` call.

```
Device.X_VDEV_DiskMonitor.UsedPercent   live, read-only
Device.X_VDEV_DiskMonitor.Path          read-only
Device.X_VDEV_DiskMonitor.Threshold     read-write, persisted, default 90
Device.X_VDEV_DiskMonitor.SpaceLow!     event (UsedPercent, Threshold, Path)
```

## Run it

```bash
make flash PLUGINS=examples/disk-monitor      # compile onto the SD card
# seat the card in the 3D view, press reset
```

Then fill the disk from the Faults tab (or `POST /api/faults {"kind":"disk_fill","params":{"percent":95}}`)
and watch `SpaceLow!` go out on the USP tab. Clear the fault and `UsedPercent`
drops.

The plug-in reads the disk through plain POSIX and knows nothing about the
platform. The one optional platform touch is `vhal_get("diskmon.threshold")`,
compiled in only when the virtual HAL is available, to show how a value that
would come from hardware can be driven from the lab instead.

See `docs/vendor-integration.md` for the contract this follows.
