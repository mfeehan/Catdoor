# Cat Door Controller

RFID-controlled cat door built on a [Run Chicken automatic coop door](https://run-chicken.com/), a Raspberry Pi, an FDX-B microchip reader, and a VL53L1X time-of-flight sensor.

The Run Chicken door provides the mechanical actuator and BLE control surface. This project adds multi-factor access logic — chip-based whitelisting from outside, proximity-based egress from inside, and time-of-day scheduling — to convert it into a controlled-access cat portal.

## Status

Working. Running in production on a Pi 5 with two cats (one collar tag, one implanted chip). Reverse-engineered BLE protocol; not officially supported by Run Chicken.

## Hardware

| Component | Notes |
|---|---|
| Run Chicken automatic door | The "Premium" model was used. Other models with the same BLE control surface should work. |
| Raspberry Pi 5 | Pi 4 should also work. **Pi 3B+ has a UART/Bluetooth conflict** that prevents reliable simultaneous use of the FDX-B reader on `/dev/ttyAMA0` and BLE for the door. Don't use a 3B+. |
| FDX-B RFID reader | Any 134.2 kHz FDX-B reader with a TTL-level UART output at 9600 baud. **Verify TX is 3.3V**, not 5V — 5V will damage the Pi GPIO. |
| VL53L1X ToF sensor | Adafruit breakout, I2C. Mounted indoor-facing to detect a cat approaching the door. |
| Latching switches × 3 | Yellow (hold open), red (hold closed), green (force programmed mode). |
| Indicator LEDs × 3 | Yellow / red / green to mirror the override state. |

## Logic

The door is normally closed. Behavior is determined by override switches and a time-of-day schedule:

| State | Behavior |
|---|---|
| Red switch on | Door held closed. RFID still opens for allowed chips. |
| Yellow switch on | Door held open. |
| Green switch on (red and yellow off) | Force programmed mode regardless of schedule. |
| Default schedule, 0530–0900 daily | Held open. |
| Default schedule, 0900–2200 weekdays | Programmed mode (RFID + ToF). |
| Default schedule, 0530–2200 weekends | Held open. |
| Default schedule, 2200–0530 daily | RFID only (ToF disabled overnight). |

In programmed mode the ToF triggers an open from inside; an allowed chip read on the outside reader does the same. Unknown chip reads are logged.

The override state is persisted to disk so the controller survives restarts. Switch positions at startup take precedence over saved state.

## BLE protocol notes

The Run Chicken door's BLE write characteristic accepts a 32-byte packet ending in a CRC-8. The packet structure was reverse-engineered by capturing traffic between the official app and the door:

```
[type:1] [unix_time:4] [unix_time:4] [pad:6]
[hh:1] [mm:1] [hh:1] [mm:1] [pad:2]
[action:1] [pad:9] [crc8:1]
```

`action` is `0x01` to open, `0x02` to close. Both timestamps are seconds since the Unix epoch in little-endian. Hour/minute pairs appear twice; setting both to the current local time has worked in testing.

The write characteristic UUID seen on the unit tested is `00000000-8e22-4541-9d4c-21edae82ed19`. If your door uses a different UUID, enumerate characteristics with `bluetoothctl`.

**This protocol is not documented or supported by Run Chicken.** A firmware update could change it at any time. Pin your firmware version if your installation depends on it.

## Setup

1. Flash Raspberry Pi OS (64-bit, Bookworm or later) on a Pi 5 or Pi 4.
2. Enable I2C and UART:
   ```bash
   sudo raspi-config
   # Interface Options → I2C → Enable
   # Interface Options → Serial Port → No login shell, Yes hardware enabled
   ```
   Disable Bluetooth on the primary UART so `/dev/ttyAMA0` is free for the RFID reader. On a Pi 5, this is usually already the default; verify with `dmesg | grep tty`.
3. Wire the components. See `wiring.md` (TODO) or read the BCM pin assignments in `config.example.yaml`.
4. Install dependencies:
   ```bash
   sudo apt update
   sudo apt install python3-pip python3-venv i2c-tools bluetooth
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
5. Discover the door's BLE MAC address:
   ```bash
   sudo bluetoothctl
   scan on
   # Look for a device named "RC-..." or similar and copy its MAC.
   ```
6. Discover your pets' chip IDs:
   ```bash
   python3 catdoor.py --scan
   # Hold the chip near the reader and copy the HEX value printed.
   ```
7. Copy the example config and fill in your values:
   ```bash
   cp config.example.yaml config.yaml
   $EDITOR config.yaml
   ```
8. Run:
   ```bash
   python3 catdoor.py
   ```
9. (Optional) Install as a systemd service. See `catdoor.service` (TODO).

## Configuration

All site-specific values live in `config.yaml` (gitignored). Edit `config.example.yaml` as your starting point. Schedule, GPIO pin assignments, ToF threshold, and timing values are all configurable there.

## Known issues

- BLE writes occasionally complete without an acknowledgment from the door. The packet still appears to be received based on the door's behavior. The script logs `BLE OPEN: sent (no ack)` in that case.
- The ToF sensor can be defeated by very low approach angles. Mount with the cone of detection covering the indoor approach path.
- Water ingress on the RFID antenna detunes the LC tank circuit and stops reads. Waterproof the antenna and the cable gland on the outdoor enclosure.

## License

MIT — see [LICENSE](LICENSE).

## Acknowledgments

Run Chicken makes a genuinely well-engineered door. Their BLE control surface, while undocumented, is stable enough to integrate against. This project is unaffiliated with Run Chicken d.o.o.
