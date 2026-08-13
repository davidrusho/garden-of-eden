"""Tests for light_scheduler.py — the I/O half of the local photoperiod (T-527.5).

    python3 -m unittest tests.test_light_scheduler

NO STUBS, for the same reason tests/test_light_schedule.py has none: the module
under test imports nothing but the standard library and light_schedule, and the
lamp arrives as a constructor argument. So this file must NOT appear in
tests/test_suite_isolation.py's STUBBING_MODULES — that registry asserts set
equality in both directions, and listing a file that installs no stub fails it
just as surely as omitting one that does.

WHAT IS TESTED HERE OR NOWHERE. The target is a Pi with no console, no
keyboard, no SD removal and no reimage, and the branches that decide whether
its garden goes dark cannot be demonstrated on it:

  * a boot with no network, so the clock is untrustworthy — forcing that on the
    host means taking down its only management path.
  * a persisted state file truncated by a power cut.
  * `timedatectl` itself failing, which is the branch that decides whether a
    broken tool can freeze the lamp indefinitely.
  * an unwritable state directory.

Every one of them is a fake here and a lived incident there.
"""
import inspect
import io
import json
import logging
import re
import os
import subprocess
import sys
import tempfile
import threading
import unittest
import unittest.mock
from datetime import datetime, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import light_schedule as ls  # noqa: E402
import light_scheduler as lsr  # noqa: E402


SHIPPED = "GARDYN_LIGHT_SCHEDULE=03:00=50,04:00=100,18:00=50,19:00=0\n"

# Bodies a truncated, half-written or power-cut state file can hold. Hoisted to
# a module constant with a size guard below rather than inlined, because a
# corpus is the one thing a mutation battery cannot defend by itself: emptying
# an inline list makes its subTest loop vacuous and every assertion in it
# passes. This is the corpus whose narrowing costs the most — every entry here
# must read as ABSENT rather than as zero, or an unsynced boot holds the garden
# dark.
#
# "+50" IS DELIBERATELY ABSENT, and the omission is a finding rather than an
# oversight. It was in this tuple first and the test failed: int("+50") is 50,
# not an error. That is the right answer — a signed decimal is a lenient
# spelling of a legitimate brightness, not corruption, and nothing here can
# turn it into a dark garden because it cannot read as zero. The corpus is for
# bodies that a naive parser would silently turn INTO a number, chiefly zero.
CORRUPT_STATE_BODIES = ("", "   ", "\x00\x00", "5 0", "fifty", "50%", "50.0",
                        "0x32", " 50 50", "50\n60", "None", "-")


class FakeLight:
    """Stands in for app/sensors/light/light.py's Light.

    It MODELS THE HARDWARE rather than recording calls: `get_brightness`
    returns what `set_duty_cycle` was last given, because that round-trip is
    exactly what the scheduler's "is the lamp already there?" branch reads. A
    pure call-recorder would make that branch untestable, since it would never
    report the lamp as having moved.

    `fail_on_set` / `fail_on_get` raise a real exception with a message, not a
    bare sentinel — a double whose error path is a silent `raise Exception()`
    cannot show that the caller logs anything useful.
    """

    def __init__(self, brightness=0.0):
        self.brightness = float(brightness)
        self.commands = []
        self.fail_on_set = None
        self.fail_on_get = None

    def set_duty_cycle(self, value):
        if self.fail_on_set is not None:
            raise self.fail_on_set
        if not 0 <= value <= 100:
            # The real Light raises exactly here. Reproduced so a regression in
            # the scheduler's clamping shows up as a failure rather than as a
            # fake quietly accepting an illegal duty cycle.
            raise ValueError("Speed must be between 0 and 100")
        self.commands.append(value)
        self.brightness = float(value)

    def get_brightness(self):
        if self.fail_on_get is not None:
            raise self.fail_on_get
        return self.brightness


def fake_ntp(answer="yes", returncode=0, stderr="", raises=None):
    """A stand-in for subprocess.run against `timedatectl`.

    Records the argv it was handed so a test can assert WHICH question was
    asked — a probe that runs the wrong command and gets a plausible answer is
    the failure mode this whole file exists to rule out elsewhere.

    IT HONOURS text= AND capture_output= RATHER THAN IGNORING THEM, which is
    the T-527.20 finding it was rebuilt for. Written from the happy path, this
    double returned a `str` stdout whatever kwargs it was handed — so mutants
    deleting `text=True` or `capture_output=True` from the real call SURVIVED a
    full battery. Both are load-bearing against the real subprocess.run:
    without `capture_output` stdout is None and the probe reads `""`; without
    `text` it is `bytes`, and `b"yes"` never equals `"yes"`, so the gate would
    report an unrecognised answer forever. A double that cannot tell those
    apart makes the mutant undetectable no matter how the assertion is written.
    """
    calls = []

    class _Completed:
        def __init__(self, stdout, stderr):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if raises is not None:
            raise raises
        if not kwargs.get("capture_output"):
            # subprocess.run without capture_output leaves the child's output
            # on the parent's stdout and sets .stdout to None.
            return _Completed(None, None)
        if kwargs.get("text"):
            return _Completed(answer, stderr)
        return _Completed(answer.encode(), stderr.encode())

    run.calls = calls
    return run


class ParseEnvTests(unittest.TestCase):

    def test_plain_key_value(self):
        self.assertEqual({"A": "1", "B": "2"}, lsr.parse_env("A=1\nB=2\n"))

    def test_comments_and_blank_lines_are_skipped(self):
        self.assertEqual({"A": "1"}, lsr.parse_env("# note\n\n  \nA=1\n"))

    def test_leading_export_is_stripped(self):
        self.assertEqual({"A": "1"}, lsr.parse_env("export A=1\n"))

    def test_matching_surrounding_quotes_are_stripped(self):
        self.assertEqual({"A": "1", "B": "2"}, lsr.parse_env("A='1'\nB=\"2\"\n"))

    def test_mismatched_quotes_are_left_alone(self):
        self.assertEqual({"A": "'1\""}, lsr.parse_env("A='1\"\n"))

    def test_a_line_with_no_equals_is_skipped(self):
        self.assertEqual({"A": "1"}, lsr.parse_env("nonsense\nA=1\n"))

    def test_an_empty_key_is_skipped(self):
        self.assertEqual({"A": "1"}, lsr.parse_env("=orphan\nA=1\n"))

    def test_a_value_containing_equals_keeps_the_rest(self):
        # GARDYN_LIGHT_SCHEDULE's own values contain '=', so this is not an
        # edge case here — it is the format.
        self.assertEqual({"S": "03:00=50,04:00=100"},
                         lsr.parse_env("S=03:00=50,04:00=100\n"))

    def test_none_and_empty_give_an_empty_mapping(self):
        self.assertEqual({}, lsr.parse_env(None))
        self.assertEqual({}, lsr.parse_env(""))


class LoadScheduleTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t527-cfg-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "light.env")

    def _write(self, body):
        with open(self.path, "w") as fh:
            fh.write(body)

    def test_a_good_file_is_read(self):
        self._write("GARDYN_LIGHT_SCHEDULE=06:00=40,20:00=0\n")
        schedule, note = lsr.load_schedule(self.path)
        self.assertIsNone(note)
        self.assertEqual(
            [(time(6, 0), 40), (time(20, 0), 0)],
            [(b.at, b.brightness) for b in schedule.boundaries],
        )

    def test_a_missing_file_falls_back_and_says_so(self):
        schedule, note = lsr.load_schedule(os.path.join(self.tmp, "nope.env"))
        self.assertIs(ls.DEFAULT_SCHEDULE, schedule)
        self.assertIn("nope.env", note)

    def test_an_unreadable_file_falls_back(self):
        def boom(_path):
            raise PermissionError(13, "Permission denied")
        schedule, note = lsr.load_schedule(self.path, _open=boom)
        self.assertIs(ls.DEFAULT_SCHEDULE, schedule)
        self.assertIn("Permission denied", note)

    def test_a_binary_file_falls_back(self):
        """A power cut mid-write, or somebody's stray dd. UnicodeDecodeError is
        a ValueError subclass, so without its own clause it would be caught by
        the ScheduleConfigError branch by accident and reported as a content
        problem in a file that was never read."""
        with open(self.path, "wb") as fh:
            fh.write(b"\xff\xfe\x00binary")
        schedule, note = lsr.load_schedule(self.path)
        self.assertIs(ls.DEFAULT_SCHEDULE, schedule)
        self.assertIn("not text", note)

    def test_a_malformed_schedule_falls_back(self):
        self._write("GARDYN_LIGHT_SCHEDULE=3:00=50\n")
        schedule, note = lsr.load_schedule(self.path)
        self.assertIs(ls.DEFAULT_SCHEDULE, schedule)
        self.assertIn("not usable", note)

    def test_an_empty_file_falls_back(self):
        self._write("")
        schedule, note = lsr.load_schedule(self.path)
        self.assertIs(ls.DEFAULT_SCHEDULE, schedule)

    def test_the_fallback_is_the_photoperiod_the_template_ships(self):
        """The .env.example header claims its values are DEFAULT_SCHEDULE
        "verbatim". That is a claim about two files, so assert it rather than
        maintaining it by hand — a drifting template is how somebody comes to
        believe the fallback is something it is not."""
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        template = os.path.join(
            repo, "services", "etc", "gardyn", "light.env.example"
        )
        with open(template) as fh:
            env = lsr.parse_env(fh.read())
        from_template = ls.parse_schedule(env)
        self.assertEqual(ls.DEFAULT_SCHEDULE.boundaries, from_template.boundaries)
        self.assertEqual(
            ls.DEFAULT_SCHEDULE.unsynced_fallback, from_template.unsynced_fallback
        )

    def test_load_schedule_never_raises_for_any_body(self):
        """The caller contract in light_schedule.py's docstring is "log loudly
        and fall back, never exit". A raise out of here reaches the scheduler
        thread, which dies silently with the process still at exit status 0."""
        for body in ("", "GARDYN_LIGHT_SCHEDULE=\n", "GARDYN_LIGHT_SCHEDULE=,\n",
                     "GARDYN_LIGHT_SCHEDULE=03:00=50,\n",
                     "GARDYN_LIGHT_SCHEDULE=25:00=50\n",
                     "GARDYN_LIGHT_SCHEDULE=03:00=50 50\n",
                     "GARDYN_LIGHT_SCHEDULE=03:00=999\n",
                     "GARDYN_LIGHT_SCHEDULE=03:00=50,03:00=60\n",
                     "GARDYN_LIGHT_SCHEDULE=03:00=50\nGARDYN_LIGHT_UNSYNCED_FALLBACK=abc\n",
                     "\x00\x01\x02"):
            with self.subTest(body=body):
                self._write(body)
                schedule, note = lsr.load_schedule(self.path)
                self.assertIsNotNone(note, "a bad body reported no problem")
                self.assertIs(ls.DEFAULT_SCHEDULE, schedule)


