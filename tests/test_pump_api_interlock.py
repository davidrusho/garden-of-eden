"""The Flask REST API must offer no way to energise the pump.

The low-water interlock lives in mqtt.py's start_pump(), and it can only live
there: it reads module globals that are bound to the ONE process holding the
GPIO. The Flask app is a different process with its own Pump on the same pin,
so it cannot call start_pump() and cannot obtain the interlock without a second
copy of the decision - two writers of one safety rule, which is worse than the
bypass it would be fixing.

So the pump-START routes are gone, and this file is what keeps them gone. Every
assertion here is about an ABSENCE, which is the shape of test that says least
on its own: a route that was never added passes it for free. That is why the
mutation battery beside it (tests/mutate_pump_api_interlock.py) is built almost
entirely out of mutants that REINTRODUCE a start route rather than break one.

Stopping is deliberately still reachable. A guard that can refuse to start a
pump but cannot stop one is not a safety control, and mqtt.py makes the same
call - see test_button_can_always_stop_a_running_pump in
tests/test_water_interlock.py.

Why this cannot be exercised anywhere but here: the Gardyn's own pump was
replaced by a third-party unit on a smart plug that is not on the network, so
GPIO 24 drives nothing. No live system can show that an HTTP request failed to
start a pump, because no live system has a pump to start. This suite is the
only place the behaviour exists.
"""

import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


# --- stubs ------------------------------------------------------------------
#
# Two independent problems are solved here, and conflating them is how this
# file would silently stop testing anything.
#
# 1. The `app` package imports real hardware drivers at module scope
#    (gpiozero, pigpio, smbus, ina219, board, adafruit_*). They are stubbed
#    UNCONDITIONALLY, including on the Pi. tests/test_api.py does not, and on
#    the Pi that means running the suite builds a real PWMLED on GPIO 24 while
#    mqtt.service holds the same pin - gpiozero's reservation is per-process,
#    so nothing complains and two processes drive the pump. A test must not be
#    able to move hardware.
#
# 2. tests/test_water_interlock.py replaces `app` and its subpackages in
#    sys.modules with stub modules so that mqtt.py can be imported without a
#    Pi. Under `python -m unittest` discovery that runs BEFORE this file
#    (tests.test_camera_quality imports it, and sorts earlier), so by the time
#    this module is imported `from app import create_app` would resolve against
#    a stub with no create_app on it. The stubs are evicted below and the real
#    package re-imported. That helper is left completely alone - it is shared,
#    it is being changed concurrently, and this file must work either side of
#    that change.
#
# Both are done inside a window that is CLOSED again: sys.modules is put back
# exactly as it was found, because this module is imported into the middle of a
# shared process. Leaving the stubs in place changed how tests.test_pump failed
# - its nine errors became nine failures - which is one suite reaching outside
# itself, and it was only noticeable because those tests happened to be red
# already for an unrelated reason. The Flask app is BUILT inside the window and
# handed out as an object, so nothing here re-resolves a name through
# sys.modules after the window closes.

_HARDWARE_STUBS = {
    "gpiozero": dict(Button=MagicMock(), PWMLED=MagicMock(),
                     DistanceSensor=MagicMock(name="DistanceSensor")),
    "gpiozero.pins": dict(__path__=[]),
    "gpiozero.pins.pigpio": dict(PiGPIOFactory=MagicMock()),
    "pigpio": dict(pi=MagicMock()),
    "smbus": dict(SMBus=MagicMock()),
    "ina219": dict(INA219=MagicMock(),
                   DeviceRangeError=type("DeviceRangeError", (Exception,), {})),
    "board": dict(I2C=MagicMock()),
    "adafruit_ahtx0": dict(AHTx0=MagicMock()),
    "adafruit_am2320": dict(AM2320=MagicMock()),
    "adafruit_pct2075": dict(PCT2075=MagicMock()),
}

_ABSENT = object()


