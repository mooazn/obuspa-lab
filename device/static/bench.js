/*
 * The Device tab's bench view: the board as its parts - power, ports, radios,
 * slots - each showing its state beside the controls that change it, the way
 * the box looks on a bench. Every change goes to the device directly, as the
 * hardware (/api/set and the physical-control endpoints), not through the
 * agent; the USP tab's Browse view is the controller's side.
 *
 * Knows the reference board's layout: WAN on IP.Interface.1, LAN on
 * IP.Interface.2, radios, access points, one cellular modem, one SD slot.
 * Device profiles will describe a board instead.
 */

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

function badge(text, good) {
  return el("span", "badge " + (good === undefined ? "" : good ? "up" : "down"), text);
}

function rows(state, schema) {
  const object = state.objects.find(o => o.schema === schema);
  return object ? object.rows : [];
}

function row(state, schema, path) {
  return rows(state, schema).find(r => r.path === path);
}

function formatUptime(s) {
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return h < 48 ? `${h}h ${m % 60}m` : `${Math.floor(h / 24)}d ${h % 24}h`;
}

// A card with a title row; returns [card, body]
function card(title, ...right) {
  const c = el("div", "card bench-card");
  const head = el("div", "bench-head");
  head.append(el("span", "bench-title", title), el("span", "spacer"), ...right.filter(Boolean));
  const body = el("div", "bench-body");
  c.append(head, body);
  return [c, body];
}

function line(...parts) {
  const l = el("div", "bench-line");
  l.append(...parts.filter(Boolean));
  return l;
}

function kv(k, v) {
  const s = el("span", "bench-kv");
  s.append(el("span", "k", k), el("span", "v", v));
  return s;
}

// A checkbox bound to a boolean parameter, set as the hardware
function toggle(api, path, value, label) {
  const wrap = el("label", "bench-toggle");
  const box = document.createElement("input");
  box.type = "checkbox";
  box.checked = !!value;
  box.dataset.bench = path;
  box.onchange = () => api.setParam(path, box.checked);
  wrap.append(box, document.createTextNode(label));
  return wrap;
}

// A text or number field bound to a parameter; applied on Enter or blur
function field(api, path, value, { numeric = false, width = "" } = {}) {
  const input = document.createElement("input");
  input.type = "text";
  input.className = "bench-input" + (width ? ` w-${width}` : "");
  input.value = String(value ?? "");
  input.dataset.bench = path;
  input.onkeydown = (e) => { if (e.key === "Enter") input.blur(); };
  input.onblur = () => {
    if (input.value === String(value ?? "")) return;
    api.setParam(path, numeric ? Number(input.value) : input.value);
  };
  return input;
}

function action(label, cls, onclick) {
  const b = el("button", cls, label);
  b.onclick = onclick;
  return b;
}

// ---------------------------------------------------------------- cards

function powerCard(state, api) {
  const sys = state.system || {};
  const agent = sys.agent || {};
  const booted = sys.bootedFrom || {};
  const reboot = action(sys.rebooting ? "Rebooting…" : "Reboot", "danger",
    () => api.post("/api/reboot", { cause: "LocalReboot" }));
  reboot.disabled = !!sys.rebooting;
  const factory = action("Factory reset", "ghost", () => {
    if (confirm("Factory reset: the device and the agent forget their configuration. Continue?")) {
      api.post("/api/factory-reset", { cause: "FactoryReset" });
    }
  });
  const agentBadge = agent.crashLooping ? badge("agent restarting repeatedly", false)
    : agent.eventsConnected ? badge("agent up", true) : badge("agent starting", false);
  const [c, body] = card("Power", agentBadge);

  if (sys.rebooting) {
    body.appendChild(el("div", "rebooting", "Rebooting - the agent has lost the device until it comes back."));
  }
  const stats = el("div", "sys");
  for (const [k, v] of [
    ["uptime", sys.rebooting ? "-" : formatUptime(sys.upTime || 0)],
    ["boots", String(sys.bootCount ?? 0)],
    ["last cause", sys.rebootCause || "-"],
  ]) {
    const stat = el("div", "stat");
    stat.append(el("span", "k", k), el("span", "v", v));
    stats.appendChild(stat);
  }
  body.appendChild(stats);
  body.appendChild(line(kv("firmware", booted.softwareVersion || booted.from || "unknown")));

  const jobs = sys.runningJobs || [];
  for (const job of jobs) {
    const j = el("div", "job");
    j.append(el("span", "spin"), el("span", "", `${job.path}  request ${job.requestId}`));
    body.appendChild(j);
  }
  body.appendChild(line(reboot, factory));
  return c;
}

