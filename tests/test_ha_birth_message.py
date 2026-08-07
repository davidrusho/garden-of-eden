"""Tests for the homeassistant/status birth-message re-announce (T-527.1).

WHAT THIS COVERS AND WHY THE SHAPE IS UNUSUAL

The change adds a path that fires when SOMEBODY ELSE restarts. It cannot be
exercised on the deployed host without restarting Home Assistant, and its
failure mode is silence — the device keeps publishing camera frames, keeps
answering ICMP, keeps its MQTT session, and HA simply has no entities for it.
That is the 2026-08-05 outage exactly: four `gardyn*` entities `unavailable`
from 16:38:50, eighteen `light.turn_on/off` calls failing log-only, the grow
light stuck at 100% from 04:00 until a human intervened at 20:20. So this suite
is the only place the behaviour is ever exercised.

Three things it has to be able to catch, in descending order of how badly they
would end:

  1. START_PUBLISHER_THREADS MOVING INTO THE ANNOUNCE. Each call spawns a fresh
     set of PCB and camera threads with no check for existing ones. Called on
     every birth message it would leak a full set per HA restart, unbounded, on
     a 512 MB Zero W with no console and no physical recovery path. Nothing
     about the device would look wrong until it ran out of memory. This is the
     worst outcome available in this change and it is asserted directly.
  2. `offline` TREATED AS A TRIGGER. HA publishes its LWT on the same topic, so
     a payload check that is merely truthy re-announces on HA going AWAY. Not
     harmful in itself, but it would make the log lie about what happened
     during the next outage, which is the artifact these incidents are
     reconstructed from.
  3. THE ANNOUNCE NOT HAPPENING AT ALL — the original bug, coming back. Guarded
     positively: the four surviving entities must be announced, retained and
     non-empty, not merely "not absent".

Every topic is written out as a LITERAL rather than imported from mqtt. A test
that imports the constant it is checking proves the module agrees with itself
and passes happily after someone edits that constant — the same reasoning
tests/test_retired_entities.py records for its own lists.

Stubs and the RecordingClient come from the existing test modules rather than
being re-installed. tests.test_water_interlock owns the sys.modules hardware
stubs and the real `import mqtt`; a second stubbing module fights the first,
which is what made test_light_logging inert under `unittest discover` once
before. Only non-TestCase names are imported, so nothing here re-runs another
module's cases.

Run:  python3 -m unittest tests.test_ha_birth_message
"""

import unittest
from unittest.mock import MagicMock, patch

from tests.test_water_interlock import mqtt_mod
from tests.test_retired_entities import RecordingClient

# As the stubbed config sets it. Written out, not imported.
ID = "gardyn-xx"

HA_STATUS = "homeassistant/status"

# The four entities that exist and must be rebuilt whenever HA comes back.
SURVIVING_DISCOVERY = [
    f"homeassistant/light/gardyn/{ID}_light/config",
    f"homeassistant/sensor/gardyn/{ID}_pcb_temp/config",
    f"homeassistant/image/gardyn/{ID}_upper_camera/config",
    f"homeassistant/image/gardyn/{ID}_lower_camera/config",
]

# What the device says about its own availability. Every entity's
# availability_config follows this topic, so an announce that rebuilt the
# entities without it would leave all four announced-but-unavailable.
DEVICE_STATUS = "gardyn/status"

LIGHT_STATE = "gardyn/light/state"


class BirthMessageTestBase(unittest.TestCase):
    def setUp(self):
        self.client = RecordingClient()
        mqtt_mod.client = self.client
        mqtt_mod.pump = MagicMock()
        mqtt_mod.pump.get_speed.return_value = 0
        mqtt_mod.light = MagicMock()
        mqtt_mod.light.get_brightness.return_value = 42
        self.sensor = MagicMock()
        self.sensor.sample_count.return_value = 9
        self.sensor.measure_once.return_value = 8.0
        mqtt_mod.distance_sensor = self.sensor
        mqtt_mod.WATER_LOW_CM = 11.0
        mqtt_mod.WATER_VALID_MIN_CM = 3.0
        mqtt_mod.WATER_VALID_MAX_CM = 25.0
        patch.object(mqtt_mod, "flash_lights").start()
        # Patched in the BASE, not per-test. The assertion that a birth message
        # does NOT start publisher threads is the most important one in this
        # file, and it is only meaningful if the real function would have been
        # observable had it been called.
        self.threads = patch.object(mqtt_mod, "start_publisher_threads").start()
        # MODULE-SCOPED STATE, reset per test. _last_birth_announce persists
        # across test methods otherwise, so whether a case sees a debounce would
        # depend on unittest's method ordering - the kind of coupling that makes
        # one test fail only when the whole class runs.
        mqtt_mod._last_birth_announce = None
        self.addCleanup(setattr, mqtt_mod, "_last_birth_announce", None)
        self.addCleanup(patch.stopall)

    def _deliver(self, topic, payload):
        """Push one message through the real on_message, as paho would."""
        msg = MagicMock()
        msg.topic = topic
        msg.payload = payload.encode()
        mqtt_mod.on_message(self.client, None, msg)

    def _birth(self, payload="online"):
        self._deliver(HA_STATUS, payload)


