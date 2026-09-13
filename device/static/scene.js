/*
 * A 3D gateway you can pick up and poke.
 *
 * Everything visible is driven by the same state snapshot the rest of the page
 * uses: LEDs follow interface status, the SIM and SD card sit in or out of
 * their slots, and a reboot darkens the front panel. The three physical
 * controls call the same HTTP API the buttons below the canvas do - the model
 * is a view, not a second source of truth.
 *
 * Controls: drag to orbit, wheel to zoom, right-drag to pan, double-click to
 * reframe. Reset button: press to reboot, hold 3s to factory reset.
 */

import * as THREE from "three";
import { OrbitControls } from "/static/vendor/OrbitControls.js";

// ---------------------------------------------------------------- helpers

const rowValues = (state, path) => {
  for (const object of state.objects || []) {
    for (const row of object.rows) if (row.path === path) return row.values;
  }
  return null;
};

const up = (values, key = "Status") =>
  !!values && ["Up", "Enabled"].includes(values[key]);

function roundedSlab(w, h, d, r) {
  // A box with rounded vertical edges: extrude a rounded rectangle
  const shape = new THREE.Shape();
  const x = -w / 2, y = -d / 2;
  shape.moveTo(x + r, y);
  shape.lineTo(x + w - r, y);
  shape.quadraticCurveTo(x + w, y, x + w, y + r);
  shape.lineTo(x + w, y + d - r);
  shape.quadraticCurveTo(x + w, y + d, x + w - r, y + d);
  shape.lineTo(x + r, y + d);
  shape.quadraticCurveTo(x, y + d, x, y + d - r);
  shape.lineTo(x, y + r);
  shape.quadraticCurveTo(x, y, x + r, y);
  const geometry = new THREE.ExtrudeGeometry(shape, {
    depth: h, bevelEnabled: true, bevelThickness: 0.12, bevelSize: 0.12,
    bevelSegments: 3, curveSegments: 12,
  });
  geometry.rotateX(-Math.PI / 2);         // extrude along +Y
  geometry.translate(0, 0, 0);
  return geometry;
}

// ---------------------------------------------------------------- materials

const M = {
  body:   new THREE.MeshStandardMaterial({ color: 0x24272c, roughness: 0.82, metalness: 0.08 }),
  top:    new THREE.MeshStandardMaterial({ color: 0x2f333a, roughness: 0.6,  metalness: 0.1 }),
  port:   new THREE.MeshStandardMaterial({ color: 0x0c0d10, roughness: 0.9 }),
  portRim:new THREE.MeshStandardMaterial({ color: 0x3a3e46, roughness: 0.7, metalness: 0.3 }),
  wanRim: new THREE.MeshStandardMaterial({ color: 0x2b64c8, roughness: 0.6 }),
  button: new THREE.MeshStandardMaterial({ color: 0xc23b3b, roughness: 0.5 }),
  antenna:new THREE.MeshStandardMaterial({ color: 0x1b1d21, roughness: 0.7 }),
  sim:    new THREE.MeshStandardMaterial({ color: 0xf2f2f2, roughness: 0.5 }),
  simChip:new THREE.MeshStandardMaterial({ color: 0xd4a017, roughness: 0.3, metalness: 0.8 }),
  sd:     new THREE.MeshStandardMaterial({ color: 0x1f5fbf, roughness: 0.55 }),
  sdLabel:new THREE.MeshStandardMaterial({ color: 0xeeeeee, roughness: 0.8 }),
  ledOff: new THREE.MeshStandardMaterial({ color: 0x111317, roughness: 0.4, emissive: 0x000000 }),
};

const LED_COLORS = {
  power: 0xffffff, internet: 0x3ddc84, wifi24: 0x4aa3ff, wifi5: 0x4aa3ff,
  lan: 0xffb020, cellular: 0xb388ff, sd: 0x3ddc84, activity: 0x4aa3ff,
};

// ---------------------------------------------------------------- scene

