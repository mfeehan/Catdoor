# Wiring

Pin assignments and wiring notes for the cat door controller. All Pi pin numbers below use **BCM numbering** (the GPIO numbers, not the physical pin numbers). The corresponding physical pin is given in parentheses for reference.

## Bill of materials

| Qty | Item | Notes |
|---|---|---|
| 1 | Raspberry Pi 5 (4GB or 8GB) | Pi 4 also works. **Do not use a Pi 3B+** — UART/Bluetooth conflict prevents simultaneous reliable use of the FDX-B reader and BLE. |
| 1 | microSD card, 32GB+ | Class 10 / A1 or better |
| 1 | Pi 5 power supply, 27W USB-C | Pi 5 will throttle on lower-wattage supplies |
| 1 | Run Chicken automatic door | Premium model with BLE was used |
| 1 | FDX-B RFID reader, 134.2 kHz, UART output | Verify TX is **3.3V TTL**, not 5V. 5V will damage the Pi GPIO. |
| 1 | VL53L1X ToF breakout (Adafruit or equivalent) | I2C |
| 3 | Latching pushbutton or rocker switches | Yellow, red, green |
| 3 | LEDs | Yellow, red, green; 3mm or 5mm |
| 3 | Resistors, 330Ω | Current-limiting for LEDs |
| — | Hookup wire, dupont jumpers, project enclosure, cable glands | |

## Pin map (BCM)

| Function | BCM pin | Physical pin | Direction | Notes |
|---|---:|---:|---|---|
| RFID reader RX (data **from** reader **to** Pi) | GPIO 15 (UART RX) | 10 | Input | `/dev/ttyAMA0` |
| RFID reader TX (Pi to reader) | GPIO 14 (UART TX) | 8 | Output | Usually unused — most FDX-B readers transmit only |
| ToF SDA | GPIO 2 (I2C1 SDA) | 3 | Bidirectional | I2C bus, 3.3V |
| ToF SCL | GPIO 3 (I2C1 SCL) | 5 | Bidirectional | I2C bus, 3.3V |
| Yellow button (hold open) | GPIO 22 | 15 | Input | Pull-down enabled in software |
| Red button (hold closed) | GPIO 23 | 16 | Input | Pull-down enabled in software |
| Green button (force full mode) | GPIO 12 | 32 | Input | Pull-down enabled in software |
| Yellow LED | GPIO 17 | 11 | Output | Through 330Ω resistor |
| Red LED | GPIO 25 | 22 | Output | Through 330Ω resistor |
| Green LED | GPIO 8 | 24 | Output | Through 330Ω resistor |
| Reserved (legacy motion) | GPIO 27 | 13 | Input | Pulled down; not used by current code |
| 3.3V supply | — | 1, 17 | — | For ToF sensor and pull-up rails if needed |
| 5V supply | — | 2, 4 | — | If your RFID reader requires 5V power (most do) |
| Ground | — | 6, 9, 14, 20, 25, 30, 34, 39 | — | Use any |

A full Pi 5 GPIO header reference is at <https://pinout.xyz>.

## RFID reader

Power per the reader's spec sheet (typically 5V or 9–12V). Data line goes to **Pi GPIO 15 (physical pin 10), UART RX**. Connect the reader's signal ground to a Pi ground pin.

```
RFID reader              Raspberry Pi 5
─────────────              ──────────────
VCC  ──────────────────►  5V (pin 2)        [if reader is 5V]
                          or external supply [if reader is 9–12V]
GND  ──────────────────►  GND (pin 6)
TXD  ──────────────────►  GPIO 15 / RX (pin 10)
                          ▲ MUST BE 3.3V TTL
```

**Critical:** Many cheap FDX-B readers ship with 5V TTL on the data line. Connecting that directly to the Pi will damage the GPIO. Verify with a multimeter, or use a level shifter (e.g., BSS138-based bidirectional level shifter board) between the reader's TXD and the Pi's RX.

If your reader has both RX and TX (some do), wire RFID TX → Pi RX and RFID RX → Pi TX. Most FDX-B-only readers transmit unsolicited and don't need the Pi to talk back.

### Enabling the UART on a Pi 5

Pi 5 by default exposes the UART on `/dev/ttyAMA0`. Run `sudo raspi-config` → Interface Options → Serial Port:
- Login shell over serial: **No**
- Serial port hardware enabled: **Yes**

Reboot. Verify `/dev/ttyAMA0` exists with `ls -l /dev/ttyAMA0`.

### Antenna placement and waterproofing

The FDX-B antenna is an LC tank tuned to 134.2 kHz. **Water across the coil detunes it** and stops reads. If the antenna lives outside, pot it in clear silicone (RTV) or a thin layer of two-part epoxy. Avoid metal-filled or carbon-black-filled compounds — they kill range.

