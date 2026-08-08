"""Tests for the refused-CONNACK gate and the escaped decode line (T-527.11).

TWO DEFECTS, ONE FILE, BECAUSE BOTH LIVE ON THE EDGE WHERE UNTRUSTED INPUT
REACHES THIS DEVICE.

1. on_connect did not gate on `rc`.

   paho calls on_connect from _handle_connack whatever the CONNACK said. On a
   refusal - "Not authorized", "Bad user name or password", "Server
   unavailable" - the old code logged `Connected with result code Not
   authorized`, subscribed, announced discovery, and then called
   start_publisher_threads() against a socket the broker was closing.

   The subscribe and the announce are wasted work and heal on the next connect.
   start_publisher_threads() does NOT heal, and the shorthand for why is wrong
   in a way worth stating: it is not that the publishers never start. It sets
   its once-only flag AND spawns both loops, so a refused CONNACK starts them
   against a client that is not connected - each loop's first publish returns
   MQTT_ERR_NO_CONN and is dropped, and both then sleep 30 minutes. The connect
   that succeeds seconds later finds the flag set and returns early, so nothing
   re-sends. Nothing leaks and nothing looks broken; the device is simply up
   with sensor.gardyn_pcb_temperature `unknown` for up to half an hour. That is
   the precise race start_publisher_threads()'s own docstring says its guard
   exists to prevent, reached through the one door the guard cannot see.

   The credential case is the one that matters on this device: a broker
   password rotated while the Pi is running produces exactly this, repeatedly,
   on a host with no console and no physical recovery path.

2. The generic decode line interpolated the payload raw.

   Since T-527.1 the subscription list reaches outside BASE_TOPIC:
   homeassistant/status is Home Assistant's topic, not this device's, so its
   contents are whatever some other client on the broker published. `'{payload}'`
   wrote a newline or an ANSI escape into gardyn.log byte for byte, which is
   enough to forge a whole log line - timestamp, logger name, level - in the
   file these incidents get reconstructed from. Nothing in this repo rotates
   that file, so a forged line stays there.

WHAT `rc` ACTUALLY IS, AND HOW THAT WAS ESTABLISHED

Not from recall. paho-mqtt 2.0.0 (the version requirements.txt pins) was
installed into a throwaway venv and a synthetic CONNACK driven through
Client._handle_connack with CallbackAPIVersion.VERSION2 registered and a
non-empty client_id, which is how mqtt.py constructs its client:

    v3 rc=0  -> on_connect CALLED, ReasonCode(Connack, 'Success')                   value=0   is_failure=False
    v3 rc=1  -> on_connect NOT called; paho downgrades to v3.1 and reconnects
    v3 rc=2  -> on_connect CALLED, ReasonCode(Connack, 'Client identifier not valid') value=133 is_failure=True
    v3 rc=3  -> on_connect CALLED, ReasonCode(Connack, 'Server unavailable')          value=136 is_failure=True
    v3 rc=4  -> on_connect CALLED, ReasonCode(Connack, 'Bad user name or password')   value=134 is_failure=True
    v3 rc=5  -> on_connect CALLED, ReasonCode(Connack, 'Not authorized')              value=135 is_failure=True

Note rc=2: paho's early return for a rejected identifier is guarded by
`self._client_id == b''`, and mqtt.py passes client_id=IDENTIFIER, so that
rejection DOES reach on_connect on this client. Note also that the value is the
v5 reason code, not the v3 one - "server unavailable" is 3 on the wire and 136
here - which is why nothing in this suite compares rc against the v3 numbers.

paho is stubbed out in these tests, so the numbers above are reproduced in
ReasonCodeDouble below rather than imported. They are written as literals for
that reason: a test that derived them would agree with itself.

Stubs and RecordingClient come from the existing test modules rather than being
re-installed - tests.test_water_interlock owns the sys.modules hardware stubs
and the real `import mqtt`, and a second stubbing module fights the first. Only
non-TestCase names are imported, so nothing here re-runs another module's cases.

Run:  python3 -m unittest tests.test_connack_refusal
"""

import unittest
from unittest.mock import MagicMock, patch

from tests.test_water_interlock import mqtt_mod
from tests.test_retired_entities import RecordingClient

# As the stubbed config sets it. Written out, not imported - see the module
# docstring of tests/test_ha_birth_message.py for why every topic here is a
# literal.
ID = "gardyn-xx"