class TestTheDeviceIsListeningAtAll(BirthMessageTestBase):
    """Without the subscription nothing downstream can ever run.

    This is the half that was missing entirely before T-527.1, and it is
    invisible from any behavioural test of on_message: on_message can be given a
    birth message by a test forever while the deployed client never receives
    one, because it never asked the broker for the topic. zigbee2mqtt and
    govee2mqtt both subscribe here and recovered on their own on 2026-08-05; the
    broker log shows the birth message delivered to exactly those two clients.
    """

    def _connect(self):
        mqtt_mod.on_connect(self.client, None, None, 0)

    def test_connect_subscribes_to_the_ha_status_topic(self):
        self._connect()
        subscribed = [t for t, _ in self.client.subscriptions]
        self.assertIn(
            HA_STATUS, subscribed,
            "the client never asks the broker for HA's birth message, so no "
            "amount of correct handling downstream can ever fire",
        )

    def test_the_ha_status_subscription_is_qos_1(self):
        # QoS 0 lets the broker drop it under load, and the load spike that
        # matters is HA restarting - which is precisely when it is published.
        self._connect()
        self.assertEqual(dict(self.client.subscriptions)[HA_STATUS], 1)

    def test_connect_still_subscribes_every_command_topic(self):
        # Positive control for the subscription change. Adding a second
        # subscribe() call is an easy way to replace the first one by accident;
        # every assertion about the new topic would stay green while the light
        # command went silent.
        self._connect()
        subscribed = [t for t, _ in self.client.subscriptions]
        for topic in ("gardyn/light/command", "gardyn/light/brightness/set",
                      "gardyn/pump/command", "gardyn/pump/speed/set",
                      "gardyn/water/low/cm/set", "gardyn/water/level/get",
                      "gardyn/pcb/temperature/get"):
            with self.subTest(topic=topic):
                self.assertIn(topic, subscribed)


class TestBirthMessageReAnnounces(BirthMessageTestBase):
    """The behaviour the ticket exists for, asserted positively."""

    def test_all_four_entities_are_re_announced(self):
        self._birth()
        for topic in SURVIVING_DISCOVERY:
            with self.subTest(topic=topic):
                self.assertEqual(
                    len(self.client.to(topic)), 1,
                    "HA came back and this entity was not re-announced - it "
                    "stays unavailable until a manual config-entry reload",
                )

    def test_the_re_announced_configs_are_retained_and_non_empty(self):
        # An unretained discovery config is delivered to whoever is subscribed
        # at that instant and to nobody afterwards, so HA loses the entity again
        # on its next restart - the same end state, one restart later.
        self._birth()
        for topic in SURVIVING_DISCOVERY:
            with self.subTest(topic=topic):
                call = self.client.to(topic)[0]
                self.assertTrue(call.retain)
                self.assertTrue(call.payload)

    def test_availability_is_re_asserted_online(self):
        # Every entity's availability_config follows gardyn/status. Rebuilding
        # the entities without re-publishing this announces four entities that
        # HA immediately marks unavailable, which looks identical to the bug.
        self._birth()
        online = self.client.to(DEVICE_STATUS)
        self.assertEqual([c.payload for c in online], ["online"])
        self.assertTrue(online[0].retain)

    def test_real_light_state_is_republished(self):
        # HA has just rebuilt the light entity and has no state for it. Without
        # this it sits at `unknown` until the next command or the 15-minute
        # re-assert - and after T-527.8 there is no re-assert.
        self._birth()
        published = self.client.to(LIGHT_STATE)
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0].payload, "ON")

    def test_the_retired_entities_are_cleared_before_discovery_is_sent(self):
        # Ordering is load-bearing and is asserted by observed call sequence,
        # not by reading the source. A clear that lands after the announcement
        # races it, and HA processes the two in whatever order they arrive.
        self._birth()
        last_clear = max(
            self.client.first_index(t)
            for t in (f"homeassistant/light/gardyn/{ID}_pump/config",
                      f"homeassistant/sensor/gardyn/{ID}_water_level/config")
        )
        first_announce = min(self.client.first_index(t)
                             for t in SURVIVING_DISCOVERY)
        self.assertLess(last_clear, first_announce)

    def test_a_second_birth_message_publishes_the_same_set(self):
        # HA restarts are not rare. Two announces must produce the same wire
        # traffic - no accumulating state, no growing payload.
        #
        # The debounce is cleared between them ON PURPOSE: this asserts that
        # announce_to_home_assistant() is idempotent, which is a different
        # question from whether the birth path rate-limits. Testing both in one
        # case would mean neither is pinned - the debounce alone would satisfy a
        # naive "same set" assertion by publishing nothing at all the second
        # time. TestTheBirthPathIsDebounced owns the other half.
        self._birth()
        first = sorted(self.client.topics)
        self.client.calls.clear()
        mqtt_mod._last_birth_announce = None
        self._birth()
        self.assertEqual(sorted(self.client.topics), first)


