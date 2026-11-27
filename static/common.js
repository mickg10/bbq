/* Small helpers shared by the dashboard (app.js) and the cook report
 * (report.js). Loads first.
 */

"use strict";

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

function escapeHTML(text) {
  const div = document.createElement("div");
  div.textContent = String(text);
  return div.innerHTML;
}

/** A unix time as a local 24-hour clock time: "14:35". */
function fmtClock(ts) {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** Seconds as "1h30m", "45m", "90s": the scorecard's durations. */
function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "–";
  if (seconds <= 0) return "0s";
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const secs = Math.round((seconds % 60) * 10) / 10;
  if (hours) return `${hours}h` + (minutes ? `${String(minutes).padStart(2, "0")}m` : "");
  if (minutes) return `${minutes}m` + (secs ? `${String(Math.round(secs)).padStart(2, "0")}s` : "");
  return `${secs}s`;
}