def _stub(name, attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod


def _build_app_under_stubs():
    """Return (flask_app, pump_routes, distance_routes), sys.modules unchanged.

    Everything the assertions need is resolved inside the window and handed
    back as objects, so the restore at the end costs nothing.
    """
    touched = list(_HARDWARE_STUBS) + ["config"] + [
        n for n in sys.modules if n == "app" or n.startswith("app.")]
    saved = {name: sys.modules.get(name, _ABSENT) for name in touched}

    try:
        for name, attrs in _HARDWARE_STUBS.items():
            _stub(name, attrs)

        # config is imported by the temperature and humidity drivers. Prefer
        # the real one; fall back to a stub so this suite runs on a machine
        # that has Flask but not the Pi's full requirements (config needs
        # python-dotenv). Whatever another suite already put here is left
        # alone - no assertion below depends on any of its values.
        if "config" not in sys.modules:
            try:
                import config  # noqa: F401
            except Exception:
                _stub("config", dict(SENSOR_TYPE="DHT20"))

        # Drop stub `app.*` entries so the REAL package can be imported. A
        # module built by types.ModuleType has no __file__ and a real one does,
        # which is the whole test - so on a tree where nothing stubbed `app`
        # this removes nothing.
        for name in [n for n in list(sys.modules)
                     if n == "app" or n.startswith("app.")]:
            if getattr(sys.modules[name], "__file__", None) is None:
                del sys.modules[name]

        from app import create_app
        from app.sensors.pump import routes as pump_routes
        from app.sensors.distance import routes as distance_routes

        # Built here, not in setUp: Flask resolves its root path through
        # sys.modules[__name__] at construction time, and after the restore
        # below that name points back at whatever stub was there.
        flask_app = create_app("default")
        flask_app.config["TESTING"] = True
        return flask_app, pump_routes, distance_routes
    finally:
        # Anything the real import ADDED goes first; the saved entries then go
        # back on top. Restoring only `saved` would leave a real `app.*` tree
        # behind on a process that had none, which is the same contamination
        # in the other direction.
        for name in [n for n in list(sys.modules)
                     if n == "app" or n.startswith("app.")]:
            if name not in saved:
                del sys.modules[name]
        for name, previous in saved.items():
            if previous is _ABSENT:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


flask_app, pump_routes, distance_routes = _build_app_under_stubs()


# The reading this unit really latched at for days with no sensor attached.
# Inside no plausibility band, outside every threshold - the canonical
# untrustworthy reservoir reading, and the one start_pump() refuses on.
UNTRUSTWORTHY_CM = 0.09

# Every method by which a Pump object can begin to move water. Asserting on the
# named methods rather than on a single call count is deliberate: a route that
# reached the pump through some other method would otherwise pass.
_START_METHODS = ("on", "set_speed", "set_duty_cycle")


class PumpApiTestBase(unittest.TestCase):
    def setUp(self):
        self.app = flask_app
        self.client = self.app.test_client()

        # Replace the module-level Pump so nothing under test can reach even a
        # stubbed PWMLED, and so every call it receives is recorded.
        self.pump = MagicMock(name="pump_control")
        self.pump.get_speed.return_value = 0
        patcher = patch.object(pump_routes, "pump_control", self.pump)
        patcher.start()
        self.addCleanup(patcher.stop)

        # An untrustworthy reservoir reading, in place for every test in this
        # file. Nothing served over HTTP may start the pump on it.
        self.distance = MagicMock(name="distance_control")
        self.distance.measure_once.return_value = UNTRUSTWORTHY_CM
        self.distance.sample_count.return_value = 9
        dist_patcher = patch.object(
            distance_routes, "distance_control", self.distance)
        dist_patcher.start()
        self.addCleanup(dist_patcher.stop)

    def assertPumpDidNotStart(self):
        for method in _START_METHODS:
            self.assertFalse(
                getattr(self.pump, method).called,
                f"the pump was started via pump_control.{method}() on an "
                f"untrustworthy reservoir reading of {UNTRUSTWORTHY_CM}cm",
            )

    def pump_post_rules(self):
        """Every URL rule under /pump that accepts POST, as (rule, methods)."""
        found = []
        for rule in self.app.url_map.iter_rules():
            if not str(rule).startswith("/pump"):
                continue
            # HEAD and OPTIONS are added by Werkzeug, never by a blueprint.
            methods = sorted(set(rule.methods) - {"HEAD", "OPTIONS"})
            if "POST" in methods:
                found.append((str(rule), methods))
        return sorted(found)


class TestNoHttpRouteStartsThePump(PumpApiTestBase):
    def test_post_pump_on_does_not_start_the_pump(self):
        response = self.client.post("/pump/on")
        self.assertNotIn(
            response.status_code, range(200, 300),
            "POST /pump/on answered successfully - a pump-start route is live",
        )
        self.assertPumpDidNotStart()

    def test_post_pump_speed_does_not_start_the_pump(self):
        response = self.client.post("/pump/speed", json={"value": 100})
        self.assertNotIn(
            response.status_code, range(200, 300),
            "POST /pump/speed answered successfully - a pump-start route is live",
        )
        self.assertPumpDidNotStart()

    def test_no_post_route_under_pump_except_off(self):
        """The invariant, and the only assertion that survives a rename.

        The two tests above name the routes that existed when this was written.
        A start route added back under any other path, or bolted onto an
        existing rule by widening its methods list, would pass both of them.
        This enumerates what the app actually serves.
        """
        self.assertEqual(
            self.pump_post_rules(),
            [("/pump/off", ["POST"])],
            "the pump blueprint serves a POST route other than /pump/off; "
            "the REST API has no access to the low-water interlock, so any "
            "route it exposes that can energise the pump is un-interlocked",
        )


class TestStoppingAndReadingStillWork(PumpApiTestBase):
    """Over-deletion is a real failure too, and it looks like success here.

    Removing the whole blueprint would satisfy every assertion in the class
    above. These fix the other edge: stopping must never be gated on a sensor,
    and the read-only endpoints have nothing to do with the interlock.
    """

    def test_pump_off_is_still_reachable_on_an_untrustworthy_reading(self):
        response = self.client.post("/pump/off")
        self.assertEqual(response.status_code, 200)
        # The body is asserted, not just the status. Without it a stop route
        # that answers 200 with anything at all passes, and this suite's
        # mutation battery proved the gap: its CONTROL B breaks exactly this
        # string, and scored GREEN until the assertion below was added.
        self.assertEqual(response.get_json(), {"message": "Pump turned off!"})
        self.pump.off.assert_called_once()
        self.assertPumpDidNotStart()

    def test_pump_speed_is_still_readable(self):
        self.pump.get_speed.return_value = 75
        response = self.client.get("/pump/speed")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"value": 75})
        self.assertPumpDidNotStart()

    def test_pump_stats_is_still_readable(self):
        with patch.object(pump_routes, "fetch_ina219_data") as fetch:
            fetch.return_value = {"BusVoltage": 12.5}
            response = self.client.get("/pump/stats")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"BusVoltage": 12.5})
        self.assertPumpDidNotStart()


if __name__ == "__main__":
    unittest.main()
