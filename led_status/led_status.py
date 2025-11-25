#!/usr/bin/env python3
import time, subprocess, logging, signal, sys
from pathlib import Path
from periphery import GPIO

# ---------- CONFIG ----------
GPIO_CHIP = "/dev/gpiochip0"

LED1_LINE = 272   # PI16  Power
LED2_LINE = 258   # PI2   Wi-Fi Link
LED3_LINE = 268   # PI12  Card Readers

LED1_ACTIVE_LOW = True
LED2_ACTIVE_LOW = True
LED3_ACTIVE_LOW = True

WIFI_CHECK_INTERVAL = 3.0
CARD_CHECK_INTERVAL = 2.0
WIFI_BLINK_PERIOD   = 0.5     # 1 Hz blink when connected
LOG_LEVEL = logging.INFO
CARD_FLAG = Path("/home/orangepi/rack_ui/readers_connected.flag")  # <-- optional flag file
# --------------------------------


def logic(val, active_low): return (not val) if active_low else val


class LED:
    def __init__(self, chip, line, name, active_low):
        self.name, self.active_low = name, active_low
        try:
            self.gpio = GPIO(chip, line, "out")
        except Exception as e:
            raise RuntimeError(f"Failed to open {name}: {e}")
    def set(self, on): self.gpio.write(logic(on, self.active_low))
    def on(self):  self.set(True)
    def off(self): self.set(False)
    def close(self):
        try: self.off()
        except Exception: pass
        try: self.gpio.close()
        except Exception: pass


def wifi_connected() -> bool:
    """Return True if any wlan interface is up with an IP."""
    try:
        out = subprocess.check_output(
            "iwgetid -r", shell=True, stderr=subprocess.DEVNULL
        ).decode().strip()
        return bool(out)
    except Exception:
        return False


def card_readers_connected() -> bool:
    """Simple hook — check for USB reader presence or flag file."""
    # Option 1 – detect PC/SC readers dynamically
    try:
        out = subprocess.check_output(
            "lsusb | grep -i 'card' || true", shell=True
        ).decode()
        if out.strip():
            return True
    except Exception:
        pass
    # Option 2 – use flag file created by your server.py
    return CARD_FLAG.exists()


class LEDManager:
    def __init__(self):
        self.power = LED(GPIO_CHIP, LED1_LINE, "Power", LED1_ACTIVE_LOW)
        self.wifi  = LED(GPIO_CHIP, LED2_LINE, "WiFi",  LED2_ACTIVE_LOW)
        self.cards = LED(GPIO_CHIP, LED3_LINE, "Cards", LED3_ACTIVE_LOW)
        self.running = True
        signal.signal(signal.SIGINT,  self.stop)
        signal.signal(signal.SIGTERM, self.stop)

    def stop(self, *_):
        logging.info("Stopping LED Manager…")
        self.running = False

    def close(self):
        for led in (self.power, self.wifi, self.cards):
            led.close()

    def run(self):
        logging.info("[INFO] LED Manager started.")
        self.power.on()  # Power LED always ON

        last_wifi_check = 0.0
        wifi_ok = False
        last_card_check = 0.0
        card_ok = False
        blink_state = False
        last_blink = 0.0

        while self.running:
            now = time.monotonic()

            # --- Wi-Fi status ---
            if now - last_wifi_check >= WIFI_CHECK_INTERVAL:
                wifi_ok = wifi_connected()
                last_wifi_check = now
                logging.debug(f"Wi-Fi connected={wifi_ok}")

            # --- Card readers status ---
            if now - last_card_check >= CARD_CHECK_INTERVAL:
                card_ok = card_readers_connected()
                last_card_check = now
                logging.debug(f"Card readers connected={card_ok}")

            # --- Wi-Fi LED behavior ---
            if wifi_ok:
                if now - last_blink >= WIFI_BLINK_PERIOD:
                    blink_state = not blink_state
                    self.wifi.set(blink_state)
                    last_blink = now
            else:
                self.wifi.off()

            # --- Card readers LED ---
            if card_ok:
                self.cards.on()
            else:
                self.cards.off()

            time.sleep(0.05)

        logging.info("[INFO] LED Manager exited.")


def main():
    logging.basicConfig(level=LOG_LEVEL,
                        format="%(asctime)s - %(levelname)s - %(message)s")
    mgr = None
    try:
        mgr = LEDManager()
        mgr.run()
    except Exception as e:
        logging.exception("Fatal error: %s", e)
    finally:
        if mgr:
            mgr.close()


if __name__ == "__main__":
    main()

