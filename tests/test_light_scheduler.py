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
import io
import json
import logging
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
    """
    calls = []

    class _Completed:
        def __init__(self):
            self.returncode = returncode
            self.stdout = answer
            self.stderr = stderr

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if raises is not None:
            raise raises
        return _Completed()

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


class ClockSyncTests(unittest.TestCase):

    def test_yes_is_synced(self):
        synced, note = lsr.clock_is_synced(_run=fake_ntp("yes\n"))
        self.assertTrue(synced)
        self.assertIsNone(note)

    def test_no_is_unsynced(self):
        synced, note = lsr.clock_is_synced(_run=fake_ntp("no\n"))
        self.assertFalse(synced)
        self.assertIsNone(note)

    def test_it_asks_the_kernel_question_and_not_some_other_one(self):
        """Which question is asked is the whole content of this probe. An
        answer of "yes" is available from several `timedatectl` properties, so
        a test that only checks the parsing would pass against a call reading
        `NTP` (is a time service enabled) instead of `NTPSynchronized` (does
        the kernel consider the clock good)."""
        run = fake_ntp("yes\n")
        lsr.clock_is_synced(_run=run)
        argv = run.calls[0][0]
        self.assertEqual(
            ["timedatectl", "show", "--property=NTPSynchronized", "--value"], argv
        )

    def test_a_timeout_is_passed_to_the_subprocess(self):
        run = fake_ntp("yes\n")
        lsr.clock_is_synced(_run=run, timeout=3)
        self.assertEqual(3, run.calls[0][1]["timeout"])

    def test_a_nonzero_exit_assumes_the_clock_is_good(self):
        """The deliberate half. Believing a bad clock costs a wrong-time
        photoperiod for a few seconds; disbelieving a good one HOLDS the lamp
        wherever it was, indefinitely, which is the dark garden T-527 exists to
        remove."""
        synced, note = lsr.clock_is_synced(
            _run=fake_ntp("", returncode=1, stderr="Failed to connect to bus\n")
        )
        self.assertTrue(synced)
        self.assertIn("Failed to connect to bus", note)

    def test_a_missing_binary_assumes_the_clock_is_good(self):
        synced, note = lsr.clock_is_synced(
            _run=fake_ntp(raises=FileNotFoundError(2, "No such file or directory"))
        )
        self.assertTrue(synced)
        self.assertIsNotNone(note)

    def test_a_hung_bus_assumes_the_clock_is_good(self):
        synced, note = lsr.clock_is_synced(
            _run=fake_ntp(raises=subprocess.TimeoutExpired("timedatectl", 10))
        )
        self.assertTrue(synced)
        self.assertIsNotNone(note)

    def test_an_unrecognised_answer_assumes_the_clock_is_good_and_says_so(self):
        synced, note = lsr.clock_is_synced(_run=fake_ntp("maybe\n"))
        self.assertTrue(synced)
        self.assertIn("maybe", note)

    def test_an_empty_answer_is_not_read_as_no(self):
        """`""` is falsy, and the tempting implementation is `answer == "yes"`.
        That reads an empty stdout — a `timedatectl` that printed nothing — as
        an unsynchronised clock, which is the freeze branch."""
        synced, note = lsr.clock_is_synced(_run=fake_ntp(""))
        self.assertTrue(synced)
        self.assertIsNotNone(note)


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
        kwargs.setdefault("synced_probe", lambda: (True, None))
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
        decision = self.build(synced_probe=lambda: (False, None)).tick()
        self.assertEqual(50, decision.brightness)
        self.assertEqual(ls.SOURCE_HOLD, decision.source)
        self.assertEqual([], self.light.commands)

    def test_an_unsynced_clock_with_no_memory_uses_the_configured_fallback(self):
        with open(self.config, "w") as fh:
            fh.write(SHIPPED + "GARDYN_LIGHT_UNSYNCED_FALLBACK=60\n")
        decision = self.build(synced_probe=lambda: (False, None)).tick()
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
        decision = self.build(synced_probe=lambda: (False, None)).tick()
        self.assertEqual(100, decision.brightness)
        self.assertEqual(ls.SOURCE_FALLBACK, decision.source)

    def test_a_clock_that_syncs_between_ticks_returns_to_the_schedule(self):
        lsr.write_last_applied(self.state, 50)
        self.light.brightness = 50
        answers = iter([(False, None), (True, None)])
        scheduler = self.build(synced_probe=lambda: next(answers))
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
        scheduler = self.build(synced_probe=lambda: (False, None))
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
            now=lambda: self.clock, synced_probe=lambda: (True, None),
        )
        self.assertEqual(100, scheduler.tick().brightness)
        self.assertEqual([100], self.light.commands)

    def test_no_publisher_at_all_is_allowed(self):
        scheduler = lsr.LightScheduler(
            self.light, None, config_path=self.config, state_path=self.state,
            now=lambda: self.clock, synced_probe=lambda: (True, None),
        )
        self.assertEqual(100, scheduler.tick().brightness)

    def test_an_unpersistable_state_file_does_not_stop_the_lamp_moving(self):
        blocker = os.path.join(self.tmp, "blocker")
        with open(blocker, "w") as fh:
            fh.write("not a directory")
        scheduler = self.build(state_path=os.path.join(blocker, "light-phase"))
        self.assertEqual(100, scheduler.tick().brightness)
        self.assertEqual([100], self.light.commands)


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

    def test_the_scheduler_starts_OUTSIDE_on_connect(self):
        """The one property that makes this feature work. Starting it from
        on_connect — next to start_publisher_threads(), which is where a reader
        would naturally put it — means no photoperiod until a broker accepts a
        CONNACK, reintroducing the exact dependency T-527 removes."""
        body = self.source.split("def on_connect(")[1].split("\ndef ")[0]
        self.assertNotIn("light_scheduler.start()", body)
        self.assertNotIn("LightScheduler(", body)

    def test_the_scheduler_starts_BEFORE_the_blocking_loop(self):
        """loop_forever() never returns, so anything after it never runs."""
        start = self.source.index("light_scheduler.start()")
        loop = self.source.index("client.loop_forever(")
        self.assertLess(start, loop)

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
