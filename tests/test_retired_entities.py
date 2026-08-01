"""Tests for the withdrawal of seven hardware-dead entities (T-475).

The change these score is almost entirely a DELETION, which makes the suite
unusually easy to get wrong in a way that looks fine:

  * Every assertion of the form "topic X is no longer published" is satisfied
    by a function that raises before publishing anything at all. A NameError
    inside send_discovery_messages(), or anywhere in on_connect ahead of it,
    would take the FOUR SURVIVING entities down with the seven retired ones -
    silently, and with every absence-assertion still green. So the survivors
    are asserted positively, end-to-end, and that is the load-bearing half of
    this file.
  * A suite that merely tolerates an absence will not notice the absent thing
    coming back. Every topic list below is written out as a LITERAL rather than
    read from mqtt.RETIRED_* - a test that imports the constant it is checking
    proves only that the module agrees with itself, and would pass happily
    after someone deleted an entry from it.

Stubs come from tests.test_water_interlock rather than being re-installed here.
That module already owns the sys.modules hardware stubs and the real
`import mqtt`; a second stubbing module fights the first, which is what made
test_light_logging inert under `unittest discover` once before.

Run:  python3 -m unittest tests.test_retired_entities
"""

import logging
import unittest
from unittest.mock import MagicMock, patch

from tests.test_water_interlock import mqtt_mod

# IDENTIFIER as the stubbed config sets it. Written out rather than imported,
# for the same reason the topic lists are.
ID = "gardyn-xx"

RETIRED_DISCOVERY = [
    f"homeassistant/light/gardyn/{ID}_pump/config",
    f"homeassistant/sensor/gardyn/{ID}_water_level/config",
    f"homeassistant/binary_sensor/gardyn/{ID}_water_low/config",
    f"homeassistant/number/gardyn/{ID}_water_low_cm/config",
    f"homeassistant/sensor/gardyn/{ID}_water_low_mode/config",
    f"homeassistant/sensor/gardyn/{ID}_temperature/config",
    f"homeassistant/sensor/gardyn/{ID}_humidity/config",
]

# The four that WORK and must keep working: the grow light, the PCB thermometer
# (PCT2075 at 0x48), and both cameras.
SURVIVING_DISCOVERY = [
    f"homeassistant/light/gardyn/{ID}_light/config",
    f"homeassistant/sensor/gardyn/{ID}_pcb_temp/config",
    f"homeassistant/image/gardyn/{ID}_upper_camera/config",
    f"homeassistant/image/gardyn/{ID}_lower_camera/config",
]

# Every state topic the retired entities were fed from that was published with
# retain=True. Verified one by one against `grep -n client.publish mqtt.py`:
# a retained message outlives the code that wrote it, so deleting the publisher
# is not what removes it from the broker.
RETIRED_RETAINED_STATE = [
    "gardyn/pump/state",
    "gardyn/pump/speed/state",
    "gardyn/water/level",
    "gardyn/water/low/state",
    "gardyn/water/low/cm",
    "gardyn/water/low/mode",
    "gardyn/water/status",
]

# NOT in the list above, deliberately. Both publishers omitted retain= and paho
# defaults it to False, so no retained message was ever created for either - and
# the full history of mqtt.py contains no version that retained them. Only their
# discovery configs need clearing.
NEVER_RETAINED_STATE = ["gardyn/temperature", "gardyn/humidity"]

# Topics belonging to entities that survive. Nothing in the clear may touch
# these: an over-broad clear would withdraw the working half of the unit, and
# every "the retired topics are gone" assertion in this file would still pass.
SURVIVING_STATE = [
    "gardyn/light/state",
    "gardyn/light/brightness/state",
    "gardyn/pcb/temperature",
    "gardyn/status",
    "gardyn/image/upper_camera",
    "gardyn/image/lower_camera",
]


class Publication:
    """One recorded publish, with retain resolved the way paho would."""

    def __init__(self, topic, payload, qos, retain):
        self.topic = topic
        self.payload = payload
        self.qos = qos
        self.retain = retain

    def __repr__(self):
        return f"<{self.topic!r} payload={self.payload!r} retain={self.retain}>"