class ClockStateTests(unittest.TestCase):
    """read_clock_state REPORTS; it decides nothing.

    The tri-state is the T-527.19 repair. An earlier version returned a bool
    and mapped every failure to True, which is the right DRIVING decision and
    destroys the distinction the latch needs: "the kernel says the clock is
    good" and "I could not ask" must not be the same value, or a permanently
    broken `timedatectl` latches the gate open on the strength of its own
    breakage. ClockVerdictTests below is where the driving policy is pinned.
    """

    def test_yes_is_synced(self):
        state, note = lsr.read_clock_state(_run=fake_ntp("yes\n"))
        self.assertEqual(lsr.CLOCK_SYNCED, state)
        self.assertIsNone(note)

    def test_no_is_unsynced(self):
        state, note = lsr.read_clock_state(_run=fake_ntp("no\n"))
        self.assertEqual(lsr.CLOCK_UNSYNCED, state)
        self.assertIsNone(note)

    def test_the_three_states_are_distinct(self):
        """Three distinct strings, asserted rather than assumed. Collapsing any
        two of them silently reinstates the bool this replaced — CLOCK_UNKNOWN
        equal to CLOCK_SYNCED would latch on a broken query, and equal to
        CLOCK_UNSYNCED would freeze the lamp on one."""
        self.assertEqual(
            3, len({lsr.CLOCK_SYNCED, lsr.CLOCK_UNSYNCED, lsr.CLOCK_UNKNOWN})
        )

    def test_it_asks_the_kernel_question_and_not_some_other_one(self):
        """Which question is asked is the whole content of this probe. An
        answer of "yes" is available from several `timedatectl` properties, so
        a test that only checks the parsing would pass against a call reading
        `NTP` (is a time service enabled) instead of `NTPSynchronized` (does
        the kernel consider the clock good)."""
        run = fake_ntp("yes\n")
        lsr.read_clock_state(_run=run)
        argv = run.calls[0][0]
        self.assertEqual(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"], argv
        )

    def test_a_timeout_is_passed_to_the_subprocess(self):
        run = fake_ntp("yes\n")
        lsr.read_clock_state(_run=run, timeout=3)
        self.assertEqual(3, run.calls[0][1]["timeout"])

    def test_it_captures_output_as_text(self):
        """Both kwargs are load-bearing and both used to be unpinned, so a
        mutant deleting either survived a full battery (T-527.20). fake_ntp
        now models what the real subprocess.run does without them; these
        assertions are what make that modelling worth anything."""
        run = fake_ntp("yes\n")
        lsr.read_clock_state(_run=run)
        kwargs = run.calls[0][1]
        self.assertTrue(kwargs.get("capture_output"))
        self.assertTrue(kwargs.get("text"))

    def test_bytes_stdout_is_not_read_as_synced(self):
        """Dropping text=True hands back bytes. b"yes" != "yes", so the gate
        would report an unrecognised answer forever — and this is the
        assertion that makes that mutant die instead of surviving."""
        def run_without_text(argv, **kwargs):
            kwargs.pop("text", None)
            return fake_ntp("yes\n")(argv, **kwargs)

        state, note = lsr.read_clock_state(_run=run_without_text)
        self.assertEqual(lsr.CLOCK_UNKNOWN, state)
        self.assertIsNotNone(note)

    def test_a_nonzero_exit_is_unknown_and_not_unsynced(self):
        """The deliberate half, and the reason it is UNKNOWN rather than
        UNSYNCED. Believing a bad clock costs a wrong-time photoperiod;
        disbelieving a good one HOLDS the lamp wherever it was, which is the
        dark garden T-527 exists to remove. _clock_verdict drives on this —
        but must not remember it as evidence of a sync."""
        state, note = lsr.read_clock_state(
            _run=fake_ntp("", returncode=1, stderr="Failed to connect to bus\n")
        )
        self.assertEqual(lsr.CLOCK_UNKNOWN, state)
        self.assertIn("Failed to connect to bus", note)

    def test_a_missing_binary_is_unknown(self):
        state, note = lsr.read_clock_state(
            _run=fake_ntp(raises=FileNotFoundError(2, "No such file or directory"))
        )
        self.assertEqual(lsr.CLOCK_UNKNOWN, state)
        self.assertIsNotNone(note)

    def test_a_hung_bus_is_unknown(self):
        state, note = lsr.read_clock_state(
            _run=fake_ntp(raises=subprocess.TimeoutExpired("timedatectl", 10))
        )
        self.assertEqual(lsr.CLOCK_UNKNOWN, state)
        self.assertIsNotNone(note)

    def test_an_unrecognised_answer_is_unknown_and_says_so(self):
        state, note = lsr.read_clock_state(_run=fake_ntp("maybe\n"))
        self.assertEqual(lsr.CLOCK_UNKNOWN, state)
        self.assertIn("maybe", note)

    def test_an_empty_answer_is_not_read_as_no(self):
        """`""` is falsy, and the tempting implementation is `answer == "yes"`.
        That reads an empty stdout — a `timedatectl` that printed nothing — as
        an unsynchronised clock, which is the freeze branch."""
        state, note = lsr.read_clock_state(_run=fake_ntp(""))
        self.assertEqual(lsr.CLOCK_UNKNOWN, state)
        self.assertIsNotNone(note)


class SecondsSinceBootTests(unittest.TestCase):
    """/proc/uptime, and every way it can fail to answer.

    None and never 0 on failure: 0 reads as "the host just booted", which
    EXTENDS the never-synced hold this value exists to bound. The failure
    direction matters more than the parsing here.
    """

    def _reader(self, body):
        import io

        def _open(path):
            return io.StringIO(body)
        return _open

    def test_it_reads_the_first_field(self):
        self.assertEqual(
            12345.67,
            lsr.seconds_since_boot(_open=self._reader("12345.67 98765.43\n")),
        )

    def test_a_missing_procfs_is_none(self):
        def _open(path):
            raise FileNotFoundError(2, "No such file or directory")
        self.assertIsNone(lsr.seconds_since_boot(_open=_open))

    def test_a_binary_body_is_none(self):
        def _open(path):
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        self.assertIsNone(lsr.seconds_since_boot(_open=_open))

    def test_an_empty_body_is_none_not_zero(self):
        self.assertIsNone(lsr.seconds_since_boot(_open=self._reader("")))

    def test_an_unparseable_body_is_none_not_zero(self):
        self.assertIsNone(lsr.seconds_since_boot(_open=self._reader("up 3 days\n")))

    def test_it_reads_the_real_file_where_there_is_one(self):
        """A control on the two failure tests above: on Linux this returns a
        real, positive number, and on a laptop with no procfs it returns None.
        Both are correct; what would be wrong is a raise, since this runs on
        the scheduler thread where a raise is silent."""
        value = lsr.seconds_since_boot()
        if value is not None:
            self.assertGreater(value, 0)


class StatePathTests(unittest.TestCase):

    def test_state_directory_is_honoured(self):
        self.assertEqual(
            "/var/lib/gardyn/light-phase",
            lsr.default_state_path({"STATE_DIRECTORY": "/var/lib/gardyn"}),
        )

    def test_the_first_of_several_state_directories_is_used(self):
        self.assertEqual(
            "/var/lib/one/light-phase",
            lsr.default_state_path({"STATE_DIRECTORY": "/var/lib/one:/var/lib/two"}),
        )

    def test_an_absent_state_directory_falls_back_to_the_literal(self):
        self.assertEqual("/var/lib/gardyn/light-phase", lsr.default_state_path({}))

    def test_an_empty_state_directory_falls_back_to_the_literal(self):
        self.assertEqual(
            "/var/lib/gardyn/light-phase",
            lsr.default_state_path({"STATE_DIRECTORY": "  "}),
        )


class LastAppliedTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t527-state-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.path = os.path.join(self.tmp, "light-phase")

    def test_a_written_value_round_trips(self):
        self.assertIsNone(lsr.write_last_applied(self.path, 55))
        self.assertEqual(55, lsr.read_last_applied(self.path))

    def test_zero_round_trips_and_is_not_confused_with_absent(self):
        """0 is a legitimate persisted brightness — the lamp is off overnight —
        and `if value:` would drop it. Absent must be None and off must be 0."""
        lsr.write_last_applied(self.path, 0)
        self.assertEqual(0, lsr.read_last_applied(self.path))

    def test_an_absent_file_reads_as_absent(self):
        self.assertIsNone(lsr.read_last_applied(os.path.join(self.tmp, "nope")))

    def test_a_truncated_file_reads_as_ABSENT_and_not_as_off(self):
        """The load-bearing distinction in this class. `_clamped` maps junk to
        0, and 0 is a lamp that is off; if a corrupt file read as 0 then
        decide() would HOLD the garden dark through an unsynced boot on the
        strength of a power cut. None routes to the configured unsynced
        fallback, which is lit."""
        for junk in CORRUPT_STATE_BODIES:
            with self.subTest(junk=junk):
                with open(self.path, "w") as fh:
                    fh.write(junk)
                self.assertIsNone(
                    lsr.read_last_applied(self.path),
                    f"{junk!r} was read as a brightness rather than as corruption",
                )

    def test_the_corruption_corpus_has_not_been_quietly_narrowed(self):
        """The test above loops over CORRUPT_STATE_BODIES with subTest, and an
        EMPTY corpus makes that loop vacuous while it still reports green — the
        one failure mode a mutation battery cannot see, because there is no
        construct left to mutate. This is the guard on the corpus itself."""
        self.assertGreaterEqual(len(CORRUPT_STATE_BODIES), 8)
        self.assertIn("", CORRUPT_STATE_BODIES, "an empty file is the likeliest shape")

    def test_an_undecodable_file_reads_as_absent(self):
        with open(self.path, "wb") as fh:
            fh.write(b"\xff\xfe50")
        self.assertIsNone(lsr.read_last_applied(self.path))

    def test_an_out_of_range_value_reads_as_absent(self):
        """Nothing here ever writes one, so seeing one means the file is not
        what we wrote. Clamping it would launder corruption into a brightness."""
        for junk in ("101", "-1", "999999"):
            with self.subTest(junk=junk):
                with open(self.path, "w") as fh:
                    fh.write(junk)
                self.assertIsNone(lsr.read_last_applied(self.path))

    def test_the_write_is_atomic_and_leaves_no_temp_behind(self):
        lsr.write_last_applied(self.path, 40)
        self.assertEqual(["light-phase"], sorted(os.listdir(self.tmp)))

    def test_a_write_that_fails_LATE_leaves_the_previous_value_intact(self):
        """What atomicity actually buys, asserted rather than assumed. The
        reader is a process starting up after an unclean shutdown, so a
        half-written file is the plausible case; a direct write to the real
        path destroys the previous value before it can fail.

        Patching os.replace is what makes this a behavioural test rather than a
        source assertion: a version that writes straight to `path` never calls
        it, so the patch does nothing, the write succeeds, and the note check
        below fails."""
        lsr.write_last_applied(self.path, 40)
        with unittest.mock.patch(
            "light_scheduler.os.replace", side_effect=OSError(28, "No space left")
        ):
            note = lsr.write_last_applied(self.path, 70)
        self.assertIsNotNone(note, "a failed persist reported success")
        self.assertEqual(
            40, lsr.read_last_applied(self.path),
            "a failed write destroyed the brightness an unsynced boot holds",
        )
        # The cleanup path, which is reachable ONLY from here. The sibling test
        # below blocks the write before a temp file can exist, so it looks like
        # it covers this and cannot: a mutant deleting the os.unlink survived
        # the first battery against exactly that corpus gap.
        self.assertEqual(
            ["light-phase"], sorted(os.listdir(self.tmp)),
            "a failed write stranded its temp file",
        )

    def test_the_directory_is_created_if_it_is_missing(self):
        nested = os.path.join(self.tmp, "a", "b", "light-phase")
        self.assertIsNone(lsr.write_last_applied(nested, 30))
        self.assertEqual(30, lsr.read_last_applied(nested))

    def test_an_unwritable_location_reports_a_note_rather_than_raising(self):
        # A path whose parent is a FILE: makedirs and open both refuse, and no
        # amount of privilege changes that, so this is stable off the Pi.
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w") as fh:
            fh.write("not a directory")
        note = lsr.write_last_applied(os.path.join(blocker, "light-phase"), 20)
        self.assertIsNotNone(note)
        self.assertIn("cannot persist", note)

    def test_a_failed_write_leaves_no_temp_file_behind(self):
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w") as fh:
            fh.write("not a directory")
        lsr.write_last_applied(os.path.join(blocker, "light-phase"), 20)
        self.assertEqual(["blocker"], sorted(os.listdir(self.tmp)))


