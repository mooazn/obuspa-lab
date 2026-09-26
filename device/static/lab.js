/*
 * The lab panels under the 3D view: console, USP timeline and browser,
 * faults and clock, HAL.
 *
 * Kept separate from index.html, which owns the data model tree. Each panel
 * is a small module-level object with `mount(el)` and optional `onState(s)`;
 * the tab strip just shows one at a time. Panels arrive slice by slice.
 */

const $ = (sel, root = document) => root.querySelector(sel);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
}

// ---------------------------------------------------------------- tabs

const panels = {};
let active = null;

function activate(name) {
  active = name;
  for (const [key, panel] of Object.entries(panels)) {
    panel.section.hidden = key !== name;
    panel.button.classList.toggle("on", key === name);
    if (key === name && panel.onShow) panel.onShow();
  }
  try { localStorage.setItem("vdev.tab", name); } catch (e) {}
}

function registerPanel(name, label, panel) {
  const strip = $("#tabs");
  const button = el("button", "tab", label);
  button.onclick = () => activate(name);
  strip.appendChild(button);

  const section = $(`section[data-tab="${name}"]`);
  // Keep the panel object itself - mount() sets state on `this`, and a spread
  // copy taken before mount would never see it.
  panel.button = button;
  panel.section = section;
  panels[name] = panel;
  if (panel.mount) panel.mount(section);
}

// ---------------------------------------------------------------- console