class TestTheAnnounceStaysNarrowerThanConnect(BirthMessageTestBase):
    """announce_to_home_assistant() is deliberately less than on_connect does.

    CORRECTED AFTER REVIEW, and worth recording because the original framing was
    wrong in a way that felt rigorous. This class was called
    TestTheAnnounceDoesNotLeakThreads and argued that start_publisher_threads()
    spawns threads unchecked, so calling it per HA restart would leak a set each
    time and eventually exhaust a 512 MB Zero W. It does not: it carries a
    lock-guarded once-only flag and returns immediately on every call after the
    first. The danger was imaginary.

    The assertions below are unchanged and still earn their place - they pin the
    boundary between per-announcement work and per-process work, and a mutant
    moving the call into the shared function does fail them. What changed is the
    STAKE. Naming a false catastrophe is not harmless: it was the loudest thing
    in this file, and it is exactly the kind of claim that stops anyone asking
    whether there is a real worst case. There was one, and it was somewhere else
    entirely - see TestTheBirthPathIsDebounced.
    """

    def test_a_birth_message_starts_no_publisher_threads(self):
        self._birth()
        self.threads.assert_not_called()

    def test_ten_birth_messages_start_no_publisher_threads(self):
        for _ in range(10):
            self._birth()
        self.threads.assert_not_called()

    def test_connect_still_starts_them_exactly_once(self):
        # The paired assertion, and the one that stops the fix over-correcting.
        # Removing the call from the connect path would satisfy every test above
        # while leaving the PCB and camera loops permanently unstarted.
        mqtt_mod.on_connect(self.client, None, None, 0)
        self.threads.assert_called_once()

    def test_a_birth_message_does_not_re_subscribe(self):
        # Subscriptions are per-connection and the session is durable, so the
        # broker already holds them. Re-subscribing on every birth message is
        # pointless traffic on the single antenna this device has.
        self._birth()
        self.assertEqual(self.client.subscriptions, [])