class SchedulerHarness(unittest.TestCase):
    """Shared scaffolding for the tick tests."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="t527-tick-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp, True)
        self.config = os.path.join(self.tmp, "light.env")
        self.state = os.path.join(self.tmp, "light-phase")
        with open(self.config, "w") as fh:
            fh.write(SHIPPED)
        self.light = FakeLight(brightness=0)
        # (decision.brightness, decision.source) per publish, which is exactly
        # the pair the scheduler promises not to republish unchanged.
        self.published = []
        self.clock = datetime(2026, 8, 9, 12, 0, 0)

    def build(self, **kwargs):
        kwargs.setdefault("config_path", self.config)
        kwargs.setdefault("state_path", self.state)
        kwargs.setdefault("now", lambda: self.clock)
        kwargs.setdefault("clock_probe", lambda: (lsr.CLOCK_SYNCED, None))
        # PINNED, NOT INHERITED, and this is not tidiness. The production
        # default reads /proc/uptime, which exists on the Pi and not on the
        # laptop these tests were written on — so an unpinned hold test would
        # pass here by reading None and fail on Linux the moment the host had
        # been up longer than the ceiling. A suite that has only ever run on
        # one machine cannot tell correct code from a satisfied machine.
        kwargs.setdefault("uptime", lambda: 0.0)
        return lsr.LightScheduler(
            self.light,
            lambda d: self.published.append((d.brightness, d.source)),
            **kwargs,
        )


class TickTests(SchedulerHarness):

    def test_a_midday_tick_drives_the_lamp_to_the_current_phase(self):
        decision = self.build().tick()
        self.assertEqual(100, decision.brightness)
        self.assertEqual(ls.SOURCE_SCHEDULE, decision.source)
        self.assertEqual([100], self.light.commands)

    def test_a_restart_at_any_hour_lands_on_the_right_phase_IMMEDIATELY(self):
        """T-527.5's acceptance criterion, and the reason the engine computes a
        phase instead of reacting to boundary edges. Each of these is a fresh
        scheduler with no memory, exactly as a process that has just been
        restarted by systemd has none."""
        for hour, expected in ((1, 0), (3, 50), (3, 50), (4, 100), (12, 100),
                               (18, 50), (19, 0), (23, 0)):
            with self.subTest(hour=hour):
                self.light = FakeLight(brightness=77)
                self.clock = datetime(2026, 8, 9, hour, 30, 0)
                decision = self.build().tick()
                self.assertEqual(expected, decision.brightness)
                self.assertEqual([expected], self.light.commands)

    def test_a_lamp_already_at_the_target_is_not_rewritten(self):
        self.light.brightness = 100
        scheduler = self.build()
        scheduler.tick()
        self.assertEqual([], self.light.commands)

    def test_an_unchanged_owner_and_brightness_is_not_republished(self):
        """The state topics are retained, so republishing an identical pair
        every 30 s is pure broker traffic on a single-antenna Zero W."""
        scheduler = self.build()
        for _ in range(4):
            scheduler.tick()
        self.assertEqual([(100, ls.SOURCE_SCHEDULE)], self.published)

    def test_a_lamp_within_rounding_of_the_target_is_not_rewritten(self):
        """PWM quantisation means get_brightness() need not return exactly what
        was commanded. Without the rounding this re-writes the pin every tick
        forever, and logs a transition every 30 s."""
        self.light.brightness = 99.6
        scheduler = self.build()
        scheduler.tick()
        self.assertEqual([], self.light.commands)

    def test_applying_persists_the_brightness(self):
        self.build().tick()
        self.assertEqual(100, lsr.read_last_applied(self.state))

    def test_applying_publishes_the_state_and_the_owner(self):
        self.build().tick()
        self.assertEqual([(100, ls.SOURCE_SCHEDULE)], self.published)

    def test_the_config_file_is_re_read_between_ticks(self):
        """No restart needed after an edit — the acceptance note on T-527.5
        says the file only has to be re-readable, and this is what makes that
        true. A schedule cached at construction would need an SSH session and a
        `systemctl restart` on a host where every deploy is remote-only."""
        scheduler = self.build()
        scheduler.tick()
        self.assertEqual([100], self.light.commands)
        with open(self.config, "w") as fh:
            fh.write("GARDYN_LIGHT_SCHEDULE=00:00=25\n")
        scheduler.tick()
        self.assertEqual([100, 25], self.light.commands)

    def test_a_broken_config_file_falls_back_without_stopping_the_tick(self):
        with open(self.config, "w") as fh:
            fh.write("GARDYN_LIGHT_SCHEDULE=nonsense\n")
        decision = self.build().tick()
        # DEFAULT_SCHEDULE at 12:00 is 100.
        self.assertEqual(100, decision.brightness)
        self.assertEqual([100], self.light.commands)

    # ------------------------------------------------------- unsynced clock

    def test_an_unsynced_clock_holds_the_persisted_brightness(self):
        lsr.write_last_applied(self.state, 50)
        self.light.brightness = 50
        decision = self.build(clock_probe=lambda: (lsr.CLOCK_UNSYNCED, None)).tick()
        self.assertEqual(50, decision.brightness)
        self.assertEqual(ls.SOURCE_HOLD, decision.source)
        self.assertEqual([], self.light.commands)

    def test_an_unsynced_clock_with_no_memory_uses_the_configured_fallback(self):
        with open(self.config, "w") as fh:
            fh.write(SHIPPED + "GARDYN_LIGHT_UNSYNCED_FALLBACK=60\n")
        decision = self.build(clock_probe=lambda: (lsr.CLOCK_UNSYNCED, None)).tick()
        self.assertEqual(60, decision.brightness)
        self.assertEqual(ls.SOURCE_FALLBACK, decision.source)

    def test_an_unsynced_clock_with_a_CORRUPT_memory_does_not_go_dark(self):
        """The compound failure this design is most exposed to: the Pi reboots
        with no network (2026-08-06, twice inside twelve minutes) AND the state
        file was mid-write when the power went. If corruption read as 0, the
        garden would hold dark for the whole outage."""
        with open(self.state, "w") as fh:
            fh.write("5")
            fh.write("\x00\x00")
        decision = self.build(clock_probe=lambda: (lsr.CLOCK_UNSYNCED, None)).tick()
        self.assertEqual(100, decision.brightness)
        self.assertEqual(ls.SOURCE_FALLBACK, decision.source)

    def test_a_clock_that_syncs_between_ticks_returns_to_the_schedule(self):
        lsr.write_last_applied(self.state, 50)
        self.light.brightness = 50
        answers = iter([(lsr.CLOCK_UNSYNCED, None), (lsr.CLOCK_SYNCED, None)])
        scheduler = self.build(clock_probe=lambda: next(answers))
        self.assertEqual(ls.SOURCE_HOLD, scheduler.tick().source)
        second = scheduler.tick()
        self.assertEqual(ls.SOURCE_SCHEDULE, second.source)
        self.assertEqual(100, second.brightness)

    # ------------------------------------------------------------ override

    def test_a_live_override_owns_the_lamp(self):
        scheduler = self.build()
        scheduler.set_override(20)
        decision = scheduler.tick()
        self.assertEqual(20, decision.brightness)
        self.assertEqual(ls.SOURCE_OVERRIDE, decision.source)

    def test_a_live_override_owns_the_lamp_even_while_the_clock_is_bad(self):
        """decide()'s gate order, asserted from the outside. A person who has
        just published a brightness is better evidence than a schedule read
        against a clock we have already admitted is wrong."""
        scheduler = self.build(clock_probe=lambda: (lsr.CLOCK_UNSYNCED, None))
        scheduler.set_override(20)
        self.assertEqual(ls.SOURCE_OVERRIDE, scheduler.tick().source)

    def test_a_hostile_override_brightness_cannot_reach_the_lamp(self):
        """`gardyn/light/brightness/set` is writable by any client with broker
        rights, and Light.set_duty_cycle raises outside 0..100 — on the
        scheduler thread, where a raise is silent. FakeLight reproduces that
        raise, so a regression in the clamping fails here."""
        for hostile, expected in ((999, 100), (-5, 0), ("55.0", 55), (float("nan"), 0),
                                  (float("inf"), 100), ("abc", 0), (b"55", 55)):
            with self.subTest(hostile=hostile):
                self.light = FakeLight(brightness=77)
                scheduler = self.build()
                scheduler.set_override(hostile)
                self.assertEqual(expected, scheduler.tick().brightness)
                self.assertEqual([expected], self.light.commands)

    def test_an_override_expires_at_the_next_boundary_and_is_cleared(self):
        scheduler = self.build()
        self.clock = datetime(2026, 8, 9, 17, 30, 0)
        scheduler.set_override(20)
        self.assertEqual(ls.SOURCE_OVERRIDE, scheduler.tick().source)
        # 18:00 is the next boundary after 17:30.
        self.clock = datetime(2026, 8, 9, 18, 0, 1)
        decision = scheduler.tick()
        self.assertEqual(ls.SOURCE_SCHEDULE, decision.source)
        self.assertEqual(50, decision.brightness)
        self.assertIsNone(
            scheduler.override,
            "the expired override was left in place; a later boundary change "
            "could bring it back to life",
        )

    def test_override_now_moves_the_lamp_in_the_same_call(self):
        """T-527.6's acceptance. A person tapping the light entity in Home
        Assistant must not wait up to a tick for the lamp to answer, and the
        move must go through the scheduler rather than beside it."""
        scheduler = self.build()
        decision = scheduler.override_now(20)
        self.assertEqual(20, decision.brightness)
        self.assertEqual(ls.SOURCE_OVERRIDE, decision.source)
        self.assertEqual([20], self.light.commands)

    def test_override_now_persists_what_it_applied(self):
        """The unsynced-clock hold reads this file, and an override IS a
        brightness that was actually applied. A command that moved the lamp but
        left the file saying otherwise would make a later unsynced boot hold a
        value the lamp has not been at since."""
        self.build().override_now(20)
        self.assertEqual(20, lsr.read_last_applied(self.state))

    def test_override_now_publishes_the_new_owner(self):
        scheduler = self.build()
        scheduler.tick()
        scheduler.override_now(20)
        self.assertEqual(
            [(100, ls.SOURCE_SCHEDULE), (20, ls.SOURCE_OVERRIDE)], self.published
        )

    def test_an_override_at_the_SCHEDULES_OWN_brightness_still_publishes(self):
        """The pin does not move, and the answer to "what owns this lamp" has
        changed completely. Publishing only on a brightness change would leave
        Home Assistant believing the schedule is running while a person holds
        it until 19:00 — and the obedience automation T-527.9 rebuilds has to
        tell those apart."""
        scheduler = self.build()
        scheduler.tick()
        scheduler.override_now(100)
        self.assertEqual([], self.light.commands[1:], "the pin should not move")
        self.assertEqual(
            [(100, ls.SOURCE_SCHEDULE), (100, ls.SOURCE_OVERRIDE)], self.published
        )

    def test_the_handback_is_published_even_though_the_lamp_does_not_move(self):
        scheduler = self.build()
        self.clock = datetime(2026, 8, 9, 17, 30, 0)
        scheduler.override_now(50)          # 18:00's brightness, applied early
        self.clock = datetime(2026, 8, 9, 18, 0, 1)
        scheduler.tick()
        self.assertEqual(
            [(50, ls.SOURCE_OVERRIDE), (50, ls.SOURCE_SCHEDULE)], self.published
        )

    def test_publish_now_resends_the_last_decision_unconditionally(self):
        """For the MQTT reconnect path: the value has not changed, the
        SUBSCRIBER has, and a retained message is delivered once per subscribe.
        Home Assistant's copy is otherwise whatever the broker still holds."""
        scheduler = self.build()
        scheduler.tick()
        scheduler.publish_now()
        scheduler.publish_now()
        self.assertEqual(
            [(100, ls.SOURCE_SCHEDULE)] * 3, self.published
        )

    def test_publish_now_before_the_first_tick_is_a_no_op(self):
        """It runs on the connect path while the scheduler thread is still
        starting, so `no decision yet` is a real state rather than a defensive
        branch."""
        scheduler = self.build()
        self.assertIsNone(scheduler.last_decision)
        scheduler.publish_now()
        self.assertEqual([], self.published)

    def test_clear_override_hands_the_lamp_back_immediately(self):
        scheduler = self.build()
        scheduler.set_override(20)
        scheduler.clear_override()
        self.assertEqual(ls.SOURCE_SCHEDULE, scheduler.tick().source)

    def test_an_override_is_stamped_from_the_schedulers_own_clock(self):
        """Override.applied_at must come from the SAME clock decide() is given,
        or "has the next boundary passed" is unanswerable during an unsynced
        boot. datetime.now() inside set_override would silently break that."""
        scheduler = self.build()
        scheduler.set_override(20)
        self.assertEqual(self.clock, scheduler.override.applied_at)

    # ------------------------------------------------------- failure paths

    def test_a_light_that_refuses_to_be_driven_does_not_kill_the_tick(self):
        self.light.fail_on_set = RuntimeError("pigpio connection lost")
        decision = self.build().tick()
        self.assertEqual(100, decision.brightness)
        self.assertEqual([], self.light.commands)

    def test_a_light_that_cannot_be_READ_is_driven_anyway(self):
        """A failed pigpio round-trip is not evidence that the lamp is already
        right, so the tick must not treat an unreadable lamp as a match."""
        self.light.fail_on_get = RuntimeError("pigpio connection lost")
        self.build().tick()
        self.assertEqual([100], self.light.commands)

    def test_a_publish_that_raises_does_not_kill_the_tick(self):
        """The broker being down is the PREMISE of T-527, not an exception."""
        def explode(_decision):
            raise OSError("broker unreachable")
        scheduler = lsr.LightScheduler(
            self.light, explode, config_path=self.config, state_path=self.state,
            now=lambda: self.clock, clock_probe=lambda: (lsr.CLOCK_SYNCED, None),
        )
        self.assertEqual(100, scheduler.tick().brightness)
        self.assertEqual([100], self.light.commands)

    def test_no_publisher_at_all_is_allowed(self):
        scheduler = lsr.LightScheduler(
            self.light, None, config_path=self.config, state_path=self.state,
            now=lambda: self.clock, clock_probe=lambda: (lsr.CLOCK_SYNCED, None),
        )
        self.assertEqual(100, scheduler.tick().brightness)

    def test_an_unpersistable_state_file_does_not_stop_the_lamp_moving(self):
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w") as fh:
            fh.write("not a directory")
        scheduler = self.build(state_path=os.path.join(blocker, "light-phase"))
        self.assertEqual(100, scheduler.tick().brightness)
        self.assertEqual([100], self.light.commands)