const consolePanel = {
  lines: null, pinned: true, socket: null, count: 0,

  mount(section) {
    const head = el("div", "panel-head");
    this.status = el("span", "pill", "agent: unknown");
    const spacer = el("span", "spacer");
    const clear = el("button", "ghost", "Clear view");
    clear.onclick = () => { this.lines.innerHTML = ""; };
    const follow = el("label", "follow");
    this.followBox = document.createElement("input");
    this.followBox.type = "checkbox"; this.followBox.checked = true;
    this.followBox.onchange = () => { this.pinned = this.followBox.checked; if (this.pinned) this.scroll(); };
    follow.append(this.followBox, document.createTextNode(" follow"));
    head.append(this.status, spacer, follow, clear);

    this.lines = el("div", "console");
    this.lines.onscroll = () => {
      const atBottom = this.lines.scrollTop + this.lines.clientHeight >= this.lines.scrollHeight - 8;
      if (!atBottom && this.pinned) { this.pinned = false; this.followBox.checked = false; }
    };
    section.append(head, this.lines);
    this.connect();
  },

  append(entry) {
    const line = el("div", "line", entry.text);
    if (/^entrypoint: ==== boot/.test(entry.text)) line.classList.add("boot");
    else if (/^entrypoint:/.test(entry.text)) line.classList.add("boot-loader");
    else if (/ERROR|Error:|Failed|error:|already exists in schema|Unable to load|undefined symbol/.test(entry.text)) line.classList.add("err");
    else if (/WARNING|Warning/.test(entry.text)) line.classList.add("warn");
    this.lines.appendChild(line);
    if (++this.count > 3000) { this.lines.removeChild(this.lines.firstChild); this.count--; }
    if (this.pinned) this.scroll();
  },

  scroll() { this.lines.scrollTop = this.lines.scrollHeight; },
  onShow() { if (this.pinned) this.scroll(); },

  connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/console`);
    ws.onmessage = (e) => this.append(JSON.parse(e.data));
    ws.onclose = () => setTimeout(() => this.connect(), 1500);
    ws.onerror = () => ws.close();
    this.socket = ws;
  },

  onState(state) {
    const agent = (state.system && state.system.agent) || {};
    const up = !!agent.eventsConnected;
    if (agent.crashLooping) {
      this.status.textContent = `agent: restarting repeatedly (${agent.recentBoots} boots in the last 5 minutes) - see the error lines above`;
    } else {
      this.status.textContent = up ? "agent: up" : (state.system && state.system.rebooting ? "agent: rebooting" : "agent: not connected");
    }
    this.status.className = "pill " + (up ? "ok" : "bad");
  },
};

// ---------------------------------------------------------------- faults

const FAULT_KINDS = {
  disk_fill:   { label: "Fill the data partition", param: "percent", unit: "%", min: 1, max: 100, def: 95,
                 help: "Ballast on the agent's tmpfs data partition. Vendor code sees it with plain statvfs()." },
  wan_latency: { label: "WAN latency", param: "ms", unit: "ms", min: 0, max: 2000, def: 400,
                 help: "Delays every byte through the WAN relay, each direction." },
  wan_down:    { label: "WAN link down", param: null,
                 help: "Cuts the agent's broker connection until cleared. It reconnects on its own retry timer." },
};

async function post(url, body, method = "POST") {
  const res = await fetch(url, { method, headers: {"Content-Type": "application/json"},
                                 body: body === undefined ? undefined : JSON.stringify(body) });
  if (!res.ok) {
    const d = await res.json().catch(() => ({detail: res.statusText}));
    throw new Error(d.detail || "request failed");
  }
  return res.json();
}

// Offsets come from a clock snapped to whole seconds, so a one-day jump reads
// as 86399.x s; round to the minute rather than truncate
function fmtDuration(seconds) {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  seconds = Math.round(seconds / 60) * 60;
  const d = Math.floor(seconds / 86400), h = Math.floor(seconds % 86400 / 3600), m = Math.floor(seconds % 3600 / 60);
  const parts = [];
  if (d) parts.push(`${d}d`);
  if (h) parts.push(`${h}h`);
  if (m || !parts.length) parts.push(`${m}m`);
  return parts.join(" ");
}

const faultsPanel = {
  mount(section) {
    const head = el("div", "panel-head");
    this.wanPill = el("span", "pill", "wan: ?");
    head.append(this.wanPill);
    section.appendChild(head);

    // The lab clock: what time the device and the firmware believe it is
    const clock = el("div", "card clock");
    const chead = el("div", "row-head");
    this.clockNow = el("span", "inst", "");
    this.clockPill = el("span", "badge", "");
    chead.append(this.clockNow, el("span", "spacer"), this.clockPill);
    const reset = el("button", "ghost", "Real time");
    reset.onclick = () => post("/api/clock", undefined, "DELETE").catch(e => alert(e.message));
    chead.append(reset);
    const jumps = el("div", "sys");
    jumps.append(el("span", "ro", "jump"));
    for (const [label, secs] of [["+1 min", 60], ["+1 h", 3600], ["+1 day", 86400]]) {
      const b = el("button", "", label);
      b.onclick = () => post("/api/clock", { jump: secs }).catch(e => alert(e.message));
      jumps.append(b);
    }
    jumps.append(el("span", "ro", "rate"));
    this.rate = document.createElement("select");
    for (const r of [1, 5, 10, 30, 60]) {
      const o = document.createElement("option"); o.value = r; o.textContent = `${r}×`; this.rate.appendChild(o);
    }
    this.rate.onchange = () => post("/api/clock", { rate: Number(this.rate.value) }).catch(e => alert(e.message));
    jumps.append(this.rate);
    clock.append(chead, jumps,
      el("div", "help", "Moves the clock for the device and the firmware together. Timers that become due fire at once; " +
                        "a rate above 1× runs every schedule that much faster. Persists across reboots like an RTC."));
    section.appendChild(clock);
    this.clock = null;
    setInterval(() => this.tickClock(), 1000);

    this.active = el("div", "faults-active");
    section.appendChild(this.active);

    // Apply form
    const form = el("div", "card fault-form");
    this.kind = document.createElement("select");
    for (const [k, v] of Object.entries(FAULT_KINDS)) {
      const o = document.createElement("option"); o.value = k; o.textContent = v.label; this.kind.appendChild(o);
    }
    this.value = document.createElement("input"); this.value.type = "number"; this.value.className = "num";
    this.unit = el("span", "ro", "");
    this.help = el("div", "help", "");
    const apply = el("button", "", "Apply");
    apply.onclick = () => this.apply();
    this.kind.onchange = () => this.syncForm();
    const row = el("div", "sys");
    row.append(this.kind, this.value, this.unit, apply);
    form.append(row, this.help);
    section.appendChild(form);
    this.syncForm();
  },

  syncForm() {
    const spec = FAULT_KINDS[this.kind.value];
    this.value.hidden = !spec.param; this.unit.hidden = !spec.param;
    if (spec.param) { this.value.min = spec.min; this.value.max = spec.max; this.value.value = spec.def; this.unit.textContent = spec.unit; }
    this.help.textContent = spec.help;
  },

  async apply() {
    const spec = FAULT_KINDS[this.kind.value];
    const params = spec.param ? { [spec.param]: Number(this.value.value) } : {};
    try { await post("/api/faults", { kind: this.kind.value, params }); }
    catch (e) { alert(e.message); }
  },

  tickClock() {
    if (!this.clock) return;
    const c = this.clock;
    const now = c.now + (Date.now() / 1000 - c.receivedAt) * c.rate;
    this.clockNow.textContent = new Date(now * 1000).toISOString().replace("T", " ").slice(0, 19) + " UTC";
  },

  onState(state) {
    const sys = state.system || {};
    const wan = sys.wan || {};

    if (sys.clock) {
      this.clock = { ...sys.clock, receivedAt: Date.now() / 1000 };
      this.tickClock();
      const c = sys.clock;
      const off = Math.abs(c.offset) < 1 ? "" : (c.offset > 0 ? "+" : "-") + fmtDuration(Math.abs(c.offset));
      this.clockPill.textContent = c.real ? "real time" : [off, c.rate !== 1 ? `${c.rate}×` : ""].filter(Boolean).join(" · ");
      this.clockPill.className = "badge " + (c.real ? "up" : "warn");
      if (document.activeElement !== this.rate) this.rate.value = String(c.rate);
    }
    this.wanPill.textContent = wan.linkUp === false
      ? `wan: down (${(wan.reasons || []).join(", ")})`
      : `wan: up · ${wan.connections ?? 0} conn · ${wan.latencyMs || 0} ms`;
    this.wanPill.className = "pill " + (wan.linkUp === false ? "bad" : "ok");

    this.active.innerHTML = "";
    const faults = sys.faults || [];
    if (!faults.length) { this.active.appendChild(el("div", "empty", "no faults active")); return; }
    for (const f of faults) {
      const card = el("div", "card");
      const head = el("div", "row-head");
      head.append(el("span", "inst", f.kind), el("span", "spacer"));
      const ok = f.applied && !f.error;
      head.append(el("span", "badge " + (ok ? "up" : "down"),
        f.error ? "error" : (f.applied ? "applied" : "pending")));
      const clear = el("button", "ghost", "Clear");
      clear.onclick = () => post(`/api/faults/${f.kind}`, undefined, "DELETE").catch(e => alert(e.message));
      head.append(clear);
      card.appendChild(head);
      const grid = el("div", "grid");
      for (const [k, v] of Object.entries(f.params || {})) { grid.append(el("label", "", k), el("span", "ro", String(v))); }
      if (f.detail) { grid.append(el("label", "", "detail"), el("span", "ro", String(f.detail))); }
      if (f.error) { grid.append(el("label", "", "error"), el("span", "ro err-text", f.error)); }
      grid.append(el("label", "", "scope"), el("span", "ro", f.scope + (f.scope === "agent" ? " (re-applied on every boot)" : "")));
      card.appendChild(grid);
      this.active.appendChild(card);
    }
  },
};

// ---------------------------------------------------------------- HAL

const halPanel = {
  mount(section) {
    const head = el("div", "panel-head");
    head.append(el("span", "help",
      "A free key/value namespace vendor code may read through vhal_get(). The platform defines no keys."));
    section.appendChild(head);
    this.table = el("div", "card");
    section.appendChild(this.table);

    const add = el("div", "card sys");
    this.newKey = document.createElement("input"); this.newKey.type = "text"; this.newKey.placeholder = "key, e.g. thermal.cpu";
    this.newVal = document.createElement("input"); this.newVal.type = "text"; this.newVal.placeholder = "value";
    const btn = el("button", "", "Set");
    btn.onclick = () => this.set(this.newKey.value.trim(), this.newVal.value).then(() => { this.newKey.value = ""; this.newVal.value = ""; });
    add.append(this.newKey, this.newVal, btn);
    section.appendChild(add);
  },

  set(key, value) {
    if (!key) return Promise.resolve();
    return post(`/api/hal/${encodeURIComponent(key)}`, { value }, "PUT").catch(e => alert(e.message));
  },

  onState(state) {
    const hal = (state.system && state.system.hal) || {};
    const focused = document.activeElement && document.activeElement.dataset ? document.activeElement.dataset.hal : null;
    this.table.innerHTML = "";
    const keys = Object.keys(hal).sort();
    if (!keys.length) { this.table.appendChild(el("div", "empty", "no keys set")); return; }
    const grid = el("div", "grid hal-grid");
    for (const key of keys) {
      grid.appendChild(el("label", "", key));
      const cell = el("div", "sys");
      const input = document.createElement("input"); input.type = "text"; input.value = hal[key]; input.dataset.hal = key;
      input.onkeydown = (e) => { if (e.key === "Enter") input.blur(); };
      input.onblur = () => { if (input.value !== hal[key]) this.set(key, input.value); };
      const del = el("button", "ghost", "×");
      del.onclick = () => post(`/api/hal/${encodeURIComponent(key)}`, undefined, "DELETE").catch(e => alert(e.message));
      cell.append(input, del);
      grid.appendChild(cell);
    }
    this.table.appendChild(grid);
    if (focused) { const again = this.table.querySelector(`[data-hal="${CSS.escape(focused)}"]`); if (again) again.focus(); }
  },
};

// ---------------------------------------------------------------- USP timeline

const uspPanel = {
  onlyNotify: false, count: 0,

  mount(section) {
    const head = el("div", "panel-head");
    this.pill = el("span", "pill", "tap: connecting");

    // Timeline (what crossed the broker) and Browse (ask the agent, as the
    // lab's own controller)
    const views = el("div", "seg");
    this.views = {};
    for (const [key, label] of [["timeline", "Timeline"], ["browse", "Browse"]]) {
      const b = el("button", "", label);
      b.onclick = () => this.show(key);
      views.appendChild(b);
      this.views[key] = b;
    }

    this.timelineTools = el("span", "tools");
    const filter = el("label", "follow");
    const box = document.createElement("input"); box.type = "checkbox";
    box.onchange = () => { this.onlyNotify = box.checked; this.list.classList.toggle("notify-only", this.onlyNotify); };
    filter.append(box, document.createTextNode(" notifications only"));
    const clear = el("button", "ghost", "Clear view");
    clear.onclick = () => { this.list.innerHTML = ""; };
    this.timelineTools.append(filter, clear);

    head.append(views, this.pill, el("span", "spacer"), this.timelineTools);
    section.appendChild(head);
    this.list = el("div", "usp");
    section.appendChild(this.list);
    this.browse = el("div", "browse");
    browseView.mount(this.browse);
    section.appendChild(this.browse);

    let view = "timeline";
    try { view = localStorage.getItem("vdev.uspView") || "timeline"; } catch (e) {}
    this.show(this.views[view] ? view : "timeline");
    this.connect();
  },

  show(view) {
    this.list.hidden = view !== "timeline";
    this.timelineTools.hidden = view !== "timeline";
    this.browse.hidden = view !== "browse";
    for (const [key, b] of Object.entries(this.views)) b.classList.toggle("on", key === view);
    try { localStorage.setItem("vdev.uspView", view); } catch (e) {}
  },

  append(e) {
    const row = el("div", "usp-row" + (e.msg_type === "NOTIFY" ? " notify" : "") + (e.msg_type ? "" : " meta"));
    const ts = new Date(e.ts * 1000).toLocaleTimeString();
    const arrow = e.direction === "controller->agent" ? "→" : "←";
    const line = el("div", "usp-line");
    line.append(el("span", "usp-ts", ts), el("span", "usp-dir", arrow),
                el("span", "usp-type", e.msg_type || e.record_type || "?"),
                el("span", "usp-sum", e.summary || ""));
    row.appendChild(line);
    if (e.body) {
      const body = el("pre", "usp-body"); body.textContent = JSON.stringify(e.body, null, 2); body.hidden = true;
      row.appendChild(body);
      line.onclick = () => { body.hidden = !body.hidden; };
      line.classList.add("clickable");
    }
    this.list.appendChild(row);
    if (++this.count > 1500) { this.list.removeChild(this.list.firstChild); this.count--; }
    this.list.scrollTop = this.list.scrollHeight;
  },

  connect() {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${location.host}/ws/usp`);
    ws.onopen = () => { this.pill.textContent = "tap: live"; this.pill.className = "pill ok"; };
    ws.onmessage = (e) => this.append(JSON.parse(e.data));
    ws.onclose = () => { this.pill.textContent = "tap: reconnecting"; this.pill.className = "pill bad"; setTimeout(() => this.connect(), 1500); };
    ws.onerror = () => ws.close();
  },
};

