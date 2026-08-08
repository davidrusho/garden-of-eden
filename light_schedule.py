"""Pure decision logic for the grow light's daily photoperiod (T-527.4).

NO I/O AND NO IMPORTS OUTSIDE THE STANDARD LIBRARY, deliberately. This module
is the only part of the local-schedule work that can be exercised off the Pi,
and the Pi is a host with no console, no keyboard, no SD removal and no
reimage — so every rule that can be decided here instead of on the target is
one fewer rule proven only in production. Importing `mqtt` or anything under
`app/` would drag in gpiozero, pigpio and Flask and destroy that property.

WHAT THIS ANSWERS: given a schedule table, a wall-clock reading, whether that
clock is trustworthy, the last brightness actually applied, and any manual
override in force — what brightness should the lamp be at right now?

WHAT IT DOES NOT DO: read the config file, talk to the light, publish MQTT, or
ask systemd about NTP. Those are T-527.5's job and they are I/O. The seam is
deliberate: everything below is a function of its arguments, so a test can put
the clock anywhere without a fixture.

PHASE COMPUTATION, NOT EDGE TRIGGERING. `decide()` answers "what phase is it
now", never "did a boundary just pass". A scheduler that fires on edges is
correct only if it was running at the edge, so a restart at 14:00 would hold
whatever the lamp happened to be at until 18:00. Computing the phase means a
process that has just started lands on the right brightness on its first tick.
This is why there is no state machine over boundaries here and no notion of a
missed event: there is nothing to miss.

THE POLICY ON BAD CONFIG IS THE OPPOSITE OF gardyn-netwatch's, ON PURPOSE.
`bin/gardyn-netwatch.py` refuses to run on any incomplete config, and its
template says so at length — because a watchdog aimed at the wrong host reboots
this Pi for no reason, and not running is strictly safer than running wrong.
Invert both halves here. A grow light that refuses to run is a DARK GARDEN,
which is the exact failure T-527 exists to remove; and a photoperiod is not
site-specific the way somebody's LAN addressing is, so a built-in default is
safe to ship in a public repo. `parse_schedule()` therefore raises on bad
input and the caller is expected to log loudly and fall back to
`DEFAULT_SCHEDULE`, never to exit. Do not "fix" this into a refusal to match
netwatch.
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from typing import NamedTuple, Optional, Sequence

# Where the decision came from. Published so HA (and a human reading the log)
# can tell which writer owns the lamp — "the light is at 100%" is not the same
# statement as "the light is at 100% because the schedule says so".
SOURCE_SCHEDULE = "schedule"
SOURCE_OVERRIDE = "override"
SOURCE_HOLD = "hold_unsynced"
SOURCE_FALLBACK = "fallback_unsynced"

MIN_BRIGHTNESS = 0
MAX_BRIGHTNESS = 100

# Config keys, read from a systemd EnvironmentFile-shaped file under
# /etc/gardyn/. Same shape as GARDYN_NETWATCH_* — this repo has no YAML parser
# in the runtime path and adding a dependency to a host with no recovery path
# was rejected in the T-527 design.
KEY_SCHEDULE = "GARDYN_LIGHT_SCHEDULE"
KEY_UNSYNCED_FALLBACK = "GARDYN_LIGHT_UNSYNCED_FALLBACK"

# "HH:MM=BB", comma-separated. Anchored, and \d{2} rather than \d+, so "3:00"
# and "003:00" are refused rather than silently accepted as three o'clock —
# a schedule is edited by hand under sudoedit and a typo must be loud.
_ENTRY_RE = re.compile(r"^(\d{2}):(\d{2})=(\d{1,3})$")


class ScheduleConfigError(ValueError):
    """A schedule string could not be parsed into an unambiguous table.

    ValueError rather than a bare Exception so a caller that wants to catch
    this specifically can, and a caller that catches ValueError broadly still
    does. See the module docstring: the correct response is a logged fall back
    to DEFAULT_SCHEDULE, never an exit.
    """


class Boundary(NamedTuple):
    """One transition: from `at` onward, the lamp sits at `brightness`."""

    at: time
    brightness: int


class Schedule(NamedTuple):
    """A full day's photoperiod plus the unsynced-clock fallback.

    `boundaries` is sorted ascending and non-empty; both are enforced by
    `parse_schedule` and by `Schedule.of`, so nothing downstream re-checks.
    """

    boundaries: tuple
    unsynced_fallback: int

    @staticmethod
    def of(pairs: Sequence, unsynced_fallback: int) -> "Schedule":
        """Build a validated Schedule from (time, brightness) pairs.

        The single construction path. Kept separate from parse_schedule so a
        test — or DEFAULT_SCHEDULE below — can build one without going through
        string formatting, while still getting every invariant checked.
        """
        if not pairs:
            raise ScheduleConfigError("a schedule needs at least one boundary")
        seen = set()
        built = []
        for at, brightness in pairs:
            if not isinstance(at, time):
                raise ScheduleConfigError(f"boundary time is not a time: {at!r}")
            if at.second or at.microsecond:
                # Sub-minute boundaries would be unreachable at any sane tick
                # cadence, so accepting one would silently do nothing.
                raise ScheduleConfigError(f"boundary {at!r} is finer than a minute")
            if at in seen:
                raise ScheduleConfigError(
                    f"two brightnesses at {at.strftime('%H:%M')} - which one wins is undefined"
                )
            seen.add(at)
            built.append(Boundary(at, _validated_brightness(brightness, "boundary brightness")))
        built.sort(key=lambda b: b.at)
        return Schedule(
            tuple(built),
            _validated_brightness(unsynced_fallback, KEY_UNSYNCED_FALLBACK),
        )


class Override(NamedTuple):
    """A manual command that has taken the lamp off schedule.

    `applied_at` is a wall-clock reading taken from the SAME clock `decide()`
    is later given. That matters more than it looks: on an unsynced boot the
    reading is wrong in absolute terms, but it is wrong consistently with
    `now`, so "has the next boundary passed" stays answerable. When NTP then
    steps the clock forward, the override's expiry lands in the past and the
    scheduler retakes the lamp — which is the behaviour we want anyway, since
    a clock correction is exactly when a schedule becomes trustworthy again.

    Overrides are deliberately NOT persisted across a restart of the owning
    process. T-527.5's acceptance is that a restart lands on the correct
    scheduled brightness immediately; an override that survived a restart
    would contradict that, and a crash loop would then pin the lamp off
    schedule with nothing able to clear it.
    """

    brightness: int
    applied_at: datetime


class Decision(NamedTuple):
    """What the lamp should be at, and which writer decided it."""

    brightness: int
    source: str


def _validated_brightness(value, what: str) -> int:
    """Coerce to an int in 0..100 or raise. Used only at CONFIG time."""
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ScheduleConfigError(f"{what} is not a whole number: {value!r}") from None
    if not MIN_BRIGHTNESS <= number <= MAX_BRIGHTNESS:
        raise ScheduleConfigError(
            f"{what} must be {MIN_BRIGHTNESS}-{MAX_BRIGHTNESS}, got {number}"
        )
    return number


def _clamped(value) -> int:
    """Force any value into 0..100, at DECISION time. Never raises.

    Separate from _validated_brightness because the two guard different
    threats and must behave differently. Config is read once by a human and a
    bad value there should be loud. A decision-time value can arrive from
    `gardyn/light/brightness/set`, which any broker client can publish, or
    from a persisted last-applied file that was truncated by a power cut — and
    `Light.set_duty_cycle` raises ValueError outside 0..100.

    That raise is the whole reason this exists. The scheduler runs on its own
    thread; an exception there kills the THREAD, not the process, so systemd's
    Restart=always never fires and nothing looks wrong. The lamp simply stops
    following its schedule, silently, until somebody notices the garden is
    dark. Clamping is the boring outcome and the boring outcome is correct.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return MIN_BRIGHTNESS
    return max(MIN_BRIGHTNESS, min(MAX_BRIGHTNESS, number))


