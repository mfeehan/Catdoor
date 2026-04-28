#!/usr/bin/env python3
"""
Cat door controller for Run Chicken automatic coop door
================================================================

Repurposes a Run Chicken automatic chicken coop door as an RFID-controlled
cat door with multi-factor access logic. Reverse-engineered BLE protocol;
not officially supported by Run Chicken.

Hardware:
- Raspberry Pi 5 (Pi 4 should also work; Pi 3B+ has UART/BT conflicts)
- FDX-B RFID reader on /dev/ttyAMA0 (outside - chip triggers open)
- VL53L1X ToF sensor on I2C (inside - proximity triggers open)
- Run Chicken automatic door (BLE control)
- Latching switches: yellow=hold open, red=hold closed, green=force full mode
- Status LEDs: yellow=open override, red=closed override, green=auto mode

Logic:
- Door is normally closed
- Allowed pets identified by FDX-B chip ID (microchip or collar tag)
- Schedule with weekend/weekday modes and night-time RFID-only operation
- RFID always opens door for allowed chips, regardless of mode or override
  (except when red/closed override is active)
- All unknown chip reads logged
- State persisted across restarts; switch positions win over saved state

Schedule (defaults; edit constants below):
  Daily        0530-0900  hold open
  Daily        0900-2200  full program (RFID + ToF)
  Weekends     0530-2200  hold open
  Daily        2200-0530  RFID only (no ToF)

Configuration:
  Copy config.example.yaml to config.yaml and edit. config.yaml is gitignored.
  At minimum you must set:
    - door_mac: BLE MAC of your Run Chicken door (discover with bluetoothctl)
    - allowed_chips: dict mapping hex chip IDs to pet names

License: MIT (see LICENSE)
"""

import serial
import asyncio
import datetime as dt
import struct
import sys
import os
import logging
import yaml
import crc8
import busio
import board
import adafruit_vl53l1x
import RPi.GPIO as GPIO
from bleak import BleakClient
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Config loading ────────────────────────────────────────────────────────────
CONFIG_PATH = Path(__file__).parent / "config.yaml"

if not CONFIG_PATH.exists():
    print(f"ERROR: {CONFIG_PATH} not found.")
    print("Copy config.example.yaml to config.yaml and edit before running.")
    sys.exit(1)

with open(CONFIG_PATH) as f:
    CFG = yaml.safe_load(f)

# ── Serial ───────────────────────────────────────────────────────────────────
SERIAL_PORT = CFG.get("serial_port", "/dev/ttyAMA0")
BAUD_RATE   = CFG.get("baud_rate", 9600)

# ── BLE Door ──────────────────────────────────────────────────────────────────
DOOR_MAC        = CFG["door_mac"]
WRITE_CHAR_UUID = CFG.get("write_char_uuid", "00000000-8e22-4541-9d4c-21edae82ed19")

# ── GPIO pins ─────────────────────────────────────────────────────────────────
PINS = CFG.get("pins", {})
PIN_MOTION      = PINS.get("motion",     27)
PIN_BTN_OPEN    = PINS.get("btn_open",   22)
PIN_BTN_CLOSE   = PINS.get("btn_close",  23)
PIN_BTN_GREEN   = PINS.get("btn_green",  12)
PIN_LED_YELLOW  = PINS.get("led_yellow", 17)
PIN_LED_RED     = PINS.get("led_red",    25)
PIN_LED_GREEN   = PINS.get("led_green",   8)

# ── ToF sensor ────────────────────────────────────────────────────────────────
TOF_THRESHOLD_MM = CFG.get("tof_threshold_mm", 50)

# ── Timing ────────────────────────────────────────────────────────────────────
HOLD_SECONDS    = CFG.get("hold_seconds",    45)
LOCKOUT_SECONDS = CFG.get("lockout_seconds", 60)