class ClockVerdictTests(SchedulerHarness):
    """The latch and its ceiling — T-527.19, and the reason the deploy stopped.

    `NTPSynchronized` answers "has NTP checked in recently", not "is the clock
    good". Measured on the Pi on 2026-08-09 with a read-only adjtimex(2) probe:
    maxerror climbs at exactly 500.0 µs/s toward systemd's 16 s threshold, so a
    PERFECTLY ACCURATE clock is declared unsynchronised 8.89 h into any network
    outage. The old gate then held the last applied phase for the rest of the
    outage — darkness until the network returned, if it tipped over after 19:00.

    Two rules replace it, and both are pinned here:
      1. Once this process has seen a real sync, trust the clock from then on.
      2. A process that has NEVER seen one holds only until the host has been
         up NEVER_SYNCED_HOLD_SECONDS, then follows the schedule regardless.
    """

    UNSYNCED = (lsr.CLOCK_UNSYNCED, None)
    SYNCED = (lsr.CLOCK_SYNCED, None)
    UNKNOWN = (lsr.CLOCK_UNKNOWN, "cannot read NTP sync state (boom)")

    def _probe(self, *answers):
        """A clock probe returning each answer in turn, then repeating the last.

        Repeating rather than raising StopIteration matters: the outage this
        models does not end, and a probe that died at the end of its list would
        make the ceiling test pass for the wrong reason.
        """
        seq = list(answers)

        def probe():
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return probe

    # ---------------------------------------------------------------- latch

    def test_a_clock_that_synced_once_is_trusted_when_the_query_later_says_no(self):
        """THE HEADLINE FIX. Tick one syncs; tick two is the 8.9-hour staleness
        trip on a clock that is still accurate. Before the latch this returned
        SOURCE_HOLD and the lamp stopped following the photoperiod."""
        lsr.write_last_applied(self.state, 0)
        self.light.brightness = 0
        scheduler = self.build(clock_probe=self._probe(self.SYNCED, self.UNSYNCED))
        self.assertEqual(ls.SOURCE_SCHEDULE, scheduler.tick().source)
        second = scheduler.tick()
        self.assertEqual(ls.SOURCE_SCHEDULE, second.source)
        self.assertEqual(100, second.brightness)

    def test_the_latch_survives_an_arbitrary_run_of_unsynced_answers(self):
        """An outage is not two ticks long. Without the latch the 20th tick
        holds exactly as the 2nd does, which is the whole complaint."""
        scheduler = self.build(clock_probe=self._probe(self.SYNCED, self.UNSYNCED))
        scheduler.tick()
        for _ in range(20):
            self.assertEqual(ls.SOURCE_SCHEDULE, scheduler.tick().source)

    def test_a_process_that_has_never_synced_still_holds(self):
        """The latch must not become "always trust the clock". The scenario the
        gate exists for — a boot with no network, where timesyncd restored a
        time-of-day that is stale by however long the Pi was off — still holds
        inside the ceiling."""
        lsr.write_last_applied(self.state, 50)
        self.light.brightness = 50
        decision = self.build(clock_probe=self._probe(self.UNSYNCED)).tick()
        self.assertEqual(ls.SOURCE_HOLD, decision.source)
        self.assertEqual(50, decision.brightness)

    def test_a_broken_query_drives_but_does_NOT_latch(self):
        """The reason read_clock_state returns three states rather than a bool.
        CLOCK_UNKNOWN drives — believing a good clock is the cheap mistake — but
        recording it as a sync would let a permanently broken `timedatectl`
        pin the gate open on the strength of its own breakage. So the following
        honest `no` must still hold."""
        lsr.write_last_applied(self.state, 50)
        self.light.brightness = 50
        scheduler = self.build(clock_probe=self._probe(self.UNKNOWN, self.UNSYNCED))
        self.assertEqual(ls.SOURCE_SCHEDULE, scheduler.tick().source)
        self.assertEqual(ls.SOURCE_HOLD, scheduler.tick().source)

    def test_the_latch_is_per_process_and_a_restart_does_not_inherit_it(self):
        """It is a statement about THIS process's evidence, and a restarted one
        has none. Deliberately not persisted — a latch on disk would survive the
        power cut that produced the stale clock it exists to distrust."""
        first = self.build(clock_probe=self._probe(self.SYNCED))
        first.tick()
        lsr.write_last_applied(self.state, 50)
        self.light.brightness = 50
        fresh = self.build(clock_probe=self._probe(self.UNSYNCED))
        self.assertEqual(ls.SOURCE_HOLD, fresh.tick().source)

    # -------------------------------------------------------------- ceiling

    def test_a_never_synced_hold_ends_at_the_ceiling(self):
        lsr.write_last_applied(self.state, 50)
        self.light.brightness = 50
        scheduler = self.build(
            clock_probe=self._probe(self.UNSYNCED),
            uptime=lambda: lsr.NEVER_SYNCED_HOLD_SECONDS + 1,
        )
        decision = scheduler.tick()
        self.assertEqual(ls.SOURCE_SCHEDULE, decision.source)
        self.assertEqual(100, decision.brightness)

    def test_the_ceiling_is_exclusive_at_exactly_the_boundary(self):
        """`<` and `<=` differ by one second here and by nothing anyone would
        notice in production, which is exactly why it gets left unpinned."""
        for elapsed, expected in (
            (lsr.NEVER_SYNCED_HOLD_SECONDS - 1, ls.SOURCE_HOLD),
            (lsr.NEVER_SYNCED_HOLD_SECONDS, ls.SOURCE_SCHEDULE),
        ):
            with self.subTest(elapsed=elapsed):
                lsr.write_last_applied(self.state, 50)
                self.light = FakeLight(brightness=50)
                self.published = []
                scheduler = self.build(
                    clock_probe=self._probe(self.UNSYNCED), uptime=lambda: elapsed
                )
                self.assertEqual(expected, scheduler.tick().source)

    def test_a_LEGITIMATE_persisted_zero_does_not_hold_the_garden_dark_forever(self):
        """The residual finding from the T-527.7 review, and the other half of
        T-527.19. decide()'s docstring argues at length that a CORRUPT state
        file must not read as 0 because "0 held through an unsynced window is a
        dark garden" — while a LEGITIMATE 0, the lamp really having been off at
        19:00, produced the identical outcome with no mitigation at all. The
        ceiling is what mitigates it."""
        lsr.write_last_applied(self.state, 0)
        self.light.brightness = 0
        scheduler = self.build(
            clock_probe=self._probe(self.UNSYNCED),
            uptime=lambda: lsr.NEVER_SYNCED_HOLD_SECONDS + 1,
        )
        decision = scheduler.tick()
        self.assertEqual(100, decision.brightness)
        self.assertEqual([100], self.light.commands)

    def test_ending_the_hold_is_reported_once_ACROSS_AN_ADVANCING_UPTIME(self):
        """It is a real degradation — the schedule is now running on a clock
        nothing has corroborated — so it must reach the log. Once, though: the
        condition persists for the whole outage.

        THE UPTIME ADVANCES HERE, AND THAT IS THE ENTIRE POINT OF THE TEST.
        The first version of this passed a CONSTANT uptime, which made the
        assertion true by its own input: _report dedupes on message TEXT, and
        the note interpolated `{elapsed / 3600:.1f}`, so in production — where
        uptime grows — it emitted a new distinct string every six minutes.
        Measured at 246 ERROR lines a day into an unrotated gardyn.log on an SD
        card, against a docstring claiming one. Four hours of real ticks are
        simulated below because two identical ones could never show it."""
        elapsed = [lsr.NEVER_SYNCED_HOLD_SECONDS + 1]
        scheduler = self.build(
            clock_probe=self._probe(self.UNSYNCED),
            uptime=lambda: elapsed[0],
        )
        with self.assertLogs(lsr.logger, level="ERROR") as captured:
            for _ in range(480):          # 4 h at the shipped 30 s cadence
                scheduler.tick()
                elapsed[0] += 30
        holds = [line for line in captured.output if "never synchronised" in line]
        self.assertEqual(
            1, len(holds),
            f"the hold note is not deduped across a growing uptime: {len(holds)} lines",
        )

    def test_the_ceiling_is_measured_from_BOOT_not_from_process_start(self):
        """A crash loop must not reset it. mqtt.service carries Restart=always
        with RestartSec=10, so a process-local timer would grant a fresh two
        hours every ten seconds and hold the lamp forever — reinstating the
        unbounded hold through the back door. This scheduler was constructed a
        microsecond ago and follows the schedule anyway, because the HOST has
        been up longer than the ceiling."""
        lsr.write_last_applied(self.state, 0)
        self.light.brightness = 0
        scheduler = self.build(
            clock_probe=self._probe(self.UNSYNCED),
            uptime=lambda: lsr.NEVER_SYNCED_HOLD_SECONDS * 10,
        )
        self.assertEqual(ls.SOURCE_SCHEDULE, scheduler.tick().source)

    def test_an_unreadable_uptime_falls_back_to_this_process_age(self):
        """No /proc/uptime — a laptop, or a container without procfs.

        The fallback UNDER-reports across a restart, which LENGTHENS the hold,
        and that is the DANGEROUS direction, not the safe one: an unbounded
        hold is the dark garden the ceiling exists to close. It is tolerable
        only because /proc/uptime always exists on the deploy target, so this
        branch is reachable here and essentially nowhere else."""
        lsr.write_last_applied(self.state, 50)
        self.light.brightness = 50
        # The FIRST reading is consumed at construction, as the anchor. Both
        # directions are asserted, or this passes against a fallback that
        # always holds and against one that never does.
        elapsed = iter([0.0, 10.0, lsr.NEVER_SYNCED_HOLD_SECONDS + 1])
        scheduler = self.build(
            clock_probe=self._probe(self.UNSYNCED),
            uptime=lambda: None,
            monotonic_clock=lambda: next(elapsed),
        )
        self.assertEqual(ls.SOURCE_HOLD, scheduler.tick().source)
        self.assertEqual(ls.SOURCE_SCHEDULE, scheduler.tick().source)

    def test_the_hold_ceiling_is_a_plausible_number(self):
        """Pins the policy value itself, not a relation to it. Every test above
        computes its uptime FROM the constant, so all of them move with a mutant
        that changes it — a ceiling of a week would leave them green and the
        garden dark for six days."""
        self.assertEqual(2 * 60 * 60, lsr.NEVER_SYNCED_HOLD_SECONDS)


