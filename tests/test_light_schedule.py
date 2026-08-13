"""Tests for light_schedule.py — the pure phase engine (T-527.4).

    python3 -m unittest tests.test_light_schedule

NO STUBS, AND THAT IS THE POINT. Every other test module that reaches mqtt.py
has to install fakes for gpiozero, pigpio, paho and dotenv before it can even
import, because mqtt.py constructs hardware at module scope. light_schedule
imports nothing but the standard library, so this file just imports it — and
test_the_module_stays_importable_with_no_hardware below is what keeps that
true. It follows that this module must NOT appear in
tests/test_suite_isolation.py's STUBBING_MODULES: that registry asserts set
equality in both directions, so listing a file that installs nothing is as
much a failure as omitting one that does.

The engine decides what brightness a grow light sits at on a host with no
console, so the branches that matter most are the ones the Pi cannot be made
to demonstrate: an unsynchronised clock, a corrupted persisted state file, and
a hostile brightness published by any client with broker rights. Those are
tested here or nowhere.
"""
import json
import re
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, time, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import light_schedule as ls  # noqa: E402


def _sched(pairs, fallback=100):
    """Build a Schedule from ("HH:MM", brightness) pairs, for brevity."""
    return ls.Schedule.of(
        [(time(int(t[:2]), int(t[3:])), b) for t, b in pairs], fallback
    )


def _at(hhmm, day=8, second=0):
    """A datetime on 2026-08-<day> at HH:MM."""
    return datetime(2026, 8, day, int(hhmm[:2]), int(hhmm[3:]), second)


# The photoperiod actually running on this garden, written out as literals.
# Deliberately NOT derived from ls.DEFAULT_SCHEDULE — a test that computes its
# expectation from the constant it is pinning moves with any edit to that
# constant and can never fail.
LIVE_PHOTOPERIOD = (("03:00", 50), ("04:00", 100), ("18:00", 50), ("19:00", 0))


class PhaseLookupTests(unittest.TestCase):
    """phase_at() — which phase contains a given wall-clock time."""

    def setUp(self):
        self.schedule = _sched(LIVE_PHOTOPERIOD)

    def test_a_boundary_owns_its_own_instant(self):
        # The transition is inclusive at its start: 03:00:00 is already 50%,
        # not still the previous phase. An off-by-one here would make every
        # boundary land a full tick late.
        self.assertEqual(ls.phase_at(self.schedule, time(3, 0)), 50)
        self.assertEqual(ls.phase_at(self.schedule, time(4, 0)), 100)
        self.assertEqual(ls.phase_at(self.schedule, time(18, 0)), 50)
        self.assertEqual(ls.phase_at(self.schedule, time(19, 0)), 0)

    def test_the_minute_before_a_boundary_is_still_the_previous_phase(self):
        self.assertEqual(ls.phase_at(self.schedule, time(2, 59)), 0)
        self.assertEqual(ls.phase_at(self.schedule, time(3, 59)), 50)
        self.assertEqual(ls.phase_at(self.schedule, time(17, 59)), 100)
        self.assertEqual(ls.phase_at(self.schedule, time(18, 59)), 50)

    def test_the_long_middle_of_a_phase(self):
        self.assertEqual(ls.phase_at(self.schedule, time(12, 0)), 100)
        self.assertEqual(ls.phase_at(self.schedule, time(23, 59)), 0)

    def test_before_the_first_boundary_wraps_to_the_last_one(self):
        # 00:30 is not "unscheduled" — it is inside the phase that opened at
        # 19:00 yesterday.
        self.assertEqual(ls.phase_at(self.schedule, time(0, 0)), 0)
        self.assertEqual(ls.phase_at(self.schedule, time(0, 30)), 0)

    def test_the_wrap_returns_the_last_phase_not_the_number_zero(self):
        # THE TEST THE MODULE'S OWN DOCSTRING ASKS FOR. With the shipped table
        # the wrapped phase happens to be 0, so a broken implementation that
        # returns a literal 0 for "no boundary applies" passes every case
        # above. Give the night phase a non-zero brightness and the two
        # answers separate.
        night_lit = _sched((("06:00", 30), ("22:00", 80)))
        self.assertEqual(ls.phase_at(night_lit, time(2, 0)), 80)
        self.assertEqual(ls.phase_at(night_lit, time(5, 59)), 80)
        self.assertEqual(ls.phase_at(night_lit, time(6, 0)), 30)

    def test_a_single_boundary_schedule_covers_the_whole_day(self):
        always = _sched((("12:00", 60),))
        for hhmm in ("00:00", "11:59", "12:00", "23:59"):
            self.assertEqual(
                ls.phase_at(always, time(int(hhmm[:2]), int(hhmm[3:]))), 60, hhmm
            )

    def test_boundaries_are_sorted_regardless_of_the_order_given(self):
        scrambled = _sched((("19:00", 0), ("03:00", 50), ("18:00", 50), ("04:00", 100)))
        self.assertEqual(
            [b.at.strftime("%H:%M") for b in scrambled.boundaries],
            ["03:00", "04:00", "18:00", "19:00"],
        )
        self.assertEqual(ls.phase_at(scrambled, time(12, 0)), 100)


class NextBoundaryTests(unittest.TestCase):
    """next_boundary_after() — when the scheduler next retakes the lamp."""

    def setUp(self):
        self.schedule = _sched(LIVE_PHOTOPERIOD)

    def test_it_is_strictly_after_so_a_boundary_does_not_expire_itself(self):
        # An override applied at exactly 19:00:00 must be held to 03:00, not
        # expired by the boundary it was applied on.
        self.assertEqual(
            ls.next_boundary_after(self.schedule, _at("19:00")), _at("03:00", day=9)
        )

    def test_mid_phase_finds_the_next_boundary_today(self):
        self.assertEqual(
            ls.next_boundary_after(self.schedule, _at("03:30")), _at("04:00")
        )
        self.assertEqual(
            ls.next_boundary_after(self.schedule, _at("12:00")), _at("18:00")
        )

    def test_after_the_last_boundary_wraps_to_tomorrows_first(self):
        self.assertEqual(
            ls.next_boundary_after(self.schedule, _at("19:05")), _at("03:00", day=9)
        )
        self.assertEqual(
            ls.next_boundary_after(self.schedule, _at("23:59")), _at("03:00", day=9)
        )

    def test_before_the_first_boundary_finds_it_today_not_tomorrow(self):
        self.assertEqual(
            ls.next_boundary_after(self.schedule, _at("01:00")), _at("03:00")
        )

    def test_seconds_below_a_boundary_still_resolve_to_that_boundary(self):
        # 18:59:59 must find 19:00 today. Truncating to minutes anywhere in
        # here would push it to the following boundary.
        self.assertEqual(
            ls.next_boundary_after(self.schedule, _at("18:59", second=59)), _at("19:00")
        )

    def test_a_single_boundary_schedule_wraps_to_the_same_time_tomorrow(self):
        always = _sched((("12:00", 60),))
        self.assertEqual(ls.next_boundary_after(always, _at("12:00")), _at("12:00", day=9))

    def test_boundaries_off_the_hour_keep_their_minutes(self):
        # A CORPUS GAP, not a weak assertion. Every other schedule in this file
        # sits on the hour, so `today.replace(hour=..., minute=...)` and
        # `today.replace(hour=...)` are identical across all of them — a mutant
        # deleting the minute survived until this schedule existed. parse_schedule
        # accepts any minute, so this is a production-reachable path.
        offbeat = _sched((("06:45", 40), ("21:05", 0)))
        self.assertEqual(
            ls.next_boundary_after(offbeat, _at("06:00")), datetime(2026, 8, 8, 6, 45)
        )
        self.assertEqual(
            ls.next_boundary_after(offbeat, _at("06:45")), datetime(2026, 8, 8, 21, 5)
        )
        self.assertEqual(
            ls.next_boundary_after(offbeat, _at("22:00")), datetime(2026, 8, 9, 6, 45)
        )

    def test_an_override_on_an_off_the_hour_schedule_expires_on_the_minute(self):
        offbeat = _sched((("06:45", 40), ("21:05", 0)))
        override = ls.Override(15, datetime(2026, 8, 8, 6, 30))
        self.assertTrue(
            ls.override_is_live(offbeat, override, datetime(2026, 8, 8, 6, 44, 59))
        )
        self.assertFalse(
            ls.override_is_live(offbeat, override, datetime(2026, 8, 8, 6, 45))
        )

    def test_it_crosses_a_month_end(self):
        # 2026-08-31 -> 2026-09-01. Naive date arithmetic that added a day by
        # replacing the day number would raise or wrap wrongly here.
        end_of_month = datetime(2026, 8, 31, 19, 30)
        self.assertEqual(
            ls.next_boundary_after(self.schedule, end_of_month), datetime(2026, 9, 1, 3, 0)
        )