# ── Schedule ──────────────────────────────────────────────────────────────────
SCHED = CFG.get("schedule", {})
OPEN_START_MIN  = SCHED.get("open_start_min",   5 * 60 + 30)
PROG_START_MIN  = SCHED.get("prog_start_min",   9 * 60)
NIGHT_START_MIN = SCHED.get("night_start_min", 22 * 60)

# ── Allowed chips ──────────────────────────────────────────────────────────────
# config.yaml stores chips as {name: hex_string}; we invert and encode.
ALLOWED_CHIPS = {
    chip_hex.encode(): name
    for name, chip_hex in CFG.get("allowed_chips", {}).items()
}

if not ALLOWED_CHIPS:
    print("WARNING: no allowed_chips configured. No pets will be admitted.")

# ── State / log paths ─────────────────────────────────────────────────────────
STATE_FILE = CFG.get("state_file", str(Path.home() / "catdoor_state.txt"))
LOG_FILE   = CFG.get("log_file",   str(Path.home() / "catdoor.log"))

# ── Logging ───────────────────────────────────────────────────────────────────
_handler = RotatingFileHandler(LOG_FILE, maxBytes=1 * 1024 * 1024, backupCount=5)
_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.getLogger().setLevel(logging.INFO)
logging.getLogger().addHandler(_handler)

def log(msg):
    print(msg)
    logging.info(msg)

# ── Schedule logic ─────────────────────────────────────────────────────────────
def get_schedule_mode():
    now = dt.datetime.now()
    is_weekend = now.weekday() >= 5
    time_mins = now.hour * 60 + now.minute

    if time_mins < OPEN_START_MIN or time_mins >= NIGHT_START_MIN:
        return 'rfid_only'
    elif is_weekend:
        return 'open'
    elif time_mins < PROG_START_MIN:
        return 'open'
    else:
        return 'full'

# ── State persistence ──────────────────────────────────────────────────────────
def save_state(state: str):
    try:
        with open(STATE_FILE, 'w') as f:
            f.write(state)
    except Exception as e:
        log(f"State save failed: {e}")

# ── LED control ────────────────────────────────────────────────────────────────
def set_leds(override: str):
    GPIO.output(PIN_LED_YELLOW, GPIO.HIGH if override == 'open'   else GPIO.LOW)
    GPIO.output(PIN_LED_RED,    GPIO.HIGH if override == 'closed' else GPIO.LOW)
    GPIO.output(PIN_LED_GREEN,  GPIO.HIGH if override == 'auto'   else GPIO.LOW)

# ── BLE ───────────────────────────────────────────────────────────────────────
def create_packet(open_door: bool) -> bytes:
    """Build a Run Chicken BLE control packet.

    Packet structure (reverse-engineered, not officially documented):
      [type:1] [unix_time:4] [unix_time:4] [pad:6]
      [hh:1] [mm:1] [hh:1] [mm:1] [pad:2]
      [action:1] [pad:9] [crc8:1]
    Action byte: 0x01 = open, 0x02 = close.
    """
    packet_time = dt.datetime.now(dt.UTC)
    crc_obj = crc8.crc8()
    packet = bytes([0])
    unix_time = int(packet_time.timestamp())
    packet += struct.pack("<I", unix_time)
    packet += struct.pack("<I", unix_time)
    packet += struct.pack("<6x")
    packet += struct.pack("<B", packet_time.hour)
    packet += struct.pack("<B", packet_time.minute)
    packet += struct.pack("<B", packet_time.hour)
    packet += struct.pack("<B", packet_time.minute)
    packet += struct.pack("<2x")
    packet += struct.pack("<B", 0x01 if open_door else 0x02)
    packet += struct.pack("<9x")
    crc_obj.update(packet)
    packet += struct.pack("<B", crc_obj.digest()[0])
    return packet

