# Firmware control: what the vendor's firmware shows

Status: first pass (2026-11-27). **The control law itself has not been
located in the firmware.** This is what could be verified by reading it, and
what that leaves open.

Labels used below: **verified** means the instructions were read and the
meaning does not depend on an interpretation. **Inferred** marks a reading
that does. Addresses are byte addresses in the v3.1 image.

## Summary

- The controller is a Microchip PIC18F67K22 (8-bit), programmed in C with the
  CCS compiler. The image is plain code and text: not packed, not encrypted.
- **Probes**: one A/D input, four probes multiplexed onto it. Every reading is
  smoothed with a weight of 1/8 before anything uses it.
- **Outputs**: two pulse-width-modulated outputs (RC2 and E7), driven from a
  demand in percent. The E7 one is what the front panel shows as OUTPUT.
- **A second device**: the PIC talks to it over a bit-banged four-wire link
  with a register protocol. Every setting lives in its registers.
- **`main`'s control block** computes a ramp target, probe-fault fallbacks,
  deviation and food-done statuses, and a rate-of-rise counter.
- **Open**: nothing in the image has been found that computes the fan demand
  during a cook. Either it is reached by a path not yet traced, or the second
  device computes it. See "Open questions".

## What was analysed

The vendor's official updater package for firmware v2.3/3.1, taken from an
archived copy of the vendor's own download with the owner's approval, and
disassembled offline with gputils. This device reports firmware 3.1, so the
v3.1 build is the one read. The vendor's hex file for it omits one `FF` byte
at 30 places inside the application, so the first disassembly was missing 30
instructions; they were restored from the full image before anything below
was read. No instruction among them was a call or a jump.

## Timing

**Verified.** The clock is an external RC oscillator. All timing comes from
Timer 0, 16 bits, instruction clock, prescaler 1:256, so one tick is
1024 / Fosc, counted into a 32-bit value by polling the overflow flag. The
scheduler in `main` advances four scheduled times by 62,500 ticks each
(0xEB70) and one more by 936,476 ticks (0xEE26); other waits are 121,707,
870,555 and 1,333,719 ticks.

Because the clock is an RC oscillator, the image alone does not say how long
a tick is. Those periods would be 1 s and 15 s only at 64 MHz. The firmware
times one A/D conversion with Timer 0 (0x64CA), which may be a calibration of
its own clock (**inferred**); that has not been decoded.

## Temperature path

**Verified** unless marked.

- **One A/D input, four probes.** E3, E5, E4 and E2 select the probe
  (0x147C); each scan converts one probe and moves to the next.
- **Smoothing.** For each probe the previous value `t` and the raw reading
  `r` are combined as `t -= t >> 3; t += r >> 3` (0x8674-0x86E2). That is a
  one-pole average with weight 1/8: `t = 7/8 * t + 1/8 * r`. Its time constant
  is eight scan periods, and the scan period is not known (see Timing).
- **Validation and units.** A routine at 0x92EE turns the smoothed value into
  the working temperature and counts bad readings per probe (0x956C). Ten or
  more consecutive bad readings mark the probe faulty (0xEAE2) (**inferred**:
  which condition counts as bad is not decoded). Working temperatures are
  clamped at 0x118E (0x3890).
- **Where they live.** An array of four 16-bit words at 0xA22: pit, food 1,
  food 2, food 3. `main` and the ramp routine read them through one accessor
  (0x3684) that also answers "is this probe faulty". 0xA2A holds the previous
  pit temperature, and 0xA2C, 0xA2E, 0xA30 hold the three food setpoints.

For modelling: the temperature the device displays and controls is already
an average, not the raw probe. A simulation that compares its own readings
with a recording should smooth the same way once the scan period is known.

## Outputs

**Verified.**

- Two routines each drive one output (0x3236 for RC2, 0x32B4 for E7). A
  value of 0 holds the pin low, 100 or more holds it high, anything between
  sets a 10-bit duty: Timer 2 with period register 25 and prescaler 1:16,
  which gives 26 steps and a carrier of Fosc / (4 × 16 × 26).
