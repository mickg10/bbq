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
    proportional.py  "as documented" CyberQ behaviour (the baseline, below)
    firmware.py   placeholder for the reverse-engineered algorithm
  device.py       CyberQ state machine: setpoints, timer, statuses, cook-hold, ramp, alarms
  clock.py        simulated clock, real-time or accelerated (--speed N)
  server.py       HTTP facade: byte-compatible with the real box (all.xml, status.xml,
                  config.xml, the htm pages, every form POST), driven by device.py
  scenario.py     scenario loader (YAML): ambient, fuel load, wind, lid-open events,
                  setpoint/timer changes, probe plug/unplug at given times
  fit.py          system identification: fit plant parameters from the dashboard's SQLite history
  cli.py          python -m sim run|serve|fit|compare
scenarios/        brisket-12h.yaml, ribs-6h.yaml, lid-open.yaml, fuel-out.yaml, cold-windy.yaml
tests/sim/        deterministic (seeded) unit tests and scenario regression tests
```

## Plant model (start simple, fit later)
- **Pit**: one thermal mass `C_pit`, with `C_pit dT/dt = Q_fuel - UA_pit (T - T_amb) - Q_food - Q_lid`.
- **Fuel/airflow**: `Q_fuel = q_max * fuel_frac * (draft_min + (1 - draft_min) * fan_duty)`, with a first-order fire-response lag `tau_fire` (coals take minutes to respond), and fuel burning down in proportion to the heat released.
- **Lid open**: `UA` is multiplied for the event's duration, giving a fast pit drop and slow recovery.
- **Food**: a lumped mass per probe, heated by `h (T_pit - T_food)`, with an evaporative-cooling term that produces the 150–170 °F stall.
- **Sensors**: first-order probe lag, Gaussian noise, quantized to 0.1 °F. Unplugged reads `OPEN`.
- **Fit**: estimate every parameter from recorded history in `fit.py`. Ship defaults that look like a kamado or WSM.

## Baseline controller ("as documented")
Implements what the manual and the web UI imply. The firmware phase will confirm or correct it:
- **Output**: `fan% = clamp((set - pit) / PROPBAND * 100, 0, 100)`. The fan runs `fan% * CYCTIME` seconds of each `CYCTIME` window.
- **Open-lid detect**: when the pit drops fast, hold the fan off for a recovery window.
- **Ramp**: as the chosen food probe approaches its setpoint, taper the pit setpoint toward food set.
- **Timer and food done**: `TIMEOUT_ACTION` (none, hold, alarm) and `COOKHOLD` take over the pit setpoint.
- **Statuses**: `ALARMDEV` gives HIGH or LOW, food reaching set gives DONE, OPEN gives ERROR, plus HOLD and ALARM. These map to codes 0..7 exactly as the device reports them.

## Contracts
- `python -m sim serve --scenario scenarios/brisket-12h.yaml --speed 60 --port 8099` serves a live, evolving CyberQ that the dashboard can point at (`CYBERQ_URL=http://127.0.0.1:8099`). Writes from the dashboard change the simulated device.
- `python -m sim run ... --out run.csv` runs headless, as fast as possible, and writes a time series plus a summary: overshoot, settle time, fan duty and fuel used.
- `python -m sim compare --db bbq.db --from ... --to ...` replays a recorded real cook's setpoints and ambient through the model and reports RMSE against the measured pit and food temperatures.
- The server reuses `dev/fake_cyberq.py`'s page rendering and form handling, so there is one source of truth for the wire format. The existing 73 tests must keep passing.

## Firmware phase (after the simulator lands)
- Download the **latest official CyberQ WiFi firmware from BBQ Guru's own site or update tool only**. Record the URL, version and sha256.
- Analyse it **offline only**. Never flash or update the device, and never send anything to it beyond what the dashboard already does.
- Firmware images, disassembly and decompiled output stay in a gitignored `firmware/` directory and are never pushed to the public repo, because they're vendor code. Only our own write-up of the algorithm and `controllers/firmware.py`, a clean reimplementation, may be published, subject to Mick's OK.
- Deliverable: `docs/FIRMWARE-CONTROL.md`. It describes the control law (P/PI/PID terms, any integral windup handling, cycle-time PWM, lid-detect thresholds, ramp formula, units and fixed-point scaling), with addresses as evidence. Plus `controllers/firmware.py`, and a comparison of baseline vs firmware controller across all scenarios.
