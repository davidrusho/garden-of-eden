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
        # HA restarts are not rare, and this path is reachable by anyone with
        # broker access. It has to be a no-op the second time.
        self._birth()
        first = sorted(self.client.topics)
        self.client.calls.clear()
        self._birth()
        self.assertEqual(sorted(self.client.topics), first)


class TestTheAnnounceDoesNotLeakThreads(BirthMessageTestBase):
    """The worst outcome available in this change, asserted on its own.

    start_publisher_threads() spawns the PCB and camera loops with no check for
    threads it already started. Moving it inside the shared announce sequence
    would leak a full set on every HA restart - unbounded, on a 512 MB Zero W
    that has no console, no keyboard and no SD-card recovery. Nothing would look
    wrong from the outside: the device would keep publishing, faster and faster,
    until it died.

    This is the reason announce_to_home_assistant() exists as a narrower thing
    than "what on_connect does", and the reason that is written in its docstring
    rather than left to be inferred.
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