class OverrideLivenessTests(unittest.TestCase):
    """override_is_live() — does a manual command still own the lamp?"""

    def setUp(self):
        self.schedule = _sched(LIVE_PHOTOPERIOD)

    def test_no_override_is_never_live(self):
        self.assertFalse(ls.override_is_live(self.schedule, None, _at("12:00")))

    def test_live_until_the_next_boundary(self):
        override = ls.Override(30, _at("12:00"))
        self.assertTrue(ls.override_is_live(self.schedule, override, _at("12:01")))
        self.assertTrue(ls.override_is_live(self.schedule, override, _at("17:59")))

    def test_dead_at_the_boundary_instant_itself(self):
        override = ls.Override(30, _at("12:00"))
        self.assertFalse(ls.override_is_live(self.schedule, override, _at("18:00")))
        self.assertFalse(ls.override_is_live(self.schedule, override, _at("18:01")))

    def test_the_documented_evening_case_holds_until_morning(self):
        # The accepted cost recorded in the T-527 design, asserted rather than
        # left in prose: 19:00 is the last boundary of the day, so an override
        # just after it owns the lamp all night.
        override = ls.Override(20, _at("19:05"))
        self.assertTrue(ls.override_is_live(self.schedule, override, _at("23:59")))
        self.assertTrue(ls.override_is_live(self.schedule, override, _at("02:59", day=9)))
        self.assertFalse(ls.override_is_live(self.schedule, override, _at("03:00", day=9)))

    def test_a_clock_that_stepped_backwards_leaves_the_override_live(self):
        # NTP correcting a fast clock backwards puts `now` before applied_at,
        # and the override keeps the lamp — the safe direction, since a
        # person's command beats a clock we have just caught being wrong.
        override = ls.Override(30, _at("12:00"))
        self.assertTrue(ls.override_is_live(self.schedule, override, _at("09:00")))

    def test_a_backwards_clock_step_is_NOT_bounded_by_one_day(self):
        # An earlier comment on the test above claimed the exposure was
        # "bounded by one day". It is not, and the arithmetic says why: the
        # expiry is one boundary after `applied_at`, not after `now`, so a
        # correction backwards by N days leaves the override live for N+1.
        # Asserted rather than left in prose, because this is the fact that
        # decides whether the TTL the design calls "additive on top of this"
        # is ever needed.
        override = ls.Override(30, datetime(2027, 1, 1, 12, 0))
        self.assertTrue(
            ls.override_is_live(self.schedule, override, datetime(2026, 8, 8, 19, 30))
        )
        self.assertEqual(
            ls.decide(self.schedule, datetime(2026, 8, 8, 23, 0), True, 100, override),
            (30, "override"),
        )
        # Reachability is low - fake-hwclock and systemd-timesyncd both step a
        # boot clock FORWARD - which is why this is documented rather than
        # guarded. It is not zero.


class DecisionOrderingTests(unittest.TestCase):
    """decide() — the three gates and the order they fire in."""

    def setUp(self):
        self.schedule = _sched(LIVE_PHOTOPERIOD)

    def test_the_schedule_decides_when_nothing_else_applies(self):
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), True),
            (100, "schedule"),
        )

    def test_a_live_override_beats_the_schedule(self):
        override = ls.Override(25, _at("11:00"))
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), True, 100, override),
            (25, "override"),
        )

    def test_a_live_override_beats_an_unsynced_clock(self):
        # Gate 1 before gate 2: somebody publishing a brightness right now is
        # better evidence than a schedule read off a clock we distrust.
        override = ls.Override(25, _at("11:00"))
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), False, 70, override),
            (25, "override"),
        )

    def test_an_expired_override_falls_through_to_the_schedule(self):
        override = ls.Override(25, _at("11:00"))
        self.assertEqual(
            ls.decide(self.schedule, _at("18:30"), True, 100, override),
            (50, "schedule"),
        )

    def test_an_expired_override_under_an_unsynced_clock_holds_not_schedules(self):
        # The ordering case that is easy to get wrong: once the override is
        # gone the clock gate must still fire, so this must NOT return the
        # 18:30 scheduled value.
        override = ls.Override(25, _at("11:00"))
        self.assertEqual(
            ls.decide(self.schedule, _at("18:30"), False, 70, override),
            (70, "hold_unsynced"),
        )

    def test_an_unsynced_clock_holds_the_last_applied_brightness(self):
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), False, 70),
            (70, "hold_unsynced"),
        )

    def test_holding_zero_is_a_hold_not_a_missing_value(self):
        # last_applied=0 is falsy but present. An implementation testing
        # `if not last_applied` would light a garden that had been correctly
        # dark, at whatever hour the network died.
        self.assertEqual(
            ls.decide(self.schedule, _at("23:00"), False, 0),
            (0, "hold_unsynced"),
        )

    def test_an_unsynced_clock_with_no_history_uses_the_configured_fallback(self):
        # First boot, no network, nothing persisted. The design chose lit over
        # dark here explicitly.
        self.assertEqual(
            ls.decide(_sched(LIVE_PHOTOPERIOD, fallback=80), _at("12:00"), False, None),
            (80, "fallback_unsynced"),
        )

    def test_the_synced_path_ignores_last_applied_entirely(self):
        # Once the clock is trustworthy the persisted value is history, not
        # input. A leak from gate 2 into gate 3 would pin the lamp forever.
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), True, 5),
            (100, "schedule"),
        )