class SerialisationTests(SchedulerHarness):
    """T-527.20 — three defects from one root, and the suite could see none.

    tick() was unserialised (the lock guarded the override slot alone) and
    _last_published was written BEFORE the publish. An independent 15-mutant
    battery left 10 survivors, including `with self._lock:` -> `if True:` in
    both places, because there was no concurrent test in the file at all — and
    the harness's own "constructs with no mutant" preamble did not name
    threading, so the largest untested surface was the one declared covered by
    omission.

    EVERY TEST HERE FAILS WITHOUT THE LOCK. That is the point of the blocking
    probe: it holds a tick open at a known point so a second thread can be made
    to interleave deterministically, rather than hoping a stress loop hits the
    ~0.1%-per-command window.
    """

    def _blocking_probe(self, entered, release, park_on=(1,)):
        """A clock probe that parks inside SPECIFIC ticks until released.

        Stands in for the real `timedatectl` fork+exec, which is where a tick
        actually spends its time (0.169 s median on the Pi, up to
        NTP_QUERY_TIMEOUT_SECONDS) and therefore where a second thread lands.

        `park_on` IS THE FIX FOR AN ASSERTION THAT PASSED FOR THE WRONG REASON.
        An earlier version parked on EVERY call, so a second thread entering
        its own tick blocked on the probe rather than on the lock — which meant
        the "did not interleave" assertion below stayed true with the lock
        removed, and the mutant was killed four lines later by a different
        mechanism than the comment claimed. Parking only the named call leaves
        the lock as the only thing that can hold the second thread up.
        """
        calls = []

        def probe():
            calls.append(None)
            if len(calls) in park_on:
                entered.set()
                self.assertTrue(release.wait(5), "the test never released the tick")
            return (lsr.CLOCK_SYNCED, None)
        return probe

    def _run_off_thread(self, fn):
        thread = threading.Thread(target=fn, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 5)
        return thread

    def _run_when_started(self, fn):
        """Run `fn` off-thread, returning once the thread is REALLY running.

        The two interleave tests below assert that something cannot finish
        within a short window. That window has to contain only the call under
        test — if it also contains Python's thread start-up, then a loaded
        machine makes the assertion pass because nothing had begun yet, which
        is a mutant surviving for a reason unrelated to the lock. Waiting on a
        started-flag first removes that from the window; what is left is the
        lock, which is the thing being measured.
        """
        started, done = threading.Event(), threading.Event()

        def body():
            started.set()
            fn()
            done.set()

        self._run_off_thread(body)
        self.assertTrue(started.wait(5), "the helper thread never started")
        return done

    def test_a_command_landing_inside_a_tick_is_not_reverted(self):
        """DEFECT 3. You press the button to turn the lamp off; before this it
        came back on for up to a cadence, Home Assistant was told the SCHEDULE
        owned it, and the state file recorded the scheduled brightness — so a
        power cut in that window restored 100 rather than the 0 you asked for.
        Roughly 0.1% per command, and the button, the HA toggle and the HA
        brightness slider all traverse it."""
        entered, release = threading.Event(), threading.Event()
        scheduler = self.build(clock_probe=self._blocking_probe(entered, release))

        self._run_off_thread(scheduler.tick)
        self.assertTrue(entered.wait(5), "the tick never started")

        done = self._run_when_started(lambda: scheduler.override_now(0))
        # The command must NOT complete while a tick is in flight, and the LOCK
        # has to be the only thing stopping it — the probe parks call 1 only,
        # so the command's own tick would sail through if the lock were gone.
        # Verified by applying the battery's `with self._lock:` -> `if True:`
        # mutant and watching this line go red, rather than assumed.
        self.assertFalse(done.wait(0.2), "the command interleaved with the tick")

        release.set()
        self.assertTrue(done.wait(5), "the command never completed")

        self.assertEqual(0, self.light.brightness)
        self.assertEqual((0, ls.SOURCE_OVERRIDE), self.published[-1])
        self.assertEqual(0, lsr.read_last_applied(self.state))

    def test_publish_now_cannot_interleave_with_a_tick(self):
        """DEFECT 2. publish_now() runs on paho's network thread from
        announce_to_home_assistant(); tick() runs on the scheduler thread. It
        used to read the decision, write _last_published, and land its publish
        LAST — after a tick that had already published a newer one. The
        broker's retained `gardyn/light/source` was left naming `schedule`
        while a person held the lamp, held until the override expired, and the
        T-527.9 obedience automation is specified to condition on that topic."""
        entered, release = threading.Event(), threading.Event()
        scheduler = self.build(
            clock_probe=self._blocking_probe(entered, release, park_on=(2,))
        )
        scheduler.tick()  # seed a decision for publish_now to republish
        self._run_off_thread(scheduler.tick)
        self.assertTrue(entered.wait(5), "the second tick never started")

        published = self._run_when_started(scheduler.publish_now)
        self.assertFalse(
            published.wait(0.2), "publish_now interleaved with a tick"
        )

        release.set()
        self.assertTrue(published.wait(5), "publish_now never completed")

        # The invariant the race broke: what we remember publishing is what was
        # published last. Nothing can observe a torn pair from outside.
        self.assertEqual(scheduler._last_published, self.published[-1])

    def test_the_remembered_pair_matches_the_last_publish_under_contention(self):
        """The same invariant as a stress run rather than a staged interleave.

        THE OVERRIDES ARE WHAT MAKE THE INVARIANT MEAN ANYTHING. Without them
        every thread reaches the identical decision, so the whole run publishes
        one distinct pair and `_last_published == published[-1]` compares a
        value to itself — it cannot fail under any interleaving, which makes it
        decoration rather than a check. Alternating an override in and out
        keeps at least two pairs in flight, so a torn read is observable.

        The liveness half is asserted separately and is worth having on its
        own: nothing here may deadlock.
        """
        scheduler = self.build()
        scheduler.tick()
        actions = (scheduler.tick, scheduler.publish_now,
                   lambda: scheduler.override_now(20), scheduler.clear_override)
        threads = [self._run_off_thread(actions[index % len(actions)])
                   for index in range(40)]
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive(), "a thread deadlocked")

        self.assertGreater(
            len(set(self.published)), 1,
            "the stress run only ever published one pair, so the invariant "
            "below compares a value to itself",
        )
        self.assertEqual(scheduler._last_published, self.published[-1])

    # --------------------------------------------- the failed-drive republish

    def test_a_failed_drive_forces_a_republish_on_the_next_tick(self):
        """DEFECT 1, and the one that strands Home Assistant PERMANENTLY.

        The dedupe key is the INTENDED (brightness, source); the payload is the
        OBSERVED brightness, because publish_light_decision() re-reads the lamp.
        So tick 1 with pigpio down caught the raise, did not move the lamp, and
        published the hardware's 0 — while recording (100, schedule) as sent.
        Tick 2, with pigpio back, drove the lamp to 100, found the decision pair
        unchanged, and deduped. Home Assistant showed the grow light OFF while
        it was on, until the next boundary (up to 8 h) or an MQTT reconnect.
        RuntimeError("pigpio connection lost") is a failure FakeLight already
        models, so nothing about this is hypothetical."""
        scheduler = self.build()
        self.light.fail_on_set = RuntimeError("pigpio connection lost")
        with self.assertLogs(lsr.logger, level="ERROR"):
            scheduler.tick()
        self.assertEqual(0.0, self.light.brightness)
        self.assertEqual([(100, ls.SOURCE_SCHEDULE)], self.published)

        self.light.fail_on_set = None
        scheduler.tick()
        self.assertEqual(100.0, self.light.brightness)
        self.assertEqual(
            2, len(self.published), "the recovery tick was deduped away"
        )

    def test_a_failed_drive_republishes_even_when_the_DECISION_has_not_changed(self):
        """The case the first battery had no corpus for, and the reason
        `or not applied` is not redundant with _record_published_locked.

        Every other failed-drive test starts from a CHANGED pair, which
        publishes on the dedupe's own terms — so the clause could be deleted
        and nothing went red. Reaching it needs the pair to be UNCHANGED while
        the lamp has drifted off target, which happens whenever something else
        writes the pin: the physical button, a flash_lights() burst, or an MQTT
        command handler. Then the drive back fails, and without the clause the
        scheduler stays silent and Home Assistant keeps a retained value that
        no longer describes the lamp."""
        scheduler = self.build()
        scheduler.tick()
        self.assertEqual([(100, ls.SOURCE_SCHEDULE)], self.published)

        # Something else moved the pin, and pigpio is now failing.
        self.light.brightness = 0.0
        self.light.fail_on_set = RuntimeError("pigpio connection lost")
        with self.assertLogs(lsr.logger, level="ERROR"):
            scheduler.tick()
        self.assertEqual(
            2, len(self.published),
            "an unchanged decision with a failed drive published nothing",
        )
        self.assertIsNone(scheduler._last_published)

    def test_a_failed_PUBLISH_forces_a_republish_on_the_next_tick(self):
        """The publish-path twin of the failed-drive rule, and it was missing.

        f631652 stopped a failed DRIVE being recorded as published; a failed
        PUBLISH was recorded regardless, one line away, because _publish
        swallowed its exception and returned None. Nothing left the process, the
        pair was remembered as sent, and every later tick deduped against it —
        so Home Assistant held a retained value that did not describe the lamp
        until the next boundary (up to 8 h) or an MQTT reconnect.

        Not hypothetical: publish_light_decision() begins with
        light.get_brightness(), the same pigpio round-trip _read_actual() wraps
        precisely because it can raise."""
        attempts = []

        def flaky(decision):
            attempts.append((decision.brightness, decision.source))
            if len(attempts) == 1:
                raise OSError("broker unreachable")
            self.published.append((decision.brightness, decision.source))

        scheduler = lsr.LightScheduler(
            self.light, flaky, config_path=self.config, state_path=self.state,
            now=lambda: self.clock, clock_probe=lambda: (lsr.CLOCK_SYNCED, None),
            uptime=lambda: 0.0,
        )
        with self.assertLogs(lsr.logger, level="ERROR"):
            scheduler.tick()
        self.assertEqual([], self.published, "the broker got it after all?")
        self.assertIsNone(
            scheduler._last_published,
            "a publish that raised was remembered as sent",
        )

        scheduler.tick()
        self.assertEqual([(100, ls.SOURCE_SCHEDULE)], self.published)
        self.assertEqual((100, ls.SOURCE_SCHEDULE), scheduler._last_published)

    def test_publish_now_before_the_first_tick_is_a_SILENT_no_op(self):
        """The guard's own regression test, and it was only ever enforced by
        accident.

        `publish_now()` runs from announce_to_home_assistant() on the connect
        path, while the scheduler's first tick is still pending on its own
        thread — a real window. The `decision is None` guard is what makes that
        a no-op. It used to be pinned only by the AttributeError that escaped
        when the guard was removed, and making _publish() return a bool meant
        _publish swallowed that AttributeError instead: the mutant deleting the
        guard SURVIVED the battery, silently. So assert the observable that
        remains, which is the better one anyway — nothing published, and
        nothing logged. A traceback per broker reconnect is not a no-op."""
        scheduler = self.build()
        with self.assertNoLogs(lsr.logger, level="ERROR"):
            scheduler.publish_now()
        self.assertEqual([], self.published)
        self.assertIsNone(scheduler._last_published)

    def test_a_publish_that_SUCCEEDS_is_still_deduped(self):
        """The paired assertion that stops the fix over-correcting into
        republishing an unchanged pair on every tick."""
        scheduler = self.build()
        for _ in range(3):
            scheduler.tick()
        self.assertEqual([(100, ls.SOURCE_SCHEDULE)], self.published)

    def test_a_scheduler_with_no_publisher_does_not_spin(self):
        """`publish_state=None` means nothing failed — there is no subscriber.
        Returning False there would make every tick look like a failed publish
        and defeat the dedupe permanently."""
        scheduler = lsr.LightScheduler(
            self.light, None, config_path=self.config, state_path=self.state,
            now=lambda: self.clock, clock_probe=lambda: (lsr.CLOCK_SYNCED, None),
            uptime=lambda: 0.0,
        )
        scheduler.tick()
        self.assertEqual((100, ls.SOURCE_SCHEDULE), scheduler._last_published)

    def test_a_failed_drive_is_not_recorded_as_persisted_either(self):
        """The state file is the unsynced hold's memory, so recording a
        brightness the lamp never reached would make a later hold restore a
        value that was never applied."""
        scheduler = self.build()
        self.light.fail_on_set = RuntimeError("pigpio connection lost")
        with self.assertLogs(lsr.logger, level="ERROR"):
            scheduler.tick()
        self.assertIsNone(lsr.read_last_applied(self.state))

    def test_a_successful_drive_is_still_deduped(self):
        """The paired assertion, and the one that stops the fix over-correcting.
        Republishing an unchanged pair every 30 s is pure broker traffic on
        retained topics — the behaviour T-527.6 built the dedupe for."""
        scheduler = self.build()
        scheduler.tick()
        scheduler.tick()
        scheduler.tick()
        self.assertEqual([(100, ls.SOURCE_SCHEDULE)], self.published)

    def test_publish_now_after_a_failed_drive_does_not_re_arm_the_dedupe(self):
        """publish_now() is unconditional, so it must not record a pair the
        lamp never reached — that would silently reinstate defect 1 through the
        reconnect path, which is exactly where it looks most like recovery."""
        scheduler = self.build()
        self.light.fail_on_set = RuntimeError("pigpio connection lost")
        with self.assertLogs(lsr.logger, level="ERROR"):
            scheduler.tick()
        scheduler.publish_now()
        self.assertIsNone(scheduler._last_published)

        self.light.fail_on_set = None
        before = len(self.published)
        scheduler.tick()
        self.assertEqual(before + 1, len(self.published))


