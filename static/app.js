/* BBQ dashboard logic (task 5) — vanilla JS, no build step.
 *
 * Talks only to this server's API (/api/*), never to the device directly.
 * Status polling every 2 s; the history chart via uPlot (vendored).
 */

"use strict";

const PROBES = [
  { key: "cook", label: "Cook", min: 32, max: 475 },
  { key: "food1", label: "Food 1", min: 32, max: 300 },
  { key: "food2", label: "Food 2", min: 32, max: 300 },
  { key: "food3", label: "Food 3", min: 32, max: 300 },
];

const STATUS = ["OK", "HIGH", "LOW", "DONE", "ERROR", "HOLD", "ALARM", "SHUTDOWN"];

const RANGES = {
  "1h": { seconds: 3600, step: 30 },
  "6h": { seconds: 3600 * 6, step: 60 },
  "24h": { seconds: 3600 * 24, step: 300 },
  cook: { seconds: null, step: 300 }, // from the active cook's start
};

const SERIES = [
  { key: "cook", label: "Cook", color: "#ff7043", width: 2 },
  { key: "food1", label: "Food 1", color: "#42a5f5", width: 2 },
  { key: "food2", label: "Food 2", color: "#66bb6a", width: 2 },
  { key: "food3", label: "Food 3", color: "#ab47bc", width: 2 },
  { key: "cook_set", label: "Cook set", color: "#8d6e63", width: 1 },
  { key: "food1_set", label: "Food 1 set", color: "#5c8fb7", width: 1 },
  { key: "food2_set", label: "Food 2 set", color: "#4e7d52", width: 1 },
  { key: "food3_set", label: "Food 3 set", color: "#7d5688", width: 1 },
];

const state = {
  sample: null,
  device: null,
  cooks: [],
  activeCook: null,
  range: "24h",
  plot: null,
  config: null,
};

const $ = (id) => document.getElementById(id);

async function fetchJSON(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = "";
    try {
      detail = (await response.json()).detail || "";
    } catch (err) { /* not JSON */ }
    throw new Error(`${response.status} ${response.statusText}${detail ? ` — ${detail}` : ""}`);
  }
  return response.json();
}

// ---------------------------------------------------------------------------
// status polling and rendering
// ---------------------------------------------------------------------------

function fmtTemp(value) {
  if (value === null || value === undefined) return null;
  return (Math.round(value * 10) / 10).toFixed(1);
}

function renderDevice() {
  const pill = $("device-pill");
  const banner = $("banner");
  const device = state.device;
  if (!device) return;
  if (device.reachable) {
    pill.className = "pill up";
    pill.innerHTML = `<span class="dot"></span>device OK · ${device.age_s ?? "?"}s ago`;
    banner.classList.add("hidden");
  } else {
    pill.className = "pill down";
    pill.innerHTML = `<span class="dot"></span>unreachable`;
    banner.textContent = `Device unreachable${
      device.last_error ? ` (${device.last_error})` : ""
    } — showing last known values.`;
    banner.classList.remove("hidden");
  }
}

function renderSample() {
  const sample = state.sample;
  if (!sample) return;
  for (const probe of PROBES) {
    const info = sample.probes[probe.key];
    const card = $(`card-${probe.key}`);
    if (!card) continue;
    const temp = fmtTemp(info.temp);
    const tempEl = $(`temp-${probe.key}`);
    tempEl.className = `probe-temp st-${info.status}`;
    tempEl.innerHTML = temp === null
      ? `<span class="open">OPEN</span>`
      : `${temp}<span class="unit"> °F</span>`;
    $(`name-${probe.key}`).textContent = info.name || probe.label;
    $(`set-${probe.key}`).innerHTML = `<b>${fmtTemp(info.set)}°F</b>`;
    const chip = $(`status-${probe.key}`);
    chip.className = `chip st-${info.status}`;
    chip.textContent = STATUS[info.status] ?? "?";
  }
  $("fan-fill").style.width = `${sample.output_pct}%`;
  $("fan-pct").textContent = `${sample.output_pct}%`;
  $("timer").textContent = sample.timer_curr || "--:--:--";
  $("timer-status").textContent = sample.timer_status === 1 ? "running" : "idle";
}

