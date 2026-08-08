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
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, time

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