class HeartbeatTests(SchedulerHarness):
    """The liveness counter (T-527.22).

    WHAT THIS IS FOR, restated because every assertion below is only meaningful
    against it: the state publish is DEDUPED, so a healthy scheduler that keeps
    reaching the same decision sends Home Assistant nothing for hours. HA
    therefore cannot distinguish "nothing has changed" from "nothing is
    deciding" — and a scheduler THREAD that dies under a live broker connection
    produces no LWT, leaves the retained override latched, and silences both
    notify-only checks at once. The heartbeat is the only topic whose silence
    is itself the signal, so the property under test throughout is that it
    publishes where everything else declines to.

    THE FIRING PAIR each test is built around: a tick that changes nothing
    (deduped, no state publish) must still emit a heartbeat, and a tick inside
    the interval must not. Those two differ only in elapsed time, which is the
    thing under test.
    """

    def setUp(self):
        super().setUp()
        # The counter values handed to the sink, in order. Recording the VALUE
        # and not merely the call count is what lets the "does it advance on a
        # failed publish" tests below say something.
        self.beats = []
        self.beat_clock = 0.0
        self.beat_fails_with = None

    def _sink(self, count):
        if self.beat_fails_with is not None:
            raise self.beat_fails_with
        self.beats.append(count)

    def build(self, **kwargs):
        kwargs.setdefault("publish_heartbeat", self._sink)
        kwargs.setdefault("heartbeat_clock", lambda: self.beat_clock)
        return super().build(**kwargs)

    # ------------------------------------------------------------- cadence

    def test_the_first_completed_tick_publishes_a_heartbeat(self):
        """A restart is exactly when HA most needs telling the scheduler is
        deciding again. Waiting out the interval would leave a window in which
        a fresh process is indistinguishable from a dead one."""
        self.build().tick()
        self.assertEqual([1], self.beats)

    def test_a_second_tick_inside_the_interval_does_not_publish(self):
        scheduler = self.build(heartbeat_seconds=120)
        scheduler.tick()
        self.beat_clock = 119.0
        scheduler.tick()
        self.assertEqual([1], self.beats)

    def test_a_tick_past_the_interval_publishes_and_the_counter_advances(self):
        scheduler = self.build(heartbeat_seconds=120)
        scheduler.tick()
        self.beat_clock = 120.0
        scheduler.tick()
        self.beat_clock = 240.0
        scheduler.tick()
        self.assertEqual([1, 2, 3], self.beats)

    def test_the_boundary_is_inclusive(self):
        """At exactly heartbeat_seconds it publishes. Named because `>` and
        `>=` are the mutation a maintainer actually makes here, and one tick of
        difference is invisible in every other test."""
        scheduler = self.build(heartbeat_seconds=120)
        scheduler.tick()
        self.beat_clock = 120.0
        scheduler.tick()
        self.assertEqual([1, 2], self.beats)

    # ---------------------------------------------------- the whole point

    def test_a_DEDUPED_tick_still_publishes_a_heartbeat(self):
        """THE property. The second tick reaches the identical decision, so the
        state publish is suppressed — and that suppression is the T-527.20
        behaviour that must not be touched. The heartbeat has to survive it or
        this ticket has shipped nothing."""
        scheduler = self.build(heartbeat_seconds=120)
        scheduler.tick()
        published_after_first = len(self.published)
        self.beat_clock = 300.0
        scheduler.tick()
        self.assertEqual(
            published_after_first, len(self.published),
            "the state publish was NOT deduped, so this test is not exercising "
            "the case it was written for")
        self.assertEqual([1, 2], self.beats)

    def test_a_failed_drive_still_publishes_a_heartbeat(self):
        """A pigpio drop means the lamp is wrong; it does not mean the
        scheduler stopped deciding, and reporting it as dead would point the
        alarm at the wrong subsystem."""
        scheduler = self.build(heartbeat_seconds=120)
        scheduler.tick()
        self.light.fail_on_set = RuntimeError("pigpio connection lost")
        self.light.fail_on_get = RuntimeError("pigpio connection lost")
        self.beat_clock = 300.0
        scheduler.tick()
        self.assertEqual([1, 2], self.beats)

    def test_an_override_tick_does_NOT_publish_a_heartbeat(self):
        """THE CONTRACT FLIP, and the most important test in this class.

        An earlier version of this file asserted the OPPOSITE — that
        override_now() beats too, on the reasoning that it routes through
        _tick_locked and is therefore a completed tick. That reasoning is
        true and irrelevant: override_now() is reached from mqtt.py's
        apply_light_override() on PAHO'S NETWORK THREAD, and from
        toggle_light() on the physical-button thread. Both are alive in
        exactly the failure this heartbeat detects.

        The cost of the old contract, confirmed by probe: with the scheduler
        thread genuinely dead, a person tapping the light in Home Assistant
        emitted a beat and reset the staleness alarm for a full threshold.
        Tapping the light is precisely what somebody does on noticing the
        garden is wrong, so the diagnostic action suppressed the diagnostic,
        and repeating it suppressed it indefinitely.

        Found by review. The code, the tests and the commit message had all
        agreed on the weaker contract, which is why no assertion caught it."""
        scheduler = self.build(heartbeat_seconds=120)
        scheduler.tick()
        self.beat_clock = 300.0
        scheduler.override_now(25)
        self.assertEqual(
            [1], self.beats,
            "override_now() emitted a heartbeat; it is reachable from paho's "
            "network thread and must never speak for the scheduler thread")

    def test_a_beat_from_a_NON_scheduler_thread_is_refused(self):
        """Belt to the placement's braces. Moving the call into tick() excludes
        the two paho-thread callers that exist TODAY; this excludes a future
        one, because tick() is public and nothing stops another thread calling
        it. Once run_forever is running it is the only caller entitled to speak
        for the loop's liveness."""
        scheduler = self.build(heartbeat_seconds=0)
        # Stand in for run_forever having claimed the loop on another thread.
        scheduler._scheduler_ident = threading.get_ident() + 1
        scheduler.tick()
        self.assertEqual([], self.beats)
        # And the guard is not simply "never beat": the real loop still does.
        scheduler._scheduler_ident = threading.get_ident()
        scheduler.tick()
        self.assertEqual([1], self.beats)

    def test_the_guard_is_INERT_before_any_loop_is_running(self):
        """The permissive half, named so it is not 'tidied' into a hard check.
        Before run_forever there is no loop to speak for, so a direct tick()
        may beat — which is what every other test in this class relies on, and
        what makes the whole class runnable without threads."""
        scheduler = self.build()
        self.assertIsNone(scheduler._scheduler_ident)
        scheduler.tick()
        self.assertEqual([1], self.beats)

    def test_run_forever_claims_the_ident_on_the_thread_it_runs_on(self):
        """The stamp has to name the thread actually running the loop, or the
        guard above rejects the real scheduler and the heartbeat never
        publishes at all — a silent, total failure of the feature."""
        scheduler = self.build(heartbeat_seconds=0)
        seen = []

        def stop_after_one(_seconds):
            seen.append(scheduler._scheduler_ident)
            scheduler.stop()

        thread = threading.Thread(
            target=scheduler.run_forever, kwargs={}, daemon=True)
        scheduler._sleeper = stop_after_one
        thread.start()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual([thread.ident], seen)
        self.assertEqual([1], self.beats)

    # ------------------------------------------------------------ failure

    def test_a_raising_sink_does_not_escape_the_tick(self):
        """The broker being down is the PREMISE of T-527. A heartbeat that
        could kill the tick would make the observability feature able to stop
        the photoperiod it observes."""
        scheduler = self.build()
        self.beat_fails_with = RuntimeError("broker unreachable")
        with self.assertLogs("light_scheduler", level="ERROR"):
            decision = scheduler.tick()
        self.assertEqual(100, decision.brightness)
        self.assertEqual([100], self.light.commands)

    def test_a_failed_publish_does_not_advance_the_counter_or_the_clock(self):
        """Both halves matter and they fail in opposite directions. Advancing
        the COUNTER would mean the next successful beat skips a number, which
        is harmless — but advancing the CLOCK would make a broker outage
        shorter than the interval swallow a whole beat, so the retry would wait
        out a fresh interval instead of trying again on the next tick."""
        scheduler = self.build(heartbeat_seconds=120)
        self.beat_fails_with = RuntimeError("broker unreachable")
        with self.assertLogs("light_scheduler", level="ERROR"):
            scheduler.tick()
        self.assertEqual([], self.beats)
        self.beat_fails_with = None
        # No clock movement at all: if the failure had stamped the clock, this
        # tick would be inside the interval and publish nothing.
        scheduler.tick()
        self.assertEqual([1], self.beats)

    def test_no_sink_configured_is_not_an_error(self):
        """The scheduler is constructible without a broker — that is what makes
        every test in this file runnable off the Pi."""
        scheduler = self.build(publish_heartbeat=None)
        self.assertEqual(100, scheduler.tick().brightness)

    # ------------------------------------------------------ who may write it

    def test_publish_now_does_NOT_emit_a_heartbeat(self):
        """publish_now() runs on paho's network thread, which is alive in
        precisely the failure this heartbeat detects: broker connected,
        scheduler thread dead. A heartbeat from there would report liveness for
        the thread that stopped, using the thread that did not — the check
        would then never fire, and it is the only check for that fault."""
        scheduler = self.build(heartbeat_seconds=120)
        scheduler.tick()
        self.beat_clock = 9000.0
        scheduler.publish_now()
        scheduler.publish_now()
        self.assertEqual([1], self.beats)

    def test_a_tick_that_raises_leaves_no_heartbeat(self):
        """The heartbeat sits LAST in _tick_locked so that it means a tick RAN
        TO COMPLETION. A tick killed part-way through must not claim one — that
        is the difference between "the scheduler is deciding" and "the
        scheduler is being called"."""
        def explode():
            raise RuntimeError("the wall clock read blew up")

        # Injected rather than patched: `now` is read inside _tick_locked
        # AFTER the config and clock reads and BEFORE decide(), so this kills
        # the tick in the middle, which is the shape being tested.
        scheduler = self.build(now=explode)
        with self.assertRaises(RuntimeError):
            scheduler.tick()
        self.assertEqual([], self.beats)

    def test_the_heartbeat_clock_is_not_the_never_synced_anchor(self):
        """Separate injectables on purpose. `monotonic_clock` exists only for
        the never-synced hold's fallback anchor and tests feed it FINITE
        iterators; if the heartbeat shared it, a heartbeat test would die of
        StopIteration inside an unrelated branch. Pinning it here so a
        'simplification' that collapses the two is caught by a named test
        rather than by a confusing failure somewhere else."""
        exhausted = iter([0.0])
        scheduler = self.build(monotonic_clock=lambda: next(exhausted))
        # The one value is consumed by __init__'s _started_monotonic. If the
        # heartbeat also drew from it, this tick would raise StopIteration.
        scheduler.tick()
        self.assertEqual([1], self.beats)


