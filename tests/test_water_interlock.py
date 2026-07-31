"""Tests for the reservoir plausibility band, the fail-closed water state, and
the pump interlock.

These run without a Pi and without gpiozero. mqtt.py builds hardware objects at
import time, so the hardware and broker modules are stubbed in sys.modules
before it is imported.

Why this file exists at all: the reservoir ultrasonic on the unit these tests
were written for is physically disconnected, so a live system can only ever
exercise the untrustworthy branch. Every threshold comparison and every
trustworthy-reading path is reachable here and nowhere else.
"""

import json
import sys
import os
import types
import unittest
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(_REPO_ROOT)


# --- stub the hardware and broker modules mqtt.py imports at module scope ---

def _install_stubs():
    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    mod("gpiozero", Button=MagicMock(), PWMLED=MagicMock(),
        DistanceSensor=MagicMock(name="DistanceSensor"))
    mod("gpiozero.pins", __path__=[])
    mod("gpiozero.pins.pigpio", PiGPIOFactory=MagicMock())

    paho = mod("paho", __path__=[])
    paho_mqtt = mod("paho.mqtt", __path__=[])
    mod("paho.mqtt.client", Client=MagicMock(), CallbackAPIVersion=MagicMock())
    paho.mqtt = paho_mqtt

    mod("app", __path__=[])
    mod("app.sensors", __path__=[])
    for pkg in ("light", "pump", "pcb_temp", "temperature", "humidity"):
        mod(f"app.sensors.{pkg}", __path__=[])
    mod("app.sensors.light.light", Light=MagicMock())
    mod("app.sensors.pump.pump", Pump=MagicMock())
    mod("app.sensors.pcb_temp.pcb_temp", get_pcb_temperature=MagicMock(return_value=20.0))
    mod("app.sensors.temperature.temperature", temperature_sensor=MagicMock())
    mod("app.sensors.humidity.humidity", humidity_sensor=MagicMock())

    # distance is the exception: point the package at its real directory so the
    # REAL driver loads (it imports nothing but gpiozero, which is stubbed
    # above). Stubbing it would mean mqtt.py caught a fake SensorBusy that no
    # real code path can raise - the interlock tests would then prove nothing
    # about the exception the driver actually throws.
    mod(
        "app.sensors.distance",
        __path__=[os.path.join(_REPO_ROOT, "app", "sensors", "distance")],
    )

    # Deterministic config, independent of whatever .env happens to say.
    mod(
        "config",
        USERNAME="u", PASSWORD="p", BROKER="localhost", PORT=1883,
        KEEP_ALIVE_INTERVAL=60, BASE_TOPIC="gardyn", IDENTIFIER="gardyn-xx",
        MODEL="gardyn 2.0", VERSION="1.0.0",
        WATER_LOW_CM=11.0,
        WATER_VALID_MIN_CM=3.0, WATER_VALID_MAX_CM=25.0,
        UPPER_CAMERA_DEVICE="/dev/video0", LOWER_CAMERA_DEVICE="/dev/video2",
        UPPER_IMAGE_PATH="/tmp/u.jpg", LOWER_IMAGE_PATH="/tmp/l.jpg",
        CAMERA_RESOLUTION="640x480", UPPER_CAMERA_RESOLUTION="640x480",
        LOWER_CAMERA_RESOLUTION="640x480", IMAGE_INTERVAL_SECONDS=3600,
    )


_install_stubs()
import mqtt as mqtt_mod  # noqa: E402
from app.sensors.distance.distance import (  # noqa: E402
    Distance, MeasurementError, SensorBusy,
)

_MeasurementError = MeasurementError
_SensorBusy = SensorBusy