class ClampingTests(unittest.TestCase):
    """Nothing leaves decide() that Light.set_duty_cycle would raise on.

    A raise inside the scheduler thread kills the THREAD and not the process,
    so systemd's Restart=always never fires and the lamp silently stops
    following its schedule. Every one of these inputs is reachable in
    production: the override values from any client with broker rights, the
    last_applied values from a state file truncated by a power cut.
    """

    def setUp(self):
        self.schedule = _sched(LIVE_PHOTOPERIOD)

    def test_an_override_above_the_maximum_is_clamped(self):
        override = ls.Override(999, _at("11:00"))
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), True, None, override), (100, "override")
        )

    def test_a_negative_override_is_clamped(self):
        override = ls.Override(-5, _at("11:00"))
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), True, None, override), (0, "override")
        )

    def test_a_corrupt_persisted_brightness_is_clamped_not_raised(self):
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), False, 4096), (100, "hold_unsynced")
        )
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), False, -1), (0, "hold_unsynced")
        )

    def test_a_non_numeric_persisted_brightness_becomes_zero_not_an_exception(self):
        for junk in ("", "nan-ish", [], {}):
            self.assertEqual(
                ls.decide(self.schedule, _at("12:00"), False, junk),
                (0, "hold_unsynced"),
                repr(junk),
            )

    def test_a_float_brightness_truncates_rather_than_raising(self):
        override = ls.Override(55.9, _at("11:00"))
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), True, None, override), (55, "override")
        )

    def test_an_infinite_brightness_is_clamped_rather_than_raising(self):
        # int(inf) raises OverflowError, which is a subclass of NEITHER
        # TypeError nor ValueError - so an except clause naming only those two
        # lets it straight through, and the docstring promising "never raises"
        # is false. Reachable: a caller parsing the brightness payload with
        # float() turns "inf", "Infinity" and "1e999" all into float('inf').
        for value in (float("inf"), float("1e999")):
            self.assertEqual(
                ls.decide(self.schedule, _at("12:00"), False, value),
                (100, "hold_unsynced"),
                repr(value),
            )
            self.assertEqual(
                ls.decide(
                    self.schedule, _at("12:00"), True, None, ls.Override(value, _at("11:00"))
                ),
                (100, "override"),
                repr(value),
            )
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), False, float("-inf")),
            (0, "hold_unsynced"),
        )

    def test_a_nan_brightness_is_clamped_by_a_different_route(self):
        # int(nan) raises ValueError, not OverflowError. Separate case so that
        # narrowing the except clause back down is visible.
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), False, float("nan")),
            (0, "hold_unsynced"),
        )

    def test_the_clamp_has_no_discontinuity_at_the_float_limit(self):
        # 1e308 is finite and 1e309 is inf, and they take different branches.
        # They must still give the same answer, or "too bright" and
        # "unreadable" flip places either side of a limit nobody chose.
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), False, 1e308)[0],
            ls.decide(self.schedule, _at("12:00"), False, 1e309)[0],
        )
        self.assertEqual(ls.decide(self.schedule, _at("12:00"), False, 1e308)[0], 100)

    def test_every_shape_a_caller_can_produce_lands_in_the_same_place(self):
        # The string column is the one that was broken. int() refuses any
        # string with a decimal point, so "55.0" - which the docstring itself
        # cites as a legitimate brightness - used to become 0, a dark garden
        # from a well-formed publish, while 55.9 became 55. A caller cannot
        # know which shape it holds when a payload arrives as bytes on a wire.
        cases = [
            (55.9, 55), ("55", 55), ("55.0", 55), (55, 55),
            (float("inf"), 100), ("inf", 100), (1e308, 100), ("1e999", 100),
            (-1, 0), ("-1", 0), (float("-inf"), 0),
            (float("nan"), 0), ("nan", 0), ("abc", 0), (b"xyz", 0), ([], 0),
            (10 ** 400, 100), (-(10 ** 400), 0),
            # float() accepts bytes, so an undecoded MQTT payload clamps
            # correctly rather than becoming 0. Worth pinning: it means the
            # T-527.5 seam does not have to decode before clamping, and a
            # future switch back to int()-style parsing would break it
            # silently in the dark-garden direction.
            (b"55", 55), (b"55.0", 55), (b"999", 100),
            # bytearray, because _clamped's docstring names it as covered
            # (T-527.16, which narrowed that contract from an unqualified
            # "Never raises" to an enumerated set). A shape the prose claims
            # and nothing executes is the defect this ticket keeps finding, so
            # every name in that sentence gets a case here.
            (bytearray(b"55"), 55), (memoryview(b"55"), 55),
            # …and the non-numeric shapes at the other end of the same
            # sentence, which must degrade rather than raise.
            ([], 0), ({}, 0), (object(), 0),
        ]
        for value, expected in cases:
            # _clamped directly, because `None` is a SENTINEL at the decide()
            # boundary (it means "no history", routing to the fallback branch)
            # rather than a value to clamp. Going through decide() here would
            # conflate the two, which is how the first draft of this test
            # failed. The branch-level tests below cover the routing.
            self.assertEqual(
                ls._clamped(value), expected, f"{value!r} should clamp to {expected}"
            )
        self.assertEqual(ls._clamped(None), 0)
        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), False, None)[1],
            "fallback_unsynced",
            "None must stay a sentinel at the decide() boundary, not a clamped 0",
        )

    def test_the_clamp_is_applied_on_the_SCHEDULE_branch_too(self):
        # A Schedule built through the bare NamedTuple constructor skips
        # Schedule.of's validation - which the Schedule docstring now says out
        # loud, and which is exactly what makes this clamp load-bearing rather
        # than decorative. Deleting it survived the whole suite until this
        # existed, while decide()'s docstring claimed EVERY returned brightness
        # goes through _clamped.
        rogue = ls.Schedule((ls.Boundary(time(0, 0), 999),), 100)
        self.assertEqual(ls.decide(rogue, _at("12:00"), True), (100, "schedule"))

    def test_the_clamp_is_applied_on_the_FALLBACK_branch_too(self):
        rogue = ls.Schedule((ls.Boundary(time(0, 0), 50),), 4096)
        self.assertEqual(
            ls.decide(rogue, _at("12:00"), False, None), (100, "fallback_unsynced")
        )

    def test_an_object_that_overflows_AND_refuses_comparison_does_not_raise(self):
        # The inner guard in _clamped's OverflowError branch. Unreachable from
        # a payload or a state file - only a custom object gets here - but the
        # function's contract is that it never raises, and a raise on the tick
        # thread is the failure mode the whole module is built around. Given a
        # test, it is also mutable; without one it is unfalsifiable prose.
        class Awkward:
            def __float__(self):
                raise OverflowError("too big to say")

            def __gt__(self, other):
                raise TypeError("no ordering here")

        self.assertEqual(
            ls.decide(self.schedule, _at("12:00"), False, Awkward()),
            (0, "hold_unsynced"),
        )


class ScheduleValidationTests(unittest.TestCase):
    """Schedule.of() — the single construction path, and what it refuses."""

    def test_an_empty_schedule_is_refused(self):
        with self.assertRaises(ls.ScheduleConfigError):
            ls.Schedule.of([], 100)

    def test_an_empty_ITERATOR_is_refused_too(self):
        # `not pairs` asks the container, and an exhausted iterator is truthy -
        # so this used to build a Schedule with no boundaries and defer the
        # failure to an IndexError inside phase_at, a long way from the cause.
        with self.assertRaises(ls.ScheduleConfigError):
            ls.Schedule.of(iter([]), 100)

    def test_the_bare_namedtuple_constructor_is_NOT_a_validated_path(self):
        # Documenting the limit rather than pretending it is closed. Schedule
        # is a plain NamedTuple, so this builds and then breaks downstream.
        # Every caller must go through Schedule.of or parse_schedule; this test
        # exists so that requirement is discoverable from the suite and so a
        # future guard has something to flip.
        unvalidated = ls.Schedule((), 100)
        with self.assertRaises(IndexError):
            ls.phase_at(unvalidated, time(12, 0))

    def test_a_duplicate_boundary_time_is_refused(self):
        with self.assertRaises(ls.ScheduleConfigError):
            ls.Schedule.of([(time(6, 0), 50), (time(6, 0), 80)], 100)

    def test_a_sub_minute_boundary_is_refused(self):
        # Unreachable at any sane tick cadence, so accepting one would
        # silently do nothing.
        with self.assertRaises(ls.ScheduleConfigError):
            ls.Schedule.of([(time(6, 0, 30), 50)], 100)

    def test_a_sub_second_boundary_is_refused_too(self):
        # Separate case, not redundant with the one above: the guard is an
        # `or` over two fields, and a boundary carrying only microseconds
        # satisfies neither the second-hand test nor a reader's intuition. A
        # battery mutant dropping the microsecond half survives without this.
        with self.assertRaises(ls.ScheduleConfigError):
            ls.Schedule.of([(time(6, 0, 0, 500), 50)], 100)

    def test_a_non_time_boundary_is_refused(self):
        with self.assertRaises(ls.ScheduleConfigError):
            ls.Schedule.of([("06:00", 50)], 100)

    def test_an_out_of_range_brightness_is_refused_at_config_time(self):
        # Contrast with ClampingTests: config is edited by a human under
        # sudoedit and must be loud; a runtime value must never raise.
        with self.assertRaises(ls.ScheduleConfigError):
            ls.Schedule.of([(time(6, 0), 101)], 100)
        with self.assertRaises(ls.ScheduleConfigError):
            ls.Schedule.of([(time(6, 0), -1)], 100)

    def test_an_out_of_range_fallback_is_refused(self):
        with self.assertRaises(ls.ScheduleConfigError):
            ls.Schedule.of([(time(6, 0), 50)], 101)


