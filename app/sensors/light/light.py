# Reviewed: 2026-07-31 against 469e3d2 (T-470 follow-on: light command attribution)
import argparse
from gpiozero import PWMLED
from gpiozero.pins.pigpio import PiGPIOFactory
import pigpio
import logging

# A module logger, not the root one. Every call below used to log through the
# bare `logging.info(...)` root shim, and mqtt.py pins the root at WARNING - so
# no light command has ever reached a handler, legitimate or otherwise. When an
# unexplained 50% assert hit the grow light at 20:08 on 2026-07-31 (real, not a
# reporting glitch: the Zigbee plug metered 55W for 28s), there was nothing to
# attribute it with, and the same silence covers every correct command too.
logger = logging.getLogger(__name__)

# Module-owned policy, set at import so it cannot be lost to import ordering.
#
# It lives HERE rather than in mqtt.py because mqtt.py cannot be imported
# without paho/gpiozero/pigpio - a level set there is untestable, and an
# untested control on an appliance whose failures are silent is the exact
# pattern this codebase keeps getting bitten by.
#
# Scoped to this module on purpose - for SIGNAL, not for disk wear. Measured on
# the host before writing this: gardyn.log grows ~26 KB/day against 25 GB free,
# and journald is capped at 64M, so volume was never the real argument. The
# argument is that a blanket INFO buries four meaningful light events a day
# under the camera path's ~576 lines. get_duty_cycle() below is demoted to
# debug for that reason, and it is safe to demote because its value is
# published as retained MQTT state on light/brightness/state immediately after
# every command - HA's recorder holds it at better resolution than the log did.
#
# One residual, deliberately accepted: the log now records COMMANDED values,
# never OBSERVED ones. If PWMLED silently fails to apply a duty cycle, the log
# asserts a change that did not happen.
#
# This works despite the root sitting at WARNING because Logger.callHandlers
# walks the ancestor chain and consults each HANDLER's level - never the
# ancestor LOGGER's. Measured with controls, not assumed; see
# tests/test_light_logging.py, which fails if either half regresses.
logger.setLevel(logging.INFO)


class GPIOController:
    def __init__(self, pin, pin_factory=None):
        self.pin = pin
        self.factory = pin_factory
        if pin_factory:
            self.pi = pigpio.pi()
        else:
            self.pi = pigpio.pi()
        
        if not self.pi.connected:
            raise RuntimeError("Failed to connect to pigpiod daemon. Ensure it's running and accessible.")

    def set_frequency(self, frequency):
        if self.pi:
            self.pi.set_PWM_frequency(self.pin, frequency)
        else:
            raise RuntimeError("pigpio.pi client is not initialized.")

class Light:
    def __init__(self, pin=18, frequency=8000, pin_factory=None):
        # pigpiod is running on port 8888
        # Note: for docker: PiGPIOFactory(host='pigpiod', port=8888)
        self.pin = pin
        self.pin_factory = pin_factory if pin_factory else PiGPIOFactory()
        self.led = PWMLED(self.pin, pin_factory=self.pin_factory)
        self.gpio = GPIOController(pin, pin_factory)
        self.set_frequency(frequency)

    def on(self):
        """
        Turn on lights.
        """
        if self.led.value > 0:
            logger.info("Light already on, skipping")
            return

        logger.info("Turning light on")
        self.led.value = 1

    def off(self):
        """
        Turn off lights.
        """
        logger.info("Turning light off")
        self.led.value = 0
    
    def set_brightness(self, brightness_percentage):
        """
        Wrapper function around set_duty_cycle. Provides more intuitive function name.

        Args:
        - brightness_percentage (int): A value between 0 (off) and 100 (max brightness).
        """
        self.set_duty_cycle(brightness_percentage)

    def get_brightness(self):
        """
        Wrapper function around get_duty_cycle. Provides more intuitive function name.

        Returns:
        - float: The current duty cycle percentage.
        """
        return self.get_duty_cycle()

    def set_frequency(self, frequency):
        logger.info(f"Setting light frequency to {frequency}")
        self.gpio.set_frequency(frequency)
    
    def set_duty_cycle(self, duty_cycle_percentage):
        """
        Set the duty cycle percentage, i.e. brightness level.

        Args:
        - duty_cycle_percentage (int): A value between 0 (off) and 100 (full brightness).
        """
        if 0 <= duty_cycle_percentage <= 100:
            # gpiozero's PWMLED uses a 0-1 scale for duty cycle
            duty = duty_cycle_percentage / 100.0
            logger.info(f"Setting light duty_cycle to {duty_cycle_percentage}%")
            self.led.value = duty
        else:
            raise ValueError("Speed must be between 0 and 100")
        
    def get_duty_cycle(self):
        """
        Get the current duty cycle percentage.

        Returns:
        - float: The current duty cycle percentage.
        """
        duty_cycle = self.led.value * 100
        logger.debug(f"Light duty_cycle is {duty_cycle}%")
        return duty_cycle

    def close(self):
        self.led.close()

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Control an IoT light.')
    parser.add_argument('--on', action='store_true', help='Turn the light on.')
    parser.add_argument('--off', action='store_true', help='Turn the light off.')
    parser.add_argument('--brightness', type=int, default=None,
                        help='Set the brightness level (0-100).')

    args = parser.parse_args()

    light = Light(18)  # Default frequency of 8kHz

    if args.on:
        light.on()
        if args.brightness is not None:
            light.set_brightness(args.brightness)
    elif args.off:
        light.off()
    elif args.brightness is not None:
        light.on()
        light.set_brightness(args.brightness)
    else:
        logger.info("No action specified. Use --on, --off, or --brightness.")
