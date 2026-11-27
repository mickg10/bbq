/* Cook report (report.html): a cook as it went, from GET /api/report.
 *
 * Talks only to this server's API, never to the device directly. The chart
 * and the small helpers are shared with the dashboard (chart.js, common.js).
 */

"use strict";

const params = new URLSearchParams(window.location.search);
const query = new URLSearchParams();
if (params.get("cook")) query.set("cook", params.get("cook"));
if (params.get("hours")) query.set("hours", params.get("hours"));

const FOOD_LABELS = { food1: "Food 1", food2: "Food 2", food3: "Food 3" };
const MINS = 60;
const HOUR = 3600;

const num = (value, digits = 1, unit = "") =>
  (value === null || value === undefined) ? "–" : `${value.toFixed(digits)}${unit}`;
const signed = (value, digits = 1) =>
  (value === null || value === undefined) ? "–" : `${value > 0 ? "+" : ""}${value.toFixed(digits)}`;
const clock = (ts) => (ts === null || ts === undefined) ? "–" : fmtClock(ts);
const moment = (ts) => {
  if (ts === null || ts === undefined) return "–";
  const d = new Date(ts * 1000);
  return `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${fmtClock(ts)}`;
};
const kv = (label, value) =>
  `<div class="kv-row"><span>${escapeHTML(String(label))}</span><b>${escapeHTML(String(value))}</b></div>`;
const note = (text) => `<div class="muted report-note">${escapeHTML(text)}</div>`;

/** What was scored, and what was left out: as `sim compare` reads a recording. */
function rangeLines(report) {
  const range = report.range;
  const cook = range.cook;
  let head;
  if (range.basis === "cook") {
    head = `Cook #${cook.id}${cook.name ? `, “${cook.name}”` : ""}, from ${moment(range.from)} `
      + `to ${cook.ended_ts == null ? "now" : moment(range.to)}`;
  } else if (range.basis === "latest cook") {
    head = `The latest cook, #${cook.id}${cook.name ? `, “${cook.name}”` : ""}, from ${moment(range.from)} `
      + `to ${cook.ended_ts == null ? "now" : moment(range.to)}`;
  } else if (range.basis === "hours") {
    head = `The last ${range.hours} h, to ${moment(range.to)}`;
  } else {
    head = `No cook on record: the last ${range.hours} h, to ${moment(range.to)}`;
  }
  const lines = [head];
  const history = report.history;
  if (!history.samples) return lines;
  const stretch = history.stretch;
  lines.push(`${history.samples} samples`);
  if (history.skipped.length) {
    const leftOut = history.skipped.reduce((sum, piece) => sum + piece.samples, 0);
    lines.push(`Scored on the longest unbroken stretch, ${moment(stretch.from)} to ${moment(stretch.to)} `
      + `(${stretch.samples} samples); ${history.skipped.length} shorter piece${history.skipped.length === 1 ? "" : "s"} `
      + `skipped (${leftOut} samples)`);
  }
  return lines;
}

function scorecardHtml(summary) {
  if (!summary) {
    return [note("Nothing to score in this range: no stretch of samples with a setpoint and a fan reading.")];
  }
  const out = [];
  const satAt = summary.setpoint_f + (summary.steady_error_f ?? 0);
  out.push(kv("Setpoint", `${num(summary.setpoint_f, 0)} °F`));
  const pitOut = summary.pit_readings === 0;           // the probe read OPEN for the whole stretch
  out.push(kv("Reached it", pitOut ? "pit probe out" : summary.to_setpoint_ts === undefined
    ? "never"
    : `${clock(summary.to_setpoint_ts)}, ${fmtDuration(summary.to_setpoint_s)} after the start`));
  out.push(kv("Overshoot", pitOut ? "pit probe out" : `${num(summary.overshoot_f)} °F`));
  out.push(kv("Held steady for", summary.held_s ? fmtDuration(summary.held_s) : "–"));
  if (pitOut) {
    out.push(note("The pit probe read OPEN for the whole stretch: nothing to score the control on."));
  } else if (summary.off_target) {
    out.push(kv("Where it sat", `${num(satAt, 0)} °F, ${num(Math.abs(summary.steady_error_f), 0)} °F `
      + `${summary.steady_error_f < 0 ? "under" : "over"} the setpoint`));
    out.push(note("The pit never held near its setpoint: the cooker was idle, or did not get up to temperature."));
  } else if (summary.settled_at_ts === undefined) {
    out.push(kv("Settled at", "never"));               // nothing held steady for half an hour
    out.push(kv("Steady error", "–"));
  } else {
    out.push(kv("Settled at", `${clock(summary.settled_at_ts)}, ${fmtDuration(summary.settled_at_s)} after the start`));
    out.push(kv("Steady error", `${signed(summary.steady_error_f)} °F (spread ${num(summary.steady_sd_f)})`));
    if (summary.fan_hold_pct !== null && summary.fan_hold_pct !== undefined) {
      out.push(kv("Fan while it held", `${num(summary.fan_hold_pct, 0, " %")}`));
    }
  }
  out.push(kv("Fan, mean over the stretch", `${num(summary.fan_mean_pct, 0, " %")}`));
  out.push(kv("Fan, peak", `${num(summary.fan_peak_pct, 0, " %")}`));
  out.push(kv("Cook status", statusSpans(summary.cook_status_s)));
  const timer = Object.keys(summary.timer_status_s ?? {}).filter((name) => name !== "OK");
  if (timer.length) out.push(kv("Timer status", statusSpans(summary.timer_status_s)));
  return out;
}