class RecordingClient:
    """A stand-in for paho's Client that records `retain` FAITHFULLY.

    Deliberately not a MagicMock. paho's signature is

        publish(topic, payload=None, qos=0, retain=False, properties=None)

    so `retain` can arrive positionally or by keyword, and its default is False.
    A double that reads call.kwargs would report retain=False for a positional
    call, and - much worse - would score an OMITTED retain identically to an
    explicit retain=False. Omitting it is precisely the bug this file has to be
    able to catch: a clear published without retain=True deletes nothing. The
    broker forwards the empty payload to whoever is subscribed right now and
    keeps serving the old retained message to everyone who subscribes later, so
    HA re-creates all seven entities on its next restart while the clear looks
    like it worked.

    Declaring the real signature makes Python itself resolve the two calling
    forms, so the double cannot disagree with the library about what was meant.
    """

    def __init__(self):
        self.calls = []
        self.subscriptions = []

    def publish(self, topic, payload=None, qos=0, retain=False, properties=None):
        self.calls.append(Publication(topic, payload, qos, retain))
        return MagicMock()

    def subscribe(self, topic, qos=0, options=None, properties=None):
        # Recorded, not swallowed. This double returned None and kept no record
        # at first, and a mutant deleting the whole
        # `client.subscribe(COMMAND_SUBSCRIPTIONS)` line SURVIVED - every
        # "topic X is no longer published" assertion stayed green while the
        # light command, the pump command and water/low/cm/set were all silent
        # on the wire. Asserting membership of COMMAND_SUBSCRIPTIONS proves
        # only that the module agrees with itself; nothing connected the
        # constant to the client until this recorded it.
        if isinstance(topic, list):
            self.subscriptions.extend(topic)
        else:
            self.subscriptions.append((topic, qos))

    # --- read helpers -------------------------------------------------------

    @property
    def topics(self):
        return [c.topic for c in self.calls]

    def to(self, topic):
        return [c for c in self.calls if c.topic == topic]

    def first_index(self, topic):
        return self.topics.index(topic)


class RetiredEntitiesTestBase(unittest.TestCase):
    def setUp(self):
        self.client = RecordingClient()
        mqtt_mod.client = self.client
        mqtt_mod.pump = MagicMock()
        mqtt_mod.pump.get_speed.return_value = 0
        mqtt_mod.light = MagicMock()
        mqtt_mod.light.get_brightness.return_value = 0
        self.sensor = MagicMock()
        self.sensor.sample_count.return_value = 9
        self.sensor.measure_once.return_value = 8.0
        mqtt_mod.distance_sensor = self.sensor
        mqtt_mod.WATER_LOW_CM = 11.0
        mqtt_mod.WATER_VALID_MIN_CM = 3.0
        mqtt_mod.WATER_VALID_MAX_CM = 25.0
        patch.object(mqtt_mod, "flash_lights").start()
        self.addCleanup(patch.stopall)


class TestDiscoveryNoLongerAnnouncesDeadHardware(RetiredEntitiesTestBase):
    def test_no_retired_discovery_config_is_published(self):
        mqtt_mod.send_discovery_messages(self.client)
        for topic in RETIRED_DISCOVERY:
            with self.subTest(topic=topic):
                self.assertEqual(
                    self.client.to(topic), [],
                    "a retired entity is still being announced",
                )

    def test_the_four_surviving_entities_are_still_announced(self):
        """THE positive control for the whole change.

        Every other assertion in this file is about an absence, and an absence
        is what a function that dies early produces for free. This one can only
        pass if send_discovery_messages() runs to completion.
        """
        mqtt_mod.send_discovery_messages(self.client)
        for topic in SURVIVING_DISCOVERY:
            with self.subTest(topic=topic):
                self.assertEqual(
                    len(self.client.to(topic)), 1,
                    "a WORKING entity stopped being announced",
                )

    def test_surviving_configs_are_retained_and_non_empty(self):
        # Symmetry with the clear: a discovery config published without retain
        # would leave HA with no entity after a restart, which is the same
        # outcome as clearing it.
        mqtt_mod.send_discovery_messages(self.client)
        for topic in SURVIVING_DISCOVERY:
            with self.subTest(topic=topic):
                call = self.client.to(topic)[0]
                self.assertTrue(call.retain)
                self.assertTrue(call.payload)

    def test_discovery_announces_nothing_beyond_the_four(self):
        # Catches a retired block that was moved rather than removed, and any
        # eighth entity quietly reintroduced.
        mqtt_mod.send_discovery_messages(self.client)
        announced = sorted(t for t in self.client.topics
                           if t.startswith("homeassistant/"))
        self.assertEqual(announced, sorted(SURVIVING_DISCOVERY))


