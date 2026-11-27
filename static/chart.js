/* The history chart, drawn with uPlot (vendored). Shared by the dashboard
 * (app.js) and the cook report (report.js): the series drawn, the hover tip,
 * and what uPlot is given. common.js loads first.
 */

"use strict";

/* The history endpoint's columns, as drawn. */
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


/** Hover popup: time plus each series' value, in that series' colour.
 * Series with no value at the cursor are left out. */
function tooltipPlugin(stepSeconds) {
  let tip;
  return {
    hooks: {
      init: (u) => {
        tip = document.createElement("div");
        tip.className = "chart-tip hidden";
        u.over.appendChild(tip);
        u.over.addEventListener("mouseleave", () => tip.classList.add("hidden"));
      },
      setCursor: (u) => {
        const idx = u.cursor.idx;
        if (idx === null || idx === undefined || u.cursor.left < 0) {
          tip.classList.add("hidden");
          return;
        }
        // Over a gap (device off), the nearest point can be hours away: show
        // nothing rather than a reading from another time.
        const step = stepSeconds();
        if (Math.abs(u.posToVal(u.cursor.left, "x") - u.data[0][idx]) > Math.max(2 * step, 10)) {
          tip.classList.add("hidden");
          return;
        }
        const rows = [];
        SERIES.forEach((s, i) => {
          const v = u.data[i + 1][idx];
          if (v === null || v === undefined || !u.series[i + 1].show) return;
          rows.push(
            `<div class="tip-row"><i style="background:${s.color}"></i>` +
            `<span>${s.label}</span><b style="color:${s.color}">${v.toFixed(1)} °F</b></div>`
          );
        });
        if (!rows.length) { tip.classList.add("hidden"); return; }
        const when = new Date(u.data[0][idx] * 1000).toLocaleString([], {
          month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit",
        });
        tip.innerHTML = `<div class="tip-time">${when}</div>${rows.join("")}`;
        tip.classList.remove("hidden");
        const { left, top } = u.cursor;
        const w = tip.offsetWidth, h = tip.offsetHeight;
        const ow = u.over.clientWidth, oh = u.over.clientHeight;
        tip.style.left = `${left + 14 + w > ow ? left - w - 14 : left + 14}px`;
        tip.style.top = `${Math.max(0, Math.min(top - h / 2, oh - h))}px`;
      },
    },
  };
}

function chartOptions(container, stepSeconds) {
  return {
    plugins: [tooltipPlugin(stepSeconds)],
    width: Math.min(container.clientWidth || 900, 1040),
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

/** What uPlot is given for a list of history points. */
function chartData(points) {
  return [
    points.map((p) => p.ts),
    ...SERIES.map((s) => points.map((p) => p[s.key] ?? null)),
  ];
}

function emptyChartData() {
  return [new Array(0), ...SERIES.map(() => new Array(0))];
}