HA_STATUS = "homeassistant/status"
LIGHT_COMMAND = "gardyn/light/command"

# paho's ReasonCode.is_failure is `self.value >= 0x80` (paho 2.0.0,
# src/paho/mqtt/reasoncodes.py). 0x80 is 128.
FAILURE_THRESHOLD = 128


class ReasonCodeDouble:
    """Stands in for paho.mqtt.reasoncodes.ReasonCode, which is stubbed away.

    Deliberately not a MagicMock. Three of this class's behaviours decide
    whether the code under test is right, and a MagicMock would satisfy all
    three by accident:

      is_failure  a property, `value >= 0x80`. A MagicMock's attribute is a
                  truthy Mock, so EVERY rc would read as a refusal and the
                  accepted path would never be exercised.
      __eq__      compares against a bare int by value, which is what makes
                  `rc != 0` a legal thing to write about a ReasonCode at all.
      __str__     the reason name. This is what f-strings put in the log, and
                  it is why the pre-fix log line read `Connected with result
                  code Not authorized`.

    Written from a measured run of the real library rather than from the happy
    path - see the table in this module's docstring.
    """

    def __init__(self, value, name):
        self.value = value
        self.name = name

    @property
    def is_failure(self):
        return self.value >= FAILURE_THRESHOLD

    def __eq__(self, other):
        if isinstance(other, int):
            return self.value == other
        if isinstance(other, ReasonCodeDouble):
            return self.value == other.value
        return NotImplemented

    def __hash__(self):
        return hash(self.value)

    def __str__(self):
        return self.name


ACCEPTED = ReasonCodeDouble(0, "Success")
NOT_AUTHORIZED = ReasonCodeDouble(135, "Not authorized")
BAD_CREDENTIALS = ReasonCodeDouble(134, "Bad user name or password")
SERVER_UNAVAILABLE = ReasonCodeDouble(136, "Server unavailable")
IDENTIFIER_REJECTED = ReasonCodeDouble(133, "Client identifier not valid")

EVERY_REFUSAL = [NOT_AUTHORIZED, BAD_CREDENTIALS, SERVER_UNAVAILABLE,
                 IDENTIFIER_REJECTED]


class ConnackTestBase(unittest.TestCase):
    def setUp(self):
        self.client = RecordingClient()
        mqtt_mod.client = self.client
        mqtt_mod.pump = MagicMock()
        mqtt_mod.pump.get_speed.return_value = 0
        mqtt_mod.light = MagicMock()
        mqtt_mod.light.get_brightness.return_value = 42
        mqtt_mod.distance_sensor = MagicMock()
        patch.object(mqtt_mod, "flash_lights").start()

        # start_publisher_threads() is NOT patched here, unlike in the sibling
        # suites. Its once-only flag IS the thing the acceptance criterion names
        # ("leaves the publisher-threads guard unburned"), so a double in front
        # of it would move the assertion off the state and onto a call count.
        #
        # What is patched instead is the two THREAD TARGETS it hands to
        # threading.Thread. Both are `while True:` loops in the real module, so
        # letting them start would leave the suite with two spinning daemon
        # threads publishing camera frames. A MagicMock target runs once and the
        # thread exits, which keeps the real guard, the real lock and the real
        # thread creation in the path.
        patch.object(mqtt_mod, "publish_pcb_temperature").start()
        patch.object(mqtt_mod, "publish_images").start()

        # MODULE-SCOPED STATE. The flag is a process global and survives between
        # test methods, so whether a case saw an unburned guard would otherwise
        # depend on unittest's method ordering.
        mqtt_mod._publisher_threads_started = False
        self.addCleanup(setattr, mqtt_mod, "_publisher_threads_started", False)
        mqtt_mod._last_birth_announce = None
        self.addCleanup(setattr, mqtt_mod, "_last_birth_announce", None)
        self.addCleanup(patch.stopall)

    def assertGuardUnburned(self):
        self.assertFalse(
            mqtt_mod._publisher_threads_started,
            "the publisher-threads once-only guard was burned on a connection "
            "that never came up, so the loops are running against a dead "
            "socket and the connect that succeeds will correctly decline to "
            "re-start them - PCB temperature `unknown` for up to 30 minutes",
        )


