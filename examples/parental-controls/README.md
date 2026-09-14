# parental-controls — vendor logic acting on the hardware

A vendor object a controller manages — `Device.X_VDEV_ParentalControls.Rule.{i}`,
each rule naming a client's MAC — and a background thread that keeps the
device's firewall in step with it. An enabled rule becomes a Drop rule in
`Device.Firewall.Chain.1`; the device derives the effect, and the client's
`Hosts.Host` and `AssociatedDevice` rows go inactive.

```
Device.X_VDEV_ParentalControls.Enable                 read-write, persisted
Device.X_VDEV_ParentalControls.Rule.{i}.Enable        read-write, persisted
Device.X_VDEV_ParentalControls.Rule.{i}.MACAddress    read-write, persisted
Device.X_VDEV_ParentalControls.Rule.{i}.Description   read-write, persisted
Device.X_VDEV_ParentalControls.Rule.{i}.Status        "Blocking" | "Idle"
Device.X_VDEV_ParentalControls.RuleApplied!           event
```

It reaches the hardware the way real firmware does. Values are the data
model's: it reads through obuspa's API on the data model thread
(`USP_PROCESS_DoWorkSync`) and changes values with
`USP_PROCESS_DM_SetParameterValue` from its own thread. Rows are the
hardware's: it creates and deletes firewall rules through the HAL
(`vhal_dm_add`, `vhal_dm_set`, `vhal_dm_delete`), because obuspa's API cannot
create instances and may only delete them inside a transaction it opened
itself. On a real device those are calls into the firewall engine, which
then informs obuspa.

## Run it

```bash
make flash PLUGINS=examples/parental-controls
# seat the card, press reset
```

Add a client from the UI, then over USP:

```python
controller.add("Device.X_VDEV_ParentalControls.Rule.", {"MACAddress": mac})
```

Within a couple of seconds `Device.Firewall.Chain.1.Rule.N` exists with that
MAC, `RuleApplied!` goes out, and the client's `Active` is `false`. Disable
the rule and it comes back; delete the rule and the firewall rule goes too.
Reboot: the rules are persisted on both sides and re-linked on boot.