class TestClearRetiredEntities(RetiredEntitiesTestBase):
    def test_every_retired_discovery_topic_is_cleared(self):
        mqtt_mod.clear_retired_entities(self.client)
        for topic in RETIRED_DISCOVERY:
            with self.subTest(topic=topic):
                self.assertEqual(len(self.client.to(topic)), 1)

    def test_every_retired_retained_state_topic_is_cleared(self):
        mqtt_mod.clear_retired_entities(self.client)
        for topic in RETIRED_RETAINED_STATE:
            with self.subTest(topic=topic):
                self.assertEqual(len(self.client.to(topic)), 1)

    def test_the_clear_payload_is_empty(self):
        # An empty payload is what MQTT reads as "delete the retained message"
        # and what HA reads as "remove this discovered entity". Any other
        # payload is a normal publish that replaces the retained value.
        mqtt_mod.clear_retired_entities(self.client)
        for topic in RETIRED_DISCOVERY + RETIRED_RETAINED_STATE:
            with self.subTest(topic=topic):
                self.assertEqual(self.client.to(topic)[0].payload, "")

    def test_the_clear_is_retained(self):
        # Its own test, named for the failure: without retain=True the clear
        # deletes NOTHING, the broker keeps serving the old retained message to
        # every later subscriber, and HA re-creates all seven on next restart.
        mqtt_mod.clear_retired_entities(self.client)
        for topic in RETIRED_DISCOVERY + RETIRED_RETAINED_STATE:
            with self.subTest(topic=topic):
                self.assertIs(
                    self.client.to(topic)[0].retain, True,
                    "clear published without retain=True deletes nothing",
                )

    def test_the_clear_touches_nothing_that_survives(self):
        mqtt_mod.clear_retired_entities(self.client)
        for topic in SURVIVING_DISCOVERY + SURVIVING_STATE:
            with self.subTest(topic=topic):
                self.assertEqual(
                    self.client.to(topic), [],
                    "the clear is withdrawing a working entity",
                )

    def test_the_clear_publishes_exactly_the_expected_set(self):
        mqtt_mod.clear_retired_entities(self.client)
        self.assertEqual(
            sorted(self.client.topics),
            sorted(RETIRED_DISCOVERY + RETIRED_RETAINED_STATE),
        )

    def test_never_retained_state_topics_are_not_cleared(self):
        # gardyn/temperature and gardyn/humidity were published without retain=,
        # so there is no retained message to delete. Pinning this stops the list
        # growing defensive entries that imply a retained value existed.
        mqtt_mod.clear_retired_entities(self.client)
        for topic in NEVER_RETAINED_STATE:
            with self.subTest(topic=topic):
                self.assertEqual(self.client.to(topic), [])

    def test_the_clear_is_idempotent(self):
        mqtt_mod.clear_retired_entities(self.client)
        first = sorted(self.client.topics)
        self.client.calls.clear()
        mqtt_mod.clear_retired_entities(self.client)
        self.assertEqual(sorted(self.client.topics), first)