class TestARefusedConnackDoesNothing(ConnackTestBase):
    """The acceptance criterion, asserted directly on each of its three halves.

    Every case here runs the REAL on_connect against a refused reason code. The
    accepted-path class below is what makes these assertions mean anything: on
    its own, "the guard is unburned" is also what a permanently broken
    on_connect would report.
    """

    def test_a_refusal_leaves_the_publisher_thread_guard_unburned(self):
        for rc in EVERY_REFUSAL:
            with self.subTest(rc=str(rc)):
                mqtt_mod._publisher_threads_started = False
                mqtt_mod.on_connect(self.client, None, None, rc)
                self.assertGuardUnburned()

    def test_a_refusal_announces_nothing(self):
        for rc in EVERY_REFUSAL:
            with self.subTest(rc=str(rc)):
                self.client.calls.clear()
                mqtt_mod.on_connect(self.client, None, None, rc)
                self.assertEqual(
                    [], self.client.calls,
                    "discovery, availability or light state was published into "
                    "a connection the broker is closing",
                )

    def test_a_refusal_subscribes_to_nothing(self):
        for rc in EVERY_REFUSAL:
            with self.subTest(rc=str(rc)):
                self.client.subscriptions.clear()
                mqtt_mod.on_connect(self.client, None, None, rc)
                self.assertEqual([], self.client.subscriptions)

    def test_a_refusal_is_logged_as_an_error_naming_the_reason(self):
        """The device's failure mode is silence, so the log is the only place a
        refusal is ever visible. Asserted on "REFUSED", which appears in exactly
        one string in mqtt.py - matching on the reason name instead would also
        match the generic decode line if a payload happened to contain it."""
        with self.assertLogs(mqtt_mod.logger, level="ERROR") as captured:
            mqtt_mod.on_connect(self.client, None, None, NOT_AUTHORIZED)
        messages = [r.getMessage() for r in captured.records]
        refusals = [m for m in messages if "REFUSED" in m]
        self.assertEqual(1, len(refusals), messages)
        self.assertIn("Not authorized", refusals[0],
                      "the log names the refusal but not which one, so a "
                      "credential problem is indistinguishable from a broker "
                      "that is down")

    def test_a_refusal_does_not_claim_the_client_connected(self):
        """The original symptom, and the reason this went unnoticed for so long:
        the log line said `Connected with result code Not authorized`."""
        with self.assertLogs(mqtt_mod.logger, level="INFO") as captured:
            mqtt_mod.on_connect(self.client, None, None, NOT_AUTHORIZED)
        for message in (r.getMessage() for r in captured.records):
            self.assertNotIn(
                "Connected with result code", message,
                "the log claims a connection that was refused",
            )


class TestAnAcceptedConnackStillDoesEverything(ConnackTestBase):
    """The other direction, and the control on the class above.

    An over-correction - gating too much, or inverting the comparison - is not
    a smaller bug than the one being fixed. It would leave the device connected
    and permanently silent, which is the 2026-08-05 outage again.
    """

    def test_an_accepted_connack_burns_the_publisher_thread_guard(self):
        mqtt_mod.on_connect(self.client, None, None, ACCEPTED)
        self.assertTrue(
            mqtt_mod._publisher_threads_started,
            "the publishers never started on a good connection, so the PCB "
            "temperature and camera entities never update",
        )

    def test_an_accepted_connack_announces(self):
        mqtt_mod.on_connect(self.client, None, None, ACCEPTED)
        topics = [c.topic for c in self.client.calls]
        self.assertIn("gardyn/status", topics)
        self.assertIn(f"homeassistant/light/gardyn/{ID}_light/config", topics)

    def test_an_accepted_connack_subscribes(self):
        mqtt_mod.on_connect(self.client, None, None, ACCEPTED)
        self.assertIn(HA_STATUS, [t for t, _ in self.client.subscriptions])

    def test_a_bare_int_zero_is_still_an_acceptance(self):
        """paho's VERSION1 + MQTTv3 callback passes the bare v3 return code, and
        every existing case in this repo's suite calls on_connect(..., 0). A
        gate that only understood ReasonCode would refuse both."""
        mqtt_mod.on_connect(self.client, None, None, 0)
        self.assertTrue(mqtt_mod._publisher_threads_started)
        self.assertNotEqual([], self.client.calls)