class ParseTests(unittest.TestCase):
    """parse_schedule() — the /etc/gardyn/ config schema."""

    def test_a_well_formed_schedule_parses(self):
        parsed = ls.parse_schedule(
            {
                "GARDYN_LIGHT_SCHEDULE": "03:00=50,04:00=100,18:00=50,19:00=0",
                "GARDYN_LIGHT_UNSYNCED_FALLBACK": "60",
            }
        )
        self.assertEqual(
            [(b.at.strftime("%H:%M"), b.brightness) for b in parsed.boundaries],
            [("03:00", 50), ("04:00", 100), ("18:00", 50), ("19:00", 0)],
        )
        self.assertEqual(parsed.unsynced_fallback, 60)

    def test_whitespace_around_entries_is_tolerated(self):
        parsed = ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": " 06:00=40 , 22:00=0 "})
        self.assertEqual(len(parsed.boundaries), 2)

    def test_a_missing_schedule_key_is_refused(self):
        # assertRaisesRegex, not assertRaises. Both this and the empty-value
        # case below still raise if the missing-key guard is deleted - they
        # just fall through to the empty-entry check instead - so asserting
        # only the exception TYPE makes that guard unkillable, and a mutant
        # removing it survived until these pinned the message.
        with self.assertRaisesRegex(ls.ScheduleConfigError, "missing or empty"):
            ls.parse_schedule({})

    def test_an_empty_schedule_value_is_refused(self):
        with self.assertRaisesRegex(ls.ScheduleConfigError, "missing or empty"):
            ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": "   "})

    def test_trailing_junk_after_a_valid_entry_is_refused(self):
        # The `$` anchor, which had no test and no mutant. Un-anchored,
        # "03:00=50x" parses as three o'clock at 50 and the junk vanishes -
        # exactly what a truncated or fat-fingered edit looks like.
        for bad in ("03:00=50x", "03:00=50 50", "03:00=50;18:00=0"):
            with self.assertRaises(ls.ScheduleConfigError, msg=bad):
                ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": bad})

    def test_junk_before_a_valid_entry_is_refused(self):
        # NOT a test of the `^` anchor, though an earlier version of this
        # comment said it was. `.match()` anchors at position 0 by itself, so
        # this passes identically with `^` removed — review caught the comment
        # claiming coverage that does not exist, which is worse than the gap,
        # because it is what stops the next reader looking. See the note on
        # _ENTRY_RE: `^` is dormant insurance and nothing can pin it today.
        for bad in ("x03:00=50", " -03:00=50"):
            with self.assertRaises(ls.ScheduleConfigError, msg=bad):
                ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": bad})

    def test_a_trailing_newline_is_refused_by_the_pattern_itself(self):
        # `\Z`, not `$` — in Python `$` also matches before a trailing newline,
        # so "03:00=50\n" satisfied the old pattern and was stopped only by the
        # .strip() at the call site. Asserted against _ENTRY_RE directly
        # because parse_schedule strips first, which would hide it.
        self.assertIsNone(ls._ENTRY_RE.match("03:00=50\n"))
        self.assertIsNotNone(ls._ENTRY_RE.match("03:00=50"))

    def test_a_non_iterable_schedule_is_refused_as_a_config_error(self):
        # Not a TypeError. The module tells callers to catch
        # ScheduleConfigError and fall back to DEFAULT_SCHEDULE rather than
        # exit; anything escaping that lineage makes the instruction wrong, and
        # on Restart=always with StartLimitIntervalSec=0 the cost is a
        # permanent crash loop. Materialising `pairs` introduced exactly that
        # regression and this is what would have caught it.
        for bad in (None, 0, 3.5):
            with self.assertRaises(ls.ScheduleConfigError, msg=repr(bad)):
                ls.Schedule.of(bad, 100)
            self.assertTrue(issubclass(ls.ScheduleConfigError, ValueError))

    def test_non_ascii_digits_are_refused(self):
        # \d matches Unicode decimal digits in Python 3, so the pattern used to
        # accept Eastern Arabic numerals and int() used to convert them. The
        # parsed value would have been correct, which is what makes it a
        # defect in the "a typo must be loud" claim rather than in the output.
        with self.assertRaises(ls.ScheduleConfigError):
            ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": "٠٣:٠٠=٥٠"})

    def test_a_trailing_comma_is_refused_rather_than_skipped(self):
        # Leniency here would make a truncated edit indistinguishable from a
        # complete one.
        with self.assertRaises(ls.ScheduleConfigError):
            ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": "06:00=40,"})

    def test_an_unpadded_hour_is_refused(self):
        with self.assertRaises(ls.ScheduleConfigError):
            ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": "6:00=40"})

    def test_an_impossible_clock_time_is_refused(self):
        for bad in ("25:00=40", "12:60=40"):
            with self.assertRaises(ls.ScheduleConfigError, msg=bad):
                ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": bad})

    def test_a_malformed_entry_is_refused(self):
        for bad in ("06:00", "06:00=x", "0600=40", "06:00=", "=40"):
            with self.assertRaises(ls.ScheduleConfigError, msg=bad):
                ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": bad})

    def test_a_missing_fallback_inherits_the_shipped_default(self):
        parsed = ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": "06:00=40"})
        self.assertEqual(parsed.unsynced_fallback, 100)

    def test_an_empty_fallback_inherits_rather_than_becoming_zero(self):
        # "KEY=" in the env file must not read as "hold the lamp at 0", which
        # is a dark garden by typo.
        parsed = ls.parse_schedule(
            {"GARDYN_LIGHT_SCHEDULE": "06:00=40", "GARDYN_LIGHT_UNSYNCED_FALLBACK": ""}
        )
        self.assertEqual(parsed.unsynced_fallback, 100)

    def test_an_explicit_zero_fallback_is_honoured(self):
        # The counterpart to the test above: 0 is a legitimate choice and must
        # survive, so the empty-string check cannot be a falsiness check.
        parsed = ls.parse_schedule(
            {"GARDYN_LIGHT_SCHEDULE": "06:00=40", "GARDYN_LIGHT_UNSYNCED_FALLBACK": "0"}
        )
        self.assertEqual(parsed.unsynced_fallback, 0)

    def test_a_bad_fallback_is_refused(self):
        for bad in ("101", "-1", "bright"):
            with self.assertRaises(ls.ScheduleConfigError, msg=bad):
                ls.parse_schedule(
                    {
                        "GARDYN_LIGHT_SCHEDULE": "06:00=40",
                        "GARDYN_LIGHT_UNSYNCED_FALLBACK": bad,
                    }
                )