class TestConnectSequencing(RetiredEntitiesTestBase):
    """The connect path end to end.

    on_connect is where the clear and the announcement meet, and where an
    exception in the former silently skips the latter. These assert the ORDER by
    observed call sequence rather than by reading the source, and every one of
    them requires on_connect to reach its final statement.
    """

    def setUp(self):
        super().setUp()
        # Don't spawn real 30-minute publisher threads.
        self._threads = patch.object(mqtt_mod, "start_publisher_threads").start()

    def _connect(self):
        mqtt_mod.on_connect(self.client, None, None, 0)

    def test_every_retired_topic_is_cleared_before_any_discovery_is_sent(self):
        self._connect()
        last_clear = max(self.client.first_index(t)
                         for t in RETIRED_DISCOVERY + RETIRED_RETAINED_STATE)
        first_announce = min(self.client.first_index(t)
                             for t in SURVIVING_DISCOVERY)
        self.assertLess(
            last_clear, first_announce,
            "a retired entity is cleared after discovery is announced - HA "
            "would process the two in whatever order it happened to receive",
        )

    def test_connect_reaches_the_end_and_announces_the_survivors(self):
        # The failure this exists for: a raise anywhere in clear_retired_
        # entities takes discovery for the WORKING entities down with it, and
        # every absence-assertion above stays green while it does.
        self._connect()
        for topic in SURVIVING_DISCOVERY:
            with self.subTest(topic=topic):
                self.assertEqual(len(self.client.to(topic)), 1)
        self.assertEqual(len(self.client.to("gardyn/light/state")), 1)
        self._threads.assert_called_once()

    def test_connect_actually_subscribes_the_command_topics(self):
        # Asserted against LITERALS and against what reached the client, not
        # against COMMAND_SUBSCRIPTIONS. Deleting the subscribe call silences
        # every inbound command - including water/low/cm/set, the only runtime
        # path to the interlock's threshold - while leaving all the
        # absence-assertions in this file perfectly green.
        self._connect()
        subscribed = [t for t, _ in self.client.subscriptions]
        for topic in ("gardyn/light/command", "gardyn/light/brightness/set",
                      "gardyn/pump/command", "gardyn/pump/speed/set",
                      "gardyn/water/low/cm/set", "gardyn/water/level/get",
                      "gardyn/pcb/temperature/get"):
            with self.subTest(topic=topic):
                self.assertIn(topic, subscribed)

    def test_connect_does_not_subscribe_the_retired_get_topics(self):
        self._connect()
        subscribed = [t for t, _ in self.client.subscriptions]
        self.assertNotIn("gardyn/temperature/get", subscribed)
        self.assertNotIn("gardyn/humidity/get", subscribed)

    def test_commands_are_subscribed_at_qos_1_so_the_broker_queues_them(self):
        # QoS 1 paired with the durable session is what lets a command survive
        # a brief drop. At QoS 0 the broker discards it silently.
        self._connect()
        qos = dict(self.client.subscriptions)
        for topic in ("gardyn/light/command", "gardyn/light/brightness/set",
                      "gardyn/pump/command", "gardyn/pump/speed/set",
                      "gardyn/water/low/cm/set"):
            with self.subTest(topic=topic):
                self.assertEqual(qos[topic], 1)

    def test_connect_announces_the_device_online(self):
        self._connect()
        online = self.client.to("gardyn/status")
        self.assertEqual([c.payload for c in online], ["online"])
        self.assertTrue(online[0].retain)

    def test_connect_publishes_no_retired_state_value(self):
        # The half that makes the clear stick. If any publisher for a retired
        # entity survived, it would re-populate the retained topic that was
        # just cleared - on this very connect, or 30 minutes later.
        self._connect()
        for topic in RETIRED_RETAINED_STATE + NEVER_RETAINED_STATE:
            with self.subTest(topic=topic):
                payloads = [c.payload for c in self.client.to(topic)]
                self.assertTrue(
                    all(p == "" for p in payloads),
                    f"a retired topic was re-populated on connect: {payloads}",
                )

    def test_connect_does_not_take_a_reading(self):
        # on_connect used to refresh the reservoir. With the water entities
        # gone there is nothing to refresh, and the sensor is not touched.
        self._connect()
        self.sensor.measure_once.assert_not_called()


class TestPublisherLoops(RetiredEntitiesTestBase):
    """The 30-minute loops, which are what would UNDO the clear.

    This is the part of the change that makes part 3 stick. Clearing a retained
    topic while a publisher still writes to it every half hour is not a clear at
    all - the broker is repopulated on the next cycle and nothing looks wrong.

    Asserted against the deployed source rather than by running the loops: each
    one is `while True: ... sleep(1800)`, so calling it does not return.
    """

    def _thread_targets(self):
        import inspect
        import re

        source = inspect.getsource(mqtt_mod)
        match = re.search(r"for target in \(([^)]*)\)", source)
        self.assertIsNotNone(
            match, "start_publisher_threads no longer has a target tuple")
        return [t.strip() for t in match.group(1).split(",") if t.strip()]

    def test_only_the_surviving_loops_are_started(self):
        self.assertEqual(
            self._thread_targets(),
            ["publish_pcb_temperature", "publish_images"],
        )

    def test_no_loop_for_a_retired_entity_is_defined_at_all(self):
        # Not merely unstarted: a defined-but-unstarted loop is one edit away
        # from being started again, and it would silently repopulate the topics
        # clear_retired_entities() just deleted.
        for gone in ("publish_temperature", "publish_humidity",
                     "publish_water_level", "publish_pump_state",
                     "publish_water_sensor_status", "publish_water_low_mode",
                     "publish_water_low_threshold", "update_water_low_state",
                     "refresh_water_state"):
            with self.subTest(symbol=gone):
                self.assertFalse(
                    hasattr(mqtt_mod, gone),
                    f"{gone}() is back; it writes to a retired topic",
                )