# The photoperiod this garden has actually been running, read live from
# automation.gardyn_grow_light_schedule on 2026-08-08: 03:00 50%, 04:00 100%,
# 18:00 50%, 19:00 off. Shipped as the fallback for an unreadable or malformed
# config file, per the module docstring.
#
# DEFINED HERE, BELOW THE HELPERS, AND NOT UP WITH THE CLASSES. Schedule.of()
# calls _validated_brightness() at CALL time, and this line calls it at IMPORT
# time — so a definition placed with the other module constants raises
# NameError on import. On this host that is not a cosmetic ordering question:
# mqtt.service carries Restart=always with StartLimitIntervalSec=0, so an
# import-time exception is a permanent 10-second crash loop that takes the
# grow light down with it, on a Pi with no console.
DEFAULT_SCHEDULE = Schedule.of(
    (
        (time(3, 0), 50),
        (time(4, 0), 100),
        (time(18, 0), 50),
        (time(19, 0), 0),
    ),
    unsynced_fallback=100,
)


def parse_schedule(env: dict) -> Schedule:
    """Build a Schedule from an already-parsed KEY=value mapping.

    Takes the mapping, not a path or a file body, so this stays pure — reading
    /etc/gardyn/ and splitting KEY=value lines is the caller's job, and
    bin/gardyn-netwatch.py's parse_env() is the shape to copy for it.

    Format: GARDYN_LIGHT_SCHEDULE=HH:MM=BB,HH:MM=BB,...
    A missing GARDYN_LIGHT_UNSYNCED_FALLBACK inherits DEFAULT_SCHEDULE's,
    because it is a safety floor rather than a site-specific fact; a missing
    GARDYN_LIGHT_SCHEDULE raises, because an empty photoperiod is not a
    photoperiod.
    """
    raw = (env.get(KEY_SCHEDULE) or "").strip()
    if not raw:
        raise ScheduleConfigError(f"{KEY_SCHEDULE} is missing or empty")

    pairs = []
    for chunk in raw.split(","):
        entry = chunk.strip()
        if not entry:
            # A trailing comma is a typo, not a boundary. Skipping it silently
            # would make "03:00=50," and "03:00=50" produce identical output,
            # which is the kind of leniency that hides a truncated edit.
            raise ScheduleConfigError(f"{KEY_SCHEDULE} has an empty entry")
        match = _ENTRY_RE.match(entry)
        if not match:
            raise ScheduleConfigError(
                f"{KEY_SCHEDULE} entry {entry!r} is not HH:MM=BRIGHTNESS"
            )
        hour, minute, brightness = (int(g) for g in match.groups())
        if hour > 23 or minute > 59:
            raise ScheduleConfigError(f"{KEY_SCHEDULE} entry {entry!r} is not a valid time")
        pairs.append((time(hour, minute), brightness))

    fallback = env.get(KEY_UNSYNCED_FALLBACK)
    if fallback is None or str(fallback).strip() == "":
        fallback = DEFAULT_SCHEDULE.unsynced_fallback
    return Schedule.of(pairs, fallback)