async def ble_command(open_door: bool) -> bool:
    action = "OPEN" if open_door else "CLOSE"
    try:
        packet = create_packet(open_door)
        async with BleakClient(DOOR_MAC) as client:
            await client.write_gatt_char(WRITE_CHAR_UUID, packet)
        log(f"BLE {action}: command sent")
    except Exception as e:
        log(f"BLE {action}: sent (no ack)")
    return True

# ── ToF sensor ────────────────────────────────────────────────────────────────
def tof_triggered(tof) -> bool:
    """Return True if something is closer than threshold. Safe if tof is None."""
    if tof is None:
        return False
    try:
        if tof.data_ready:
            d = tof.distance
            tof.clear_interrupt()
            if d is not None and d < TOF_THRESHOLD_MM:
                return True
    except Exception as e:
        log(f"ToF read error: {e}")
    return False

# ── RFID ──────────────────────────────────────────────────────────────────────
def parse_chip(raw: bytes):
    """Return (name, chip_bytes) if allowed, (None, chip_bytes) if unknown,
    (None, None) if invalid."""
    if len(raw) < 2:
        return None, None
    data = raw.strip()
    if data.startswith(b'\x02'):
        data = data[1:]
    if data.endswith(b'\x03'):
        data = data[:-3]
    if not data:
        return None, None
    for chip_bytes, name in ALLOWED_CHIPS.items():
        if data == chip_bytes or data.endswith(chip_bytes):
            return name, data
    return None, data

# ── RFID handler (runs in all modes) ──────────────────────────────────────────
async def handle_rfid(ser, locked_until, door_open, mode):
    """Read RFID and open door for allowed chips regardless of mode.
    Returns (door_open, locked_until) updated values."""
    raw = ser.readline()
    if not raw:
        return door_open, locked_until

    name, chip = parse_chip(raw)

    if name:
        log(f"RFID seen: {name} ({chip.hex()})")
        if not door_open:
            log(f"RFID allowed: {name} ({chip.hex()}) - opening door")
            await ble_command(open_door=True)
            door_open = True
            await asyncio.sleep(HOLD_SECONDS)
            log("Hold expired - closing door")
            await ble_command(open_door=False)
            door_open = False
            await asyncio.sleep(15)
    elif chip:
        log(f"RFID rejected: unknown chip ({chip.hex()})")

    return door_open, locked_until