class TestTheBirthPathIsDebounced(BirthMessageTestBase):
    """The real worst case, found by review after the imaginary one was removed.

    homeassistant/status is the first topic this client subscribes to OUTSIDE
    its own namespace, so it is writable by anyone with broker publish rights
    rather than only by Home Assistant. Measured against the real on_message:
    ONE ~30-byte inbound birth message produces 21 outbound publishes and
    ~1.8 KB, plus a synchronous pigpio round-trip for the light's real duty
    cycle, all on paho's network-loop thread - a ~90x byte amplification, on a
    single-antenna Zero W whose Wi-Fi headroom this file's own comments describe
    as already spent, writing into an unrotated log.

    The direction of the trade is deliberate and is the thing to preserve if
    this is ever tuned: SUPPRESSING A LEGITIMATE RE-ANNOUNCE IS THE WORSE
    FAILURE, because a missed announce is the outage this whole ticket exists to
    fix. Ten seconds stops a flap or a hostile publisher while being far shorter
    than any real HA startup, so no genuine restart pair can land inside it.
    """

    def test_a_second_birth_inside_the_window_announces_nothing(self):
        self._birth()
        self.client.calls.clear()
        self._birth()
        self.assertEqual(
            self.client.calls, [],
            "a burst of birth messages is amplified onto the wire unthrottled",
        )

    def test_a_flood_costs_exactly_one_announce(self):
        for _ in range(50):
            self._birth()
        self.assertEqual(len(self.client.to(SURVIVING_DISCOVERY[0])), 1)

    def test_the_window_is_ten_seconds(self):
        """Pin the VALUE, not just the mechanism.

        Every other test in this class expresses itself relative to
        BIRTH_DEBOUNCE_SECONDS, which means they all move with it: a mutant
        setting it to 86400 SURVIVED the first version of this battery, because
        the expiry test computed its own fake timestamp from the same constant
        it was supposed to be checking. The module agreed with itself.

        The number is a policy decision with a direction, so it is pinned as
        one. Ten seconds is long enough to kill a flap and far shorter than any
        real Home Assistant startup, so no genuine restart pair can land inside
        it. Raising this materially trades away the guarantee the ticket exists
        for - a suppressed legitimate re-announce IS the 2026-08-05 outage.
        """
        self.assertEqual(mqtt_mod.BIRTH_DEBOUNCE_SECONDS, 10)

    def test_a_birth_after_the_window_announces_again(self):
        # The paired assertion, and the one that stops the fix over-correcting.
        # A debounce that never expires is just a broken feature: HA would
        # restart an hour later and find nothing had been re-announced.
        self._birth()
        self.client.calls.clear()
        mqtt_mod._last_birth_announce = (
            mqtt_mod.monotonic() - mqtt_mod.BIRTH_DEBOUNCE_SECONDS - 1
        )
        self._birth()
        self.assertEqual(len(self.client.to(SURVIVING_DISCOVERY[0])), 1)

    def test_the_window_is_measured_on_the_monotonic_clock(self):
        # This Pi has no RTC and takes its time from NTP after boot, so a wall
        # clock can step - backwards, which would disable the debounce, or
        # forwards, which would extend it past a real HA restart. Asserted by
        # stepping a fake wall clock and checking the debounce does not care.
        self._birth()
        self.client.calls.clear()
        with patch.object(mqtt_mod, "monotonic",
                          return_value=mqtt_mod._last_birth_announce + 1):
            with patch("time.time", return_value=0):
                self._birth()
        self.assertEqual(self.client.calls, [])

    def test_connect_is_never_debounced(self):
        # A reconnect must ALWAYS announce, whatever happened moments ago. The
        # broker drops this client roughly 25 times a day; a debounced reconnect
        # is a device that reappears with no entities.
        self._birth()
        self.client.calls.clear()
        mqtt_mod.on_connect(self.client, None, None, 0)
        for topic in SURVIVING_DISCOVERY:
            with self.subTest(topic=topic):
                self.assertEqual(len(self.client.to(topic)), 1)

    def test_a_debounced_birth_is_logged_loudly(self):
        # WARNING, not INFO. A sustained burst means something is wrong with HA
        # or with another client on the broker, and the root logger sits at
        # WARNING - so an INFO line here would be recorded in the file handler
        # and invisible to anything watching the journal at default level.
        self._birth()
        with self.assertLogs("mqtt", level="WARNING") as captured:
            self._birth()
        self.assertTrue(
            any("skipping" in line for line in captured.output),
            f"a suppressed birth message left no loud trace: {captured.output}",
        )