def phase_at(schedule: Schedule, when: time) -> int:
    """The brightness the schedule calls for at `when`.

    THE WRAP IS THE WHOLE FUNCTION. A time before the day's first boundary is
    not unscheduled — it is still inside the phase that opened at the LAST
    boundary of the previous day. With the shipped table, 01:30 falls under
    the 19:00 boundary and so is off; reading it as "no boundary applies" and
    returning 0 would give the right answer for the wrong reason and the wrong
    answer the moment somebody schedules a night phase above zero.
    """
    active = None
    for boundary in schedule.boundaries:
        if boundary.at <= when:
            active = boundary
        else:
            break  # boundaries are sorted, so nothing later can match
    if active is None:
        active = schedule.boundaries[-1]
    return active.brightness


def next_boundary_after(schedule: Schedule, when: datetime) -> datetime:
    """The first boundary strictly later than `when`, as a datetime.

    STRICTLY later, so an override applied at exactly 19:00:00 is held until
    03:00 rather than expiring on the boundary it was applied at. That is the
    accepted cost recorded in the T-527 design — an override late in the
    evening owns the lamp until the next morning, because 19:00 is the last
    boundary of the day.

    Naive local time throughout, and that is a deliberate limit rather than an
    oversight: APScheduler was rejected in the design specifically because DST
    handling is not needed here. What makes it not needed is that US DST
    transitions happen at 02:00 local, and none of the shipped boundaries
    (03:00, 04:00, 18:00, 19:00) lands in the hour that is skipped or repeated.
    A boundary added between 02:00 and 03:00 would break that, so it is a
    constraint on future edits, not a property of the code.
    """
    today = datetime.combine(when.date(), time(0, 0))
    later = [
        candidate
        for candidate in (today.replace(hour=b.at.hour, minute=b.at.minute) for b in schedule.boundaries)
        if candidate > when
    ]
    if later:
        return min(later)
    first = schedule.boundaries[0].at
    tomorrow: date = when.date() + timedelta(days=1)
    return datetime.combine(tomorrow, time(first.hour, first.minute))