class TestRetiredCommandSurface(RetiredEntitiesTestBase):
    """What this client still listens to, and what it no longer answers."""

    def _send(self, topic_suffix, payload):
        msg = MagicMock()
        msg.topic = f"gardyn/{topic_suffix}"
        msg.payload = payload.encode()
        mqtt_mod.on_message(self.client, None, msg)

    def test_temperature_and_humidity_get_are_no_longer_subscribed(self):
        # Both handlers read a sensor object that is None on this unit, so the
        # only thing a subscription bought was a logged traceback per request.
        subscribed = [t for t, _ in mqtt_mod.COMMAND_SUBSCRIPTIONS]
        self.assertNotIn("gardyn/temperature/get", subscribed)
        self.assertNotIn("gardyn/humidity/get", subscribed)

    def test_the_interlocked_command_topics_are_still_subscribed(self):
        # Deliberate: these are how the low-water interlock is exercised and
        # how its threshold is moved at runtime. Dropping them would leave the
        # interlock reachable only from the physical button, and would make
        # _threshold_is_acceptable() dead code.
        subscribed = [t for t, _ in mqtt_mod.COMMAND_SUBSCRIPTIONS]
        for topic in ("gardyn/pump/command", "gardyn/pump/speed/set",
                      "gardyn/water/low/cm/set", "gardyn/water/level/get"):
            with self.subTest(topic=topic):
                self.assertIn(topic, subscribed)

    def test_the_surviving_command_topics_are_still_subscribed(self):
        subscribed = [t for t, _ in mqtt_mod.COMMAND_SUBSCRIPTIONS]
        for topic in ("gardyn/light/command", "gardyn/light/brightness/set",
                      "gardyn/pcb/temperature/get"):
            with self.subTest(topic=topic):
                self.assertIn(topic, subscribed)

    def test_a_pump_command_publishes_no_state(self):
        self.sensor.measure_once.return_value = 8.0
        self._send("pump/command", "ON")
        self.assertEqual(self.client.to("gardyn/pump/state"), [])
        self.assertEqual(self.client.to("gardyn/pump/speed/state"), [])

    def test_a_pump_command_still_reaches_the_pump(self):
        # The complement of the test above, and the reason it is not enough on
        # its own: "publishes nothing" is also what a broken handler does.
        self.sensor.measure_once.return_value = 8.0
        self._send("pump/command", "ON")
        mqtt_mod.pump.set_speed.assert_called_once()

    def test_a_water_level_probe_publishes_nothing(self):
        self._send("water/level/get", "")
        self.assertEqual(self.client.to("gardyn/water/level"), [])
        self.assertEqual(self.client.to("gardyn/water/status"), [])

    def test_a_water_level_probe_still_reads_the_sensor(self):
        self._send("water/level/get", "")
        self.sensor.measure_once.assert_called_once()

    def test_a_threshold_change_publishes_nothing(self):
        self._send("water/low/cm/set", "9.5")
        self.assertEqual(self.client.to("gardyn/water/low/cm"), [])
        self.assertEqual(self.client.to("gardyn/water/low/mode"), [])

    def test_the_light_still_publishes_its_state(self):
        # The control that separates "the retired publishers are gone" from
        # "state publishing is broken".
        self._send("light/command", "ON")
        self.assertEqual(len(self.client.to("gardyn/light/state")), 1)
        self.assertTrue(self.client.to("gardyn/light/state")[0].retain)