class ShippedDefaultTests(unittest.TestCase):
    """DEFAULT_SCHEDULE is what a broken config file falls back to."""

    def test_it_matches_the_photoperiod_this_garden_actually_runs(self):
        # Literals, not a derivation from the constant being pinned. Read live
        # from automation.gardyn_grow_light_schedule on 2026-08-08.
        self.assertEqual(
            [(b.at.hour, b.at.minute, b.brightness) for b in ls.DEFAULT_SCHEDULE.boundaries],
            [(3, 0, 50), (4, 0, 100), (18, 0, 50), (19, 0, 0)],
        )

    def test_its_fallback_lights_the_garden_rather_than_darkening_it(self):
        # The design's stated preference under a first boot with no network.
        self.assertGreater(ls.DEFAULT_SCHEDULE.unsynced_fallback, 0)

    def test_a_boundary_in_the_dst_skipped_hour_is_accepted_and_self_corrects(self):
        # THIS TEST REPLACED ONE THAT ASSERTED THE WRONG THING. The original
        # required no DEFAULT_SCHEDULE boundary to have hour == 2, on the
        # stated grounds that 02:00-03:00 is "the hour DST skips or repeats".
        # Two errors: those are different hours (02:00-02:59 is skipped,
        # 01:00-01:59 is repeated — measured against the tzdb), and a boundary
        # is added in /etc/gardyn/, which that assertion could never see. It
        # was a guard over the wrong corpus, checking the wrong hour.
        #
        # The honest statement is that such a boundary is ACCEPTED, and that
        # phase computation is what makes it harmless. On spring-forward
        # morning the 02:30 phase is simply never entered, and the first tick
        # after the jump already reports the correct later phase.
        dst_risky = ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": "02:30=50,12:00=100"})
        self.assertEqual(len(dst_risky.boundaries), 2)
        self.assertEqual(ls.phase_at(dst_risky, time(3, 0)), 50)
        self.assertEqual(ls.phase_at(dst_risky, time(12, 0)), 100)
        # ...and the repeated hour, which the old assertion did not cover at all.
        repeated_hour = ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": "01:30=50,12:00=100"})
        self.assertEqual(ls.phase_at(repeated_hour, time(1, 30)), 50)
        self.assertEqual(ls.phase_at(repeated_hour, time(1, 29)), 100)


# --- T-527.14: the DST table, derived rather than restated ------------------
#
# next_boundary_after's docstring carries a measured tzdb table. Nothing pinned
# it, and an earlier version of that docstring named ONE hour for both
# transitions and shipped. A table that is load-bearing evidence and that no
# test reads is exactly the artifact that goes stale silently — the more so
# with live US DST legislation, which would move these hours without touching
# a line of this repo.
#
# The class below derives the hours from zoneinfo and fails if they move.
# It does NOT restate them from literals anywhere except in the assertion that
# the derivation still matches what the docstring claims, which is the whole
# point: the literal is the thing under test, not the input to it.

_DENVER = "America/Denver"


def _tz_available(name=_DENVER):
    """Is the tzdb reachable? A missing tzdb is a SKIP, not a failure.

    light_schedule uses naive local time on purpose and needs no tzdb at
    runtime, so its absence says nothing about the code under test — only that
    this particular instrument is unavailable here.
    """
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
    except Exception:
        return False
    return True


class LoadBearingProseAroundTheDstTableIsPinned(unittest.TestCase):
    """Deliberately NOT under the tzdb skipUnless below.

    It lived inside DstTableIsDerivedFromTheTzdb until the review of
    2a8c951 pointed out that the class carries
    `@unittest.skipUnless(_tz_available(), ...)` and this test needs no tzdb
    at all - it reads a docstring. Measured under
    `PYTHONTZPATH=/nonexistent`: `OK (skipped=8)`, the tripwire among them.
    A tripwire coupled to a dependency it does not use is a tripwire that
    goes quiet on exactly the machines least like the one it was written on.
    """

    def test_the_load_bearing_SENTENCES_around_the_table_are_pinned(self):
        """A TRIPWIRE, not a proof — and it is worth being explicit about
        which.

        The test above reads every column of the table. It reads none of the
        prose, and the prose is where the conclusion lives: the table only
        says WHEN the transitions are, while the sentences under it say why
        naive local time is safe anyway. The 094eac0 review measured three
        edits that leave the whole suite green and invert the argument —
        `hour != 2` to `hour != 1`, "are NOT the same hour" to "are the same
        hour", and "Both are self-correcting within one tick." to "Neither is
        self-correcting; the lamp stays wrong until the next day."

        Asserting free prose properly is a different and much harder problem,
        so this does the crude thing instead and says so: three claims are
        pinned as exact strings, whitespace-normalised so reflowing the
        docstring is not a failure. Rewording one of them reddens this test,
        which is the intent — these are the sentences a reader relies on when
        deciding whether a boundary near a transition is safe, and they should
        not be edited without somebody noticing.

        What this canNOT do: it cannot tell a correct rewrite from a wrong one.
        A red result here means "a load-bearing sentence changed, go read it",
        never "the new sentence is false".
        """
        doc = ls.next_boundary_after.__doc__
        self.assertIsNotNone(doc)
        flat = " ".join(doc.split())

        claims = (
            # The two transitions are different hours. Inverting this is what
            # the whole table exists to prevent, and the table itself stays
            # consistent under the inversion because it is a separate reader.
            "the two transitions are NOT the same hour",
            # Which guard is only half a guard. `hour != 2` to `hour != 1`
            # reads as a correction and is the opposite of one.
            'a rule of the form "keep boundaries out of the transition hour" '
            "would have to name both, and `hour != 2` guards only half of it",
            # The conclusion. Without this sentence the paragraph above it
            # reads as a list of hazards rather than as an argument that they
            # cost nothing.
            "Both are self-correcting within one tick.",
        )
        for claim in claims:
            with self.subTest(claim=claim[:48]):
                self.assertIn(
                    claim, flat,
                    f"a load-bearing sentence in next_boundary_after's "
                    f"docstring has been edited or removed. This test cannot "
                    f"judge the new wording — go and read it, and if the "
                    f"change is deliberate, update the string here.")