function renderCooks() {
  const info = $("cook-info");
  const endBtn = $("btn-cook-end");
  state.activeCook = state.cooks.find((cook) => cook.ended_ts === null) || null;
  if (state.activeCook) {
    const started = new Date(state.activeCook.started_ts * 1000);
    info.innerHTML = `<b>${escapeHTML(state.activeCook.name)}</b><br>
      <span class="muted">since ${started.toLocaleString()}</span>`;
    endBtn.disabled = false;
  } else {
    info.textContent = "No active cook";
    endBtn.disabled = true;
  }
}

function escapeHTML(text) {
  const div = document.createElement("div");
  div.textContent = String(text);
  return div.innerHTML;
}

async function pollStatus() {
  try {
    const body = await fetchJSON("/api/status");
    state.sample = body.sample;
    state.device = body.device;
    renderSample();
    renderDevice();
  } catch (err) {
    state.device = { reachable: false, last_error: String(err) };
    renderDevice();
  }
}

async function pollCooks() {
  try {
    state.cooks = await fetchJSON("/api/cooks");
    renderCooks();
  } catch (err) { /* transient */ }
}

// ---------------------------------------------------------------------------
// probe cards
// ---------------------------------------------------------------------------

function buildProbeCards() {
  const container = $("probes");
  container.innerHTML = "";
  for (const probe of PROBES) {
    const card = document.createElement("div");
    card.className = "card clickable";
    card.id = `card-${probe.key}`;
    card.title = "Click to edit name / setpoint";
    card.innerHTML = `
      <div class="card-title">${probe.label} <span class="chip st-0" id="status-${probe.key}">…</span></div>
      <div class="probe-name" id="name-${probe.key}">${probe.label}</div>
      <div class="probe-temp" id="temp-${probe.key}">…</div>
      <div class="probe-meta">
        <span class="probe-set">set <b id="set-${probe.key}">–</b></span>
      </div>`;
    card.addEventListener("click", () => openProbeDialog(probe));
    container.appendChild(card);
  }
}

function openProbeDialog(probe) {
  const info = state.sample && state.sample.probes[probe.key];
  if (!info) return;
  $("probe-dialog-title").textContent = `Edit ${probe.label}`;
  $("probe-name").value = info.name || "";
  $("probe-set").value = info.set;
  $("probe-set").min = probe.min;
  $("probe-set").max = probe.max;
  $("probe-hint").textContent = `Setpoint ${probe.min}–${probe.max} °F (values are clamped).`;
  $("dialog-error").textContent = "";
  const dialog = $("probe-dialog");
  dialog.returnValue = "";
  dialog.showModal();
  dialog.onclose = async () => {
    if (dialog.returnValue !== "ok") return;
    const name = $("probe-name").value.trim();
    const set = $("probe-set").value;
    if (!name && !set) return;
    const body = {};
    if (name) body.name = name;
    if (set !== "") body.set_f = Number(set);
    try {
      const result = await fetchJSON(`/api/probes/${probe.key}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      state.sample = result.sample;
      renderSample();
    } catch (err) {
      $("dialog-error").textContent = String(err.message || err);
      dialog.showModal();
    }
  };
}

function openTimerDialog() {
  const dialog = $("timer-dialog");
  $("timer-input").value = state.sample ? state.sample.timer_curr : "";
  $("timer-dialog-error").textContent = "";
  dialog.returnValue = "";
  dialog.showModal();
  dialog.onclose = async () => {
    if (dialog.returnValue !== "ok") return;
    const value = $("timer-input").value.trim();
    if (!value) return;
    try {
      const result = await fetchJSON("/api/timer", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ hhmmss: value }),
      });
      state.sample = result.sample;
      renderSample();
    } catch (err) {
      $("timer-dialog-error").textContent = String(err.message || err);
      dialog.showModal();
    }
  };
}

function openCookDialog() {
  const dialog = $("cook-dialog");
  $("cook-name").value = "";
  $("cook-dialog-error").textContent = "";
  dialog.returnValue = "";
  dialog.showModal();
  dialog.onclose = async () => {
    if (dialog.returnValue !== "ok") return;
    const name = $("cook-name").value.trim();
    if (!name) return;
    try {
      await fetchJSON("/api/cooks", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      await pollCooks();
      if (state.range === "cook") loadHistory();
    } catch (err) {
      $("cook-dialog-error").textContent = String(err.message || err);
      dialog.showModal();
    }
  };
}

async function endCook() {
  if (!state.activeCook) return;
  if (!window.confirm(`End cook "${state.activeCook.name}"?`)) return;
  try {
    await fetchJSON(`/api/cooks/${state.activeCook.id}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ end: true }),
    });
    await pollCooks();
  } catch (err) {
    window.alert(String(err.message || err));
  }
}

