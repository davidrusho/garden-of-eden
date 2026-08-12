"""Tests for the reservoir plausibility band, the fail-closed water state, and
the pump interlock.

These run without a Pi and without gpiozero. mqtt.py builds hardware objects at
import time, so the hardware and broker modules are stubbed in sys.modules
before it is imported.

Why this file exists at all: the reservoir ultrasonic on the unit these tests
were written for cannot produce a usable reading, so a live system can only ever
exercise the untrustworthy branch. Every threshold comparison and every
trustworthy-reading path is reachable here and nowhere else. T-475 made that
permanent rather than temporary - the fitted DYP-A01A's 28 cm dead zone covers
the entire plausibility band - which raises this file from useful to the only
place the interlock is ever exercised at all.

T-475 withdrew the seven MQTT entities that were dead by hardware fact, and
that removed the PUBLISHING these tests used to observe. The classes that
scored a publisher are gone with it; TestPumpInterlock is deliberately
UNCHANGED, and its passing unmodified is the evidence that retiring the
entities did not retire the decisions. The withdrawal itself is scored by
tests/test_retired_entities.py.
"""

import json
import logging
import sys
import os
import types
import unittest
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(_REPO_ROOT)


# --- stub the hardware and broker modules mqtt.py imports at module scope ---
#
# THE STUBS ARE WITHDRAWN AGAIN AS SOON AS `import mqtt` HAS RUN. They only have
# to exist while that import executes; mqtt.py binds every name it needs with
# `from X import Y` at module scope, so the module object goes on working once
# sys.modules has been put back.
#
# Leaving them in place is a whole-suite regression rather than an untidiness.
# `python -m unittest` - the invocation the README documents - discovers modules
# alphabetically, and this file is pulled in early by tests/test_camera_quality.py
# ("c" sorts before "d", "l" and "p"). Every later module that wants a REAL
# app.sensors.* then gets a MagicMock instead, and it does not fail as a missing
# import: tests/test_distance.py turned one honest ImportError into six
# `InvalidSpecError: Cannot autospec attr 'DistanceSensor' ... already mocked`.
#
# _install_stubs() records everything it displaces, including the modules
# the import machinery loaded WHILE the stubs were active (mqtt itself, and the
# real distance driver, which is bound to a mocked gpiozero and must not be
# handed to a later test as if it were clean).

_STUB_ROOTS = ("gpiozero", "paho", "app", "config", "mqtt")

# name -> the sys.modules entry displaced by the stub install, or None if the
# name was not present. Populated by _install_stubs(), consumed by
# _withdraw_stubs().
_displaced = {}


def _owned(name):
    return name.split(".")[0] in _STUB_ROOTS


def _install_stubs():
    # A snapshot rather than only the names mod() sets: importing mqtt drags in
    # submodules nobody stubbed, and those are the ones a later test would
    # silently inherit.
    #
    # Then EVICT them. Overwriting the stubbed names is not enough: `mqtt` and
    # the real distance driver are never mod()-ed, so if either is already in
    # sys.modules - tests/test_api.py sorts first and imports the app tree on
    # any host with flask - the `import mqtt` below is a no-op that hands back
    # the pre-existing module and the whole stub apparatus is bypassed with no
    # error. The file's promise of a config independent of the developer's .env
    # would then quietly not hold.
    _displaced.clear()
    for name, module in list(sys.modules.items()):
        if _owned(name):
            _displaced[name] = module
            del sys.modules[name]

    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        # sys.modules.get(name), not None: mod() is only ever called on names
        # under _STUB_ROOTS today, which the loop above has already recorded -
        # but a stub added under some other root would otherwise be recorded as
        # "was absent" and DELETED at withdrawal instead of restored. The
        # correctness of the restore should not depend on a list elsewhere.
        _displaced.setdefault(name, sys.modules.get(name))
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
    # above). Stubbing it would mean mqtt.py caught a fake MeasurementError
    # that no real code path can raise - the interlock tests would then prove
    # nothing about the exception the driver actually throws.
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
        LOWER_CAMERA_RESOLUTION="640x480",
        # Distinct per camera on purpose: a stub that gave both the same number
        # could not tell "each camera got its own quality" apart from "one
        # value was used twice".
        UPPER_CAMERA_JPEG_QUALITY=85, LOWER_CAMERA_JPEG_QUALITY=70,
        IMAGE_INTERVAL_SECONDS=3600,
    )