class TestTheGuardTheFixProtects(ConnackTestBase):
    """The defect end to end, counted in threads rather than in flags.

    Everything above asserts the flag, which is a PROXY for the thing that
    actually breaks: the periodic publishers never starting. This class counts
    the publisher threads themselves, so it holds even if the guard is ever
    reimplemented with different state.

    threading.Thread is patched here and nowhere else in this file, because
    every other case wants the real thread creation exercised. Nothing else in
    the process spawns a thread inside these windows.
    """

    def _connect(self, rc):
        mqtt_mod.on_connect(self.client, None, None, rc)

    def test_one_good_connect_starts_both_publishers(self):
        with patch.object(mqtt_mod.threading, "Thread") as thread:
            self._connect(ACCEPTED)
        self.assertEqual(2, thread.call_count,
                         "the PCB temperature and camera loops are the two "
                         "publishers on_connect is responsible for starting")

    def test_a_second_good_connect_does_not_start_them_again(self):
        """The invariant the fix's whole rationale rests on: the flag is
        per-PROCESS, and on_connect fires on every reconnect (roughly 25 times a
        day). Without this, "burning the guard" would not be a durable loss."""
        with patch.object(mqtt_mod.threading, "Thread") as thread:
            self._connect(ACCEPTED)
            self._connect(ACCEPTED)
        self.assertEqual(2, thread.call_count,
                         "a reconnect spawned a second set of publisher "
                         "threads; they accumulate on a 512 MB host")

    def test_the_publishers_start_on_the_connect_that_actually_succeeded(self):
        """THE DEFECT, AS IT ACTUALLY HAPPENS - a broker password rotated under
        a running Pi, so the first CONNACK is refused and the next succeeds.

        WHICH ASSERTION HERE IS THE DISCRIMINATING ONE. The intermediate 0 is:
        pre-fix, the refused pass spawned both loops against a client that was
        not connected, so it read 2 and this line fails. The final 2 is NOT
        discriminating - it holds pre-fix too, because those same two threads
        already existed. It is kept as a guard against over-correcting into a
        gate that swallows the healthy connect as well, which is the failure
        mode that would silence the device completely.
        """
        with patch.object(mqtt_mod.threading, "Thread") as thread:
            self._connect(NOT_AUTHORIZED)
            self.assertEqual(
                0, thread.call_count,
                "publisher loops were spawned against a refused connection; "
                "their first publish is dropped with MQTT_ERR_NO_CONN and the "
                "PCB temperature entity is `unknown` for the next 30 minutes",
            )
            self._connect(ACCEPTED)
        self.assertEqual(
            2, thread.call_count,
            "the gate swallowed a HEALTHY connect - no publishers at all",
        )


class TestWhichQuestionTheGateAsks(unittest.TestCase):
    """_connack_refused() in isolation, including the case that separates the
    two plausible spellings of it."""

    def test_the_measured_connack_refusals_are_all_refusals(self):
        # The numbers are the ones a real paho 2.0.0 produced - see the table in
        # this module's docstring. Written as literals rather than derived from
        # FAILURE_THRESHOLD, so a mutant that moves the threshold cannot move
        # the expectation with it.
        for value, name in [(133, "Client identifier not valid"),
                            (134, "Bad user name or password"),
                            (135, "Not authorized"),
                            (136, "Server unavailable")]:
            with self.subTest(name=name):
                self.assertTrue(
                    mqtt_mod._connack_refused(ReasonCodeDouble(value, name)))

    def test_success_is_not_a_refusal(self):
        self.assertFalse(mqtt_mod._connack_refused(ReasonCodeDouble(0, "Success")))

    def test_it_reads_is_failure_rather_than_comparing_against_zero(self):
        """The one input on which the two spellings disagree.

        BE PRECISE ABOUT WHAT THIS PINS. No CONNACK reason code is both non-zero
        and not a failure - for CONNACK the two spellings agree everywhere - so
        this input is not reachable through a real connection. It is here
        because `is_failure` and `rc != 0` are interchangeable by inspection,
        which is exactly the condition under which somebody simplifies one into
        the other. Other packet types do use the low values (SUBACK's granted-
        QoS codes are 0, 1 and 2), so the distinction is real in the library
        even where it is unreachable in this callback.
        """
        low_but_successful = ReasonCodeDouble(1, "Granted QoS 1")
        self.assertNotEqual(0, low_but_successful.value)
        self.assertFalse(low_but_successful.is_failure)
        self.assertFalse(
            mqtt_mod._connack_refused(low_but_successful),
            "the gate is comparing rc against 0 rather than asking whether the "
            "reason code is a failure",
        )

    def test_the_failure_boundary_is_where_paho_puts_it(self):
        self.assertFalse(mqtt_mod._connack_refused(ReasonCodeDouble(127, "below")))
        self.assertTrue(mqtt_mod._connack_refused(ReasonCodeDouble(128, "Unspecified error")))

    def test_bare_ints_fall_back_to_a_comparison_against_zero(self):
        """An int has no is_failure, so the fallback is the only thing that can
        classify it. 1..5 are the v3.1.1 refusal codes."""
        self.assertFalse(mqtt_mod._connack_refused(0))
        for v3_code in (1, 2, 3, 4, 5):
            with self.subTest(v3_code=v3_code):
                self.assertTrue(mqtt_mod._connack_refused(v3_code))


