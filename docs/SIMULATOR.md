# CyberQ simulator platform

A full model of the cooker, its probes, the CyberQ controller and the device's web interface. It's for:
1. developing and testing the dashboard against realistic, time-varying behaviour (not static fixtures);
2. trying out control settings (prop band, cycle time, ramp, open-lid detect) offline;
3. serving as the test bench for the PID/control algorithm recovered from the firmware (see "Firmware phase" below). That algorithm drops in as a controller plugin, and the simulator is validated against our recorded real cooks.

## Layout
```
sim/
  plant.py        cooker thermal model (pit + fuel + airflow + ambient + lid)
  food.py         food probe model (lumped mass, evaporative stall)
  probes.py       sensor model: tenths-°F quantization, noise, OPEN/unplugged, lag
  controllers/
    base.py       Controller protocol: step(t, pit_f, food_f[], cfg) -> fan_pct, status bits
    proportional.py  "as documented" CyberQ behaviour (the baseline, below), its bias
                    preset and a pi variant, for comparing laws
    firmware.py   placeholder for the reverse-engineered algorithm
  device.py       CyberQ state machine: setpoints, timer, statuses, cook-hold, ramp, alarms
  clock.py        simulated clock, real-time or accelerated (--speed N)
  engine.py       the Simulator: wires the parts above in a fixed step order, so a
                  run replays bit for bit from its seed
  server.py       HTTP facade: byte-compatible with the real box (all.xml, status.xml,
                  config.xml, the htm pages, every form POST), driven by device.py
  scenario.py     scenario loader (YAML): ambient, fuel load, wind, lid-open events,
                  setpoint/timer changes, probe plug/unplug at given times
  run.py          sim run: plays a scenario and writes its time series
  summary.py      what a run adds up to: its summary, and the scorecard table
  history.py      recorded history: reading it, replaying it through the model, scoring it
  fit.py          system identification: fit plant parameters from the dashboard's SQLite history
  identify.py     which control law a recording shows: P, P + offset, PI, PI + a rate term,
                  fitted to the recorded fan output
  cli.py          python -m sim run|serve|fit|compare|identify
scenarios/        brisket-12h.yaml, ribs-6h.yaml, lid-open.yaml, fuel-out.yaml, cold-windy.yaml
tests/sim/        deterministic (seeded) unit tests and scenario regression tests
```

## Plant model (start simple, fit later)
- **Pit**: one thermal mass `C_pit`, with `C_pit dT/dt = Q_fuel - UA_pit (T - T_amb) - Q_food - Q_lid`.
- **Fuel/airflow**: `Q_fuel = q_max * fuel_frac * (draft_min + (1 - draft_min) * fan_duty)`, with a first-order fire-response lag `tau_fire` (coals take minutes to respond), and fuel burning down in proportion to the heat released. Two things the formula glosses over:
  - a bed of coals holds its power until nearly gone, so `fuel_frac` in it is the power the load can still give: full until the last `fuel_tail` of it, then falling off. As a straight fraction of fuel left it made every long cook sag;
  - the heat a step releases is the coals' **time-average over the step**, not their state at its end. With the end value a 10 s step ran 1.3 °F hot during a heat-up; with the average, any step size gives the same answer.
- **Lid open**: `UA` is multiplied for the event's duration, giving a fast pit drop and slow recovery.
- **Food**: a lumped mass per probe, heated by `h (T_pit - T_food)`, with an evaporative-cooling term that produces the 150–170 °F stall.
- **Sensors**: first-order probe lag, Gaussian noise, quantized to 0.1 °F. Unplugged reads `OPEN`. The default noise, 0.12 °F, is measured on the real device; the lag is an estimate.
- **Fit**: estimate what a recording can show (next section) from recorded history in `fit.py`. Ship defaults that look like a kamado or WSM.

## Fitting a recording
`sim fit` and `sim compare` work on the dashboard's `samples` table, opened read-only. Three things decide whether the result means anything:

- **Only ratios can be fitted.** Divide the pit's heat balance through by `UA_pit` and just two things are left that temperatures can see: the time constant `C_pit / UA_pit` and the gain `q_max / UA_pit` (plus `tau_fire`, roughly). Scale `C_pit`, `UA_pit` and `q_max` together and the temperature does not change at all, so no recording can choose that scale. `fit` therefore **holds `UA_pit` fixed** and fits `C_pit`, `q_max`, `tau_fire` and `draft_min`. Fuel use is only right in absolute BTU if `UA_pit` is. `draft_min` only shows when the fan stops, so it stays **weakly identified**: regularised toward its starting value, and to be read as "somewhere near", not as a measurement.
- **Name the food** (`--food food1=brisket`, optionally `:lb`). A probe with nothing named reads the pit and draws nothing from it, so meat the model was not told about comes out of the fire instead: 12 lb of cold brisket unmodelled was a 5 °F error in the pit and a 6 % bias in `q_max`.
- **Skip lids** (`--skip 4h55m:5h40m`). A recording cannot say when the lid came off. Its aftermath, about two hours while the pit finds its level again, is not the plant's doing and will dominate the fit if it is left in.