@unittest.skipUnless(_tz_available(), f"tzdb entry for {_DENVER} unavailable")
class DstTableIsDerivedFromTheTzdb(unittest.TestCase):
    """Pins next_boundary_after's docstring table against the real tzdb."""

    @classmethod
    def setUpClass(cls):
        from zoneinfo import ZoneInfo

        cls.tz = ZoneInfo(_DENVER)

    # -- the two predicates, and why they are not the same question ---------

    def _is_skipped(self, naive):
        """True if this wall-clock time does not exist on that date."""
        aware = naive.replace(tzinfo=self.tz)
        round_tripped = aware.astimezone(timezone.utc).astimezone(self.tz)
        return round_tripped.replace(tzinfo=None) != naive

    def _is_ambiguous(self, naive):
        """True if fold=0 and fold=1 disagree about the UTC offset."""
        return (
            naive.replace(tzinfo=self.tz, fold=0).utcoffset()
            != naive.replace(tzinfo=self.tz, fold=1).utcoffset()
        )

    def _is_repeated(self, naive):
        """True if this wall-clock time occurs TWICE on that date."""
        return self._is_ambiguous(naive) and not self._is_skipped(naive)

    def test_ambiguity_alone_cannot_tell_a_gap_from_a_fold(self):
        # THIS TEST EXISTS TO STOP _is_repeated BEING "SIMPLIFIED" BACK TO
        # _is_ambiguous. PEP 495 gives `fold` a meaning on BOTH sides of a
        # transition, so differing offsets are true of the SKIPPED hour as
        # well as the repeated one. Written naively, the derivation below
        # would report 02:30 on spring-forward morning as "repeated" — the
        # exact conflation of the two hours that the docstring shipped once
        # already and that this whole class exists to prevent recurring.
        spring_gap = datetime(2026, 3, 8, 2, 30)
        self.assertTrue(self._is_ambiguous(spring_gap))  # the trap
        self.assertTrue(self._is_skipped(spring_gap))
        self.assertFalse(self._is_repeated(spring_gap))  # the fix

    # -- the derivation -----------------------------------------------------

    def _transition_dates(self, year):
        """Days in `year` where the noon UTC offset differs from the day before."""
        found = []
        day = datetime(year, 1, 1, 12)
        previous = day.replace(tzinfo=self.tz).utcoffset()
        while day.year == year:
            day += timedelta(days=1)
            current = day.replace(tzinfo=self.tz).utcoffset()
            if current != previous:
                found.append(day.date())
            previous = current
        return found

    def test_the_docstring_table_still_matches_the_tzdb(self):
        spring, fall = self._transition_dates(2026)

        # The dates the docstring names.
        self.assertEqual((spring.month, spring.day), (3, 8))
        self.assertEqual((fall.month, fall.day), (11, 1))

        skipped = [
            hour
            for hour in range(24)
            if self._is_skipped(datetime(2026, spring.month, spring.day, hour, 30))
        ]
        repeated = [
            hour
            for hour in range(24)
            if self._is_repeated(datetime(2026, fall.month, fall.day, hour, 30))
        ]

        # "02:00-02:59 SKIPPED" and "01:00-01:59 REPEATED", as measured.
        self.assertEqual(skipped, [2])
        self.assertEqual(repeated, [1])

        # And the claim the wrong version of the docstring got wrong: these
        # are NOT the same hour, so a rule of the form "keep boundaries out of
        # the transition hour" would have to name both.
        self.assertNotEqual(skipped, repeated)

    def test_the_docstring_itself_is_read_and_matched_against_the_tzdb(self):
        # THE TEST ABOVE CLOSES ONLY ONE DIRECTION. It compares the tzdb
        # against literals living in THIS file, so it fails if the tzdb moves
        # — and stays green if somebody edits the docstring wrongly, which is
        # the failure that actually happened here: an earlier version named
        # one hour for both transitions and shipped.
        #
        # So parse the table out of the docstring and check it against the
        # same derivation.
        #
        # WHAT THAT DOES AND DOES NOT COVER. This comment said "Now either
        # half going wrong is a red test" until 2026-08-12, naming the second
        # half as "somebody edits the docstring wrongly" — false for the
        # SENTENCES around the table, as the review of 094eac0 measured:
        # inverting "Both are self-correcting within one tick." to "Neither is
        # self-correcting; the lamp stays wrong until the next day." negates
        # the entire safety argument for naive local time with the full suite
        # green. This test reads the TABLE. The prose is pinned separately and
        # far more crudely, by
        # test_the_load_bearing_SENTENCES_around_the_table_are_pinned below.
        doc = ls.next_boundary_after.__doc__
        self.assertIsNotNone(doc, "the table lives in the docstring; -OO would remove it")

        # EVERY COLUMN IS CAPTURED AND ASSERTED. The pattern here read
        #     r".*?(\d{2}):00-(\d{2}):59\s+"
        # until 2026-08-12, and that `.*?` swallowed the whole offset column —
        # `01:00 MST -> 03:00 MDT` — which was therefore compared against
        # nothing. The T-527.27 review measured the consequence: with two
        # positive controls red first, SIX wrong-table edits stayed green,
        # including `-> 03:00 MST` and `-> 02:00 MDT`. A field this test does
        # not read is a field the table can lie in, and the table is the whole
        # evidence for naive local time being safe in this module.
        matches = re.findall(
            r"^\s*(spring forward|fall back)\s+"
            r"(\d{4}-\d{2}-\d{2})\s+"
            r"(\d{2}):00\s+([A-Z]{2,5})\s*->\s*(\d{2}):00\s+([A-Z]{2,5})\s+"
            r"(\d{2}):00-(\d{2}):59\s+"
            r"(SKIPPED|REPEATED)\s*$",
            doc,
            re.M,
        )

        # COUNT THE ROWS LOOSELY FIRST. The duplicate refusal below and the
        # column assertions further down can only ever see rows the strict
        # pattern PARSES, so a row it cannot read is discarded in the same
        # silence the dict used to discard duplicates in — one level up, and
        # noted by the 094eac0 review as an over-claim in the comment below.
        # A wrong spring row spelled with one-digit hours (`1:00 MST -> 3:00
        # MDT`), or with lowercase abbreviations, matches nothing above and
        # vanishes. So identify a row by the one thing a row must have — a
        # transition label opening the line — and require the strict pattern to
        # have read every one of them.
        #
        # THE LABEL ALONE, deliberately. This also required an ISO date until
        # the review of 2a8c951 pointed out that both patterns then share the
        # same gap: an extra row carrying a malformed date (`2026-3-08`) is
        # invisible to the loose count and the strict one alike, the two counts
        # agree, and the suite stays green. Measured, and the gate's own
        # comment claimed to cover "a row this test cannot parse" generally.
        # The cost of dropping the date is that prose beginning a line with
        # "spring forward" or "fall back" now reddens this test — loud, and the
        # message says what to do about it, which is the right direction for a
        # gate whose entire history is failing silently.
        loose = re.findall(r"^\s*(spring forward|fall back)\b.*$", doc, re.M)
        self.assertEqual(
            len(matches), len(loose),
            f"the docstring table has {len(loose)} row(s) and the strict "
            f"pattern read {len(matches)} of them; a row this test cannot "
            f"parse is a row the table can lie in",
        )

        # REFUSE DUPLICATES BEFORE BUILDING THE DICT. `rows = {label: rest ...}`
        # keeps the LAST row per label and drops the rest in silence, so a
        # wrong row inserted ABOVE the correct one parsed, was discarded, and
        # the test passed — which is precisely the historical error this test
        # exists to prevent (an earlier docstring named one hour for both
        # transitions), re-admitted by the reader meant to catch it.
        labels = [m[0] for m in matches]
        self.assertEqual(
            len(labels), len(set(labels)),
            f"the docstring table has more than one row per transition "
            f"({labels}); one of them is being silently discarded",
        )
        rows = {label: rest for label, *rest in matches}
        self.assertEqual(
            set(rows), {"spring forward", "fall back"},
            "the docstring table did not parse — it has been reformatted, and "
            "this test is the only reader of it",
        )

        (spring_date, s_wall_from, s_abbr_from, s_wall_to, s_abbr_to,
         s_from, s_to, s_kind) = rows["spring forward"]
        (fall_date, f_wall_from, f_abbr_from, f_wall_to, f_abbr_to,
         f_from, f_to, f_kind) = rows["fall back"]

        # The docstring says which hour, and which KIND each transition is.
        self.assertEqual((s_kind, f_kind), ("SKIPPED", "REPEATED"))
        self.assertEqual(s_from, s_to)
        self.assertEqual(f_from, f_to)

        derived_spring, derived_fall = self._transition_dates(2026)
        self.assertEqual(spring_date, derived_spring.isoformat())
        self.assertEqual(fall_date, derived_fall.isoformat())
        self.assertTrue(
            self._is_skipped(datetime(2026, derived_spring.month, derived_spring.day,
                                      int(s_from), 30)),
            f"the docstring calls {s_from}:00 skipped; the tzdb disagrees",
        )
        self.assertTrue(
            self._is_repeated(datetime(2026, derived_fall.month, derived_fall.day,
                                       int(f_from), 30)),
            f"the docstring calls {f_from}:00 repeated; the tzdb disagrees",
        )

        # THE OFFSET COLUMN, derived rather than restated — the half `.*?` used
        # to skip. Each abbreviation is read back off zoneinfo on the date the
        # table itself names, so a wrong one is red without anything here
        # hardcoding "MST" or "MDT".
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(_DENVER)

        def abbr(day, hour, fold=0):
            return datetime(2026, day.month, day.day, hour, 30,
                            fold=fold, tzinfo=tz).tzname()

        # Spring: the two sides sit on either side of the gap, so ordinary
        # lookups reach them.
        self.assertEqual(abbr(derived_spring, int(s_wall_from)), s_abbr_from)
        self.assertEqual(abbr(derived_spring, int(s_wall_to)), s_abbr_to)
        self.assertEqual(
            int(s_wall_to) - int(s_wall_from), 2,
            f"spring forward skips an hour, so the wall clock steps 01:00 -> "
            f"03:00; the table says {s_wall_from}:00 -> {s_wall_to}:00",
        )

        # Fall: BOTH sides are the same wall-clock hour — that is what makes
        # it ambiguous rather than skipped — so `fold` is the only thing that
        # separates them, and reading them any other way conflates the two
        # transitions, which is the conflation this whole class exists to stop.
        self.assertEqual(abbr(derived_fall, int(f_wall_from), fold=0),
                         f_abbr_from)
        self.assertEqual(abbr(derived_fall, int(f_wall_to), fold=1),
                         f_abbr_to)
        self.assertEqual(
            int(f_wall_to), int(f_wall_from),
            f"fall back REPEATS an hour, so both sides read the same wall "
            f"clock; the table says {f_wall_from}:00 -> {f_wall_to}:00, which "
            f"describes a skip",
        )

        # RESTORED 2026-08-12, hours after being deleted as dead. It is not
        # dead, and the reasoning that deleted it is worth keeping because it
        # is the plausible mistake:
        #
        #   "Each of the four assertEqual(abbr(...), ...) checks above pins an
        #   abbreviation against zoneinfo's answer for that side of the
        #   transition, and those two answers differ by construction, so a
        #   table whose two abbreviations matched had already failed one."
        #
        # The flaw is that `abbr()` is called with hours taken FROM THE TABLE.
        # Move both sides of a row onto the same side of the transition and
        # zoneinfo agrees with both, honestly. Measured by the review of
        # 2a8c951, and reproduced here before restoring:
        #
        #   spring forward … 03:00 MDT -> 05:00 MDT  02:00-02:59 SKIPPED
        #   fall back       … 05:00 MST -> 05:00 MST  01:00-01:59 REPEATED
        #
        # Both tables are wrong - the first says the wall clock steps 03:00 to
        # 05:00 on spring-forward morning, the second names 05:00 as the
        # repeated hour - and both pass every other assertion in this test.
        # With the loop restored each fails on `'MDT' == 'MDT'` / `'MST' ==
        # 'MST'`.
        #
        # "Deleting it flipped no arm of the nine-perturbation control" was
        # true and was a CORPUS gap, not evidence: neither shape was among the
        # nine. The `steps by 2` check above is load-bearing too, and remains
        # the sole catcher of `01:00 MST -> 04:00 MDT`, which this loop does
        # NOT catch - the two overlap in neither direction.
        for label, before, after in (("spring forward", s_abbr_from, s_abbr_to),
                                     ("fall back", f_abbr_from, f_abbr_to)):
            self.assertNotEqual(
                before, after,
                f"the {label} row shows the same abbreviation on both sides, "
                f"so it describes two readings from the same side of the "
                f"transition rather than a transition at all - and every "
                f"other assertion here agrees with it, because they read the "
                f"hours the table itself supplied")

    def test_each_transition_day_has_exactly_one_anomalous_hour(self):
        # Guards the derivation itself rather than the table: if a future tzdb
        # introduced a half-hour or double transition, the lists above would
        # still be [2] and [1] by luck of the :30 sample. Sample every quarter
        # hour and assert the anomaly is exactly one contiguous hour wide.
        spring, fall = self._transition_dates(2026)
        quarter_hours = [(h, m) for h in range(24) for m in (0, 15, 30, 45)]

        gaps = [
            (h, m)
            for h, m in quarter_hours
            if self._is_skipped(datetime(2026, spring.month, spring.day, h, m))
        ]
        folds = [
            (h, m)
            for h, m in quarter_hours
            if self._is_repeated(datetime(2026, fall.month, fall.day, h, m))
        ]
        self.assertEqual(gaps, [(2, 0), (2, 15), (2, 30), (2, 45)])
        self.assertEqual(folds, [(1, 0), (1, 15), (1, 30), (1, 45)])


