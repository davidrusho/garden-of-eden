"""Tests that a light command is actually RECORDED, and that nothing else is.

Why this file exists. `light.py` logged every command through the bare root
shim (`logging.info(...)`), and `mqtt.py` configures the root at WARNING - so
the records were discarded before reaching a handler. The effect was total
rather than partial: no light command had ever been logged, correct or
otherwise. It surfaced on 2026-07-31 when the grow light asserted 50% at 20:08,
over an hour after its 19:00 scheduled off, and nothing in the system could say
what commanded it. The Zigbee plug metered 55W for 28s, so the event was real.

Two halves, and BOTH must hold - a blanket `logging.basicConfig(level=INFO)`
would satisfy the first and fail the second, which is precisely the wrong fix
for an SD card that is the single copy of this deployment:

  1. a light COMMAND emits a record even though the root logger is at WARNING
  2. the per-cycle READ path and unrelated modules stay silent at INFO

The root-at-WARNING configuration is reproduced here rather than imported,
because mqtt.py cannot be imported without paho/gpiozero/pigpio.
"""
import importlib
import importlib.util
import io
import logging
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.append(_REPO_ROOT)

_LIGHT_NAME = "app.sensors.light.light"


def _load_real_light_module():
    """Import the REAL light.py, without dragging in flask or a Pi.

    `app/__init__.py` imports flask, so the `app.*` packages are stubbed to keep
    it from executing; the leaf module is then loaded from its file under its
    true dotted name. The name matters beyond tidiness - it IS the logger name
    under test, so loading it as anything else would test the wrong logger.

    EVERY STUB IS WITHDRAWN BEFORE THIS RETURNS. They are only needed while the
    exec_module() below runs, and leaving them installed breaks every later
    module `python -m unittest` discovers: a stub `app` with an empty __path__
    turns tests/test_pump.py's honest "No module named 'flask'" into
    "No module named 'app.sensors.pump'", which reads as a missing package
    rather than as this file's doing. The `pigpio` stub is the same hazard on
    the Pi, where that module is real.
    """
    displaced = {}

    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        displaced.setdefault(name, sys.modules.get(name))
        sys.modules[name] = m
        return m

    mod("gpiozero", PWMLED=MagicMock(), Button=MagicMock(),
        DistanceSensor=MagicMock())
    mod("gpiozero.pins", __path__=[])
    mod("gpiozero.pins.pigpio", PiGPIOFactory=MagicMock())
    mod("pigpio", pi=MagicMock())

    mod("app", __path__=[])
    mod("app.sensors", __path__=[])
    mod("app.sensors.light", __path__=[])

    path = os.path.join(_REPO_ROOT, "app", "sensors", "light", "light.py")
    spec = importlib.util.spec_from_file_location(_LIGHT_NAME, path)
    module = importlib.util.module_from_spec(spec)
    displaced.setdefault(_LIGHT_NAME, sys.modules.get(_LIGHT_NAME))
    sys.modules[_LIGHT_NAME] = module
    try:
        spec.loader.exec_module(module)
    finally:
        for name, previous in displaced.items():
            if previous is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous
    return module


light_mod = _load_real_light_module()
Light = light_mod.Light