- They are reached only through the apply-a-command routine (0xC1F8), which
  takes the demand in tenths of a percent and divides by 10 (0xBD40).
- The front panel's OUTPUT line reads the byte the E7 routine keeps (RAM
  0x0074, 0xABF0), so E7 is the output the user sees.

Nothing in the image shows the cycle-time pulsing the guide describes
("the fan will be at its maximum speed for a length of time ... then it will
turn off for the remainder"). Whether that is done somewhere not yet read, or
by the second device, is open.

## The register store on the far side of a serial link

**Verified** unless marked.

- **The link.** Four port-D lines, driven by software (0x15A2-0x15E0): D4
  data out, D6 clock, D5 data in, D2 raised as a select line. This is
  separate from the Wi-Fi module, which uses the hardware SPI.
- **The protocol.** Eight-byte requests: an operation, a sub-operation, a
  32-bit address, a count. Read is operation 0xE1; a word write is 0xDF; a
  byte write is 0x20 (0x1A38, 0x1D14, 0x1D3A).
- **What lives there.** Every setting the user can change:
  - 0x01C9 to 0x01CF: four 16-bit values written by the four setpoint
    commands; the loader copies three of them out as the food setpoints
    (**inferred** that all four are setpoints)
  - 0x01D2 and 0x01D3: the two output demands, in percent
  - 0x01D9 (maximum 100) and 0x01DB (maximum 50)
  - 0x01DE: 1 to 30, a count of 62,500-tick periods
  - 0x01E0: the ramp setting, 0 to 3
  - 0x01A7: a version marker, 31 for this firmware
- **What crosses the link at run time.** Settings, written when the user
  changes one. Network profiles (two of them) and their defaults. Single
  bytes of 0 or 1 into small registers, which look like flags being set and
  cleared (**inferred**). The PIC reads back only settings it wrote itself,
  the version marker, and a few registers at boot.
- **No control defaults on the PIC.** Its defaults routine (0x1DDC) writes a
  zero to register 0x0100, re-creates the network profiles and writes the
  version marker. It sets no setpoint, band or cycle time. So the default
  proportional band and cycle time are not in this image.
- At boot both outputs are set from registers 0x01D2 and 0x01D3 (0x3338).

## The control block in `main`

`main` is one loop. Each pass runs a time check that resets the chip if it
overran, the probe scan, the state-dependent code (the keypad, display and
network state machines live in it), and the control block at 0xEB28-0xEEB4,
then returns to the top with the watchdog cleared. **Verified** structure;
the order of the state-dependent parts was not traced, and item meanings are
marked.

1. **Scheduler.** Four scheduled times, each advanced by 62,500 ticks when
   due (0xEB28-0xEB7E).
2. **Ramp routine** (0x3B34, called every pass), see the next section.
3. **Target when the pit probe is faulty.** The target becomes the cook
   setpoint, or `S0/2 - X` when `S0 <= X` (0xEC26-0xEC64) (**inferred**; the
   halving is not understood).
4. **Status bits** (0xEC66-0xECD6): "pit above target", and "pit below
   `S0/2 + X` and above target". Here `S0` is the cook setpoint (0xA03A) and
   `X` the ramp routine's result.
5. **Food done** for food 1 and 2: temperature at or above the food
   setpoint (0xED6E-0xEE00).
6. **Rate of rise, every 936,476 ticks** (0xEE26-0xEEAC): the pit's change
   over the period, and a counter from 0 to 20 that counts up when the pit
   rose by more than 4 units in the period and down otherwise.

The time constants of items 1 and 6 are in ticks; see Timing.

## Ramp routine

**Verified** structure, **open** arithmetic.

- The ramp setting (0 to 3) selects a food probe; `d` is that probe's
  setpoint and `e` is `pit - d`.
- `X` is a ramped target candidate. When the pit probe is faulty, or when the
  pit is between 1 and about 44 units below the food setpoint, the
  computation is skipped and `X` equals the chosen setpoint `P` (0x3DD0).
  `P` is one of two setpoint values at 0xA33 and 0xA36, chosen by a status
  bit (0x3BB0).
- Otherwise `X` is the low 16 bits of a signed 32-bit expression in `P`, `e`
  and `d`, clamped at 0 when negative (0x3C2E-0x3DC8). It multiplies `P` by
  `|e|` twice with opposite signs, one of them scaled by 16, adds
  `(e + 1000) * ((d + 300) shifted)`, scales the result by 244, and subtracts
  `8*d - 100` when the pit is below `d`.
- The routine also tests the pit's change against +25 and -46 (0x3B72-0x3BAE)
  and sets a flag, and elsewhere writes the rate counter (0x3E0A, 0x3E90).
  How the two are connected was not traced.

The coefficients are recorded as read; what the expression computes has not
been worked out, and should be treated as unknown until it is either decoded
or measured.

## What the firmware confirms from the guide

These agree with the CyberQ Wi-Fi guide (Firmware v 2.3/3.1 and the V1.7
guide's table of `status.xml` values), and the simulator's baseline now
follows them (see `docs/SIMULATOR.md`):

- Each status field has its own codes. The cook probe: OK, HIGH, LOW, ERROR.
  A food probe: OK, DONE, ERROR. The timer: OK, HOLD, ALARM, SHUTDOWN. A
  running timer is not a status.
- The ramp uses the food *setpoint*, as the guide says.
- Cook hold comes from the cook timer with `TIMEOUT_ACTION`. The firmware
  has `TIMEOUT HOLD` as a timer status; it has no path that starts cook hold
  because food is done.
- The firmware's own status messages cover the same ground: `COOK TEMP HIGH`
  and `COOK TEMP LOW`, `FOOD n DONE`, `TIMEOUT HOLD`, `TIMEOUT ALARM`,
  `TIMEOUT SHDN`, and `OPEN ERROR` for each probe.

## Open questions

1. **Where is the fan demand computed?** The two output routines are called
   from only three places each: boot, the key handler (a manual adjustment),
   and the apply-a-command routine. That routine is reached only from the
   front-panel editors, on the Enter key, and from the web-form handler.
   Checks made and found negative:
   - the interrupt handlers: one is empty, the other serves only the Wi-Fi
     module;
   - indirect dispatch: six computed jumps (41 entries) and every occurrence
     of the routines' addresses in tables or pointers;
   - run-time writes of the two demand registers over the link: none outside
     the apply-a-command routine and the key handler.
2. **Is the second device storage or the controller?** It behaves like
   storage: the PIC never reads a temperature or an output state from it, and
   reads its own probes and drives its own outputs. But the PIC holds no
   control defaults and sends a reset command to the device, which a plain
   memory would not need. The device's identity is not known.
3. **Cycle-time pulsing**: not found on the PIC.
4. **Units and clock.** The temperature unit (the clamp is 0x118E), the tick
   length, and the scan period are not known from the image.
5. **Ramp arithmetic** (above).
6. **Hardware revision.** The v3.1 build is the one with fan-short detection
   (`FAN IS SHORTED`, `FAN CURRENT PROTECTION`). Whether it is gated on the
   hardware revision at run time has not been checked.
7. **The hardware and version string** the Wi-Fi page shows. There is no
   format string for it in the firmware; where it comes from is not traced.

## What this means for the simulator

- The baseline's documented behaviour stands as implemented, with the
  corrections in `docs/SIMULATOR.md`.
- Until question 1 or 2 is answered, `sim/controllers/firmware.py` has
  nothing to implement: there is no verified law to reimplement.
- A recorded cook is the other way to the law. `sim fit` and `sim identify`
  can settle P against PI and the band from the fan demand the device reports
  against the pit error, provided the recording has the pit probe plugged in
  and the sampling is at least as fast as the device updates (its own page
  polls once a second).
- The device's reported temperature is already smoothed with weight 1/8; a
  fit should expect that lag once the scan period is known.