class WaterTestBase(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.sensor = MagicMock()
        mqtt_mod.distance_sensor = self.sensor
        mqtt_mod.client = self.client
        mqtt_mod.pump = MagicMock()
        # Real number, not a bare Mock: publish_pump_state() compares get_speed()
        # against 0, and a Mock there raises a TypeError that on_message's
        # catch-all swallows - which would let these tests pass while the state
        # publish they are meant to exercise never runs.
        mqtt_mod.pump.get_speed.return_value = 0
        mqtt_mod.WATER_LOW_CM = 11.0
        mqtt_mod.WATER_VALID_MIN_CM = 3.0
        mqtt_mod.WATER_VALID_MAX_CM = 25.0
        # flash_lights spawns a thread; the interlock's decision is what matters.
        self._flash = patch.object(mqtt_mod, "flash_lights").start()
        self.addCleanup(patch.stopall)

    def published(self, topic):
        """Payloads published to `topic`, in order."""
        return [
            c.args[1]
            for c in self.client.publish.call_args_list
            if c.args and c.args[0] == topic
        ]

    def topics(self):
        return [c.args[0] for c in self.client.publish.call_args_list if c.args]


class TestPlausibilityBand(WaterTestBase):
    def test_reading_inside_band_is_returned(self):
        self.sensor.measure.return_value = 12.5
        self.assertEqual(mqtt_mod.safe_distance_measure(), 12.5)

    def test_latched_near_zero_reading_is_rejected(self):
        # The exact value this unit reported for days with no sensor attached.
        self.sensor.measure.return_value = 0.09
        self.assertIsNone(mqtt_mod.safe_distance_measure())

    def test_out_of_range_reading_is_rejected(self):
        # Also observed on the same dead sensor. Note it is ABOVE the low-water
        # threshold, so a bare comparison would call it "water low" - the same
        # garbage produces opposite alarms depending only on where it latched.
        self.sensor.measure.return_value = 83.0
        self.assertIsNone(mqtt_mod.safe_distance_measure())

    def test_zero_reading_is_rejected(self):
        # partial=True returns 0.0 from an empty queue rather than blocking.
        self.sensor.measure.return_value = 0.0
        self.assertIsNone(mqtt_mod.safe_distance_measure())

    def test_band_edges_are_inclusive(self):
        for edge in (3.0, 25.0):
            with self.subTest(edge=edge):
                self.sensor.measure.return_value = edge
                self.assertEqual(mqtt_mod.safe_distance_measure(), edge)

    def test_just_outside_band_is_rejected(self):
        for value in (2.99, 25.01):
            with self.subTest(value=value):
                self.sensor.measure.return_value = value
                self.assertIsNone(mqtt_mod.safe_distance_measure())

    def test_busy_sensor_yields_no_reading(self):
        self.sensor.measure.side_effect = _SensorBusy("busy")
        self.assertIsNone(mqtt_mod.safe_distance_measure())

    def test_measurement_error_yields_no_reading(self):
        self.sensor.measure.side_effect = _MeasurementError("boom")
        self.assertIsNone(mqtt_mod.safe_distance_measure())


class TestFailClosedPublishing(WaterTestBase):
    def test_untrustworthy_reading_publishes_offline_and_nothing_else(self):
        self.sensor.measure.return_value = 0.09
        self.assertIsNone(mqtt_mod.refresh_water_state(self.client))
        self.assertEqual(self.published("gardyn/water/status"), ["offline"])
        # The critical assertion: no level, and above all no "OFF" claiming the
        # reservoir is fine on the strength of a reading we just rejected.
        self.assertNotIn("gardyn/water/level", self.topics())
        self.assertNotIn("gardyn/water/low/state", self.topics())

    def test_trustworthy_reading_above_threshold_reports_low(self):
        self.sensor.measure.return_value = 20.0
        self.assertEqual(mqtt_mod.refresh_water_state(self.client), 20.0)
        self.assertEqual(self.published("gardyn/water/status"), ["online"])
        self.assertEqual(self.published("gardyn/water/level"), ["20.00"])
        self.assertEqual(self.published("gardyn/water/low/state"), ["ON"])

    def test_trustworthy_reading_below_threshold_reports_ok(self):
        self.sensor.measure.return_value = 8.0
        self.assertEqual(mqtt_mod.refresh_water_state(self.client), 8.0)
        self.assertEqual(self.published("gardyn/water/status"), ["online"])
        self.assertEqual(self.published("gardyn/water/level"), ["8.00"])
        self.assertEqual(self.published("gardyn/water/low/state"), ["OFF"])

    def test_trust_topic_recovers_when_readings_become_valid(self):
        self.sensor.measure.return_value = 0.09
        mqtt_mod.refresh_water_state(self.client)
        self.sensor.measure.return_value = 9.0
        mqtt_mod.refresh_water_state(self.client)
        self.assertEqual(
            self.published("gardyn/water/status"), ["offline", "online"]
        )

    def test_update_water_low_state_never_falls_back_to_off(self):
        mqtt_mod.update_water_low_state(self.client, None)
        self.assertNotIn("gardyn/water/low/state", self.topics())

    def test_disabled_checking_publishes_off_explicitly(self):
        mqtt_mod.WATER_LOW_CM = None
        mqtt_mod.update_water_low_state(self.client)
        self.assertEqual(self.published("gardyn/water/low/state"), ["OFF"])

    def test_supplied_distance_avoids_a_second_measurement(self):
        mqtt_mod.update_water_low_state(self.client, 9.0)
        self.sensor.measure.assert_not_called()


class TestPumpInterlock(WaterTestBase):
    def test_refuses_to_start_on_untrustworthy_reading(self):
        self.sensor.measure.return_value = 0.09
        self.assertFalse(mqtt_mod.start_pump(100, self.client))
        mqtt_mod.pump.set_speed.assert_not_called()

    def test_refuses_to_start_when_water_is_low(self):
        self.sensor.measure.return_value = 20.0
        self.assertFalse(mqtt_mod.start_pump(100, self.client))
        mqtt_mod.pump.set_speed.assert_not_called()

    def test_starts_when_water_is_present(self):
        self.sensor.measure.return_value = 8.0
        self.assertTrue(mqtt_mod.start_pump(100, self.client))
        mqtt_mod.pump.set_speed.assert_called_once_with(100)

    def test_starts_unconditionally_when_checking_disabled(self):
        mqtt_mod.WATER_LOW_CM = None
        self.sensor.measure.return_value = 0.09
        self.assertTrue(mqtt_mod.start_pump(100, self.client))
        mqtt_mod.pump.set_speed.assert_called_once_with(100)

    def _send(self, topic_suffix, payload):
        msg = MagicMock()
        msg.topic = f"gardyn/{topic_suffix}"
        msg.payload = payload.encode()
        mqtt_mod.on_message(self.client, None, msg)

    def test_pump_command_on_path_is_interlocked(self):
        self.sensor.measure.return_value = 0.09
        self._send("pump/command", "ON")
        mqtt_mod.pump.set_speed.assert_not_called()

    def test_pump_speed_set_path_is_interlocked(self):
        # This path had no water check at all before T-472.
        self.sensor.measure.return_value = 0.09
        self._send("pump/speed/set", "80")
        mqtt_mod.pump.set_speed.assert_not_called()

    def test_pump_speed_set_still_starts_when_water_is_present(self):
        self.sensor.measure.return_value = 8.0
        self._send("pump/speed/set", "80")
        mqtt_mod.pump.set_speed.assert_called_once_with(80)

    def test_pump_speed_zero_stops_without_measuring(self):
        self._send("pump/speed/set", "0")
        mqtt_mod.pump.off.assert_called_once()
        self.sensor.measure.assert_not_called()

    def test_button_double_press_path_is_interlocked(self):
        # The physical button also had no water check before T-472.
        self.sensor.measure.return_value = 0.09
        mqtt_mod.pump.get_speed.return_value = 0
        mqtt_mod.toggle_pump()
        mqtt_mod.pump.set_speed.assert_not_called()

    def test_button_double_press_starts_when_water_is_present(self):
        self.sensor.measure.return_value = 8.0
        mqtt_mod.pump.get_speed.return_value = 0
        mqtt_mod.toggle_pump()
        mqtt_mod.pump.set_speed.assert_called_once()

    def test_button_can_always_stop_a_running_pump(self):
        # Stopping must never be gated on a sensor reading.
        self.sensor.measure.return_value = 0.09
        mqtt_mod.pump.get_speed.return_value = 50
        mqtt_mod.toggle_pump()
        mqtt_mod.pump.off.assert_called_once()


class TestDiscoveryAvailability(WaterTestBase):
    def setUp(self):
        super().setUp()
        mqtt_mod.send_discovery_messages(self.client)
        self.configs = {}
        for call in self.client.publish.call_args_list:
            if not call.args or not call.args[0].startswith("homeassistant/"):
                continue
            self.configs[call.args[0]] = json.loads(call.args[1])

    def _config(self, fragment):
        for topic, payload in self.configs.items():
            if fragment in topic:
                return payload
        self.fail(f"no discovery config published for {fragment}")

    def test_water_entities_use_an_availability_list(self):
        for fragment in ("_water_level/config", "_water_low/config"):
            with self.subTest(entity=fragment):
                cfg = self._config(fragment)
                topics = [a["topic"] for a in cfg["availability"]]
                self.assertEqual(topics, ["gardyn/status", "gardyn/water/status"])
                self.assertEqual(cfg["availability_mode"], "all")

    def test_water_entities_do_not_also_set_availability_topic(self):
        # The MQTT integration docs: availability_topic "must not be used
        # together with availability". Both keys present is a config error, and
        # merging the shared availability_config would have produced exactly it.
        for fragment in ("_water_level/config", "_water_low/config"):
            with self.subTest(entity=fragment):
                self.assertNotIn("availability_topic", self._config(fragment))

    def test_non_water_entities_keep_the_single_topic_form(self):
        for fragment in ("_light/config", "_water_low_cm/config"):
            with self.subTest(entity=fragment):
                cfg = self._config(fragment)
                self.assertEqual(cfg["availability_topic"], "gardyn/status")
                self.assertNotIn("availability", cfg)


class TestDistanceDriver(unittest.TestCase):
    """The driver-level half: the bounded read and the measurement lock."""

    def _make(self):
        with patch("app.sensors.distance.distance.DistanceSensor") as sensor_cls, \
             patch("app.sensors.distance.distance.PiGPIOFactory"):
            d = Distance()
        return d, sensor_cls

    def test_sensor_is_constructed_with_partial_true(self):
        # Without partial=True, gpiozero's GPIOQueue.value waits on
        # self.full.wait() with NO timeout, so a silent sensor hangs its reader
        # forever - on the paho network thread that wedges all MQTT handling.
        _, sensor_cls = self._make()
        self.assertIs(sensor_cls.call_args.kwargs["partial"], True)

    def test_measure_once_returns_cm(self):
        d, _ = self._make()
        d.sensor.distance = 0.12
        self.assertAlmostEqual(d.measure_once(), 12.0, places=5)

    def test_measure_once_raises_sensor_busy_when_locked(self):
        d, _ = self._make()
        d._lock.acquire()
        try:
            with self.assertRaises(SensorBusy):
                d.measure_once()
        finally:
            d._lock.release()

    def test_measure_raises_sensor_busy_when_locked(self):
        d, _ = self._make()
        d._lock.acquire()
        try:
            with self.assertRaises(SensorBusy):
                d.measure()
        finally:
            d._lock.release()

    def test_sensor_busy_is_a_measurement_error(self):
        # mqtt.py orders `except SensorBusy` before `except MeasurementError`;
        # callers that only catch the latter must still be covered.
        self.assertTrue(issubclass(SensorBusy, MeasurementError))

    def test_lock_is_released_after_a_successful_measure(self):
        d, _ = self._make()
        d.sensor.distance = 0.10
        d.measure_once()
        self.assertTrue(d._lock.acquire(blocking=False))
        d._lock.release()

    def test_lock_is_released_after_a_failed_measure(self):
        d, _ = self._make()
        type(d.sensor).distance = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("no echo"))
        )
        with self.assertRaises(MeasurementError):
            d.measure_once()
        self.assertTrue(d._lock.acquire(blocking=False))
        d._lock.release()

    def test_measure_holds_the_lock_across_every_sample(self):
        # The whole point of moving the lock up to measure(): another caller
        # must not be able to fire a trigger pulse between two of our reads and
        # have us average in its echo.
        d, _ = self._make()
        seen = []

        def peek(_self):
            seen.append(d._lock.acquire(blocking=False))
            return 0.10

        type(d.sensor).distance = property(peek)
        d.measure()
        self.assertEqual(len(seen), 10)
        self.assertTrue(all(held is False for held in seen))

    def test_measure_returns_the_median_of_its_samples(self):
        d, _ = self._make()
        values = iter([0.10] * 5 + [0.20] * 5)
        type(d.sensor).distance = property(lambda _self: next(values))
        self.assertAlmostEqual(d.measure(), 15.0, places=5)

    def test_measure_raises_when_no_sample_succeeds(self):
        d, _ = self._make()
        type(d.sensor).distance = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("no echo"))
        )
        with self.assertRaises(MeasurementError):
            d.measure()


if __name__ == "__main__":
    unittest.main()