// ---------------------------------------------------------------------------
// history chart
// ---------------------------------------------------------------------------

function chartOptions() {
  return {
    width: Math.min($("chart").clientWidth || 900, 1040),
    height: 360,
    cursor: { drag: { x: true, y: false } },
    legend: { live: false },
    scales: { x: { time: true } },
    axes: [
      {},
      {
        label: "°F",
        values: (self, ticks) => ticks.map((v) => v.toFixed(0)),
      },
    ],
    series: [
      {},
      ...SERIES.map((s) => ({
        label: s.label,
        stroke: s.color,
        width: s.width,
        points: { show: false },
        spanGaps: false,
      })),
    ],
  };
}

async function loadHistory() {
  const range = RANGES[state.range];
  const now = Math.floor(Date.now() / 1000);
  let from;
  if (state.range === "cook") {
    if (!state.activeCook) {
      $("chart-empty").classList.remove("hidden");
      return;
    }
    from = state.activeCook.started_ts;
  } else {
    from = now - range.seconds;
  }
  const step = range.step;
  try {
    const body = await fetchJSON(`/api/history?from=${from}&to=${now}&step=${step}`);
    const points = body.points;
    if (!points.length) {
      $("chart-empty").classList.remove("hidden");
      if (state.plot) state.plot.setData(emptyChartData());
      return;
    }
    $("chart-empty").classList.add("hidden");
    const data = [
      points.map((p) => p.ts),
      ...SERIES.map((s) => points.map((p) => p[s.key] ?? null)),
    ];
    if (state.plot) {
      state.plot.setData(data);
    } else {
      state.plot = new uPlot(chartOptions(), data, $("chart"));
      window.addEventListener("resize", () => {
        state.plot.setSize({
          width: Math.min($("chart").clientWidth || 900, 1040),
          height: 360,
        });
      });
    }
  } catch (err) {
    $("chart-empty").textContent = `History unavailable: ${err.message}`;
    $("chart-empty").classList.remove("hidden");
  }
}

function emptyChartData() {
  return [new Array(0), ...SERIES.map(() => new Array(0))];
}

function setRange(range) {
  state.range = range;
  for (const button of document.querySelectorAll("#range-btns [data-range]")) {
    button.classList.toggle("active", button.dataset.range === range);
  }
  if (range === "cook") pollCooks().then(loadHistory);
  else loadHistory();
}

// ---------------------------------------------------------------------------
// device setup: config panels (task 5 extension)
// ---------------------------------------------------------------------------

const ENC_NAMES = {
  0: "Open", 1: "WEP40", 2: "WEP104", 3: "WPA", 4: "WPA phrase", 5: "WPA2",
  6: "WPA2 phrase", 7: "WPA auto", 8: "WPA auto phrase", 9: "WEP auto",
};
const MODE_NAMES = { 0: "Infrastructure", 1: "Hot spot" };
const TIMEOUT_NAMES = { 0: "No action", 1: "Hold", 2: "Alarm", 3: "Shutdown" };
const RAMP_NAMES = { 0: "Off", 1: "Food 1", 2: "Food 2", 3: "Food 3" };