class TestTheBirthPathSaysWhatItDid(BirthMessageTestBase):
    """The log lines, asserted rather than assumed.

    ADDED AFTER REVIEW. Three extra mutants run by the reviewer deleted these
    lines one at a time and every one SURVIVED the suite - including the
    else-branch line, which this file's own header argues matters because it is
    the artifact an outage gets reconstructed from. Arguing that a line is
    load-bearing while nothing asserts it exists is the gap those mutants found.

    It matters more here than logging usually does. This device's failure mode
    is defined as silence - it keeps publishing, keeps answering ICMP, and HA
    simply has no entities - so the log is the only evidence that the fix ever
    fired, and the only way to tell "HA never sent a birth message" from "we
    ignored it".
    """

    def test_a_re_announce_is_logged(self):
        with self.assertLogs("mqtt", level="INFO") as captured:
            self._birth()
        self.assertTrue(
            any("re-announcing discovery" in line for line in captured.output),
            f"the fix fired and left no trace: {captured.output}",
        )

    def test_ha_going_offline_is_logged(self):
        # "HA went away at 16:38" is the single most useful line to have when
        # reconstructing an incident like 2026-08-05.
        #
        # MATCHED ON "no action", NOT ON "offline". The first version of this
        # test asserted `any("offline" in line)` and a mutant deleting the line
        # outright SURVIVED it - because on_message's generic decode line at the
        # top of the function echoes the payload, so it emits
        # `Decoded payload on homeassistant/status: 'offline'` whatever this
        # branch does. The assertion was satisfied by the test's own input.
        # "no action" appears in exactly one place in the codebase.
        with self.assertLogs("mqtt", level="INFO") as captured:
            self._birth("offline")
        self.assertTrue(
            any("no action" in line for line in captured.output),
            f"HA's departure was silent: {captured.output}",
        )

    def test_a_failing_announce_is_logged_with_a_traceback(self):
        # on_message's catch-all swallows the exception, which is correct - an
        # escape would kill paho's network loop thread and with it every inbound
        # command. But swallowed is not the same as observed: without this, a
        # re-announce that fails EVERY time is indistinguishable from one that
        # never fires, and both look like the original bug.
        with patch.object(mqtt_mod, "announce_to_home_assistant",
                          side_effect=RuntimeError("boom")):
            with self.assertLogs("mqtt", level="ERROR") as captured:
                self._birth()
        self.assertTrue(
            any("boom" in line for line in captured.output),
            f"a failing re-announce was silent: {captured.output}",
        )


class TestTheTopicCollisionIsRefused(BirthMessageTestBase):
    """BASE_TOPIC is env-configurable, and one value is a self-feeding loop.

    Set MQTT_BASETOPIC=homeassistant and STATUS_TOPIC becomes HA_STATUS_TOPIC.
    The announce publishes "online" to STATUS_TOPIC, the broker echoes it back
    to this client (MQTT 3.1.1 has no no-local option), the birth branch fires,
    and it re-announces forever - on the network-loop thread, on the device with
    no console. The debounce would slow that to one announce per 10s rather than
    stop it.

    The refusal DEGRADES rather than aborting. Refusing to start looks safer and
    is not: a service that will not boot on a host nobody can reach by hand is
    unrecoverable, while a device without the birth re-announce still drives the
    light and is merely back to the pre-T-527.1 behaviour.
    """

    def test_the_colliding_config_does_not_subscribe(self):
        with patch.object(mqtt_mod, "STATUS_TOPIC", HA_STATUS):
            mqtt_mod.on_connect(self.client, None, None, 0)
        subscribed = [t for t, _ in self.client.subscriptions]
        self.assertNotIn(HA_STATUS, subscribed)

    def test_the_colliding_config_still_runs_everything_else(self):
        # The degrade half. The light must still work.
        with patch.object(mqtt_mod, "STATUS_TOPIC", HA_STATUS):
            mqtt_mod.on_connect(self.client, None, None, 0)
        subscribed = [t for t, _ in self.client.subscriptions]
        self.assertIn("gardyn/light/command", subscribed)
        self.threads.assert_called_once()
        for topic in SURVIVING_DISCOVERY:
            with self.subTest(topic=topic):
                self.assertEqual(len(self.client.to(topic)), 1)

    def test_the_refusal_is_logged_at_error(self):
        with patch.object(mqtt_mod, "STATUS_TOPIC", HA_STATUS):
            with self.assertLogs("mqtt", level="ERROR") as captured:
                mqtt_mod.on_connect(self.client, None, None, 0)
        self.assertTrue(any("NOT subscribing" in l for l in captured.output))

    def test_the_normal_config_is_unaffected(self):
        # Positive control. A collision check with the comparison inverted would
        # silently disable the subscription on every healthy deployment, and
        # every assertion above would stay green.
        mqtt_mod.on_connect(self.client, None, None, 0)
        subscribed = [t for t, _ in self.client.subscriptions]
        self.assertIn(HA_STATUS, subscribed)