// ---------------------------------------------------------------- USP browse

// Requests go to the agent as the lab controller (self::vdev-lab,
// Controller.2), so the agent can tell them apart from the test suite's.
async function lab(op, body) {
  const res = await fetch(`/api/usp/${op}`, { method: "POST",
    headers: {"Content-Type": "application/json"}, body: JSON.stringify(body) });
  const d = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = d.detail || {};
    if (typeof detail === "string") throw new Error(detail);
    const lines = [(detail.code ? `USP error ${detail.code}: ` : "") + (detail.message || res.statusText)];
    for (const [path, code, msg] of detail.paramErrors || []) lines.push(`  ${path}: ${code} ${msg}`);
    throw new Error(lines.join("\n"));
  }
  return d;
}

// "Name=value" per line -> {Name: "value"}
function parseArgs(text) {
  const out = {};
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (!t) continue;
    const i = t.indexOf("=");
    if (i < 1) throw new Error(`expected Name=value, got "${t}"`);
    out[t.slice(0, i).trim()] = t.slice(i + 1).trim();
  }
  return out;
}

function input(cls, placeholder, value = "") {
  const i = document.createElement("input");
  i.className = cls; i.placeholder = placeholder; i.value = value; i.spellcheck = false;
  return i;
}

const browseView = {
  mount(root) {
    let last = "Device.LocalAgent.";
    try { last = localStorage.getItem("vdev.browsePath") || last; } catch (e) {}

    // Get
    const get = el("div", "browse-row");
    this.path = input("path", "Device.LocalAgent.", last);
    this.depth = document.createElement("select");
    for (const [v, label] of [[0, "all levels"], [1, "1 level"], [2, "2 levels"], [3, "3 levels"]]) {
      const o = document.createElement("option"); o.value = v; o.textContent = label; this.depth.appendChild(o);
    }
    const go = el("button", "", "Get");
    go.onclick = () => this.get();
    this.path.onkeydown = (e) => { if (e.key === "Enter") this.get(); };
    get.append(this.path, this.depth, go);

    this.error = el("pre", "browse-error"); this.error.hidden = true;
    this.note = el("div", "help", "Requests go to the agent as the lab controller, self::vdev-lab " +
      "(Controller.2). Click a value to set it; the agent decides what is writable.");
    this.results = el("div", "browse-results");

    // Add and Operate
    const add = el("div", "card browse-form");
    this.addPath = input("path", "Device.LocalAgent.Subscription.");
    this.addArgs = document.createElement("textarea"); this.addArgs.placeholder = "Name=value, one per line (optional)";
    const addBtn = el("button", "", "Add instance");
    addBtn.onclick = () => this.add();
    add.append(el("div", "browse-label", "Add"), this.addPath, this.addArgs, addBtn);

    const op = el("div", "card browse-form");
    this.command = input("path", "Device.Reboot()");
    this.opArgs = document.createElement("textarea"); this.opArgs.placeholder = "Name=value, one per line (optional)";
    const opBtn = el("button", "", "Operate");
    opBtn.onclick = () => this.operate();
    this.opOut = el("pre", "browse-out"); this.opOut.hidden = true;
    op.append(el("div", "browse-label", "Operate"), this.command, this.opArgs, opBtn, this.opOut);

    const forms = el("div", "browse-forms");
    forms.append(add, op);
    root.append(get, this.note, this.error, this.results, forms);
  },

  fail(e) { this.error.textContent = e.message; this.error.hidden = false; },
  ok() { this.error.hidden = true; },

  async get() {
    const path = this.path.value.trim();
    if (!path) return;
    try { localStorage.setItem("vdev.browsePath", path); } catch (e) {}
    try {
      const { values } = await lab("get", { path, depth: Number(this.depth.value) });
      this.ok();
      this.render(values);
    } catch (e) { this.fail(e); }
  },

  // Parameters grouped by the object they belong to; instances get a Delete
  render(values) {
    this.results.innerHTML = "";
    const paths = Object.keys(values).sort();
    if (!paths.length) { this.results.appendChild(el("div", "empty", "nothing under that path")); return; }
    const groups = new Map();
    for (const p of paths) {
      const obj = p.slice(0, p.lastIndexOf(".") + 1);
      if (!groups.has(obj)) groups.set(obj, []);
      groups.get(obj).push(p);
    }
    for (const [obj, params] of groups) {
      const group = el("div", "browse-group");
      const head = el("div", "browse-obj");
      head.append(el("span", "inst", obj), el("span", "spacer"));
      if (/\.\d+\.$/.test(obj)) {
        const del = el("button", "ghost", "Delete");
        del.onclick = () => this.remove(obj);
        head.appendChild(del);
      }
      group.appendChild(head);
      for (const p of params) {
        const row = el("div", "browse-param");
        const value = el("span", "browse-value", values[p]);
        value.title = "click to set";
        value.onclick = () => this.edit(value, p, values[p]);
        row.append(el("span", "browse-name", p.slice(obj.length)), value);
        group.appendChild(row);
      }
      this.results.appendChild(group);
    }
  },

  edit(span, path, current) {
    const box = input("browse-edit", "", current);
    span.replaceWith(box);
    box.focus(); box.select();
    const done = () => box.replaceWith(span);
    box.onkeydown = async (e) => {
      if (e.key === "Escape") return done();
      if (e.key !== "Enter") return;
      try { await lab("set", { params: { [path]: box.value } }); this.ok(); await this.get(); }
      catch (err) { this.fail(err); done(); }
    };
    box.onblur = done;
  },

  async add() {
    try {
      const params = parseArgs(this.addArgs.value);
      const { path } = await lab("add", { path: this.addPath.value.trim(), params });
      this.ok();
      this.path.value = path;
      await this.get();
    } catch (e) { this.fail(e); }
  },

  async remove(path) {
    try { await lab("delete", { path }); this.ok(); await this.get(); }
    catch (e) { this.fail(e); }
  },

  async operate() {
    try {
      const inputs = parseArgs(this.opArgs.value);
      const { output } = await lab("operate", { command: this.command.value.trim(), inputs });
      this.ok();
      const keys = Object.keys(output);
      this.opOut.textContent = keys.length
        ? keys.sort().map(k => `${k} = ${output[k]}`).join("\n")
        : "accepted - an asynchronous command reports on the timeline when it completes";
      this.opOut.hidden = false;
    } catch (e) { this.fail(e); }
  },
};

// ---------------------------------------------------------------- boot

export function initLab() {
  registerPanel("model",   "Data model", {});
  registerPanel("console", "Console",    consolePanel);
  registerPanel("usp",     "USP",        uspPanel);
  registerPanel("faults",  "Faults & clock", faultsPanel);
  registerPanel("hal",     "HAL",        halPanel);

  let initial = "model";
  try { initial = localStorage.getItem("vdev.tab") || "model"; } catch (e) {}
  activate(panels[initial] ? initial : "model");

  return {
    onState(state) {
      // One panel failing must not take the data model tree down with it
      for (const [name, panel] of Object.entries(panels)) {
        if (!panel.onState) continue;
        try { panel.onState(state); }
        catch (e) { console.error(`panel ${name}:`, e); }
      }
    },
  };
}