/** Time in each status, longest first. */
function statusSpans(times) {
  return Object.entries(times ?? {})
    .sort((a, b) => b[1] - a[1])
    .map(([name, seconds]) => `${name} ${fmtDuration(seconds)}`)
    .join(", ") || "–";
}

function foodHtml(summary) {
  if (!summary) return [note("No food probe read in this range.")];
  const out = [];
  for (const [probe, info] of Object.entries(summary.food)) {
    if (!info.plugged) continue;
    let stall = "no stall";
    if (info.stall) {
      stall = `stalled at ${num(info.stall[2], 0)} °F from ${clock(info.stall_from_ts)} `
        + `to ${clock(info.stall_to_ts)} (${fmtDuration(info.stall_to_ts - info.stall_from_ts)})`;
    }
    out.push(kv(FOOD_LABELS[probe], info.done_ts === undefined
      ? `not done; last ${num(info.final_f)} °F`
      : `done at ${clock(info.done_ts)}, ${fmtDuration(info.done_at_s)} after the start`));
    out.push(note(`${stall}.`));
  }
  return out.length ? out : [note("No food probe read in this range.")];
}

function lidHtml(summary) {
  const how = "Inferred from the pit reading: it fell faster than 5 °F a minute, from a cooking "
    + "temperature, and came back.";
  if (!summary) return [];
  if (!summary.lid.length) return [note("No lid event in the recording."), note(how)];
  const out = summary.lid.map((event) => kv(
    `Lid off at ${clock(event.at_ts)}`,
    `down to ${num(event.low_f, 0)} °F`
      + (event.back_ts === null
        ? ", never back to where it had been"
        : `, back within 5 °F after ${fmtDuration(event.back_ts - event.at_ts)}`),
  ));
  out.push(note(how));
  return out;
}

function verdictHtml(verdict) {
  if (!verdict) return [note("No samples to judge the law on.")];
  if (verdict.state === "refused") {
    return [note(`No verdict: ${verdict.message}`)];
  }
  return [
    `<div class="report-law">${escapeHTML(verdict.law)}</div>`,
    `<div class="report-line">${escapeHTML(verdict.line)}</div>`,
    note(`RMS error ${num(verdict.rmse_pp, 2)} points over ${verdict.samples} samples; a law is selected only if it `
      + `beats every simpler law by over 10 % and 0.5 points.`),
    `<pre class="report-pre">${escapeHTML(verdict.text)}</pre>`,
  ];
}

let plot = null;

/** The same chart the dashboard draws, over the report's span. */
async function drawChart(range) {
  const hours = (range.to - range.from) / HOUR;
  const step = hours <= 24 ? 60 : hours <= 72 ? 300 : 900;
  const body = await fetchJSON(`/api/history?from=${range.from}&to=${range.to}&step=${step}`);
  if (!body.points.length) {
    $("chart-empty").textContent = "No samples in this range.";
    $("chart-empty").classList.remove("hidden");
    return;
  }
  plot = new uPlot(chartOptions($("chart"), () => step), chartData(body.points), $("chart"));
  window.addEventListener("resize", () => {
    plot.setSize({ width: Math.min($("chart").clientWidth || 900, 1040), height: 360 });
  });
}

/** The cooks on record, to pick one; the report's own query decides which. */
async function fillCookPicker() {
  const pick = $("cook-pick");
  try {
    const cooks = await fetchJSON("/api/cooks");
    if (!cooks.length) {
      pick.remove();
      return;
    }
    pick.innerHTML = `<option value="">Latest cook</option>` + cooks.map((cook) =>
      `<option value="${escapeHTML(String(cook.id))}">#${escapeHTML(String(cook.id))} `
      + `${escapeHTML(cook.name || "")}${cook.ended_ts === null ? " (on)" : ""}</option>`).join("");
    pick.value = params.get("cook") ?? "";
    pick.addEventListener("change", () => {
      window.location.search = pick.value ? `?cook=${encodeURIComponent(pick.value)}` : "";
    });
  } catch (err) {
    pick.remove();  /* transient: the links still work */
  }
}

async function load() {
  const range = $("report-range");
  try {
    const report = await fetchJSON(`/api/report?${query}`);
    range.innerHTML = rangeLines(report).map(escapeHTML).join("<br>");
    $("scorecard").innerHTML = scorecardHtml(report.scorecard).join("");
    $("food").innerHTML = foodHtml(report.scorecard).join("");
    $("lid").innerHTML = lidHtml(report.scorecard).join("");
    $("verdict").innerHTML = verdictHtml(report.verdict).join("");
    await drawChart(report.range);
  } catch (err) {
    range.innerHTML = "";
    range.classList.add("report-error");
    range.textContent = `No report: ${err.message}`;
  }
}

fillCookPicker();
load();