def _withdraw_stubs():
    """Put sys.modules back exactly as _install_stubs() found it.

    Anything under a stubbed root that was NOT there beforehand is dropped -
    that covers `mqtt` and the distance driver, both of which were imported
    against mocked hardware and would poison the next test module.

    The sweep is scoped to _STUB_ROOTS on purpose. Deleting every name the
    import added would also evict stdlib modules this import happened to be the
    first to load, and `mqtt_mod` still holds references to those - a later
    `import math` would then build a SECOND math module rather than reuse the
    one the code under test is bound to. tests/test_suite_isolation.py measures
    the full delta and requires everything outside these roots to be stdlib, so
    the narrower sweep is checked rather than assumed.
    """
    for name in [n for n in sys.modules if _owned(n)]:
        if name not in _displaced:
            del sys.modules[name]
    for name, module in _displaced.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


_install_stubs()
import mqtt as mqtt_mod  # noqa: E402
# Imported as a MODULE, not only for its names: the driver-level tests patch
# attributes on it, and a string target ("app.sensors.distance.distance.sleep")
# would go back through sys.modules - which no longer holds it once the stubs
# are withdrawn, so it would re-import the real driver against real gpiozero and
# patch a different object from the one mqtt.py is using.
import app.sensors.distance.distance as distance_mod  # noqa: E402
from app.sensors.distance.distance import (  # noqa: E402
    Distance, MeasurementError,
)
_withdraw_stubs()

_MeasurementError = MeasurementError