A recording with no heat-up in it has nothing to teach about the plant. `fit` says so rather than fitting anyway, and gives what such a recording can: the air temperature and the probes' noise.

## Which control law a recording shows
`sim identify` reads the control law black-box, from the same table. It fits candidates to what the device reported for its fan (`output_pct`) against its error (set − pit), and says which fits, with what numbers and what residual. With the law not found in the firmware, a recorded cook is the way to it.

- **The candidates**, simplest first: P (`100 · err / band`); P with a fixed offset; PI; PI with a term against the pit's rate of rise. An integral law has no offset of its own: it would be the integral's starting value, which is fitted too (one per unbroken stretch, since the device's is not known). Each law is run over the recorded samples, with the simulator's anti-windup, and fitted to minimise the RMS error in percentage points.
- **Selection.** A law is selected only if it cuts the RMS error of *every* simpler law by over 10 % and over 0.5 points. Output is recorded in whole percent, so less than that is noise. Comparing each law only with the one before it would let a chain of small gains add up to a selection.
- **How well the numbers are pinned down.** Each of the selected law's numbers is then forced 20 %, 50 % and a factor of 2 from its value, the others re-fitted. "Within 20 %" means a 20 % move already made the fit worse. "Not within a factor of 2" means halving or doubling left it as good as the best fit, and that number is not a finding. Where the integral is frozen or saturated for most of a recording, its starting value comes out that way while the band and `ki` stay tight.
- **Cycle time** (`--cycle 6`): the law is then evaluated once per cycle and its on-time counted in whole seconds, as on the device, and the cycle's phase is searched for. Read blind, those steps alone leave a residual of several points.
- **It refuses, saying why**, when the recording cannot tell the laws apart: the pit probe open throughout; a constant output; under 20 minutes; or the output pinned at 0 or 100 % in all but a handful of samples, where every law looks the same. `--skip` leaves windows out (a lid that came off), as for `fit`.
- **It reads the law against the cook setpoint.** A cook made with the ramp on has a lower target early on, so skip that part (`--skip`) or use a cook made without the ramp.

It is checked against data it did not make: the simulator's own controllers, in the open loop and driving the plant, on whole-percent output with probe noise, and on the shared recorded cook (`pytest -m slow`). It cannot tell a law the device runs but none of the candidates can express.

## Baseline controller ("as documented")
Implements what the vendor's guide (CyberQ Wi-Fi User Guide, Firmware v 2.3/3.1) and the web UI say, and no more. One thing the guide says, it cannot: the box runs a "full-time adaptive control algorithm [that] learns your pit". So the baseline is deliberately plain, and the firmware phase will confirm or correct it:
- **Output**: `fan% = clamp((set - pit) / PROPBAND * 100, 0, 100)`. The fan runs `fan% * CYCTIME` seconds of each `CYCTIME` window.
- **Open-lid detect**: when the pit drops fast, hold the fan off for a recovery window. The guide gives the same setting a second job: with it on at start-up, "the rate of temperature rise of your pit will be limited to preventing over-firing" (it quotes 250 °F in a minimum of about 20–30 minutes). How is not said. Here a pit still below its target and rising faster than 8 °F/min is not fed until it slows; the default kamado climbs at most 4.8 °F/min and never meets the limit.
- **Ramp**: as the chosen food probe comes within 30 °F of its setpoint, taper the pit setpoint down to 5 °F above the food setpoint. The guide says ramp will "hold the pit temperature slightly above your food set point"; how far above is not said, so it is a parameter (`ramp_above_f`).
- **Timer**: `COOK_TIMER` with `TIMEOUT_ACTION` (no action, hold, alarm, shutdown). Cook hold comes from the timer and nothing else: HOLD shows as the *timer* status and moves the target to `COOKHOLD`; ALARM only raises the timer status; SHUTDOWN stops the fan.
- **Statuses**: each field uses its own subset of the codes 0..7, as the V1.7 guide's table of `status.xml` values gives them. The cook probe is OK, or HIGH/LOW once it is `ALARMDEV` away from the effective target, or ERROR when open; a food probe is OK, DONE at its setpoint, or ERROR when open; the timer is OK, or HOLD/ALARM/SHUTDOWN after a timeout with that action. A running timer is not a status.
- **Variants, for comparison only**: `bias` is the same law with a fixed share of the fan up front; `pi` adds integral action, clamped to 0–100 % and not accumulated while the output is already saturated. Neither is documented. They bracket what the firmware might do, and `sim run --controller proportional,bias,pi` scores all three on every scenario.

## The scorecard
`python -m sim run --controller proportional,bias,pi` prints one line per scenario and law. Every column is measured on the pit *reading* (what the device would have shown), against the setpoint the food was cooked at — the one in force for the longest time outside cook hold, after ramp:

| Column | Meaning |
|---|---|
| `reached set` | when the reading first reached the setpoint; `–` if it never did |
| `overshoot °F` | how far past the setpoint the reading went |
| `settled at` | when the pit stopped moving: the start of the longest stretch, at least 30 min, over which the reading stayed inside a 10 °F span. This is *a plateau*, not necessarily at the setpoint: for the documented law it sits a fraction of a band below it |
| `held for` | how long that stretch lasted |
| `steady err °F` | mean reading minus setpoint over that stretch |
| `fan mean %` | average fan demand over the whole run |
| `fan held %` | average fan demand over the settled stretch |
| `fuel lb` | fuel burned, as pounds of lump |
| `food1 done` | when the food1 probe first reported DONE; `–` if it never did, `no probe` if none was plugged in |
| `notes` | lids (lowest reading, whether the detector held the fan, time back within 5 °F), cook hold, alarm, shutdown |

"Settled" can come *before* "reached set" (cold-windy under `pi`: settled at 1h44m, reached set at 3h27m). That is not a contradiction: the pit levelled off just under the setpoint and only touched it much later. A single run prints the same facts as a few sentences, and `--json` writes them as data.

## Contracts
- `python -m sim serve --scenario scenarios/brisket-12h.yaml --speed 60 --port 8099` serves a live, evolving CyberQ that the dashboard can point at (`CYBERQ_URL=http://127.0.0.1:8099`). Writes from the dashboard change the simulated device.
- `python -m sim run ... --out run.csv` runs headless, as fast as possible, and writes a time series plus a summary: overshoot, settle time, fan duty and fuel used. With more than one run it prints the scorecard instead.
- `python -m sim compare --db bbq.db --from ... --to ...` replays a recorded real cook's setpoints and ambient through the model and reports RMSE against the measured pit and food temperatures. `--open-loop` drives the plant with the recorded fan output instead, with no controller in the way.
- `python -m sim fit --db bbq.db --out cooker.yaml` estimates the cooker's parameters from such a recording (see "Fitting a recording").
- `python -m sim identify --db bbq.db` reports which control law a recorded cook shows, its numbers with how well the recording pins each of them down, and the residual (see "Which control law a recording shows"). It refuses, with the reason, when the recording has no closed-loop data to read.
- The server reuses `dev/fake_cyberq.py`'s page rendering and form handling, so there is one source of truth for the wire format. The existing 73 tests must keep passing. `pytest -q` runs the fast suite; the tests that serve the simulator on a real port are marked `slow` (`pytest -m slow`).

## Firmware phase (after the simulator lands)
- Download the **latest official CyberQ WiFi firmware from BBQ Guru's own site or update tool only**. Record the URL, version and sha256.
- Analyse it **offline only**. Never flash or update the device, and never send anything to it beyond what the dashboard already does.
- Firmware images, disassembly and decompiled output stay in a gitignored `firmware/` directory and are never pushed to the public repo, because they're vendor code. Only our own write-up of the algorithm and `controllers/firmware.py`, a clean reimplementation, may be published, subject to Mick's OK.
- Deliverable: `docs/FIRMWARE-CONTROL.md`. It describes the control law (P/PI/PID terms, any integral windup handling, cycle-time PWM, lid-detect thresholds, ramp formula, units and fixed-point scaling), with addresses as evidence. Plus `controllers/firmware.py`, and a comparison of baseline vs firmware controller across all scenarios.
- Status of that phase is kept in [`FIRMWARE-CONTROL.md`](FIRMWARE-CONTROL.md): what the firmware has been shown to do, and what is still open.
- The law was not located in the image, so the route to it is recorded cooks: `sim identify` (see "Which control law a recording shows"). `controllers/firmware.py` stays a placeholder until a law is verified that way.