class ModuleDefaultTests(unittest.TestCase):
    """The constructor's defaults, which every other test overrides.

    T-527.20's battery found these unpinned: a mutant repointing CONFIG_PATH at
    a nonexistent path was SILENT, and mutants on TICK_SECONDS and
    NTP_QUERY_TIMEOUT_SECONDS survived, because every test passes those
    explicitly. A default nothing asserts is a default nothing protects.
    """

    def test_the_config_path_default_is_the_deployed_one(self):
        self.assertEqual("/etc/gardyn/light.env", lsr.CONFIG_PATH)

    def test_the_state_path_default_is_the_deployed_one(self):
        """Must agree with StateDirectory=gardyn in services/mqtt.service."""
        self.assertEqual("/var/lib/gardyn", lsr.STATE_DIR_FALLBACK)
        self.assertEqual("light-phase", lsr.STATE_FILENAME)

    def test_the_uptime_path_default_is_the_kernel_one(self):
        self.assertEqual("/proc/uptime", lsr.UPTIME_PATH)

    def test_the_tick_cadence_default_is_thirty_seconds(self):
        self.assertEqual(30, lsr.TICK_SECONDS)

    def test_the_ntp_query_timeout_default_is_ten_seconds(self):
        self.assertEqual(10, lsr.NTP_QUERY_TIMEOUT_SECONDS)

    def test_the_heartbeat_cadence_default_is_two_minutes(self):
        """Pinned because the Home Assistant staleness threshold is chosen
        against it and lives in a different system entirely — nothing in this
        repo can see that automation, so a silent change here would widen the
        detection window with no local signal at all."""
        self.assertEqual(120, lsr.HEARTBEAT_SECONDS)

    def test_the_heartbeat_cadence_is_not_derived_from_the_tick(self):
        """They are tuned against different things — the tick against
        `timedatectl` cost, the heartbeat against Home Assistant's expire_after
        and recorder rows — so a future retune of one must not move the other.
        Asserting they are independent constants, not that the numbers differ,
        which they could coincidentally stop doing.

        THE COMMENT IS STRIPPED BEFORE MATCHING, and that is not tidiness.
        Review drove five arms through this: it correctly reddens on
        `HEARTBEAT_SECONDS = 4 * TICK_SECONDS`, and it ALSO reddened on a
        correct file the moment a clarifying comment on the assignment line
        mentioned TICK_SECONDS — this repo's source assertions have twice now
        matched prose rather than code. Stripping the comment closes that.

        WHAT IT STILL CANNOT SEE, stated rather than implied: an indirection
        (`_HB = 4 * TICK_SECONDS` on one line, `HEARTBEAT_SECONDS = _HB` on the
        next) walks straight past it. A single-line source check cannot follow a
        name, and pretending otherwise is worse than the gap. The battery
        carries the direct-derivation mutant, which is the spelling anyone
        would actually write."""
        source = inspect.getsource(lsr)
        assignment = [
            line.split("#")[0] for line in source.splitlines()
            if line.startswith("HEARTBEAT_SECONDS")
        ]
        self.assertEqual(1, len(assignment))
        self.assertNotIn("TICK_SECONDS", assignment[0])

    def test_a_scheduler_built_with_no_kwargs_takes_the_module_defaults(self):
        """The constants above are only worth pinning if the constructor still
        reads them."""
        scheduler = lsr.LightScheduler(FakeLight())
        self.assertEqual(lsr.CONFIG_PATH, scheduler._config_path)
        self.assertEqual(lsr.TICK_SECONDS, scheduler._tick_seconds)
        self.assertEqual(
            lsr.NEVER_SYNCED_HOLD_SECONDS, scheduler._never_synced_hold_seconds
        )
        # Functions are not interned, so identity here is real evidence that
        # the constructor took the module's callable rather than a copy.
        self.assertIs(lsr.read_clock_state, scheduler._clock_probe)
        self.assertIs(lsr.seconds_since_boot, scheduler._uptime)

    def test_the_scalar_defaults_are_written_as_NAMES_in_the_source(self):
        """The test above cannot see a hardcoded scalar, and no runtime
        assertion can — the literal and the constant are EQUAL, so every
        instrument agrees while the single source of truth is gone. A mutant
        rewriting `config_path=CONFIG_PATH` to the same path spelled out
        survived a full battery for exactly that reason.

        So assert the DERIVATION, in the source, which is the only place the
        difference exists. `is` would not do it either: identical string
        literals in one module fold to one constant object, so identity passes
        against the copy it is meant to forbid.
        """
        import inspect

        signature = inspect.getsource(lsr.LightScheduler.__init__)
        for parameter, constant in (
            ("config_path", "CONFIG_PATH"),
            ("tick_seconds", "TICK_SECONDS"),
            ("never_synced_hold_seconds", "NEVER_SYNCED_HOLD_SECONDS"),
            ("clock_probe", "read_clock_state"),
            ("uptime", "seconds_since_boot"),
        ):
            with self.subTest(parameter=parameter):
                self.assertRegex(
                    signature, rf"\n\s*{parameter}={constant},",
                    f"{parameter}'s default is not derived from {constant}",
                )


class LoggingTests(SchedulerHarness):
    """A note that fires every tick is 2,880 identical ERROR lines a day, which
    buries the four transitions the log exists to carry."""

    def _capture(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setLevel(logging.DEBUG)
        lsr.logger.addHandler(handler)
        self.addCleanup(lsr.logger.removeHandler, handler)
        return stream

    def test_a_persistent_config_problem_is_logged_once(self):
        stream = self._capture()
        with open(self.config, "w") as fh:
            fh.write("GARDYN_LIGHT_SCHEDULE=nonsense\n")
        scheduler = self.build()
        for _ in range(4):
            scheduler.tick()
        self.assertEqual(1, stream.getvalue().count("is not usable"))

    def test_a_resolved_problem_is_announced_so_the_log_is_not_misleading(self):
        stream = self._capture()
        with open(self.config, "w") as fh:
            fh.write("GARDYN_LIGHT_SCHEDULE=nonsense\n")
        scheduler = self.build()
        scheduler.tick()
        with open(self.config, "w") as fh:
            fh.write(SHIPPED)
        scheduler.tick()
        self.assertIn("Resolved", stream.getvalue())

    def test_a_CHANGED_problem_is_logged_again(self):
        stream = self._capture()
        scheduler = self.build()
        with open(self.config, "w") as fh:
            fh.write("GARDYN_LIGHT_SCHEDULE=nonsense\n")
        scheduler.tick()
        with open(self.config, "w") as fh:
            fh.write("GARDYN_LIGHT_SCHEDULE=3:00=50\n")
        scheduler.tick()
        self.assertEqual(2, stream.getvalue().count("is not usable"))

    def test_a_transition_is_logged_at_INFO_with_its_source(self):
        stream = self._capture()
        self.build().tick()
        output = stream.getvalue()
        self.assertIn("100%", output)
        self.assertIn(ls.SOURCE_SCHEDULE, output)

    def test_the_module_raises_its_own_logger_to_INFO(self):
        """mqtt.py pins the ROOT logger at WARNING, so a transition logged at
        INFO reaches a handler only because this module set its own level at
        import — the same policy app/sensors/light/light.py carries, and for
        the same reason: a level set anywhere else depends on import order."""
        self.assertEqual(logging.INFO, lsr.logger.level)


class RunForeverTests(SchedulerHarness):

    def test_it_stops_when_asked(self):
        scheduler = self.build(tick_seconds=0, sleeper=lambda _s: None)
        ticks = []
        original = scheduler.tick

        def counting():
            ticks.append(1)
            if len(ticks) >= 3:
                scheduler.stop()
            return original()

        scheduler.tick = counting
        scheduler.run_forever()
        self.assertEqual(3, len(ticks))

    def test_an_exception_in_a_tick_does_not_escape_the_loop(self):
        """An exception on a non-main thread kills the THREAD and leaves the
        process at exit status 0, so Restart=always never fires and the lamp
        silently stops following its schedule. This is the backstop."""
        scheduler = self.build(tick_seconds=0, sleeper=lambda _s: None)
        calls = []

        def exploding():
            calls.append(1)
            if len(calls) >= 2:
                scheduler.stop()
            raise RuntimeError("something nobody predicted")

        scheduler.tick = exploding
        scheduler.run_forever()  # must not raise
        self.assertEqual(2, len(calls))

    def test_the_cadence_is_measured_from_the_START_of_a_tick(self):
        """A slow tick must eat into the gap rather than adding to it, or the
        cadence drifts by however long the timedatectl call took — 0.169 s
        median on this Pi, so ~8 minutes a day.

        THE TICK HERE REALLY TAKES TIME, and that is the whole design of this
        test. Against an instantaneous tick, `sleep(tick_seconds)` and
        `sleep(tick_seconds - elapsed)` produce the same number to within
        floating-point noise, so the bug survives any assertion loose enough to
        pass on a fast machine. Fifty milliseconds is far outside that noise
        and far inside any plausible CI budget."""
        slept = []
        scheduler = self.build(tick_seconds=30, sleeper=slept.append)

        def slow_tick():
            __import__("time").sleep(0.05)
            scheduler.stop()

        scheduler.tick = slow_tick
        scheduler.run_forever()
        self.assertEqual(1, len(slept))
        self.assertLess(slept[0], 30,
                        "the elapsed tick time was not subtracted from the gap")
        self.assertGreater(slept[0], 29.9)

    def test_a_tick_longer_than_the_cadence_sleeps_zero_rather_than_negative(self):
        slept = []
        scheduler = self.build(tick_seconds=0, sleeper=slept.append)
        scheduler.tick = lambda: scheduler.stop()
        scheduler.run_forever()
        self.assertEqual([0], slept)

    def test_start_returns_a_running_daemon_thread(self):
        scheduler = self.build(tick_seconds=0, sleeper=lambda _s: None)
        started = threading.Event()
        original = scheduler.tick

        def once():
            result = original()
            started.set()
            scheduler.stop()
            return result

        scheduler.tick = once
        thread = scheduler.start()
        self.addCleanup(scheduler.stop)
        self.assertTrue(thread.daemon)
        self.assertTrue(started.wait(5), "the scheduler thread never ticked")
        thread.join(5)
        self.assertFalse(thread.is_alive())


class PurityTests(unittest.TestCase):
    """light_scheduler must stay importable with no hardware attached.

    Same shape as tests/test_light_schedule.py's PurityTests, and the same
    control design: the negative case runs the SAME predicate against a module
    that provably pulls a forbidden name in, so a mutant emptying FORBIDDEN
    reddens it rather than making both halves vacuous.
    """

    FORBIDDEN = frozenset(
        {"gpiozero", "pigpio", "paho", "flask", "dotenv", "mqtt", "app", "config"}
    )

    def _forbidden_names_pulled_by(self, statement, extra_path=None):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = repo if extra_path is None else os.pathsep.join([extra_path, repo])
        probe = (
            "import sys, json\n"
            "before = set(sys.modules)\n"
            f"{statement}\n"
            "print(json.dumps(sorted(set(sys.modules) - before)))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=repo,
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": path},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        pulled = json.loads(result.stdout.strip().splitlines()[-1])
        return self.FORBIDDEN & {name.split(".")[0] for name in pulled}, pulled

    def test_the_module_stays_importable_with_no_hardware(self):
        hits, pulled = self._forbidden_names_pulled_by("import light_scheduler")
        self.assertIn(
            "light_scheduler",
            pulled,
            "the probe did not import light_scheduler at all, so its verdict "
            f"is about some other module. Pulled: {sorted(pulled)}",
        )
        self.assertEqual(
            hits,
            set(),
            f"light_scheduler pulled in hardware or app modules: {sorted(pulled)}",
        )

    def test_the_check_above_can_actually_fire(self):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "pigpio.py"), "w") as fh:
                fh.write("# stand-in for the real driver, for this control only\n")
            hits, _ = self._forbidden_names_pulled_by("import pigpio", extra_path=tmp)
        self.assertEqual({"pigpio"}, hits, "the purity predicate cannot fire")