class TestTheDecodeLineCannotBeForged(unittest.TestCase):
    """The generic decode line, fed a payload from outside this namespace."""

    def setUp(self):
        self.client = RecordingClient()
        mqtt_mod.light = MagicMock()
        mqtt_mod.light.get_brightness.return_value = 42
        mqtt_mod.pump = MagicMock()
        mqtt_mod.pump.get_speed.return_value = 0
        patch.object(mqtt_mod, "flash_lights").start()
        patch.object(mqtt_mod, "announce_to_home_assistant").start()
        mqtt_mod._last_birth_announce = None
        self.addCleanup(setattr, mqtt_mod, "_last_birth_announce", None)
        self.addCleanup(patch.stopall)

    def _decode_record(self, topic, raw_payload):
        """Deliver one message and return ONLY the generic decode line.

        Selected by prefix rather than by index or by searching the whole
        capture. The Home Assistant status branch logs the payload too, with
        {payload!r} of its own, so an assertion over every captured message
        would pass on that line while the line under test stayed broken.
        """
        msg = MagicMock()
        msg.topic = topic
        msg.payload = raw_payload
        with self.assertLogs(mqtt_mod.logger, level="INFO") as captured:
            mqtt_mod.on_message(self.client, None, msg)
        decoded = [r.getMessage() for r in captured.records
                   if r.getMessage().startswith("Decoded payload on ")]
        self.assertEqual(1, len(decoded),
                         f"expected exactly one decode line, got {decoded!r}")
        return decoded[0]

    def test_a_newline_in_the_payload_cannot_forge_a_log_line(self):
        forged = b"online\n2026-08-08 12:00:00,000 - mqtt - ERROR - pump seized"
        # The fixture control: the payload really does carry a raw newline, so
        # a green result below is the escaping working rather than the input
        # being harmless.
        self.assertIn(b"\n", forged)

        message = self._decode_record(HA_STATUS, forged)
        self.assertNotIn("\n", message,
                         "a remote payload put a line break into gardyn.log, so "
                         "it can write log lines of its own")
        self.assertIn("\\n", message,
                      "the line break should survive as an escape, not vanish")
        self.assertIn("pump seized", message,
                      "the payload must still be readable; escaping it is not "
                      "the same as dropping it")

    def test_a_carriage_return_in_the_payload_is_escaped(self):
        """Worse than a newline in a terminal: CR rewrites the line just
        printed, so the forgery replaces real output rather than adding to it."""
        message = self._decode_record(HA_STATUS, b"online\rall clear")
        self.assertNotIn("\r", message)
        self.assertIn("\\r", message)

    def test_an_ansi_escape_in_the_payload_is_escaped(self):
        message = self._decode_record(HA_STATUS, b"online\x1b[2Jcleared")
        self.assertNotIn("\x1b", message)
        self.assertIn("\\x1b", message)

    def test_an_ordinary_payload_still_reads_naturally(self):
        """repr() of a plain string is the quoted form the line already had, so
        the fix does not change what an operator reads for normal traffic - and
        the sibling suites that match on that shape stay valid."""
        message = self._decode_record(LIGHT_COMMAND, b"ON")
        self.assertEqual(f"Decoded payload on {LIGHT_COMMAND}: 'ON'", message)


if __name__ == "__main__":
    unittest.main()