class _ExceptionRecorder(logging.Handler):
    """Collects log records that carry an exception, i.e. came from a catch-all."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        if record.exc_info:
            self.records.append(record)


class WaterTestBase(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.sensor = MagicMock()
        # Non-zero by default: "the sampler has produced readings" is the
        # normal state, and safe_distance_measure() short-circuits on 0.
        self.sensor.sample_count.return_value = 9
        mqtt_mod.distance_sensor = self.sensor
        mqtt_mod.client = self.client
        mqtt_mod.pump = MagicMock()
        # Real number, not a bare Mock. on_message's pump/speed/set branch and
        # toggle_pump() both compare get_speed() against 0, and a Mock there
        # raises a TypeError that the catch-all swallows - which would let
        # these tests pass while the code they exercise never runs. (The
        # original reason named publish_pump_state(), which T-475 deleted; the
        # two remaining comparisons keep the requirement alive.)
        mqtt_mod.pump.get_speed.return_value = 0
        mqtt_mod.light = MagicMock()
        mqtt_mod.light.get_brightness.return_value = 0
        mqtt_mod.WATER_LOW_CM = 11.0
        mqtt_mod.WATER_VALID_MIN_CM = 3.0
        mqtt_mod.WATER_VALID_MAX_CM = 25.0
        # flash_lights spawns a thread; the interlock's decision is what matters.
        self._flash = patch.object(mqtt_mod, "flash_lights").start()
        self.addCleanup(patch.stopall)

        # on_message and the publish loops wrap everything in a catch-all that
        # logs and continues. In production that is right; in a test it means a
        # TypeError from a badly-built mock gets PRINTED while every assertion
        # still passes and the code under test never ran. That has already
        # happened twice here. Records carrying exc_info come from those
        # catch-alls; a plain logger.error (a refused threshold, say) does not.
        self._swallowed = _ExceptionRecorder()
        logging.getLogger("mqtt").addHandler(self._swallowed)
        self.addCleanup(logging.getLogger("mqtt").removeHandler, self._swallowed)
        self.addCleanup(self._assert_nothing_was_swallowed)

    def _assert_nothing_was_swallowed(self):
        if self._swallowed.records:
            first = self._swallowed.records[0]
            raise AssertionError(
                "an exception was swallowed by a catch-all during this test - "
                "the code under test may never have run: "
                f"{first.getMessage()}"
            )

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
        self.sensor.measure_once.return_value = 12.5
        self.assertEqual(mqtt_mod.safe_distance_measure(), 12.5)

    def test_latched_near_zero_reading_is_rejected(self):
        # The exact value this unit reported for days with no sensor attached.
        self.sensor.measure_once.return_value = 0.09
        self.assertIsNone(mqtt_mod.safe_distance_measure())

    def test_out_of_range_reading_is_rejected(self):
        # Also observed on the same dead sensor. Note it is ABOVE the low-water
        # threshold, so a bare comparison would call it "water low" - the same
        # garbage produces opposite alarms depending only on where it latched.
        self.sensor.measure_once.return_value = 83.0
        self.assertIsNone(mqtt_mod.safe_distance_measure())

    def test_zero_reading_is_rejected(self):
        # partial=True returns 0.0 from an empty queue rather than blocking.
        self.sensor.measure_once.return_value = 0.0
        self.assertIsNone(mqtt_mod.safe_distance_measure())

    def test_band_edges_are_inclusive(self):
        for edge in (3.0, 25.0):
            with self.subTest(edge=edge):
                self.sensor.measure_once.return_value = edge
                self.assertEqual(mqtt_mod.safe_distance_measure(), edge)

    def test_just_outside_band_is_rejected(self):
        for value in (2.99, 25.01):
            with self.subTest(value=value):
                self.sensor.measure_once.return_value = value
                self.assertIsNone(mqtt_mod.safe_distance_measure())

    def test_no_samples_yet_yields_no_reading_without_measuring(self):
        # "Not ready" is distinct from "implausible": the sampler has produced
        # nothing, so the 0.0 it would return means nothing at all.
        self.sensor.sample_count.return_value = 0
        self.assertIsNone(mqtt_mod.safe_distance_measure())
        self.sensor.measure_once.assert_not_called()

    def test_measurement_error_yields_no_reading(self):
        self.sensor.measure_once.side_effect = _MeasurementError("boom")
        self.assertIsNone(mqtt_mod.safe_distance_measure())


# TestFailClosedPublishing stood here (T-475).
#
# All seven of its tests scored refresh_water_state() and
# update_water_low_state(): that an untrustworthy reading published "offline"
# and no level, that a trustworthy one published level then trust in that
# order, that the binary sensor never fell back to OFF. Every one of those
# functions published to a topic behind an entity that no longer exists, and
# all three functions were deleted with the entities.
#
# This is a deletion because the BEHAVIOUR was deliberately removed, not
# because the tests started failing. The invariant underneath them - that an
# unreadable reservoir is never treated as a safe one - was never a property of
# the publishing. It is a property of safe_distance_measure() returning None,
# which TestPlausibilityBand above still scores in full, and of start_pump()
# refusing on that None, which TestPumpInterlock below still scores unchanged.


class TestPumpInterlock(WaterTestBase):
    def test_refuses_to_start_on_untrustworthy_reading(self):
        self.sensor.measure_once.return_value = 0.09
        self.assertFalse(mqtt_mod.start_pump(100, self.client))
        mqtt_mod.pump.set_speed.assert_not_called()

    def test_refuses_to_start_when_water_is_low(self):
        self.sensor.measure_once.return_value = 20.0
        self.assertFalse(mqtt_mod.start_pump(100, self.client))
        mqtt_mod.pump.set_speed.assert_not_called()

    def test_starts_when_water_is_present(self):
        self.sensor.measure_once.return_value = 8.0
        self.assertTrue(mqtt_mod.start_pump(100, self.client))
        mqtt_mod.pump.set_speed.assert_called_once_with(100)

    def test_starts_unconditionally_when_checking_disabled(self):
        mqtt_mod.WATER_LOW_CM = None
        self.sensor.measure_once.return_value = 0.09
        self.assertTrue(mqtt_mod.start_pump(100, self.client))
        mqtt_mod.pump.set_speed.assert_called_once_with(100)

    def _send(self, topic_suffix, payload):
        msg = MagicMock()
        msg.topic = f"gardyn/{topic_suffix}"
        msg.payload = payload.encode()
        mqtt_mod.on_message(self.client, None, msg)

    def test_pump_command_on_path_is_interlocked(self):
        self.sensor.measure_once.return_value = 0.09
        self._send("pump/command", "ON")
        mqtt_mod.pump.set_speed.assert_not_called()

    def test_pump_speed_set_path_is_interlocked(self):
        # This path had no water check at all before T-472.
        self.sensor.measure_once.return_value = 0.09
        self._send("pump/speed/set", "80")
        mqtt_mod.pump.set_speed.assert_not_called()

    def test_pump_speed_set_still_starts_when_water_is_present(self):
        self.sensor.measure_once.return_value = 8.0
        self._send("pump/speed/set", "80")
        mqtt_mod.pump.set_speed.assert_called_once_with(80)

    def test_pump_speed_zero_stops_without_measuring(self):
        self._send("pump/speed/set", "0")
        mqtt_mod.pump.off.assert_called_once()
        self.sensor.measure_once.assert_not_called()

    def test_button_double_press_path_is_interlocked(self):
        # The physical button also had no water check before T-472.
        self.sensor.measure_once.return_value = 0.09
        mqtt_mod.pump.get_speed.return_value = 0
        mqtt_mod.toggle_pump()
        mqtt_mod.pump.set_speed.assert_not_called()

    def test_button_double_press_starts_when_water_is_present(self):
        self.sensor.measure_once.return_value = 8.0
        mqtt_mod.pump.get_speed.return_value = 0
        mqtt_mod.toggle_pump()
        mqtt_mod.pump.set_speed.assert_called_once()

    def test_button_can_always_stop_a_running_pump(self):
        # Stopping must never be gated on a sensor reading.
        self.sensor.measure_once.return_value = 0.09
        mqtt_mod.pump.get_speed.return_value = 50
        mqtt_mod.toggle_pump()
        mqtt_mod.pump.off.assert_called_once()


class TestThresholdValidation(WaterTestBase):
    """The threshold arrives from an MQTT topic any client can publish to."""

    def _set_threshold(self, payload):
        msg = MagicMock()
        msg.topic = "gardyn/water/low/cm/set"
        msg.payload = payload.encode()
        mqtt_mod.on_message(self.client, None, msg)

    def test_non_finite_threshold_is_refused(self):
        # The sharp one: nan parses, is not in (None, 0) so the mode publishes
        # "Enabled", and then `distance > nan` is False for EVERY reading - the
        # alarm never fires and start_pump() starts the pump every time. A
        # fail-open on a safety interlock.
        for payload in ("nan", "inf", "-inf"):
            with self.subTest(payload=payload):
                mqtt_mod.WATER_LOW_CM = 11.0
                self._set_threshold(payload)
                self.assertEqual(mqtt_mod.WATER_LOW_CM, 11.0)

    def test_non_finite_threshold_does_not_disarm_the_pump_interlock(self):
        self._set_threshold("nan")
        self.sensor.measure_once.return_value = 20.0  # water low
        self.assertFalse(mqtt_mod.start_pump(100, self.client))
        mqtt_mod.pump.set_speed.assert_not_called()

    def test_threshold_outside_the_band_is_refused(self):
        # Below the band minimum no valid reading can ever be under it, so the
        # alarm pins on and the pump is refused forever; above the maximum it
        # can never be reached and the alarm pins off. Both look configured.
        for payload in ("1.0", "30.0"):
            with self.subTest(payload=payload):
                mqtt_mod.WATER_LOW_CM = 11.0
                self._set_threshold(payload)
                self.assertEqual(mqtt_mod.WATER_LOW_CM, 11.0)

    def test_a_refused_threshold_leaves_the_interlock_on_the_old_value(self):
        # Was test_refused_threshold_is_re_asserted_to_ha, which asserted the
        # rejected value was echoed back to HA's number entity so the slider
        # could not sit showing something the interlock was not using. That
        # entity is retired (T-475) and nothing is echoed anywhere. What the
        # rejection has to achieve is unchanged and is what is asserted now:
        # the threshold in force does not move.
        self._set_threshold("nan")
        self.assertEqual(mqtt_mod.WATER_LOW_CM, 11.0)
        self.assertNotIn("gardyn/water/low/cm", self.topics())

    def test_valid_threshold_is_applied(self):
        # The publish assertion is gone with the number entity; the assignment
        # it accompanied is the half that arms the interlock, and is unchanged.
        self.sensor.measure_once.return_value = 8.0
        self._set_threshold("9.5")
        self.assertEqual(mqtt_mod.WATER_LOW_CM, 9.5)

    def test_zero_disables_the_threshold(self):
        # Was test_zero_disables_and_is_published_as_zero. The publishing half
        # existed because a retained stale number would have left HA's slider
        # disagreeing with the interlock - a problem that cannot arise once
        # there is no slider. The state change is what still matters, and it is
        # the sharp one: disabled means start_pump() stops checking at all.
        self.sensor.measure_once.return_value = 8.0
        self._set_threshold("0")
        self.assertIsNone(mqtt_mod.WATER_LOW_CM)
        self.assertNotIn("gardyn/water/low/mode", self.topics())

    def test_unparseable_threshold_is_ignored(self):
        self._set_threshold("banana")
        self.assertEqual(mqtt_mod.WATER_LOW_CM, 11.0)


# TestConnectSequencing stood here (T-475) and now lives, rewritten, in
# tests/test_retired_entities.py.
#
# Its three tests scored the retained water TRUST topic: that it was retracted
# before the device was announced online, that a reconnect re-earned it with a
# fresh measurement, and that an exploding sensor could not leave the publisher
# threads unstarted. gardyn/water/status backed the water entities' availability
# list; with those entities gone nothing subscribes to it, and on_connect no
# longer touches the reservoir at all.
#
# The successor tests keep the same shape and the same worry - assert ordering
# by observed call sequence, and prove the connect path REACHES ITS END rather
# than only that something is absent - applied to the sequence that replaced it:
# clear the retired topics, then announce, and never the other way round.


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

    # Five tests scoring the water entities' two-topic `availability` LIST stood
    # here (T-475): that it named gardyn/status and gardyn/water/status in
    # order, that the payload keys were stated per entry rather than at the top
    # level, that water_level carried expire_after, that the threshold entity's
    # range reached the top of the plausibility band, and that neither entity
    # also set availability_topic. All five described entities that no longer
    # exist, and the compound availability config went with them.

    def test_every_surviving_entity_uses_the_single_topic_form(self):
        # Every entity left answers to the controller's liveness alone. Stated
        # over ALL four rather than a sample, so an entity that quietly grew a
        # second availability condition - or lost the topic entirely, which
        # would leave HA showing its last value forever after a dead Pi - is
        # caught here.
        for fragment in ("_light/config", "_pcb_temp/config",
                         "_upper_camera/config", "_lower_camera/config"):
            with self.subTest(entity=fragment):
                cfg = self._config(fragment)
                self.assertEqual(cfg["availability_topic"], "gardyn/status")
                self.assertEqual(cfg["payload_available"], "online")
                self.assertEqual(cfg["payload_not_available"], "offline")
                self.assertNotIn("availability", cfg)


class TestDistanceDriver(unittest.TestCase):
    """Driver-level behaviour: the bounded read and the sample-count signal."""

    def _make(self):
        with patch.object(distance_mod, "DistanceSensor") as sensor_cls, \
             patch.object(distance_mod, "PiGPIOFactory"):
            d = Distance()
        return d, sensor_cls

    def test_sensor_is_constructed_with_partial_true(self):
        # Weak by necessity - gpiozero is stubbed here, so this asserts the
        # kwarg is passed, not that the real queue stops blocking. It would
        # still pass if gpiozero renamed the parameter. The behaviour it guards
        # is verified against the pinned library source, not here: with
        # partial=False, GPIOQueue.value waits on full.wait() with no timeout,
        # and on the shared pigpio callback thread that self-deadlocks.
        _, sensor_cls = self._make()
        self.assertIs(sensor_cls.call_args.kwargs["partial"], True)

    def test_measure_once_returns_cm(self):
        d, _ = self._make()
        d.sensor.distance = 0.12
        self.assertAlmostEqual(d.measure_once(), 12.0, places=5)

    def test_measure_once_raises_on_hardware_failure(self):
        d, _ = self._make()
        type(d.sensor).distance = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("no echo"))
        )
        with self.assertRaises(MeasurementError):
            d.measure_once()

    def test_sample_count_reports_queue_depth(self):
        d, _ = self._make()
        d.sensor._queue.queue = [0.1, 0.1, 0.1]
        self.assertEqual(d.sample_count(), 3)

    def test_sample_count_is_zero_before_the_sampler_produces_anything(self):
        # The state that matters: a sensor that has never echoed. gpiozero
        # ignores its None readings, so the queue stays empty and .distance
        # would hand back 0.0 - which reads as a FULL tank.
        d, _ = self._make()
        d.sensor._queue.queue = []
        self.assertEqual(d.sample_count(), 0)

    def test_sample_count_degrades_to_none_if_gpiozero_internals_move(self):
        d, _ = self._make()
        d.sensor = object()  # no _queue attribute at all
        self.assertIsNone(d.sample_count())

    def test_measure_samples_over_time_rather_than_back_to_back(self):
        # measure()'s whole value is that it sleeps between reads. Ten
        # back-to-back reads of an already-smoothed rolling median return the
        # same number ten times and average nothing.
        d, _ = self._make()
        d.sensor.distance = 0.10
        with patch.object(distance_mod, "sleep") as slept:
            d.measure(samples=4, interval=0.07)
        self.assertEqual(slept.call_count, 3)  # n-1 gaps, no trailing sleep
        self.assertEqual({c.args[0] for c in slept.call_args_list}, {0.07})

    def test_measure_returns_the_median_of_its_samples(self):
        d, _ = self._make()
        values = iter([0.10] * 5 + [0.20] * 5)
        type(d.sensor).distance = property(lambda _self: next(values))
        with patch.object(distance_mod, "sleep"):
            self.assertAlmostEqual(d.measure(), 15.0, places=5)

    def test_measure_survives_individual_sample_failures(self):
        d, _ = self._make()
        seq = iter([0.10, RuntimeError("no echo"), 0.10])
        def nxt(_self):
            v = next(seq)
            if isinstance(v, Exception):
                raise v
            return v
        type(d.sensor).distance = property(nxt)
        with patch.object(distance_mod, "sleep"):
            self.assertAlmostEqual(d.measure(samples=3), 10.0, places=5)

    def test_measure_raises_when_no_sample_succeeds(self):
        d, _ = self._make()
        type(d.sensor).distance = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("no echo"))
        )
        with patch.object(distance_mod, "sleep"):
            with self.assertRaises(MeasurementError):
                d.measure()


class TestBandConfigValidation(unittest.TestCase):
    """config._load_water_band() is the only guard against a dead sensor
    reading as a full reservoir. It must never accept a minimum of zero, and
    must never raise - mqtt.service restarts forever on an import error."""

    def _load(self, **env):
        import importlib.util
        # Load the real config module from disk under a private name, with a
        # stubbed dotenv so load_dotenv() cannot pull in the developer's .env.
        saved = sys.modules.get("dotenv")
        sys.modules["dotenv"] = types.ModuleType("dotenv")
        sys.modules["dotenv"].load_dotenv = lambda *a, **k: None
        saved_env = {k: os.environ.get(k) for k in
                     ("WATER_VALID_MIN_CM", "WATER_VALID_MAX_CM")}
        try:
            for k, v in env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            for k in saved_env:
                if k not in env:
                    os.environ.pop(k, None)
            spec = importlib.util.spec_from_file_location(
                "_cfg_under_test", os.path.join(_REPO_ROOT, "config.py")
            )
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return m.WATER_VALID_MIN_CM, m.WATER_VALID_MAX_CM
        finally:
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            if saved is None:
                sys.modules.pop("dotenv", None)
            else:
                sys.modules["dotenv"] = saved

    def test_defaults_when_unset(self):
        self.assertEqual(self._load(), (3.0, 25.0))

    def test_valid_override_is_honoured(self):
        self.assertEqual(
            self._load(WATER_VALID_MIN_CM="4.5", WATER_VALID_MAX_CM="22.0"),
            (4.5, 22.0),
        )

    def test_zero_minimum_is_refused(self):
        # THE safety case. A minimum of 0 admits the 0.0 that a never-echoed
        # sensor reports, and 0.0 is the full-tank end of the scale: dead sensor
        # reads "tank full", pump runs dry.
        self.assertEqual(self._load(WATER_VALID_MIN_CM="0"), (3.0, 25.0))

    def test_negative_minimum_is_refused(self):
        self.assertEqual(self._load(WATER_VALID_MIN_CM="-1"), (3.0, 25.0))

    def test_inverted_band_is_refused(self):
        # Would reject every reading, so the pump could never start again.
        self.assertEqual(
            self._load(WATER_VALID_MIN_CM="25", WATER_VALID_MAX_CM="3"),
            (3.0, 25.0),
        )

    def test_non_finite_bound_is_refused(self):
        self.assertEqual(self._load(WATER_VALID_MAX_CM="inf"), (3.0, 25.0))
        self.assertEqual(self._load(WATER_VALID_MIN_CM="nan"), (3.0, 25.0))

    def test_unparseable_value_falls_back_instead_of_raising(self):
        # An exception here is not a loud failure - mqtt.service carries
        # Restart=always with StartLimitIntervalSec=0, so it is a permanent
        # crash loop that takes the lights and cameras down too.
        for bad in ("", "3,0", "abc"):
            with self.subTest(value=bad):
                self.assertEqual(self._load(WATER_VALID_MIN_CM=bad), (3.0, 25.0))


class LoggingPolicyTestCase(unittest.TestCase):
    """Asserts the DEPLOYED logging policy, not a hand copy of it.

    These live here rather than in test_light_logging.py because this module
    already owns the sys.modules stubs and the real `import mqtt` - a second
    stubbing module would fight this one, which is exactly the collision that
    made test_light_logging inert under `unittest discover`.

    Why they exist. A review found the policy in mqtt.py had no coverage at all:
    four separate regressions - including `basicConfig(level=INFO)`, the blanket
    fix a person would most plausibly write - all left the suite green. The
    battery that scored the light module was honest about what it measured, and
    what it measured was light.py only.

    The policy under test is a TRADE, and both halves have to hold:
      * mqtt.py and light.py at INFO, so a command can be attributed
      * the ROOT at WARNING, so everything else stays quiet
    Asserting only the first would pass under a blanket INFO, which is the
    regression these are here to catch.
    """

    LIGHT_LOGGER = "app.sensors.light.light"

    def test_service_logger_is_info_so_commands_are_attributable(self):
        """mqtt.py's own logger carries the inbound decode and the button/pump
        toggles. At WARNING - which is what it shipped as, under a comment
        claiming INFO - a command leaves no record of where it came from."""
        self.assertEqual(logging.getLogger("mqtt").level, logging.INFO)

    def test_light_module_still_owns_its_level(self):
        """Source-level, deliberately.

        This module stubs app.sensors.light.light with a MagicMock, so the real
        light.py never executes here and its logger reads NOTSET - asserting the
        live level in this file would test the stub, not the code. The
        behavioural assertion lives in test_light_logging.py, which loads the
        real module. What is checked here is only that the policy has not been
        DELETED from the source, which the stub cannot mask."""
        path = os.path.join(_REPO_ROOT, "app", "sensors", "light", "light.py")
        with open(path) as fh:
            source = fh.read()
        self.assertIn("logger.setLevel(logging.INFO)", source)

    def test_root_stays_at_warning(self):
        """The half that fails under a blanket fix.

        `basicConfig(level=INFO)` in mqtt.py would satisfy both tests above
        while switching on every periodic publisher and the camera path. This
        is the only assertion that tells the targeted policy apart from the
        lazy one."""
        self.assertEqual(logging.getLogger().level, logging.WARNING)

    def test_periodic_publishers_are_not_at_info(self):
        """The demotions are load-bearing, not cosmetic: they are what buys the
        headroom for raising the service logger. If these return to INFO the
        camera pair alone re-buries the command record."""
        import inspect
        import re

        source = inspect.getsource(mqtt_mod)
        offenders = re.findall(
            r'logger\.info\(f?"(?:Publishing (?:PCB Temperature|Temperature|'
            r'Humidity|Water Level|water low)|Captured\+published)', source)
        self.assertEqual(offenders, [],
                         f"periodic publisher back at INFO: {offenders}")

    def test_inbound_decode_is_recorded(self):
        """on_message's decode is the ONLY record of what arrived on the wire.
        At debug it is invisible, and a replayed queued command then looks
        identical to a fresh one.

        This asserts the LEVEL, which is the property the test is named for.
        The `{msg.topic!r}` in the literal is incidental to that - it is here
        because matching the source needs the current spelling, not because
        this test has an opinion about escaping. T-527.12 changed it from
        `{msg.topic}` and the escaping itself is pinned in
        tests/test_connack_refusal.py, which is where a failure should be read
        from."""
        import inspect

        source = inspect.getsource(mqtt_mod)
        self.assertIn('logger.info(f"Decoded payload on {msg.topic!r}', source)

    def test_logging_is_not_globally_disabled(self):
        """logging.disable() is a module-wide threshold that no logger or
        handler level reflects, so every other assertion here would still pass
        while nothing was emitted at all."""
        self.assertLess(logging.root.manager.disable, logging.INFO)

    def test_handlers_do_not_filter_above_the_logger_levels(self):
        """A handler level would silently undo all of the above.

        basicConfig leaves its handlers at NOTSET; callHandlers consults the
        HANDLER's level, never an ancestor logger's, so a handler raised to
        WARNING suppresses every INFO record while all four assertions above
        still pass."""
        for handler in logging.getLogger().handlers:
            self.assertLessEqual(
                handler.level, logging.INFO,
                f"{handler!r} filters at {handler.level}, above INFO")


if __name__ == "__main__":
    unittest.main()