function portLine(api, eth) {
  if (!eth) return null;
  const v = eth.values;
  return line(badge(v.Status, v.Status === "Up"), kv("port", v.Name),
              el("span", "ro", `${v.MaxBitRate} Mb/s ${v.DuplexMode}`),
              el("span", "spacer"), toggle(api, `${eth.path}.Enable`, v.Enable, "enabled"));
}

function addressLine(state, ipPath) {
  const addr = rows(state, "Device.IP.Interface.{i}.IPv4Address.{i}").find(r => r.path.startsWith(ipPath + "."));
  if (!addr) return null;
  const v = addr.values;
  const bits = (v.SubnetMask || "").split(".").reduce((n, o) => n + (Number(o) >>> 0).toString(2).split("1").length - 1, 0);
  return line(kv("address", `${v.IPAddress}/${bits}`), el("span", "ro", v.AddressingType));
}

function interfaceCard(state, api, title, ipPath) {
  const ip = row(state, "Device.IP.Interface.{i}", ipPath);
  if (!ip) return null;
  const eth = row(state, "Device.Ethernet.Interface.{i}", ip.values.LowerLayers);
  const [c, body] = card(title, badge(ip.values.Status, ip.values.Status === "Up"));
  body.append(...[portLine(api, eth), addressLine(state, ipPath)].filter(Boolean));
  return [c, body];
}

function wanCard(state, api) {
  const made = interfaceCard(state, api, "WAN", "Device.IP.Interface.1");
  if (!made) return null;
  const [c, body] = made;
  const wan = (state.system || {}).wan;
  if (wan) {
    body.appendChild(line(
      kv("uplink", wan.linkUp ? "up" : `down (${(wan.reasons || []).join(", ")})`),
      wan.latencyMs ? kv("latency", `${wan.latencyMs} ms`) : null));
    for (const r of wan.routes || []) {
      body.appendChild(line(el("span", "ro", `${r.listen.replace("0.0.0.0:", ":")} → ${r.upstream}`),
                            el("span", "ro", `${r.connections} connection${r.connections === 1 ? "" : "s"}`)));
    }
  }
  return c;
}

function lanCard(state, api) {
  const made = interfaceCard(state, api, "LAN", "Device.IP.Interface.2");
  if (!made) return null;
  const [c, body] = made;
  const hosts = rows(state, "Device.Hosts.Host.{i}");
  body.appendChild(el("div", "bench-sub", `hosts (${hosts.length})`));
  if (!hosts.length) body.appendChild(el("div", "ro", "none seen"));
  for (const h of hosts) {
    const v = h.values;
    body.appendChild(line(badge(v.Active ? "active" : "blocked", v.Active),
      el("span", "", v.HostName || v.PhysAddress || "(no name)"),
      el("span", "ro", [v.IPAddress, v.InterfaceType].filter(Boolean).join(" · "))));
  }
  return c;
}

function wifiCard(state, api) {
  const radios = rows(state, "Device.WiFi.Radio.{i}");
  if (!radios.length) return null;
  const [c, body] = card("Wi-Fi");
  const ssids = rows(state, "Device.WiFi.SSID.{i}");
  const aps = rows(state, "Device.WiFi.AccessPoint.{i}");
  const clients = rows(state, "Device.WiFi.AccessPoint.{i}.AssociatedDevice.{i}");

  for (const radio of radios) {
    const r = radio.values;
    const block = el("div", "bench-block");
    block.appendChild(line(badge(r.Status, r.Status === "Up"), el("span", "bench-strong", r.OperatingFrequencyBand),
      el("span", "ro", r.Name), el("span", "spacer"), toggle(api, `${radio.path}.Enable`, r.Enable, "enabled")));
    const channel = line(kv("channel", ""), field(api, `${radio.path}.Channel`, r.Channel, { numeric: true, width: "s" }),
      el("span", "ro", `${r.OperatingChannelBandwidth} · noise ${r.Noise} dBm`));
    block.appendChild(channel);

    for (const ssid of ssids.filter(s => s.values.LowerLayers === radio.path)) {
      const s = ssid.values;
      const ap = aps.find(a => a.values.SSIDReference === ssid.path);
      const apNum = ap ? ap.instances[0] : null;
      const mine = clients.filter(cl => cl.instances[0] === apNum);
      block.appendChild(line(badge(s.Status, s.Status === "Up"), kv("SSID", ""),
        field(api, `${ssid.path}.SSID`, s.SSID, { width: "l" }),
        ap ? el("span", "ro", ap.values["Security.ModeEnabled"]) : null));
      if (ap) {
        const add = action("Add client", "ghost", () => api.post("/api/clients/attach", { accessPoint: apNum }));
        block.appendChild(line(el("span", "bench-sub", `clients (${mine.length})`), el("span", "spacer"), add));
        for (const cl of mine) {
          const v = cl.values;
          const kick = action("Disassociate", "ghost",
            () => api.post("/api/clients/detach", { accessPoint: cl.instances[0], instance: cl.instances[1] }));
          block.appendChild(line(badge(v.Active ? "active" : "blocked", v.Active), el("span", "mono", v.MACAddress),
            el("span", "ro", `${v.OperatingStandard} · ${v.SignalStrength} dBm`), el("span", "spacer"), kick));
        }
      }
    }
    body.appendChild(block);
  }
  return c;
}