function kvRow(label, value) {
  return `<div class="kv-row"><span>${label}</span><b>${escapeHTML(String(value))}</b></div>`;
}

const tenthsF = (v) => (v === null || v === undefined) ? "–" : `${(v / 10).toFixed(1)} °F`;

function renderConfig() {
  const config = state.config;
  if (!config) return;

  const c = config.control;
  $("ctl-summary").innerHTML = [
    kvRow("Cook hold", tenthsF(c.cookhold)),
    kvRow("Alarm dev", tenthsF(c.alarmdev)),
    kvRow("Prop band", tenthsF(c.propband)),
    kvRow("Cycle time", `${c.cyctime} s`),
    kvRow("Timeout", TIMEOUT_NAMES[c.timeout_action] ?? c.timeout_action),
    kvRow("Ramp", RAMP_NAMES[c.cook_ramp] ?? c.cook_ramp),
    kvRow("Open detect", c.opendetect ? "On" : "Off"),
  ].join("");

  const s = config.system;
  $("sys-summary").innerHTML = [
    kvRow("Backlight", `${s.lcd_backlight} %`),
    kvRow("Contrast", `${s.lcd_contrast} %`),
    kvRow("Units", s.deg_units ? "°F" : "°C"),
    kvRow("Alarm beeps", s.alarm_beeps === 0 ? "Off" : `${s.alarm_beeps}`),
    kvRow("Key beeps", s.key_beeps ? "On" : "Off"),
    kvRow("Scrolling", s.menu_scrolling ? "On" : "Off"),
  ].join("");

  const e = config.email;
  $("eml-summary").innerHTML = [
    kvRow("Host", e.smtp_host || "–"),
    kvRow("Port", e.smtp_port === 0 ? "disabled" : e.smtp_port),
    kvRow("User", e.smtp_user || "–"),
    kvRow("To", e.smtp_to || "–"),
    kvRow("Interval", e.smtp_alert === 0 ? "alarms only" : `${e.smtp_alert} min`),
  ].join("");

  const w = config.wifi;
  const home = w.home || {};
  $("wifi-summary").innerHTML = [
    kvRow("Active", w.multi_wifi_choice ? "Away" : "Home"),
    kvRow("SSID", home.ssid || "–"),
    kvRow("Encryption", ENC_NAMES[home.wifi_enc] ?? home.wifi_enc),
    kvRow("Mode", MODE_NAMES[home.wifi_mode] ?? home.wifi_mode),
    kvRow("Addressing", home.dhcp ? "DHCP" : `static ${home.ip || "?"}`),
    kvRow("HTTP port", w.http_port),
    kvRow("HTTP login", w.http_user ? w.http_user : "none"),
  ].join("");
}

async function loadConfig() {
  try {
    state.config = await fetchJSON("/api/config");
    renderConfig();
  } catch (err) { /* the pill/banner already reports reachability */ }
}