def override_is_live(schedule: Schedule, override: Optional[Override], now: datetime) -> bool:
    """Does `override` still own the lamp at `now`?

    True until the first scheduled boundary after the override was applied.
    Note what is NOT here: any ceiling on how long an override may hold. A TTL
    was considered in the design and rejected as more state to test than the
    case justifies; if the 19:05-until-03:00 window proves annoying in
    practice, a TTL is an additive change on top of this.
    """
    if override is None:
        return False
    return now < next_boundary_after(schedule, override.applied_at)


def decide(
    schedule: Schedule,
    now: datetime,
    clock_synced: bool,
    last_applied: Optional[int] = None,
    override: Optional[Override] = None,
) -> Decision:
    """The single entry point: what should the lamp be at, and who decided.

    THE ORDER OF THE THREE GATES IS LOAD-BEARING.

    1. A live override wins, INCLUDING while the clock is unsynced. A person
       who has just published a brightness is better evidence about what the
       lamp should be doing than a schedule read against a clock we have
       already admitted is wrong. Note this makes an override applied during
       an unsynced boot expire when NTP steps the clock — see Override.

    2. An unsynced clock holds the last brightness actually applied. It does
       NOT drive to any scheduled value, because the scheduled value is
       computed from the clock under suspicion. Holding needs `last_applied`
       to have been persisted across the restart, which is why T-527.5 writes
       it to disk.

    3. Otherwise the schedule decides.

    The unsynced branch's fallback — no persisted brightness at all, i.e. a
    genuine first boot with no network — is the one case with no good answer,
    and it is resolved by asking which mistake is cheaper. Driving to the
    configured fallback can leave the lamp lit at the wrong time of day. Doing
    nothing leaves the garden dark for the whole outage. The T-527 design
    chose the first explicitly: refusing to drive "would degrade INTO the
    failure mode this work exists to avoid rather than away from it". The
    window is small in practice — NTP syncs within seconds of the network
    returning, and the next tick corrects it.

    Every returned brightness goes through _clamped(), so a corrupt persisted
    value or a hostile MQTT publish cannot reach Light.set_duty_cycle() with
    something it will raise on. Read _clamped's docstring for why a raise here
    would be silent rather than loud.
    """
    # `override is not None` is redundant with override_is_live()'s own None
    # check and is kept anyway: it is what makes the attribute access on the
    # next line provably safe to a reader and to a type checker, rather than
    # safe by a fact held in another function.
    if override is not None and override_is_live(schedule, override, now):
        return Decision(_clamped(override.brightness), SOURCE_OVERRIDE)

    if not clock_synced:
        if last_applied is None:
            return Decision(_clamped(schedule.unsynced_fallback), SOURCE_FALLBACK)
        return Decision(_clamped(last_applied), SOURCE_HOLD)

    return Decision(_clamped(phase_at(schedule, now.time())), SOURCE_SCHEDULE)