class WiringTests(unittest.TestCase):
    """What mqtt.py must do with the scheduler, asserted against its SOURCE.

    mqtt.py cannot be imported without paho, gpiozero and pigpio, and every
    other module that needs to reach it installs a stub apparatus first. These
    are architectural facts about placement rather than behaviour, and the repo
    already uses source assertions for exactly that (see the publisher-loop
    assertions in tests/test_retired_entities.py).

    Each pattern below matches a form only the CODE can produce — never a bare
    name that the surrounding prose also contains, which is a control failure
    this repo has already paid for once (T-527.1, where an assertion matched
    `connect_async` inside the comment explaining `connect_async`).
    """

    @classmethod
    def setUpClass(cls):
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo, "mqtt.py")) as fh:
            cls.source = fh.read()
        cls.repo = repo

    def _assertInSource(self, needle, why=""):
        """assertIn against mqtt.py WITHOUT inlining mqtt.py in the message.

        unittest's default reprs both sides of a failed assertIn, and this
        haystack is 1,500 lines — one failure emitted ~86 KB of output and
        buried every other result in the run. The message says what was looked
        for and nothing about what it was looked for in."""
        self.assertTrue(needle in self.source,
                        f"mqtt.py does not contain {needle!r}. {why}")

    def test_mqtt_constructs_and_starts_the_scheduler(self):
        self._assertInSource("LightScheduler(")
        self._assertInSource("light_scheduler.start()")

    def test_the_command_handlers_go_through_the_scheduler(self):
        """Two writers of one lamp differ in exactly the two things that stay
        invisible until they matter: what is persisted for the unsynced-clock
        hold, and what is published as the owner. Matching the CALL form, never
        a bare name the surrounding comments also contain."""
        self._assertInSource("def apply_light_override(")
        self._assertInSource("light_scheduler.override_now(")
        handlers = self.source.split("# === Light Logic ===")[1].split(
            "# === Water Level ===")[0]
        self.assertNotIn(
            "light.set_duty_cycle(", handlers,
            "a light command still drives the pin beside the scheduler")
        self.assertNotIn(
            "light.off()", handlers,
            "a light command still drives the pin beside the scheduler")

    def test_the_physical_button_is_an_override_too(self):
        """A person at the garden is as much an override as a person in the
        app, and before this the button's effect was reverted within a tick."""
        body = self.source.split("def toggle_light(")[1].split("\ndef ")[0]
        self.assertIn("apply_light_override(", body)
        self.assertNotIn("light.set_duty_cycle(", body)

    def test_the_reconnect_path_resends_the_owner(self):
        """A retained message is delivered once per subscribe, so an HA that
        has just come back needs it re-sent or its source entity sits at
        whatever the broker still holds."""
        body = self.source.split("def announce_to_home_assistant(")[1].split(
            "\ndef ")[0]
        # The GUARD as well as the call. `assertIn("light_scheduler.
        # publish_now()")` passes just as happily when the line sits under
        # `if False:` — a mutant doing exactly that survived the first run of
        # this battery. A source assertion has to match a form only the working
        # code can produce.
        self.assertRegex(
            body,
            r"if\s+light_scheduler\s+is\s+not\s+None:\s*\n\s*"
            r"light_scheduler\.publish_now\(\)",
            "the reconnect republish is missing, or is behind a guard that "
            "does not test the scheduler",
        )

    def test_the_owner_gets_a_discovery_entity(self):
        body = self.source.split("def send_discovery_messages(")[1].split(
            "\ndef ")[0]
        # The CALL, not the constant. A payload built into a local and never
        # published contains every name this used to look for; that mutant
        # survived the first run too.
        self.assertRegex(body, r"publish_config\(\s*SOURCE_CONFIG_TOPIC\s*,")
        self.assertIn("LIGHT_SOURCE_TOPIC", body)
        self.assertIn("_light_source", body)

    def test_the_heartbeat_gets_a_discovery_entity(self):
        """Same shape as the owner sensor above, and the same two traps: match
        the CALL rather than the constant, since a payload built into a local
        and never published contains every name a laxer assertion looks for."""
        body = self.source.split("def send_discovery_messages(")[1].split(
            "\ndef ")[0]
        self.assertRegex(body, r"publish_config\(\s*HEARTBEAT_CONFIG_TOPIC\s*,")
        self.assertIn("LIGHT_HEARTBEAT_TOPIC", body)
        self.assertIn("_light_heartbeat", body)

    def test_the_heartbeat_is_wired_into_the_scheduler(self):
        """A heartbeat nothing passes to the constructor is a heartbeat that
        never publishes — the scheduler's sink defaults to None and returns
        silently, so the whole feature would be inert with every unit test in
        this file still green."""
        self.assertRegex(
            self.source,
            r"publish_heartbeat\s*=\s*lambda\s+\w+\s*:\s*"
            r"publish_light_heartbeat\(",
            "the scheduler is constructed without a heartbeat sink",
        )

    def test_the_heartbeat_is_published_UNRETAINED(self):
        """The reverse of every sibling publish in mqtt.py, and deliberately.

        Home Assistant's MQTT sensor docs, on the expire_after this entity
        depends on: "As this could cause the sensor to become available with an
        expired state, it is not recommended to retain the sensor's state
        payload at the MQTT broker."

        The first version DID retain it and measured staleness as
        `now() - last_changed`. Review found that reasoning inverted the
        feature: last_changed is reset by an availability flap too, so
        mqtt.service in its Restart=always crash loop would have advanced it
        every ten seconds while the counter stayed frozen and the lamp stayed
        dark. A permanent all-clear during a permanent outage."""
        body = self.source.split("def publish_light_heartbeat(")[1].split(
            "\ndef ")[0]
        # [^\n]* and NOT [^)]*: the payload argument is `str(count)`, whose own
        # closing paren ends a [^)]* run before it can reach the end of the
        # call. That spelling failed on a correct file once already.
        self.assertRegex(
            body, r"client\.publish\(\s*LIGHT_HEARTBEAT_TOPIC\s*,[^\n]*\)")
        call = [line for line in body.splitlines()
                if "client.publish(LIGHT_HEARTBEAT_TOPIC" in line]
        self.assertEqual(1, len(call))
        self.assertNotIn(
            "retain", call[0],
            "the heartbeat is retained; a retained replay makes the sensor "
            "available with an expired state, which is what expire_after "
            "exists to prevent")

    def test_the_heartbeat_sensor_declares_expire_after(self):
        """expire_after IS the staleness detector - without it the entity never
        goes unavailable and the Home Assistant condition has nothing to read.
        Asserting the VALUE, not merely the key, because the number is coupled
        to the Pi's HEARTBEAT_SECONDS and lives in a different repo from the
        automation that depends on it."""
        body = self.source.split("def send_discovery_messages(")[1].split(
            "\ndef ")[0]
        payload = body.split("publish_config(HEARTBEAT_CONFIG_TOPIC,")[1]
        self.assertRegex(payload, r'"expire_after"\s*:\s*600\b')

    def test_expire_after_is_a_multiple_of_the_pi_s_heartbeat_cadence(self):
        """The two numbers are the ends of one contract and they are set in
        different files. A cadence longer than the expiry would make the sensor
        flap unavailable on a HEALTHY scheduler - a permanent false alarm - so
        the relationship, not just each value, is what needs pinning."""
        body = self.source.split("def send_discovery_messages(")[1].split(
            "\ndef ")[0]
        payload = body.split("publish_config(HEARTBEAT_CONFIG_TOPIC,")[1]
        expire = int(re.search(r'"expire_after"\s*:\s*(\d+)', payload).group(1))
        self.assertGreaterEqual(
            expire, 3 * lsr.HEARTBEAT_SECONDS,
            "expire_after leaves too little margin over the publish cadence")

    def test_the_heartbeat_topic_is_NOT_the_source_topic(self):
        """The separation is the design. Republishing the source topic on a
        timer to prove liveness would destroy the property that makes it
        readable — that a change on it means somebody took the lamp — and the
        dedupe behind it is T-527.20 behaviour that must not be touched."""
        self.assertRegex(
            self.source,
            r'LIGHT_HEARTBEAT_TOPIC\s*=\s*BASE_TOPIC\s*\+\s*"[^"]+"',
        )
        heartbeat = self.source.split("LIGHT_HEARTBEAT_TOPIC = ")[1].split(
            "\n")[0]
        source_topic = self.source.split("LIGHT_SOURCE_TOPIC = ")[1].split(
            "\n")[0]
        self.assertNotEqual(source_topic, heartbeat)

    def test_the_reconnect_path_does_NOT_refresh_the_heartbeat(self):
        """announce_to_home_assistant() runs on paho's network thread, which is
        alive in exactly the failure the heartbeat detects. A refresh from
        there reports liveness for the thread that stopped, using the thread
        that did not — and this is the only check for that fault."""
        body = self.source.split("def announce_to_home_assistant(")[1].split(
            "\ndef ")[0]
        self.assertNotIn("publish_light_heartbeat(", body)
        self.assertNotIn("LIGHT_HEARTBEAT_TOPIC", body)

    def test_the_scheduler_starts_OUTSIDE_on_connect(self):
        """The one property that makes this feature work. Starting it from
        on_connect — next to start_publisher_threads(), which is where a reader
        would naturally put it — means no photoperiod until a broker accepts a
        CONNACK, reintroducing the exact dependency T-527 removes."""
        body = self.source.split("def on_connect(")[1].split("\ndef ")[0]
        self.assertNotIn("light_scheduler.start()", body)
        self.assertNotIn("LightScheduler(", body)

    def test_the_scheduler_starts_BEFORE_the_blocking_loop(self):
        """loop_forever() never returns, so anything after it never runs.

        LINE-ANCHORED ON BOTH SIDES, because `.index()` finds the FIRST
        occurrence anywhere in the file and prose counts. This test went red on
        2026-08-12 with the wiring untouched: a comment in on_message's topic
        guard gained the words `client.loop_forever(...)` while explaining
        where that exception route could be closed, and that comment sits ~40K
        characters ahead of the real call. The failure message named the right
        property and pointed at the wrong file.

        This repo has the lesson already - a source assertion must match a form
        only the CODE can produce, never a bare name the prose about it also
        contains - and it was recorded against a `connect_async` assertion in
        T-527.1. So fix the assertion rather than the sentence: a comment is
        free to name a call, and a check that cannot survive being written
        about is not a check.
        """
        import re

        def offset(pattern, what):
            found = [m.start() for m in
                     re.finditer(pattern, self.source, re.M)]
            self.assertEqual(
                1, len(found),
                f"expected exactly one CODE occurrence of {what}, found "
                f"{len(found)} - if this is 0 the call was renamed or moved, "
                f"and if it is >1 the anchor no longer identifies it")
            return found[0]

        start = offset(r"^\s+light_scheduler\.start\(\)\s*$",
                       "light_scheduler.start()")
        loop = offset(r"^\s+client\.loop_forever\(", "client.loop_forever(")
        self.assertLess(
            start, loop,
            "light_scheduler.start() is after the blocking loop, so the "
            "photoperiod never starts")

    def test_the_unit_gives_the_scheduler_somewhere_to_persist_state(self):
        """Line-anchored, because the unit's own comment block names
        /var/lib/gardyn — a substring test would stay green after the directive
        was deleted, which is the shape tests/test_setup_units.py already
        documents for RestartSec."""
        path = os.path.join(self.repo, "services", "etc", "systemd", "system",
                            "mqtt.service")
        with open(path) as fh:
            unit = fh.read()
        self.assertRegex(unit, r"(?m)^StateDirectory=gardyn$")

    def test_the_state_path_the_module_defaults_to_matches_the_unit(self):
        """Two files asserting one path is exactly the duplicate-writer problem
        the repo keeps finding, so pin them together rather than trusting that
        `gardyn` in the unit and `/var/lib/gardyn` in the module stay in step."""
        path = os.path.join(self.repo, "services", "etc", "systemd", "system",
                            "mqtt.service")
        with open(path) as fh:
            unit = fh.read()
        name = None
        for line in unit.splitlines():
            if line.startswith("StateDirectory="):
                name = line.split("=", 1)[1].strip()
        self.assertIsNotNone(name)
        self.assertEqual(
            lsr.default_state_path({}),
            os.path.join("/var/lib", name, lsr.STATE_FILENAME),
        )


if __name__ == "__main__":
    unittest.main()