class TestInterlockSurvivesTheWithdrawal(RetiredEntitiesTestBase):
    """The acceptance criterion: retiring the ENTITIES kept the DECISIONS.

    tests/test_water_interlock.py::TestPumpInterlock covers the same refusals
    and was not modified by T-475 - that file passing unchanged is the primary
    evidence. These restate the two refusals against the retired-entity fake
    client, so a future change that reintroduces publishing cannot quietly take
    the interlock with it.
    """

    def test_refuses_to_start_on_an_untrustworthy_reading(self):
        self.sensor.measure_once.return_value = 0.09  # latched, dead sensor
        self.assertFalse(mqtt_mod.start_pump(100, self.client))
        mqtt_mod.pump.set_speed.assert_not_called()

    def test_refuses_to_start_when_the_water_is_low(self):
        self.sensor.measure_once.return_value = 20.0
        self.assertFalse(mqtt_mod.start_pump(100, self.client))
        mqtt_mod.pump.set_speed.assert_not_called()

    def test_starts_when_the_water_is_present(self):
        self.sensor.measure_once.return_value = 8.0
        self.assertTrue(mqtt_mod.start_pump(100, self.client))
        mqtt_mod.pump.set_speed.assert_called_once_with(100)

    def test_a_refusal_publishes_nothing_at_all(self):
        # A refusal used to leave a trail on the broker. It no longer can, so
        # the flash and the log are the whole record - which is why start_pump()
        # still calls flash_lights() on both refusal paths.
        self.sensor.measure_once.return_value = 0.09
        mqtt_mod.start_pump(100, self.client)
        self.assertEqual(self.client.calls, [])

    def test_a_refusal_still_flashes_the_lights(self):
        self.sensor.measure_once.return_value = 0.09
        with patch.object(mqtt_mod, "flash_lights") as flash:
            mqtt_mod.start_pump(100, self.client)
        flash.assert_called_once()

    def test_the_plausibility_band_is_unchanged(self):
        for value, expected in ((0.09, None), (0.0, None), (83.0, None),
                                (2.99, None), (3.0, 3.0), (25.0, 25.0),
                                (25.01, None), (12.5, 12.5)):
            with self.subTest(value=value):
                self.sensor.measure_once.return_value = value
                self.assertEqual(mqtt_mod.safe_distance_measure(), expected)

    def test_a_non_finite_threshold_still_cannot_disarm_the_interlock(self):
        msg = MagicMock()
        msg.topic = "gardyn/water/low/cm/set"
        msg.payload = b"nan"
        mqtt_mod.on_message(self.client, None, msg)
        self.assertEqual(mqtt_mod.WATER_LOW_CM, 11.0)
        self.sensor.measure_once.return_value = 20.0
        self.assertFalse(mqtt_mod.start_pump(100, self.client))
        mqtt_mod.pump.set_speed.assert_not_called()


class TestNothingIsSwallowed(RetiredEntitiesTestBase):
    """on_message and on_connect both wrap everything in a catch-all.

    In production that is right. In a test it means a NameError from a symbol
    this ticket deleted gets LOGGED while every assertion still passes - which
    is the exact failure mode of a deletion-heavy change, and the reason this
    class exists rather than relying on the assertions above.
    """

    def setUp(self):
        super().setUp()
        self.records = []

        class _Recorder(logging.Handler):
            def emit(_self, record):
                if record.exc_info:
                    self.records.append(record)

        self.handler = _Recorder()
        logging.getLogger("mqtt").addHandler(self.handler)
        self.addCleanup(logging.getLogger("mqtt").removeHandler, self.handler)

    def _assert_clean(self):
        self.assertEqual(
            [r.getMessage() for r in self.records], [],
            "an exception was swallowed by a catch-all - a call site probably "
            "still references something T-475 deleted",
        )

    def test_every_subscribed_topic_is_handled_without_raising(self):
        for topic, _ in mqtt_mod.COMMAND_SUBSCRIPTIONS:
            with self.subTest(topic=topic):
                msg = MagicMock()
                msg.topic = topic
                msg.payload = b"50"
                mqtt_mod.on_message(self.client, None, msg)
        self._assert_clean()

    def test_the_connect_path_raises_nothing(self):
        with patch.object(mqtt_mod, "start_publisher_threads"):
            mqtt_mod.on_connect(self.client, None, None, 0)
        self._assert_clean()

    def test_the_button_paths_raise_nothing(self):
        self.sensor.measure_once.return_value = 8.0
        mqtt_mod.toggle_light()
        mqtt_mod.pump.get_speed.return_value = 0
        mqtt_mod.toggle_pump()
        mqtt_mod.pump.get_speed.return_value = 50
        mqtt_mod.toggle_pump()
        self._assert_clean()


if __name__ == "__main__":
    unittest.main()