export function mountScene(container, api) {
  const W = 22, H = 3.2, D = 14;      // body size in scene units
  // ExtrudeGeometry's bevel grows the slab outward by BEVEL at mid-height and
  // by BEVEL above/below, so the real faces are further out than W/2, H, D/2.
  // Everything mounted on a face is placed relative to these, proud of the
  // surface - anything straddling a face z-fights with it.
  const BEVEL = 0.12;
  const FRONT = D / 2 + BEVEL, BACK = -FRONT;
  const RIGHT = W / 2 + BEVEL, LEFT = -RIGHT;
  const TOP = H + 2 * BEVEL;            // body sits with its bottom bevel on the ground
  const MID = TOP / 2;

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(38, 1, 0.1, 200);
  const HOME = new THREE.Vector3(22, 16, 26);
  camera.position.copy(HOME);

  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.12;        // lighter damping so zoom does not lag
  controls.zoomSpeed = 2.2;
  controls.zoomToCursor = true;         // zoom towards what is under the pointer
  controls.minDistance = 10;
  controls.maxDistance = 70;
  controls.maxPolarAngle = Math.PI / 2 - 0.03;   // do not go under the table
  controls.target.set(0, 1.5, 0);

  // Lights: soft sky, one key with shadows, a low fill from the front
  scene.add(new THREE.HemisphereLight(0xffffff, 0x8a8f99, 0.9));
  const key = new THREE.DirectionalLight(0xffffff, 1.6);
  key.position.set(14, 24, 10);
  key.castShadow = true;
  key.shadow.mapSize.set(2048, 2048);
  key.shadow.camera.left = key.shadow.camera.bottom = -25;
  key.shadow.camera.right = key.shadow.camera.top = 25;
  key.shadow.bias = -0.0005;
  scene.add(key);
  const fill = new THREE.DirectionalLight(0xdfe8ff, 0.5);
  fill.position.set(-12, 6, 18);
  scene.add(fill);

  // Ground: only its shadow is visible, so the page background shows through
  const ground = new THREE.Mesh(
    new THREE.PlaneGeometry(120, 120),
    new THREE.ShadowMaterial({ opacity: 0.22 }),
  );
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  scene.add(ground);

  // -------------------------------------------------------------- the box
  const device = new THREE.Group();
  scene.add(device);

  const body = new THREE.Mesh(roundedSlab(W, H, D, 1.6), M.body);
  body.position.y = BEVEL;              // lift the bottom bevel onto the ground plane
  body.castShadow = body.receiveShadow = true;
  device.add(body);

  // Top panel: sunk a touch into the body so no face is coplanar with it
  const top = new THREE.Mesh(roundedSlab(W - 2.2, 0.12, D - 2.2, 1.1), M.top);
  top.position.y = TOP + BEVEL - 0.05;
  top.receiveShadow = true;
  device.add(top);

  // Antennas at the back corners, leaning outward
  for (const side of [-1, 1]) {
    const stub = new THREE.Mesh(new THREE.CylinderGeometry(0.42, 0.5, 0.9, 16), M.antenna);
    stub.position.set(side * (W / 2 - 2.2), TOP + 0.45, -D / 2 + 1.0);
    device.add(stub);
    const mast = new THREE.Mesh(new THREE.CylinderGeometry(0.28, 0.34, 9, 16), M.antenna);
    mast.position.set(side * (W / 2 - 2.2), TOP + 0.45, -D / 2 + 1.0);
    mast.geometry.translate(0, 4.5, 0);
    mast.rotation.z = side * -0.32;
    mast.rotation.x = -0.18;
    mast.castShadow = true;
    device.add(mast);
  }

  // -------------------------------------------------------------- LEDs (front)
  const leds = {};
  const ledOrder = ["power", "internet", "wifi24", "wifi5", "lan", "cellular", "sd", "activity"];
  const ledLabels = {
    power: "Power", internet: "Internet (WAN)", wifi24: "WiFi 2.4 GHz",
    wifi5: "WiFi 5 GHz", lan: "LAN", cellular: "Cellular", sd: "SD boot",
    activity: "Activity",
  };
  ledOrder.forEach((name, i) => {
    const led = new THREE.Mesh(
      new THREE.CylinderGeometry(0.22, 0.22, 0.18, 20),
      M.ledOff.clone(),
    );
    led.rotation.x = Math.PI / 2;
    led.position.set(-W / 2 + 2.4 + i * 1.35, MID + 0.15, FRONT + 0.09 + 0.01);
    led.userData = { label: ledLabels[name], kind: "led", name };
    device.add(led);
    leds[name] = led;
  });

  // -------------------------------------------------------------- back panel
  // The group sits on the back face and is turned to face outward, so local
  // +z points away from the body. Each part is stacked strictly in front of
  // the previous one - a frame, the dark opening on its face, the link LED.
  function port(x, rim, label) {
    const g = new THREE.Group();
    const frame = new THREE.Mesh(new THREE.BoxGeometry(1.9, 1.6, 0.16), rim);
    frame.position.z = 0.01 + 0.08;
    const cavity = new THREE.Mesh(new THREE.BoxGeometry(1.5, 1.2, 0.04), M.port);
    cavity.position.z = 0.01 + 0.16 + 0.02;
    const link = new THREE.Mesh(new THREE.BoxGeometry(0.25, 0.14, 0.05), M.ledOff.clone());
    link.position.set(0.7, -0.62, 0.01 + 0.16 + 0.03);
    g.add(frame, cavity, link);
    g.position.set(x, MID, BACK);
    g.rotation.y = Math.PI;
    g.userData = { label, kind: "port" };
    g.link = link;
    device.add(g);
    return g;
  }
  const wanPort = port(-6.5, M.wanRim, "WAN  (Ethernet.Interface.1)");
  const lanPort = port(-4.2, M.portRim, "LAN  (Ethernet.Interface.2)");

  // Power jack
  const jack = new THREE.Mesh(new THREE.CylinderGeometry(0.5, 0.5, 0.5, 20), M.port);
  jack.rotation.x = Math.PI / 2;
  jack.position.set(7.5, MID, BACK - 0.26);
  device.add(jack);

  // Reset button: a red pin standing in a dark collar, both proud of the face
  const RESET_REST = BACK - 0.28, RESET_PRESSED = BACK - 0.14;
  const resetWell = new THREE.Mesh(new THREE.CylinderGeometry(0.55, 0.55, 0.3, 20), M.port);
  resetWell.rotation.x = Math.PI / 2;
  resetWell.position.set(4.6, MID, BACK - 0.16);
  device.add(resetWell);
  const resetButton = new THREE.Mesh(new THREE.CylinderGeometry(0.3, 0.3, 0.5, 20), M.button);
  resetButton.rotation.x = Math.PI / 2;
  resetButton.position.set(4.6, MID, RESET_REST);
  resetButton.userData = { label: "Reset — press: reboot · hold 3s: factory reset", kind: "reset" };
  device.add(resetButton);

  // -------------------------------------------------------------- SIM (left)
  const simSlot = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.5, 2.6), M.port);
  simSlot.position.set(LEFT - 0.16, MID, 2.5);
  device.add(simSlot);
  const sim = new THREE.Group();
  const simBody = new THREE.Mesh(new THREE.BoxGeometry(2.4, 0.16, 1.7), M.sim);
  const simChip = new THREE.Mesh(new THREE.BoxGeometry(0.7, 0.18, 0.6), M.simChip);
  simChip.position.set(0.4, 0.02, 0);
  sim.add(simBody, simChip);
  sim.rotation.y = 0;
  sim.userData = { label: "SIM card — click to insert / eject", kind: "sim" };
  simBody.userData = sim.userData; simChip.userData = sim.userData;
  device.add(sim);
  const SIM_IN  = new THREE.Vector3(LEFT + 1.0, MID, 2.5);
  const SIM_OUT = new THREE.Vector3(LEFT - 2.3, MID, 2.5);
  sim.position.copy(SIM_OUT);

  // -------------------------------------------------------------- SD (right)
  const sdSlot = new THREE.Mesh(new THREE.BoxGeometry(0.3, 0.42, 3.0), M.port);
  sdSlot.position.set(RIGHT + 0.16, MID, -2.0);
  device.add(sdSlot);
  const sd = new THREE.Group();
  const sdBody = new THREE.Mesh(new THREE.BoxGeometry(3.2, 0.2, 2.4), M.sd);
  const sdLabelMesh = new THREE.Mesh(new THREE.BoxGeometry(2.0, 0.22, 1.5), M.sdLabel);
  sdLabelMesh.position.set(-0.3, 0.02, 0);
  const sdNotch = new THREE.Mesh(new THREE.BoxGeometry(0.5, 0.24, 0.4), M.port);
  sdNotch.position.set(1.4, 0, 1.0);
  sd.add(sdBody, sdLabelMesh, sdNotch);
  sd.userData = { label: "SD card — click to seat / eject", kind: "sd" };
  for (const m of sd.children) m.userData = sd.userData;
  device.add(sd);
  const SD_IN  = new THREE.Vector3(RIGHT - 1.3, MID, -2.0);
  const SD_OUT = new THREE.Vector3(RIGHT + 2.8, MID, -2.0);
  sd.position.copy(SD_OUT);

  // -------------------------------------------------------------- overlays
  const tip = document.createElement("div");
  tip.className = "tip";
  container.appendChild(tip);

  const ring = document.createElement("div");
  ring.className = "hold";
  ring.innerHTML = `<svg viewBox="0 0 40 40"><circle class="track" cx="20" cy="20" r="16"/>
    <circle class="fill" cx="20" cy="20" r="16"/></svg><span></span>`;
  container.appendChild(ring);
  const ringFill = ring.querySelector(".fill");
  const ringText = ring.querySelector("span");
  const CIRC = 2 * Math.PI * 16;
  ringFill.style.strokeDasharray = `${CIRC}`;

  // -------------------------------------------------------------- state
  let state = null;
  let sdPresent = false, sdInserted = false, simInserted = false, rebooting = false;
  let bootedFromCard = false, activity = false;
  const clock = new THREE.Clock();

  function setLed(name, on, color) {
    const led = leds[name];
    if (!led) return;
    led.material.emissive.setHex(on ? color : 0x000000);
    led.material.emissiveIntensity = on ? 1.6 : 0;
    led.material.color.setHex(on ? color : 0x111317);
  }

  function applyState(next) {
    state = next;
    const sys = state.system || {};
    rebooting = !!sys.rebooting;
    activity = (sys.runningJobs || []).length > 0;
    simInserted = !!(sys.sim && sys.sim.inserted);
    sdPresent = !!(sys.sdcard && sys.sdcard.present);
    sdInserted = !!(sys.sdcard && sys.sdcard.inserted);
    bootedFromCard = !!(sys.bootedFrom && sys.bootedFrom.from === "sdcard");

    const eth1 = rowValues(state, "Device.Ethernet.Interface.1");
    const eth2 = rowValues(state, "Device.Ethernet.Interface.2");
    const wan  = rowValues(state, "Device.IP.Interface.1");
    const r1   = rowValues(state, "Device.WiFi.Radio.1");
    const r2   = rowValues(state, "Device.WiFi.Radio.2");
    const cell = rowValues(state, "Device.Cellular.Interface.1");

    if (rebooting) {
      for (const n of ledOrder) setLed(n, false);
      wanPort.link.material.emissive.setHex(0);
      lanPort.link.material.emissive.setHex(0);
      return;
    }
    setLed("power", true, LED_COLORS.power);
    setLed("internet", up(eth1) && up(wan), LED_COLORS.internet);
    setLed("wifi24", up(r1), LED_COLORS.wifi24);
    setLed("wifi5", up(r2), LED_COLORS.wifi5);
    setLed("lan", up(eth2), LED_COLORS.lan);
    setLed("cellular", up(cell), LED_COLORS.cellular);
    setLed("sd", sdInserted, bootedFromCard ? LED_COLORS.sd : 0xffb020);

    for (const [p, v] of [[wanPort, up(eth1)], [lanPort, up(eth2)]]) {
      p.link.material.emissive.setHex(v ? 0x3ddc84 : 0);
      p.link.material.emissiveIntensity = 1.4;
    }
  }

  // -------------------------------------------------------------- picking
  const ray = new THREE.Raycaster();
  const pointer = new THREE.Vector2();
  const pickables = [resetButton, sim, sd, ...Object.values(leds), wanPort, lanPort];
  let hovered = null;

  function pick(ev) {
    const rect = renderer.domElement.getBoundingClientRect();
    pointer.x = ((ev.clientX - rect.left) / rect.width) * 2 - 1;
    pointer.y = -((ev.clientY - rect.top) / rect.height) * 2 + 1;
    ray.setFromCamera(pointer, camera);
    const hits = ray.intersectObjects(pickables, true);
    if (!hits.length) return null;
    let o = hits[0].object;
    while (o && !o.userData.kind) o = o.parent;
    return o;
  }

  renderer.domElement.addEventListener("pointermove", (ev) => {
    if (holding) { positionRing(ev); return; }
    const o = pick(ev);
    if (o !== hovered) {
      hovered = o;
      renderer.domElement.style.cursor =
        o && ["reset", "sim", "sd"].includes(o.userData.kind) ? "pointer" : "grab";
    }
    if (o) {
      let text = o.userData.label;
      if (o.userData.kind === "sd") {
        const m = state && state.system.sdcard && state.system.sdcard.manifest;
        text = sdPresent
          ? `SD card: ${m.label} (obuspa ${m.obuspaVersion}, ${m.commit}) — click to ${sdInserted ? "eject" : "seat"}`
          : "SD card slot is empty — run `make flash` to write an image";
      } else if (o.userData.kind === "sim") {
        const s = state && state.system.sim;
        text = simInserted ? `SIM ${s.iccid} on ${s.carrier} — click to eject` : "SIM — click to insert";
      } else if (o.userData.kind === "led" && state) {
        const led = leds[o.userData.name];
        text += led.material.emissiveIntensity > 0 ? " — on" : " — off";
      }
      tip.textContent = text;
      tip.style.display = "block";
      tip.style.left = `${ev.clientX - renderer.domElement.getBoundingClientRect().left + 14}px`;
      tip.style.top = `${ev.clientY - renderer.domElement.getBoundingClientRect().top + 14}px`;
    } else {
      tip.style.display = "none";
    }
  });
  renderer.domElement.addEventListener("pointerleave", () => { tip.style.display = "none"; });

  // -------------------------------------------------------------- reset button
  const HOLD_MS = 3000;
  let holding = false, holdStart = 0, holdTimer = null;

  function positionRing(ev) {
    const rect = renderer.domElement.getBoundingClientRect();
    ring.style.left = `${ev.clientX - rect.left}px`;
    ring.style.top = `${ev.clientY - rect.top}px`;
  }

  function endHold(fire) {
    if (!holding) return;
    holding = false;
    controls.enabled = true;
    ring.style.display = "none";
    cancelAnimationFrame(holdTimer);
    resetButton.position.z = RESET_REST;
    if (!fire) return;
    const held = performance.now() - holdStart;
    if (held >= HOLD_MS) api.factoryReset();
    else api.reboot();
  }

  renderer.domElement.addEventListener("pointerdown", (ev) => {
    if (ev.button !== 0) return;
    const o = pick(ev);
    if (!o) return;
    if (o.userData.kind === "reset") {
      if (rebooting) return;
      holding = true;
      holdStart = performance.now();
      controls.enabled = false;
      resetButton.position.z = RESET_PRESSED;     // press in
      positionRing(ev);
      ring.style.display = "block";
      ring.classList.remove("armed");
      const tick = () => {
        if (!holding) return;
        const t = Math.min(1, (performance.now() - holdStart) / HOLD_MS);
        ringFill.style.strokeDashoffset = `${CIRC * (1 - t)}`;
        if (t >= 1) { ring.classList.add("armed"); ringText.textContent = "release: factory reset"; }
        else ringText.textContent = "release: reboot";
        holdTimer = requestAnimationFrame(tick);
      };
      tick();
      ev.preventDefault();
    }
  });
  window.addEventListener("pointerup", () => endHold(true));
  window.addEventListener("pointercancel", () => endHold(false));
  renderer.domElement.addEventListener("pointerleave", () => endHold(false));

  renderer.domElement.addEventListener("click", (ev) => {
    const o = pick(ev);
    if (!o) return;
    if (o.userData.kind === "sim") api.sim(simInserted ? "eject" : "insert");
    if (o.userData.kind === "sd") {
      if (!sdPresent) { api.notice("The SD card slot is empty — run `make flash SRC=...` first."); return; }
      api.sdcard(sdInserted ? "eject" : "insert");
    }
  });
  renderer.domElement.addEventListener("dblclick", () => {
    camera.position.copy(HOME);
    controls.target.set(0, 1.5, 0);
  });

  // -------------------------------------------------------------- loop
  function resize() {
    const w = container.clientWidth, h = container.clientHeight;
    renderer.setSize(w, h, false);
    camera.aspect = w / h;
    camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(container);
  resize();

  function animate() {
    requestAnimationFrame(animate);
    const t = clock.getElapsedTime();

    // Cards slide to where the state says they are
    sim.position.lerp(simInserted ? SIM_IN : SIM_OUT, 0.15);
    sd.position.lerp(sdInserted ? SD_IN : SD_OUT, 0.15);

    // Power LED breathes while rebooting; activity LED blinks while working
    if (rebooting) {
      const pulse = 0.5 + 0.5 * Math.sin(t * 6);
      leds.power.material.emissive.setHex(0xffffff);
      leds.power.material.emissiveIntensity = pulse * 1.2;
      leds.power.material.color.setHex(0xffffff);
    } else if (activity) {
      const on = Math.sin(t * 14) > 0;
      setLed("activity", on, LED_COLORS.activity);
    } else {
      setLed("activity", false);
    }

    controls.update();
    renderer.render(scene, camera);
  }
  animate();

  return { applyState };
}