function cellularCard(state, api) {
  const modem = rows(state, "Device.Cellular.Interface.{i}")[0];
  if (!modem) return null;
  const v = modem.values;
  const sim = (state.system || {}).sim || {};
  const simButton = action(sim.inserted ? "Eject SIM" : "Insert SIM", "ghost",
    () => api.post("/api/sim", { action: sim.inserted ? "eject" : "insert" }));
  const [c, body] = card("Cellular", badge(v.Status, v.Status === "Up"));
  body.appendChild(line(kv("SIM", v["USIM.Status"]), sim.carrier ? kv("carrier", sim.carrier) : null,
    el("span", "spacer"), simButton));
  if (sim.inserted) {
    body.appendChild(line(kv("access", v.CurrentAccessTechnology || "-"), kv("RSSI", `${v.RSSI} dBm`)));
    body.appendChild(line(kv("ICCID", v["USIM.ICCID"])));
  }
  body.appendChild(line(kv("IMEI", v.IMEI)));
  return c;
}

function sdCard(state, api) {
  const sys = state.system || {};
  const sd = sys.sdcard || {};
  const booted = sys.bootedFrom || {};
  const m = sd.manifest;
  const button = action(sd.inserted ? "Eject" : "Seat", "ghost",
    () => api.post("/api/sdcard", { action: sd.inserted ? "eject" : "insert" }));
  button.disabled = !sd.present && !sd.inserted;
  if (!sd.present) button.title = "run make flash first";
  const [c, body] = card("SD card", badge(sd.inserted ? "seated" : sd.present ? "in the slot" : "empty",
    sd.inserted ? true : undefined), button);
  if (m) {
    body.appendChild(line(kv("card", m.label), m.commit ? kv("commit", m.commit) : null,
      m.obuspaVersion ? kv("obuspa", m.obuspaVersion) : null));
    const plugins = (m.plugins || []).map(p => p.name);
    if (plugins.length) body.appendChild(line(kv("plug-ins", plugins.join(", "))));
  } else {
    body.appendChild(el("div", "ro", "No card. make flash writes one."));
  }
  const running = booted.from === "sdcard" ? "the card's obuspa"
    : (booted.plugins || []).length ? "built-in obuspa with the card's plug-ins" : "built-in image";
  body.appendChild(line(kv("running", running)));
  if (sd.inserted !== !!booted.cardSeated) {
    body.appendChild(el("div", "bench-note", "Takes effect at the next reset."));
  }
  return c;
}

function firewallCard(state, api) {
  const chains = rows(state, "Device.Firewall.Chain.{i}");
  if (!chains.length) return null;
  const rules = rows(state, "Device.Firewall.Chain.{i}.Rule.{i}");
  const [c, body] = card("Firewall");
  for (const chain of chains) {
    const v = chain.values;
    body.appendChild(line(el("span", "bench-strong", v.Name), el("span", "ro", `${v.RuleNumberOfEntries} rule${v.RuleNumberOfEntries === 1 ? "" : "s"}`),
      el("span", "spacer"), toggle(api, `${chain.path}.Enable`, v.Enable, "enabled")));
    for (const rule of rules.filter(r => r.instances[0] === chain.instances[0])) {
      const rv = rule.values;
      const source = rv.SourceMAC || rv.SourceIP || "any";
      body.appendChild(line(badge(rv.Status, rv.Status === "Enabled"), el("span", "", `${rv.Target} ${source}`),
        el("span", "ro", rv.Description)));
    }
  }
  return c;
}

// ---------------------------------------------------------------- entry

export function renderBench(root, state, api) {
  // Keep the caret in a field that the re-render replaces
  const active = document.activeElement;
  const focused = active && active.dataset ? active.dataset.bench : null;
  const caret = active && active.selectionStart;

  root.innerHTML = "";
  const cards = [powerCard, wanCard, lanCard, wifiCard, cellularCard, sdCard, firewallCard]
    .map(make => { try { return make(state, api); } catch (e) { console.error(make.name, e); return null; } })
    .filter(Boolean);
  const grid = el("div", "bench");
  grid.append(...cards);
  root.appendChild(grid);

  if (focused) {
    const again = root.querySelector(`[data-bench="${CSS.escape(focused)}"]`);
    if (again) {
      again.focus();
      if (caret !== null && again.setSelectionRange) again.setSelectionRange(caret, caret);
    }
  }
}