# ── Main loop ─────────────────────────────────────────────────────────────────
async def main():
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(PIN_MOTION,     GPIO.IN,  pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(PIN_BTN_OPEN,   GPIO.IN,  pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(PIN_BTN_CLOSE,  GPIO.IN,  pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(PIN_BTN_GREEN,  GPIO.IN,  pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(PIN_LED_YELLOW, GPIO.OUT)
    GPIO.setup(PIN_LED_RED,    GPIO.OUT)
    GPIO.setup(PIN_LED_GREEN,  GPIO.OUT)

    GPIO.output(PIN_LED_YELLOW, GPIO.LOW)
    GPIO.output(PIN_LED_RED,    GPIO.LOW)
    GPIO.output(PIN_LED_GREEN,  GPIO.LOW)

    # ── Init ToF sensor (non-fatal) ───────────────────────────────────────────
    tof = None
    try:
        i2c = busio.I2C(scl=board.SCL, sda=board.SDA)
        tof = adafruit_vl53l1x.VL53L1X(i2c)
        tof.distance_mode = 2
        tof.start_ranging()
        log("ToF sensor initialized")
    except Exception as e:
        log(f"ToF sensor init failed (continuing without it): {e}")

    try:
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=0.1)
    except Exception as e:
        log(f"Serial open failed: {e}")
        sys.exit(1)

    # Switch positions on startup win over saved state
    if GPIO.input(PIN_BTN_CLOSE) == 1:
        override = 'closed'
    elif GPIO.input(PIN_BTN_OPEN) == 1:
        override = 'open'
    else:
        override = 'auto'

    save_state(override)
    log(f"Cat door controller started - override={override}")

    door_open           = False
    locked_until        = 0.0
    last_mode           = None
    last_green_blink    = 0.0
    last_heartbeat_hour = -1

    try:
        while True:
            now = asyncio.get_event_loop().time()

            btn_close = GPIO.input(PIN_BTN_CLOSE) == 1
            btn_open  = GPIO.input(PIN_BTN_OPEN)  == 1
            btn_green = GPIO.input(PIN_BTN_GREEN) == 1

            new_override = override
            if btn_close:
                new_override = 'closed'
            elif btn_open:
                new_override = 'open'
            else:
                new_override = 'auto'

            if new_override != override:
                prev_override = override
                override = new_override
                save_state(override)
                log(f"Override changed: {override}")
                if prev_override == 'open' and override in ('closed', 'auto'):
                    log("Leaving open override - closing door")
                    await ble_command(open_door=False)
                    door_open = False
                    locked_until = 0.0

            if override == 'closed':
                mode = 'closed'
            elif override == 'open':
                mode = 'open'
            elif btn_green:
                mode = 'full'
            else:
                mode = get_schedule_mode()

            if mode != last_mode:
                log(f"Mode: {mode}{' (green button override)' if btn_green and mode == 'full' else ''}")
                last_mode = mode

            set_leds(override)

            if btn_green and override == 'auto':
                if now - last_green_blink >= 2.0:
                    GPIO.output(PIN_LED_GREEN, GPIO.LOW)
                    await asyncio.sleep(0.1)
                    GPIO.output(PIN_LED_GREEN, GPIO.HIGH)
                    last_green_blink = now

            if mode == 'open' and not door_open:
                log("Schedule/override: holding open")
                await ble_command(open_door=True)
                door_open = True

            elif mode == 'closed' and door_open:
                log("Schedule/override: holding closed")
                await ble_command(open_door=False)
                door_open = False

            elif mode in ('full', 'rfid_only'):
                if door_open and now >= locked_until:
                    log("Entering program mode - closing door")
                    await ble_command(open_door=False)
                    door_open = False

                if mode == 'full':
                    if not door_open and now >= locked_until and tof_triggered(tof):
                        await asyncio.sleep(0.3)
                        if tof_triggered(tof):
                            log("ToF triggered (inside) - opening door")
                            await ble_command(open_door=True)
                            door_open = True
                            await asyncio.sleep(HOLD_SECONDS)
                            log("Hold expired - closing door")
                            await ble_command(open_door=False)
                            door_open = False
                            await asyncio.sleep(15)
                            locked_until = asyncio.get_event_loop().time() + LOCKOUT_SECONDS

            if mode in ('full', 'rfid_only') and not door_open:
                current_hour = dt.datetime.now().hour
                if current_hour != last_heartbeat_hour:
                    if dt.datetime.now().minute == 0:
                        await ble_command(open_door=False)
                        last_heartbeat_hour = current_hour

            door_open, locked_until = await handle_rfid(ser, locked_until, door_open, mode)

            await asyncio.sleep(0.05)

    except KeyboardInterrupt:
        log("Stopped by user")
        if door_open:
            await ble_command(open_door=False)
    finally:
        if tof:
            tof.stop_ranging()
        GPIO.output(PIN_LED_YELLOW, GPIO.LOW)
        GPIO.output(PIN_LED_RED,    GPIO.LOW)
        GPIO.output(PIN_LED_GREEN,  GPIO.LOW)
        ser.close()
        GPIO.cleanup()

# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    if "--scan" in sys.argv:
        print(f"Scan mode on {SERIAL_PORT} at {BAUD_RATE} baud - Ctrl+C to stop\n")
        try:
            ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
            while True:
                line = ser.readline()
                if line:
                    print(f"RAW: {line!r}  HEX: {line.hex()}")
        except KeyboardInterrupt:
            print("\nStopped")
        except Exception as e:
            print(f"Error: {e}")
    else:
        asyncio.run(main())