class TestOnlyABirthPayloadTriggers(BirthMessageTestBase):
    """`offline` arrives on the same topic. It must not re-announce.

    HA publishes its own LWT here, so a truthy payload check re-announces when
    HA goes AWAY. The direct harm is small; the real cost is that the log then
    records a re-announce at the moment of an outage, and that log is the
    artifact the next incident gets reconstructed from.
    """

    def _announced_anything(self):
        return [t for t in self.client.topics if t.startswith("homeassistant/")]

    def test_offline_does_not_re_announce(self):
        self._birth("offline")
        self.assertEqual(
            self._announced_anything(), [],
            "HA's last-will was treated as a birth message",
        )

    def test_offline_publishes_nothing_at_all(self):
        self._birth("offline")
        self.assertEqual(self.client.calls, [])

    def test_an_unknown_payload_does_not_re_announce(self):
        self._birth("restarting")
        self.assertEqual(self._announced_anything(), [])

    def test_an_empty_payload_does_not_re_announce(self):
        # The shape an MQTT retained-message DELETE takes. It is not a birth.
        self._birth("")
        self.assertEqual(self._announced_anything(), [])

    def test_case_is_not_load_bearing(self):
        # HA sends lowercase and the docs quote lowercase, so this is not
        # required - but the handler lowercases, and pinning it stops a later
        # "tidy-up" removing the .lower() and turning a cosmetic difference
        # into a silent no-op.
        self._birth("ONLINE")
        self.assertEqual(len(self.client.to(SURVIVING_DISCOVERY[0])), 1)

    def test_surrounding_whitespace_is_tolerated(self):
        # on_message strips before dispatch. Asserted because the alternative
        # failure is silent: a padded payload would fall through to the else
        # branch and be logged as an unremarkable status update.
        self._birth("  online\n")
        self.assertEqual(len(self.client.to(SURVIVING_DISCOVERY[0])), 1)


class TestTheNewBranchDoesNotShadowTheRest(BirthMessageTestBase):
    """The new branch is FIRST in on_message's chain. Prove the chain survives.

    A first branch that matched too broadly would swallow every command this
    device receives, and every assertion in every class above would stay green
    while the grow light stopped answering Home Assistant entirely.
    """

    def test_a_light_command_still_reaches_the_light(self):
        self._deliver("gardyn/light/command", "OFF")
        mqtt_mod.light.off.assert_called_once()

    def test_a_brightness_command_still_reaches_the_light(self):
        self._deliver("gardyn/light/brightness/set", "70")
        mqtt_mod.light.set_duty_cycle.assert_called_with(70)

    def test_a_light_command_does_not_re_announce_discovery(self):
        # The inverse shadow: an over-broad birth branch that also fired on
        # ordinary commands would republish four retained configs on every
        # single light command.
        self._deliver("gardyn/light/command", "ON")
        announced = [t for t in self.client.topics
                     if t.startswith("homeassistant/")]
        self.assertEqual(announced, [])

    def test_a_topic_that_merely_contains_the_status_topic_is_not_a_birth(self):
        # Substring matching would make any topic ending in the right characters
        # a trigger. Equality is what is wanted; this pins it.
        self._deliver("gardyn/homeassistant/status", "online")
        announced = [t for t in self.client.topics
                     if t.startswith("homeassistant/")]
        self.assertEqual(announced, [])


class TestTheHandlerCannotKillTheClient(BirthMessageTestBase):
    """on_message wraps everything in a catch-all. The new path is inside it.

    This topic is publishable by anyone with broker access, and this callback
    runs on paho's network loop thread. An exception escaping it would take the
    thread down and with it every inbound command - the light would stop
    answering HA completely, which is worse than the bug being fixed.
    """

    def test_an_exploding_announce_does_not_propagate(self):
        with patch.object(mqtt_mod, "announce_to_home_assistant",
                          side_effect=RuntimeError("boom")):
            self._birth()  # must not raise

    def test_a_binary_payload_does_not_propagate(self):
        msg = MagicMock()
        msg.topic = HA_STATUS
        msg.payload = b"\xff\xfe\x00"
        mqtt_mod.on_message(self.client, None, msg)  # must not raise

    def test_a_binary_payload_does_not_re_announce(self):
        # The decode failure returns early. Pinned because the alternative -
        # falling through with a replacement-character payload - would be
        # invisible and would depend on what those characters happened to be.
        msg = MagicMock()
        msg.topic = HA_STATUS
        msg.payload = b"\xff\xfe\x00"
        mqtt_mod.on_message(self.client, None, msg)
        self.assertEqual(self.client.calls, [])


if __name__ == "__main__":
    unittest.main()