async function patchConfig(section, body) {
  const result = await fetchJSON(`/api/config/${section}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (state.config && result.config && result.config[section]) {
    state.config[section] = result.config[section];
    renderConfig();
  }
  return result;
}

/** Collect changed fields from a dialog: only keys whose value differs from
 * the current config are included in the PATCH (no-op fields are not
 * written and not audited). */
function dialogChanges(fields, current) {
  const changes = {};
  for (const field of fields) {
    const element = $(field.id);
    if (!element) continue;
    const raw = element.value;
    let value;
    if (field.kind === "num") value = Number(raw);
    else if (field.kind === "sel") value = Number(raw);
    else value = raw.trim();
    const currentValue = field.div10
      ? current[field.key] / 10
      : (current[field.key] ?? (field.kind === "str" ? "" : 0));
    if (value !== currentValue && raw !== "") changes[field.key] = value;
  }
  return changes;
}

function runDialog(dialogId, errorId, onOk) {
  const dialog = $(dialogId);
  $(errorId).textContent = "";
  dialog.returnValue = "";
  dialog.showModal();
  dialog.onclose = async () => {
    if (dialog.returnValue !== "ok") return;
    try {
      await onOk();
    } catch (err) {
      $(errorId).textContent = String(err.message || err);
      dialog.showModal();
    }
  };
}

function openControlDialog() {
  const c = state.config && state.config.control;
  if (!c) return;
  $("ctl-cookhold").value = (c.cookhold / 10).toFixed(1);
  $("ctl-alarmdev").value = (c.alarmdev / 10).toFixed(1);
  $("ctl-propband").value = (c.propband / 10).toFixed(1);
  $("ctl-cyctime").value = c.cyctime;
  $("ctl-timeout_action").value = c.timeout_action;
  $("ctl-cook_ramp").value = c.cook_ramp;
  $("ctl-opendetect").value = c.opendetect;
  const fields = [
    { id: "ctl-cookhold", key: "cookhold", kind: "num", div10: true },
    { id: "ctl-alarmdev", key: "alarmdev", kind: "num", div10: true },
    { id: "ctl-propband", key: "propband", kind: "num", div10: true },
    { id: "ctl-cyctime", key: "cyctime", kind: "num" },
    { id: "ctl-timeout_action", key: "timeout_action", kind: "sel" },
    { id: "ctl-cook_ramp", key: "cook_ramp", kind: "sel" },
    { id: "ctl-opendetect", key: "opendetect", kind: "sel" },
  ];
  runDialog("ctl-dialog", "ctl-error", async () => {
    const changes = dialogChanges(fields, c);
    if (!Object.keys(changes).length) return;
    await patchConfig("control", changes);
  });
}

function openSystemDialog() {
  const s = state.config && state.config.system;
  if (!s) return;
  $("sys-lcd_backlight").value = s.lcd_backlight;
  $("sys-lcd_contrast").value = s.lcd_contrast;
  $("sys-deg_units").value = s.deg_units;
  $("sys-alarm_beeps").value = s.alarm_beeps;
  $("sys-key_beeps").value = s.key_beeps;
  $("sys-menu_scrolling").value = s.menu_scrolling;
  const fields = [
    { id: "sys-lcd_backlight", key: "lcd_backlight", kind: "num" },
    { id: "sys-lcd_contrast", key: "lcd_contrast", kind: "num" },
    { id: "sys-deg_units", key: "deg_units", kind: "sel" },
    { id: "sys-alarm_beeps", key: "alarm_beeps", kind: "sel" },
    { id: "sys-key_beeps", key: "key_beeps", kind: "sel" },
    { id: "sys-menu_scrolling", key: "menu_scrolling", kind: "sel" },
  ];
  runDialog("sys-dialog", "sys-error", async () => {
    const changes = dialogChanges(fields, s);
    if (!Object.keys(changes).length) return;
    await patchConfig("system", changes);
  });
}

function openEmailDialog() {
  const e = state.config && state.config.email;
  if (!e) return;
  $("eml-smtp_host").value = e.smtp_host || "";
  $("eml-smtp_port").value = e.smtp_port;
  $("eml-smtp_user").value = e.smtp_user || "";
  $("eml-smtp_pwd").value = ""; // write-only: blank keeps the stored one
  $("eml-smtp_to").value = e.smtp_to || "";
  $("eml-smtp_from").value = e.smtp_from || "";
  $("eml-smtp_subj").value = e.smtp_subj || "";
  $("eml-smtp_alert").value = e.smtp_alert;
  runDialog("eml-dialog", "eml-error", async () => {
    const changes = {};
    if ($("eml-smtp_host").value.trim() !== (e.smtp_host || ""))
      changes.smtp_host = $("eml-smtp_host").value.trim();
    if (Number($("eml-smtp_port").value) !== e.smtp_port)
      changes.smtp_port = Number($("eml-smtp_port").value);
    if ($("eml-smtp_user").value.trim() !== (e.smtp_user || ""))
      changes.smtp_user = $("eml-smtp_user").value.trim();
    if ($("eml-smtp_pwd").value !== "")
      changes.smtp_pwd = $("eml-smtp_pwd").value; // never echoed back
    if ($("eml-smtp_to").value.trim() !== (e.smtp_to || ""))
      changes.smtp_to = $("eml-smtp_to").value.trim();
    if ($("eml-smtp_from").value.trim() !== (e.smtp_from || ""))
      changes.smtp_from = $("eml-smtp_from").value.trim();
    if ($("eml-smtp_subj").value.trim() !== (e.smtp_subj || ""))
      changes.smtp_subj = $("eml-smtp_subj").value.trim();
    if (Number($("eml-smtp_alert").value) !== e.smtp_alert)
      changes.smtp_alert = Number($("eml-smtp_alert").value);
    if (!Object.keys(changes).length) return;
    await patchConfig("email", changes);
  });
}

function openWifiDialog() {
  const w = state.config && state.config.wifi;
  if (!w) return;
  const home = w.home || {};
  const away = w.away || {};
  $("wifi-confirm").checked = false;
  $("wifi-ssid").value = home.ssid || "";
  $("wifi-wifi_key").value = "";
  $("wifi-wifi_enc").value = home.wifi_enc;
  $("wifi-wifi_mode").value = home.wifi_mode;
  $("wifi-dhcp").value = home.dhcp;
  $("wifi-ip").value = home.ip || "";
  $("wifi-gw").value = home.gw || "";
  $("wifi-nm").value = home.nm || "";
  $("wifi-dns").value = home.dns || "";
  $("wifi-away_ssid").value = away.ssid || "";
  $("wifi-away_wifi_key").value = "";
  $("wifi-away_wifi_enc").value = away.wifi_enc;
  $("wifi-away_wifi_mode").value = away.wifi_mode;
  $("wifi-away_dhcp").value = away.dhcp;
  $("wifi-away_ip").value = away.ip || "";
  $("wifi-away_gw").value = away.gw || "";
  $("wifi-away_nm").value = away.nm || "";
  $("wifi-away_dns").value = away.dns || "";
  $("wifi-multi_wifi_choice").value = w.multi_wifi_choice;
  $("wifi-http_port").value = w.http_port;
  $("wifi-http_user").value = "";
  $("wifi-http_pass").value = "";
  runDialog("wifi-dialog", "wifi-error", async () => {
    if (!$("wifi-confirm").checked) {
      throw new Error("Check the confirmation box first — network writes are risky.");
    }
    const flat = {
      ...(w.home || {}), ...(w.away ? Object.fromEntries(
        Object.entries(w.away).map(([k, v]) => [`away_${k}`, v])
      ) : {}),
      multi_wifi_choice: w.multi_wifi_choice, http_port: w.http_port,
    };
    const fields = [
      { id: "wifi-ssid", key: "ssid", kind: "str" },
      { id: "wifi-wifi_enc", key: "wifi_enc", kind: "sel" },
      { id: "wifi-wifi_mode", key: "wifi_mode", kind: "sel" },
      { id: "wifi-dhcp", key: "dhcp", kind: "sel" },
      { id: "wifi-ip", key: "ip", kind: "str" },
      { id: "wifi-gw", key: "gw", kind: "str" },
      { id: "wifi-nm", key: "nm", kind: "str" },
      { id: "wifi-dns", key: "dns", kind: "str" },
      { id: "wifi-away_ssid", key: "away_ssid", kind: "str" },
      { id: "wifi-away_wifi_enc", key: "away_wifi_enc", kind: "sel" },
      { id: "wifi-away_wifi_mode", key: "away_wifi_mode", kind: "sel" },
      { id: "wifi-away_dhcp", key: "away_dhcp", kind: "sel" },
      { id: "wifi-away_ip", key: "away_ip", kind: "str" },
      { id: "wifi-away_gw", key: "away_gw", kind: "str" },
      { id: "wifi-away_nm", key: "away_nm", kind: "str" },
      { id: "wifi-away_dns", key: "away_dns", kind: "str" },
      { id: "wifi-multi_wifi_choice", key: "multi_wifi_choice", kind: "sel" },
      { id: "wifi-http_port", key: "http_port", kind: "num" },
    ];
    const changes = dialogChanges(fields, flat);
    // write-only credentials: sent only when actually filled in
    if ($("wifi-wifi_key").value !== "") changes.wifi_key = $("wifi-wifi_key").value;
    if ($("wifi-away_wifi_key").value !== "")
      changes.away_wifi_key = $("wifi-away_wifi_key").value;
    if ($("wifi-http_user").value.trim() !== "") changes.http_user = $("wifi-http_user").value.trim();
    if ($("wifi-http_pass").value !== "") changes.http_pass = $("wifi-http_pass").value;
    if (!Object.keys(changes).length) return;
    await patchConfig("wifi", { confirm: true, ...changes });
  });
}

async function sendTestEmail() {
  if (!window.confirm("Send a test e-mail now (SMTP_NOW)?")) return;
  try {
    await fetchJSON("/api/email/test", { method: "POST" });
    window.alert("Test e-mail sent — check the recipient inbox.");
  } catch (err) {
    window.alert(String(err.message || err));
  }
}

async function startWifiScan() {
  try {
    await fetchJSON("/api/wifi/scan", { method: "POST" });
    window.alert("WiFi scan started on the device (results appear on its LCD).");
  } catch (err) {
    window.alert(String(err.message || err));
  }
}

async function clearHttpLogin() {
  if (!window.confirm(
    "Disable the device's HTTP login entirely? The dashboard and the stock " +
    "web UI will no longer ask for credentials."
  )) return;
  try {
    await patchConfig("wifi", { confirm: true, http_user: "", http_pass: "" });
  } catch (err) {
    window.alert(String(err.message || err));
  }
}

async function loadFeatures() {
  try {
    const features = await fetchJSON("/api/features");
    $("btn-reboot").hidden = !features.reboot;
  } catch (err) {
    $("btn-reboot").hidden = true;
  }
}

async function rebootDevice() {
  if (!window.confirm(
    "Reboot the CyberQ now? This is a plain reboot (not a factory reset); " +
    "control and readings pause for about a minute while it restarts."
  )) return;
  try {
    await fetchJSON("/api/reboot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm: true }),
    });
    window.alert("Reboot sent. Readings resume once the device is back.");
  } catch (err) {
    window.alert(String(err.message || err));
  }
}

// ---------------------------------------------------------------------------
// startup
// ---------------------------------------------------------------------------

function start() {
  buildProbeCards();
  $("btn-timer").addEventListener("click", openTimerDialog);
  $("btn-cook-start").addEventListener("click", openCookDialog);
  $("btn-cook-end").addEventListener("click", endCook);
  $("btn-refresh").addEventListener("click", loadHistory);
  $("btn-ctl-edit").addEventListener("click", openControlDialog);
  $("btn-sys-edit").addEventListener("click", openSystemDialog);
  $("btn-eml-edit").addEventListener("click", openEmailDialog);
  $("btn-eml-test").addEventListener("click", sendTestEmail);
  $("btn-wifi-edit").addEventListener("click", openWifiDialog);
  $("btn-wifi-scan").addEventListener("click", startWifiScan);
  $("btn-wifi-clearauth").addEventListener("click", clearHttpLogin);
  $("btn-reboot").addEventListener("click", rebootDevice);
  for (const button of document.querySelectorAll("#range-btns [data-range]")) {
    button.addEventListener("click", () => setRange(button.dataset.range));
  }

  pollStatus();
  pollCooks().then(loadHistory);
  loadConfig();
  loadFeatures();
  setInterval(pollStatus, 2000);
  setInterval(pollCooks, 30000);
  setInterval(loadConfig, 60000);
  setInterval(() => {
    if (document.visibilityState === "visible") loadHistory();
  }, 60000);
}

start();