class LightLoggingTestCase(unittest.TestCase):
    """Reproduces mqtt.py's root-at-WARNING setup around each test."""

    def setUp(self):
        self.stream = io.StringIO()
        self.handler = logging.StreamHandler(self.stream)
        self.handler.setFormatter(
            logging.Formatter("%(name)s|%(levelname)s|%(message)s")
        )

        self.root = logging.getLogger()
        self._saved_handlers = self.root.handlers[:]
        self._saved_level = self.root.level

        # Exactly what mqtt.py does: replace handlers, pin the root at WARNING.
        self.root.handlers = [self.handler]
        self.root.setLevel(logging.WARNING)

        # patch.object, NOT patch("dotted.string"). A dotted target is
        # re-resolved through sys.modules at call time, and this module's real
        # light.py is not the only thing that has ever occupied that slot -
        # tests/test_water_interlock.py builds a stub carrying only `Light`,
        # and discovery imports every test module before running any test. Both
        # files now withdraw their stubs at import time, so the collision is
        # gone at source; the object-based patch and the slot reclaim below stay
        # because neither depends on that remaining true.
        self._saved_mod = sys.modules.get(_LIGHT_NAME)
        sys.modules[_LIGHT_NAME] = light_mod

        with patch.object(light_mod, "PWMLED"), \
             patch.object(light_mod, "PiGPIOFactory"), \
             patch.object(light_mod.pigpio, "pi"):
            self.light = Light(18)
            self.light.led.value = 0

        self.stream.truncate(0)
        self.stream.seek(0)

    def tearDown(self):
        self.root.handlers = self._saved_handlers
        self.root.setLevel(self._saved_level)
        if self._saved_mod is None:
            sys.modules.pop(_LIGHT_NAME, None)
        else:
            sys.modules[_LIGHT_NAME] = self._saved_mod

    @property
    def captured(self):
        return self.stream.getvalue()

    # --- control -----------------------------------------------------------

    def test_control_root_warning_is_captured(self):
        """Positive control. If this fails the harness is dead and every other
        result in this file is meaningless - an absence would look like a pass."""
        logging.getLogger().warning("CONTROL_MARKER")
        self.assertIn("CONTROL_MARKER", self.captured)

    def test_control_unrelated_module_at_info_stays_silent(self):
        """Negative control, and the guard against a blanket INFO 'fix'.

        A sibling module left at its default level must NOT emit at INFO. If
        someone 'fixes' the original bug by dropping the root to INFO, this is
        the test that goes red."""
        logging.getLogger("app.sensors.someother.module").info("SHOULD_NOT_APPEAR")
        self.assertNotIn("SHOULD_NOT_APPEAR", self.captured)

    # --- half 1: commands are recorded -------------------------------------

    def test_turn_on_is_recorded(self):
        self.light.led.value = 0
        self.light.on()
        self.assertIn("Turning light on", self.captured)

    def test_turn_off_is_recorded(self):
        self.light.off()
        self.assertIn("Turning light off", self.captured)

    def test_noop_reassert_is_recorded_and_distinguishable(self):
        """A no-op must be distinguishable from a real change.

        NOTE on reach: this exercises the Flask/CLI surface, NOT the MQTT
        service. mqtt.py never calls Light.on() - every path goes through
        set_duty_cycle() or off(), and set_duty_cycle has no idempotence check,
        so a schedule re-assert logs a full 'Setting light duty_cycle to N%'
        unconditionally. Kept because the Flask app and bin/light.sh do reach
        it; do not cite it as evidence about the service's re-assert path."""
        self.light.led.value = 0.5
        self.light.on()
        self.assertIn("Light already on, skipping", self.captured)
        self.assertNotIn("Turning light on", self.captured)

    def test_brightness_change_records_the_value(self):
        """The 20:08 event was a brightness assert, not an on/off - the level
        itself has to appear or the record cannot identify it."""
        self.light.set_brightness(50)
        self.assertIn("Setting light duty_cycle to 50%", self.captured)

    def test_command_record_names_the_module(self):
        """Attribution needs a source, not just a message."""
        self.light.off()
        self.assertIn("app.sensors.light.light", self.captured)

    # --- half 2: the read path stays quiet ---------------------------------

    def test_read_path_is_not_recorded_at_info(self):
        """The read path must not add noise to the command record.

        NOTE: an earlier draft justified this as 'runs on every publish and
        every camera capture'. That is false - publish_light_state is called
        from exactly four event-driven places and no publisher thread touches
        the light, so there is no periodic floor. The real justification is
        signal-to-noise around the four meaningful light events per day."""
        self.light.led.value = 0.5
        self.light.get_brightness()
        self.assertNotIn("duty_cycle is", self.captured)

    def test_read_path_is_still_available_at_debug(self):
        """Demoted, not deleted - it must still be reachable when wanted."""
        self.handler.setLevel(logging.DEBUG)
        light_mod.logger.setLevel(logging.DEBUG)
        try:
            self.light.led.value = 0.5
            self.light.get_brightness()
            self.assertIn("duty_cycle is", self.captured)
        finally:
            light_mod.logger.setLevel(logging.INFO)
            self.handler.setLevel(logging.NOTSET)

    # --- the policy itself -------------------------------------------------

    def test_module_sets_its_own_level_at_import(self):
        """The level must be module-owned. Set in mqtt.py instead, it would be
        unreachable from this suite and could regress unnoticed."""
        self.assertEqual(light_mod.logger.level, logging.INFO)

    def test_policy_is_reapplied_on_a_fresh_import(self):
        """Re-executing the module from source must re-apply the policy.

        Guards the case where the level is set once by some caller rather than
        by the module itself: reset the logger, execute the file again, and the
        level has to come back on its own."""
        logging.getLogger(_LIGHT_NAME).setLevel(logging.NOTSET)
        self.assertEqual(logging.getLogger(_LIGHT_NAME).level, logging.NOTSET)

        reloaded = _load_real_light_module()
        self.assertEqual(reloaded.logger.level, logging.INFO)

    def test_no_bare_root_logging_calls_remain(self):
        """The original bug in its general form: any `logging.info(...)` in this
        module goes to the root logger and is discarded at WARNING."""
        import inspect
        import re

        source = inspect.getsource(light_mod)
        offenders = re.findall(r"^\s*logging\.(?:info|debug|warning|error)\(",
                               source, re.MULTILINE)
        self.assertEqual(offenders, [], f"bare root logging calls: {offenders}")


if __name__ == "__main__":
    unittest.main()
