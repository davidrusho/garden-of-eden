import unittest
from unittest.mock import patch
import sys
import os
# Add the root directory to the Python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.sensors.light.light import Light

# The logger these records actually go to. assertLogs' first argument is a
# LOGGER NAME - passing the expected message there (as this file used to)
# builds a context manager that is never entered and asserts nothing.
_LIGHT_LOGGER = 'app.sensors.light.light'

class TestLight(unittest.TestCase):
    @patch('app.sensors.light.light.PWMLED')
    @patch('app.sensors.light.light.PiGPIOFactory')
    @patch('app.sensors.light.light.pigpio.pi')
    def setUp(self, MockPi, MockFactory, MockPWMLED):
        self.mock_led = MockPWMLED.return_value
        self.mock_led.value = 0
        self.mock_pi = MockPi.return_value
        self.light = Light(18)

    def test_turn_on_from_0(self):
        self.mock_led.value = 0
        with self.assertLogs(_LIGHT_LOGGER, level='INFO') as cm:
            self.light.on()
        self.assertEqual(self.mock_led.value, 1)
        self.assertIn('Turning light on', '\n'.join(cm.output))

    def test_turn_on_from_nonzero(self):
        self.mock_led.value = 0.5
        with self.assertLogs(_LIGHT_LOGGER, level='INFO') as cm:
            self.light.on()
        self.assertEqual(self.mock_led.value, 0.5)
        self.assertIn('Light already on, skipping', '\n'.join(cm.output))

    def test_off(self):
        self.mock_led.value = 1
        with self.assertLogs(_LIGHT_LOGGER, level='INFO') as cm:
            self.light.off()
        self.assertEqual(self.mock_led.value, 0)
        self.assertIn('Turning light off', '\n'.join(cm.output))

    def test_set_brightness_valid(self):
        valid_brightness = 70
        self.light.set_brightness(valid_brightness)
        self.assertEqual(self.mock_led.value * 100, valid_brightness)

    def test_set_brightness_invalid(self):
        with self.assertRaises(ValueError):
            self.light.set_brightness(110)

    def test_set_frequency(self):
        freq = 10000  # 10kHz
        self.light.set_frequency(freq)
        self.mock_pi.set_PWM_frequency.assert_called_with(18, freq)

    def test_close(self):
        self.light.close()
        self.mock_led.close.assert_called_once()

if __name__ == '__main__':
    unittest.main()