Mount the antenna so a cat's collar tag or implanted chip passes within ~3 cm of the coil during normal door approach. Read range on these readers is typically 4–6 cm for FDX-B implants; collar tags can be larger and read at slightly longer range.

## VL53L1X time-of-flight sensor

Standard I2C wiring:

```
VL53L1X breakout         Raspberry Pi 5
─────────────              ──────────────
VIN  ──────────────────►  3.3V (pin 1)
GND  ──────────────────►  GND (pin 9)
SDA  ──────────────────►  GPIO 2 / SDA (pin 3)
SCL  ──────────────────►  GPIO 3 / SCL (pin 5)
```

The Adafruit breakout has on-board pull-ups. If you wire other I2C devices on the same bus, you may want to disable the breakout's pull-ups (cut the labeled jumper) to avoid stacking them.

### Enabling I2C

`sudo raspi-config` → Interface Options → I2C → Enable. Reboot. Verify the sensor is visible:

```
sudo apt install i2c-tools
sudo i2cdetect -y 1
```

You should see a device at address `0x29` (the VL53L1X default).

### Mounting

Mount **inside**, aimed at the approach path a cat would take to the door. The sensor's cone is roughly 27° at the long-range setting used in code. The 50 mm trigger threshold means the cat needs to come within ~2 inches of the sensor — adjust `tof_threshold_mm` in `config.yaml` to suit your geometry.

## Buttons (latching switches)

All three buttons use the same wiring pattern: one terminal to the Pi GPIO input, the other terminal to 3.3V. The internal pull-down resistor is enabled in software, so a pressed (closed) switch reads `1` and an open switch reads `0`.

```
3.3V (pin 1) ──┬──[Yellow switch]──── GPIO 22 (pin 15)
               ├──[Red switch]─────── GPIO 23 (pin 16)
               └──[Green switch]───── GPIO 12 (pin 32)
```

Latching switches mean the switch stays in whichever position you put it. Good for "set and forget" overrides. If you want momentary buttons instead (e.g., toggle modes by tapping), the code as written won't handle that — you'd need to add edge detection and software latching.

### Switch precedence in software

- Red wins if both red and yellow are on (defensive — closed is the safer state)
- Yellow wins if only yellow is on
- Green only takes effect when both red and yellow are off; it forces "full" programmed mode regardless of the schedule
- All three off = follow schedule (auto mode)

## Status LEDs

```
GPIO 17 (pin 11) ──[330Ω]──[Yellow LED anode]──[cathode]── GND (pin 14)
GPIO 25 (pin 22) ──[330Ω]──[Red LED anode]─────[cathode]── GND (pin 20)
GPIO 8  (pin 24) ──[330Ω]──[Green LED anode]───[cathode]── GND (pin 25)
```

330Ω is conservative for typical 20mA LEDs at 3.3V. If your LEDs are dim, drop to 220Ω. If they're too bright (likely for indicator LEDs), bump to 470Ω or 1kΩ.

The LEDs mirror the override state directly: yellow lit = open override, red lit = closed override, green lit = auto mode (no override). Green will blink when the green button is held in full-mode override while otherwise in auto.

## Run Chicken door

The door itself is wired internally and powered by its own batteries or external supply. The Pi communicates with it **only over BLE** — there are no wired connections between the Pi and the door. Just make sure the Pi is within Bluetooth range (a few meters with line-of-sight, less through walls).

Discover the door's MAC address with:

```
sudo bluetoothctl
[bluetooth]# scan on
# wait, look for a device named like "RC-..." or similar
[bluetooth]# scan off
[bluetooth]# devices
```

Copy the MAC into `config.yaml` as `door_mac`.

## Enclosure

A few practical notes from the build:

- The Pi, RFID reader board, switches, and LEDs all live in an indoor enclosure. Only the RFID antenna and ToF sensor head extend outward (antenna outside the door, ToF sensor inside).
- Use cable glands where wires exit the enclosure. Without a drip loop and proper sealing, water tracks down cables into the enclosure on rainy days or sprinkler hits.
- Heat-shrink any wire splices. Solder joints inside an enclosure that sees humidity will corrode within a year if left bare.
- A standoff or DIN rail makes the Pi removable for debugging. You will want this the first time something goes wrong at 2 AM.

## Power

The Pi 5 needs a real 27W USB-C supply. Underpowered supplies cause silent throttling — the Pi keeps running but BLE writes start failing intermittently and you'll spend hours debugging the wrong thing. The official Raspberry Pi 27W supply is the safe choice.

If the RFID reader needs 9–12V, run a separate wall wart for it. Sharing a single 5V supply between the Pi and a noisy RFID reader is asking for I/O glitches.