@unittest.skipUnless(_tz_available(), f"tzdb entry for {_DENVER} unavailable")
class DecideSelfCorrectsAcrossARealTransition(unittest.TestCase):
    """Drives decide() over the wall-clock sequence a real transition produces.

    NOTHING WAS REPLACED, and this paragraph said otherwise until 2026-08-12.
    It opened "THE TEST THIS REPLACES ASSERTED A LOOKUP", and the commit
    message said the same — but
    ShippedDefaultTests.test_a_boundary_in_the_dst_skipped_hour_is_accepted_and_self_corrects
    is still in this file, still running, and `git show 0396564 --numstat` on
    this file is `320 1`, the single deletion being an import line. So two
    tests each carried a comment claiming it had superseded a wrong
    predecessor, and a reader chasing the older one would have gone looking
    for something that was never removed.

    Worth naming plainly because of where it landed: this class exists to stop
    prose asserting coverage no test provides, and that is the defect its own
    docstring shipped with. The repo has now been bitten by it five times.

    WHAT IS ACTUALLY TRUE about the older test: it asserts a phase_at LOOKUP at
    hand-picked times, which restates the claim rather than demonstrating it —
    nothing in it crosses a transition. It is kept deliberately: it pins that a
    boundary inside either transition hour is ACCEPTED and reads back
    correctly, which is a narrower and cheaper question than the one below, and
    it needs no tzdb so it still runs where these tests skip.

    NOT "it pins the shipped default schedule", which this paragraph said from
    2026-08-12 until later the same day, when the review of 094eac0 caught it —
    a retraction of a false coverage claim that shipped a new one, which is the
    sixth time this repo has been bitten by the class and the second time
    inside a fix for it. The older test parses two ad-hoc schedules out of
    GARDYN_LIGHT_SCHEDULE strings ("02:30=50,12:00=100" and
    "01:30=50,12:00=100") and reads phase_at off them; its body makes no code
    reference to ls.DEFAULT_SCHEDULE, and asserts nothing about the shipped
    default's 03:00/04:00/18:00/19:00 boundaries. Only the enclosing CLASS is
    about those. (The body does contain the string once, in a comment
    describing the assertion it replaced — which is the reading that produced
    the wrong sentence in the first place.)

    THE REVIEW'S PROPOSED PROOF DOES NOT REPRODUCE, and the correct one is
    different, so record both. It said "renaming DEFAULT_SCHEDULE errors two
    sibling tests and leaves this one green". Measured: deleting the name
    reddens ALL THREE, because parse_schedule reads
    DEFAULT_SCHEDULE.unsynced_fallback (light_schedule.py:389) whenever
    GARDYN_LIGHT_UNSYNCED_FALLBACK is absent, so the test dies on a NameError
    from a route that has nothing to do with what it asserts — a broad death
    scored as a kill, which is the shape this repo already has a rule about.
    The probe that answers the actual question is to mutate the shipped
    default's BOUNDARIES: replacing them with 05:00/06:00/17:00/20:00 reddens
    exactly test_it_matches_the_photoperiod_this_garden_actually_runs and
    leaves this test green. That is the measurement behind the sentence above.

    What makes the naive arithmetic safe is that decide() asks what phase it is
    NOW instead of reacting to boundary edges. That is a claim about a sequence
    of clock readings, so the only honest way to test it is to generate the
    sequence the Pi's clock actually produces — walk real UTC instants across
    the transition and read each one off as naive local time, exactly as
    datetime.now() would on the host.
    """

    @classmethod
    def setUpClass(cls):
        from zoneinfo import ZoneInfo

        cls.tz = ZoneInfo(_DENVER)

    def _wall_clock_readings(self, start_utc, count, step_minutes):
        """The naive local times a host's clock shows over a real UTC span."""
        return [
            (start_utc + timedelta(minutes=step_minutes * i))
            .astimezone(self.tz)
            .replace(tzinfo=None)
            for i in range(count)
        ]

    def test_a_phase_opening_in_the_skipped_hour_is_never_entered_but_lands_on_the_next_tick(self):
        # A boundary at 02:30 on spring-forward morning. The clock reads
        # 01:55 then 03:00; 02:30 never happens.
        schedule = ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": "02:30=50,12:00=100"})
        readings = self._wall_clock_readings(
            datetime(2026, 3, 8, 8, 45, tzinfo=timezone.utc), 8, 5
        )

        # The gap is real in this sample: the clock jumps an hour.
        self.assertIn(datetime(2026, 3, 8, 1, 55), readings)
        self.assertIn(datetime(2026, 3, 8, 3, 0), readings)
        self.assertFalse(
            [r for r in readings if r.hour == 2],
            "the sample never enters the skipped hour, so it can demonstrate the gap",
        )

        decisions = [ls.decide(schedule, r, clock_synced=True) for r in readings]

        # Before the jump the lamp is on the previous day's last phase (100,
        # wrapped from 12:00); the 02:30 phase is never applied AT 02:30
        # because that time does not occur.
        before = [d for r, d in zip(readings, decisions) if r.hour == 1]
        self.assertTrue(before)
        self.assertEqual({d.brightness for d in before}, {100})

        # THE SELF-CORRECTION: the very first tick after the jump already
        # reports the 02:30 phase, without anything having fired at 02:30.
        first_after = next(d for r, d in zip(readings, decisions) if r.hour >= 3)
        self.assertEqual(first_after.brightness, 50)
        self.assertEqual(first_after.source, ls.SOURCE_SCHEDULE)

    def test_a_phase_opening_in_the_repeated_hour_is_entered_twice_with_the_same_result(self):
        # A boundary at 01:30 on fall-back morning. The clock reads 01:30
        # twice, an hour apart in real time.
        schedule = ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": "01:30=50,12:00=100"})
        readings = self._wall_clock_readings(
            datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc), 13, 10
        )

        # The fold is real in this sample: one wall-clock time, twice.
        self.assertEqual(readings.count(datetime(2026, 11, 1, 1, 30)), 2)

        decisions = [ls.decide(schedule, r, clock_synced=True) for r in readings]
        at_0130 = [
            d for r, d in zip(readings, decisions) if r == datetime(2026, 11, 1, 1, 30)
        ]

        # Entered twice, and the second entry applies the SAME brightness from
        # the SAME source — so the repeated hour costs a duplicate decision,
        # never a flap.
        self.assertEqual(len(at_0130), 2)
        self.assertEqual(at_0130[0], at_0130[1])
        self.assertEqual(at_0130[0], ls.Decision(50, ls.SOURCE_SCHEDULE))

    def test_an_overrides_expiry_runs_an_extra_REAL_hour_across_the_fold(self):
        # The one cost the docstring admits to — "an override's expiry can
        # land an hour early or late across a transition" — MEASURED, not
        # restated. The measurement has to be in real elapsed time, because in
        # wall-clock terms nothing is wrong at all: that is the whole point.
        schedule = ls.parse_schedule({"GARDYN_LIGHT_SCHEDULE": "01:30=50,12:00=100"})

        applied_at = datetime(2026, 11, 1, 1, 45)  # first pass through 01:45
        override = ls.Override(brightness=7, applied_at=applied_at)
        expires_at = ls.next_boundary_after(schedule, applied_at)
        self.assertEqual(expires_at, datetime(2026, 11, 1, 12, 0))

        # Still the override's lamp a wall-clock minute before expiry, and the
        # schedule's the moment it lands. (Without this pair the durations
        # below would be arithmetic about a boundary nothing honours.)
        self.assertEqual(
            ls.decide(schedule, expires_at - timedelta(minutes=1),
                      clock_synced=True, override=override),
            ls.Decision(7, ls.SOURCE_OVERRIDE),
        )
        self.assertEqual(
            ls.decide(schedule, expires_at, clock_synced=True, override=override),
            ls.Decision(100, ls.SOURCE_SCHEDULE),  # 12:00 opens the 100 phase
        )

        # By the wall clock the override held for 10h15m...
        self.assertEqual(expires_at - applied_at, timedelta(hours=10, minutes=15))

        # ...and in real time for an hour longer, because the clock read
        # 01:00-01:59 twice on the way. fold=0 picks the FIRST pass through
        # 01:45, which is the one a host actually stamps when the override
        # arrives; 12:00 is unambiguous so its fold does not matter.
        real_applied = applied_at.replace(tzinfo=self.tz, fold=0).astimezone(timezone.utc)
        real_expires = expires_at.replace(tzinfo=self.tz).astimezone(timezone.utc)
        self.assertEqual(real_expires - real_applied, timedelta(hours=11, minutes=15))

        # Stated as the delta, so this fails if the fold ever stops costing
        # exactly one hour here.
        self.assertEqual(
            (real_expires - real_applied) - (expires_at - applied_at),
            timedelta(hours=1),
        )


class PurityTests(unittest.TestCase):
    """The module's one architectural promise, asserted behaviourally.

    THE CONTROL HERE HAD TO BE REBUILT, and the reason is worth keeping. The
    first version proved the subprocess machinery worked by importing
    xml.etree — which says nothing about FORBIDDEN. A mutant emptying that set
    made the real test vacuous and survived the battery, because the control
    could not tell an empty set from a satisfied one. The control below runs
    the SAME predicate against a module that provably does pull a forbidden
    name in, so an empty FORBIDDEN reddens it.
    """

    # The names whose absence is the promise: hardware drivers, the MQTT
    # client, the Flask app package, and this repo's own config module (which
    # calls load_dotenv at import). Any of them arriving means light_schedule
    # can no longer be exercised off the Pi.
    FORBIDDEN = frozenset(
        {"gpiozero", "pigpio", "paho", "flask", "dotenv", "mqtt", "app", "config"}
    )

    def _forbidden_names_pulled_by(self, statement, extra_path=None):
        """Import `statement` in a clean interpreter; return the FORBIDDEN hits.

        A source scan for `import` lines would miss a transitive pull. This
        cannot: it diffs sys.modules across the import in a fresh process.
        """
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
        hits, pulled = self._forbidden_names_pulled_by("import light_schedule")
        # The subject assertion comes FIRST and is not decoration. Without it,
        # redirecting the probe at any innocuous module leaves this test green
        # while measuring nothing - a mutant swapping the statement for
        # `import json` survived, and neither the returncode check nor the
        # control below could see it, because the control imports something
        # else on purpose.
        self.assertIn(
            "light_schedule",
            pulled,
            "the probe did not import light_schedule at all, so its verdict "
            f"is about some other module. Pulled: {sorted(pulled)}",
        )
        self.assertEqual(
            hits,
            set(),
            f"light_schedule pulled in hardware or app modules: {sorted(pulled)}",
        )

    def test_the_check_above_can_actually_fire(self):
        # Same helper, same FORBIDDEN set, against a module that IS one of the
        # forbidden names. Shadowing `pigpio` on a temp path rather than
        # importing the real driver keeps this runnable on a laptop — and the
        # point being proved is that the predicate fires, not that a Pi is
        # attached.
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "pigpio.py"), "w") as fh:
                fh.write("# stand-in for the real driver, for this control only\n")
            hits, pulled = self._forbidden_names_pulled_by(
                "import pigpio", extra_path=tmp
            )
        self.assertIn(
            "pigpio",
            hits,
            "the purity check cannot detect a forbidden import, so the test "
            f"above proves nothing. Pulled: {sorted(pulled)}",
        )

    def test_the_forbidden_set_names_the_things_that_would_break_the_promise(self):
        # Literals, not a derivation. Emptying or narrowing FORBIDDEN is the
        # quiet way to make the purity test pass forever.
        # `dotenv` was missing from this list while being present in FORBIDDEN,
        # so dropping it survived the battery — the one-name-narrower gap the
        # comment above warns about, sitting in the test written to close it.
        # It is also the transitive tell for config.py, which calls load_dotenv
        # at import, so it is the least safe name to lose.
        for name in ("gpiozero", "pigpio", "paho", "flask", "dotenv", "mqtt", "app", "config"):
            self.assertIn(name, self.FORBIDDEN)
        self.assertEqual(len(self.FORBIDDEN), 8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
