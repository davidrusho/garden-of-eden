"""The I/O half of the local grow-light schedule (T-527.5).

`light_schedule.py` decides what the lamp should be at. This module is
everything that decision needs from the outside world and nothing else: read
the config file, ask the kernel whether the clock is trustworthy, remember the
last brightness actually applied, drive the lamp, and publish the result. The
seam between the two is deliberate and is described at length in
light_schedule.py's docstring — do not move logic across it.

STILL STANDARD-LIBRARY ONLY, and still no import of `mqtt`, `app.*`, paho or
gpiozero. The lamp arrives as a constructor argument. That is what keeps this
file testable on a laptop, which matters because the target is a Pi with no
console, no keyboard, no SD removal and no reimage: every rule proven here is
one fewer rule proven only in production. tests/test_light_scheduler.py pins
the property the same way tests/test_light_schedule.py does for its module.

WHAT RUNS IT. A daemon thread inside the existing mqtt.service process, started
from mqtt.py's __main__ BEFORE the broker connection and independent of it.
That is the whole point of T-527: the photoperiod must survive Home Assistant
being down, the broker being down, and this Pi being off the network. Nothing
in the tick path talks to the broker except the optional state publish, and
that is best-effort.

WHY EVERY FAILURE HERE FALLS BACK RATHER THAN RAISING. An exception on a
non-main thread kills the THREAD and leaves the process alive with exit status
0, so systemd's Restart=always never fires and the lamp simply stops following
its schedule with nothing to notice. `run_forever` therefore wraps each tick,
and every read below returns a value plus a note instead of raising. The note
is logged; the tick continues. See `_clamped`'s docstring in light_schedule.py
for the measured version of this argument.
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
from datetime import datetime
from time import monotonic, sleep

from light_schedule import (
    DEFAULT_SCHEDULE,
    MAX_BRIGHTNESS,
    MIN_BRIGHTNESS,
    Override,
    ScheduleConfigError,
    decide,
    override_is_live,
    parse_schedule,
)

logger = logging.getLogger(__name__)
# Module-owned policy, set at import, for the same reason app/sensors/light/
# light.py does it: mqtt.py pins the root logger at WARNING, so a level set
# anywhere but here would depend on import ordering. Four scheduled transitions
# a day is exactly the signal the log exists to carry.
logger.setLevel(logging.INFO)

# Read every tick. Absent or malformed is NOT fatal — see load_schedule.
CONFIG_PATH = "/etc/gardyn/light.env"

# Where the last applied brightness is remembered across a restart. systemd
# creates this directory for us and exports $STATE_DIRECTORY (StateDirectory=
# gardyn in mqtt.service); the literal below is what a hand-run process gets.
STATE_DIR_FALLBACK = "/var/lib/gardyn"
STATE_FILENAME = "light-phase"

# Seconds between ticks, and therefore the worst-case lateness at a boundary.
#
# Chosen against a MEASURED cost rather than a round number. Each tick spawns
# one `timedatectl`, and on this Pi Zero 2 W that is 0.169 s median over seven
# runs (min 0.165, max 0.184). That figure is WALL TIME for a fork+exec, not
# CPU time, so the derived "~0.56% of one core" is an upper bound rather than a
# measurement — the real CPU share is smaller by however much of the 0.169 s
# was spent waiting on D-Bus. Either way it sits against a camera path on the
# same host that spawns fswebcam twice per cycle. Half a minute of lateness on
# a photoperiod boundary is not a quantity a plant can measure; the boundary
# latency HA achieved was 4-7 s and nothing depended on it. Going much shorter
# buys nothing and multiplies the only recurring cost this feature has.
TICK_SECONDS = 30

# `timedatectl` talks to systemd-timedated over D-Bus, which is activated on
# demand, so the first call after boot is slower than the steady state. Ten
# seconds is far past anything measured and exists only so a wedged bus cannot
# stall the tick loop forever.
NTP_QUERY_TIMEOUT_SECONDS = 10

# org.freedesktop.timedate1(5): NTPSynchronized "shows whether the kernel
# reports the time as synchronized (c.f. adjtimex(3))". Read from the KERNEL,
# so it stays correct if this host is ever moved from systemd-timesyncd to
# chrony — which is why this is preferred over stat()ing
# /run/systemd/timesync/synchronized, a file systemd-timesyncd(8) documents as
# its own and which chrony would never create. Verified against the man page on
# the host (systemd 252) rather than from recall. (An earlier version of this
# comment also credited systemd.exec(5), which documents StateDirectory= below
# but says nothing about NTPSynchronized.)
NTP_QUERY = ("timedatectl", "show", "--property=NTPSynchronized", "--value")

# The three answers read_clock_state() can give. A tri-state and NOT a bool,
# because the latch below must fire on evidence of a real synchronisation and
# on nothing else — see LightScheduler._clock_verdict. Collapsing "the kernel
# says no" and "the query broke" into one False, or into one True, is what
# makes the latch either useless or dishonest.
CLOCK_SYNCED = "synced"
CLOCK_UNSYNCED = "unsynced"
CLOCK_UNKNOWN = "unknown"

# How long a process that has NEVER seen a synchronised clock holds the last
# applied brightness before following the schedule regardless (T-527.19).
#
# The hold exists for one scenario: a boot with no network, where timesyncd
# restores the clock from /var/lib/systemd/timesync/clock and the time of day
# is wrong by however long the Pi was powered off. Held without a ceiling, a
# persisted 0 keeps the garden dark for the whole outage — which is the failure
# T-527 exists to remove, reached through the mechanism built to prevent it.
# Two hours is long enough for a normal boot to reach the network (netwatch
# reboots the host well inside it) and short enough that the worst case is a
# photoperiod shifted by the outage rather than absent for its duration. A
# shifted photoperiod is something plants tolerate; no photoperiod is not.
NEVER_SYNCED_HOLD_SECONDS = 2 * 60 * 60

# Seconds between heartbeat publishes (T-527.22).
#
# WHY THIS EXISTS AT ALL, since the state topics are already published. Those
# are DEDUPED — _tick_locked publishes only when (brightness, source) changes
# or a drive failed — and that dedupe is deliberate (T-527.20) and is not to be
# touched. The consequence is that Home Assistant receives nothing from a
# healthy, unchanging scheduler, so it cannot tell "nothing has changed" from
# "nothing is deciding". Measured 2026-08-11: sensor.gardyn_light_schedule_source
# had written 5 states in its entire life and its last_reported was 22.2 h old
# against the ping sensor's 24 s in the same render. A scheduler thread that
# dies while mqtt.service keeps its broker connection therefore latches the
# retained override forever, no LWT fires, and both notify-only checks suppress.
#
# So this topic is the one thing in the file that is published UNCONDITIONALLY,
# and its only contract is that it moves while ticks are completing.
#
# NOT every tick. At TICK_SECONDS=30 that is 2,880 publishes a day and 2,880
# recorder rows in Home Assistant, for a signal that only has to beat the
# staleness threshold. Two minutes is 720 a day and still catches a wedged
# thread well inside any threshold worth setting. It is its own constant rather
# than a multiple of TICK_SECONDS so that changing the tick cadence — which is
# tuned against `timedatectl` cost — does not silently change what Home
# Assistant is watching.
HEARTBEAT_SECONDS = 120

# proc(5): the first field is seconds since boot. Read rather than derived from
# monotonic() because the anchor has to survive a RESTART — mqtt.service carries
# Restart=always with RestartSec=10, so a crash loop on a networkless boot would
# otherwise reset a process-local timer every ten seconds and hold the lamp
# forever, which is the exact hole NEVER_SYNCED_HOLD_SECONDS closes.
UPTIME_PATH = "/proc/uptime"


def parse_env(raw):
    """Parse a systemd EnvironmentFile-shaped KEY=VALUE body into a dict.

    The same shape as bin/gardyn-netwatch.py's parse_env(), and COPIED rather
    than shared on purpose. That file is a standalone script under bin/ with a
    hyphen in its name, so importing it means importlib-by-path from a service
    that must not fail at import on a host with no console — a real risk traded
    against twenty lines of duplication. light_schedule.py's parse_schedule()
    docstring already names it as the shape to copy.

    Lenient about SHAPE and strict about CONTENT, again matching netwatch: a
    stray line is skipped here and parse_schedule() is the single place that
    refuses. That keeps every refusal in one place, so there is no path where
    a malformed line yields a partial schedule that still runs.
    """
    out = {}
    if not raw:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def load_schedule(path=CONFIG_PATH, _open=open):
    """Read the config file into a Schedule. Returns (schedule, note).

    `note` is None on success and a one-line explanation otherwise; the caller
    logs it. NOTHING here raises, and the fallback is DEFAULT_SCHEDULE, which
    is the policy light_schedule.py's module docstring sets out at length and
    labels "do not fix this into a refusal": a grow light that refuses to run
    is the dark garden T-527 exists to remove. It is deliberately the OPPOSITE
    of gardyn-netwatch's refuse-on-bad-config stance, because a misconfigured
    watchdog reboots the host while a misconfigured photoperiod is a plausible
    default nobody has to guess.

    THREE FAILURE SHAPES, IN TWO SEPARATE try BLOCKS, AND THE THIRD IS THE ONE
    THAT GETS FORGOTTEN. OSError covers absent, unreadable and a directory in
    the way. ScheduleConfigError covers every content refusal parse_schedule()
    can make. UnicodeDecodeError is neither, and it is what a partially-written
    or power-cut file looks like: it is raised by handle.read(), which is in the
    READ block, so the ScheduleConfigError clause below is not merely a poor
    catch for it — it is in a different statement and could never see it at all.
    Without its own clause it propagates out of load_schedule entirely, aborts
    the tick, and the lamp is not driven that pass. (It does subclass
    ValueError, which is what makes `except ValueError` here look like it would
    do; it would, and it would also swallow content errors under the wrong
    message. Both clauses are named on purpose.)
    """
    try:
        with _open(path) as handle:
            raw = handle.read()
    except OSError as exc:
        return DEFAULT_SCHEDULE, f"cannot read {path} ({exc.strerror or exc}); using the built-in photoperiod"
    except UnicodeDecodeError:
        return DEFAULT_SCHEDULE, f"{path} is not text; using the built-in photoperiod"
    try:
        return parse_schedule(parse_env(raw)), None
    except ScheduleConfigError as exc:
        return DEFAULT_SCHEDULE, f"{path} is not usable ({exc}); using the built-in photoperiod"


def read_clock_state(_run=subprocess.run, timeout=NTP_QUERY_TIMEOUT_SECONDS):
    """What does the kernel say about the clock? Returns (state, note).

    `state` is CLOCK_SYNCED, CLOCK_UNSYNCED or CLOCK_UNKNOWN. THE THIRD ONE IS
    THE POINT OF THIS FUNCTION'S SHAPE, and it used to be missing: an earlier
    version returned a bool and folded "the query broke" into True, which is
    the right DRIVING decision (see _clock_verdict) and the wrong thing to
    remember. The latch in _clock_verdict must fire on evidence that the clock
    really synchronised; a `timedatectl` that cannot be run is evidence about
    `timedatectl`. Keeping the two apart is what stops a permanently broken
    query from latching the gate open on no evidence at all.

    NOTHING HERE DECIDES WHETHER TO DRIVE. This function reports; the policy —
    which mistake is cheaper, and for how long — lives in one place, in
    _clock_verdict, so there is exactly one paragraph to read when it changes.

    WHAT NTPSynchronized ACTUALLY ANSWERS, because the name misleads and it
    cost this ticket a blocked deploy (T-527.19). It is NOT "is the clock
    accurate". systemd v252's src/timedate/timedated.c computes it as
    `txc.maxerror < 16000000` — 16 seconds of accumulated ERROR BOUND — while
    the kernel's second_overflow() (kernel/time/ntp.c) adds MAXFREQ, 500 µs, to
    maxerror EVERY SECOND, unconditionally. NTP does not slow that climb; a
    successful sync RESETS maxerror, via ADJ_MAXERROR. The distinction matters
    because it says where the clock stands the moment the network drops: at
    whatever the last sync left, climbing at a fixed rate from there.

    So a perfectly accurate clock reports `no` after 16,000,000 / 500 =
    32,000 s = 8.89 h FROM A STANDING START — an upper bound, since maxerror is
    rarely exactly 0 at the last sync, so the real trip comes sooner. Confirmed
    on this Pi on 2026-08-09 by a read-only adjtimex(2) probe that read both
    candidate struct timex layouts so the wrong one was its own control:
    exactly 500.0 µs/s. It is a STALENESS reading, not an accuracy one.

    ONE PROPERTY OF THE KERNEL IS WHAT MAKES THE LATCH SOUND, and it is worth
    naming here because the latch looks reckless without it: ntp_clear() sets
    time_maxerror to NTP_PHASE_LIMIT, and clock_settime()/settimeofday() route
    through timekeeping_update() with TK_CLEAR_NTP. So a clock restored from
    /var/lib/systemd/timesync/clock, or set by hand with `date -s`, forces
    NTPSynchronized=no. The latch CANNOT fire on a clock that was restored from
    disk — which is precisely the scenario the gate exists for.
    """
    try:
        proc = _run(list(NTP_QUERY), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return CLOCK_UNKNOWN, f"cannot read NTP sync state ({exc})"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return CLOCK_UNKNOWN, f"cannot read NTP sync state ({detail})"
    answer = (proc.stdout or "").strip()
    if answer == "yes":
        return CLOCK_SYNCED, None
    if answer == "no":
        return CLOCK_UNSYNCED, None
    # A third answer is a contract change in systemd, not a clock fact.
    return CLOCK_UNKNOWN, f"NTPSynchronized answered {answer!r}, which is neither yes nor no"


def seconds_since_boot(path=UPTIME_PATH, _open=open):
    """Seconds since the kernel booted, or None if that cannot be read.

    None rather than a raise, and None rather than 0, for the usual reason in
    this file: the caller runs on a non-main thread where a raise is silent,
    and a 0 here would read as "the host just booted" and extend the hold that
    NEVER_SYNCED_HOLD_SECONDS exists to bound. The caller substitutes its own
    process age instead and says so.

    Not available off Linux, which is why the caller has a fallback at all —
    every test in tests/test_light_scheduler.py runs on a laptop.
    """
    try:
        with _open(path) as handle:
            raw = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        return float(raw.split()[0])
    except (IndexError, ValueError):
        return None


def default_state_path(environ=os.environ):
    """Where to persist the last applied brightness.

    $STATE_DIRECTORY is set by systemd from StateDirectory= in mqtt.service and
    is documented (systemd.exec(5)) as colon-separated when several are named,
    hence the split — a path containing a literal colon would be a systemd
    contract change, not a case to handle here. The fallback literal is what a
    process run by hand from a shell gets.
    """
    first = (environ.get("STATE_DIRECTORY") or "").split(":")[0].strip()
    return os.path.join(first or STATE_DIR_FALLBACK, STATE_FILENAME)


def read_last_applied(path):
    """The persisted brightness, or None if there isn't a trustworthy one.

    None AND NOT ZERO FOR A CORRUPT FILE, which is the whole reason this does
    not simply reuse `_clamped`. `_clamped` maps junk to 0, and 0 is a lamp
    that is off; decide() would then HOLD the garden dark through an unsynced
    boot on the strength of a truncated file. None routes to the configured
    unsynced fallback instead, which is lit. An out-of-range integer counts as
    corrupt for the same reason: nothing here ever writes one, so seeing one
    means the file is not what we wrote.
    """
    try:
        with open(path) as handle:
            raw = handle.read()
    except (OSError, UnicodeDecodeError):
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    if not MIN_BRIGHTNESS <= value <= MAX_BRIGHTNESS:
        return None
    return value


def write_last_applied(path, brightness):
    """Persist the brightness atomically. Returns a note on failure, else None.

    Temp file plus os.replace, because the reader is a process starting up
    after an unclean shutdown — exactly the case where a half-written file is
    plausible. os.replace() is atomic within a filesystem, and the temp file is
    a sibling so that holds.

    A failure here degrades ONE thing: the unsynced branch loses its hold value
    and falls back to the configured brightness. It is not worth interrupting a
    tick over, so it is a note rather than a raise.
    """
    tmp = path + ".tmp"
    try:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(tmp, "w") as handle:
            handle.write(f"{brightness}\n")
        os.replace(tmp, path)
    except OSError as exc:
        # Best-effort cleanup; a stranded .tmp is harmless but untidy, and a
        # second failure here must not mask the first.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return f"cannot persist the applied brightness to {path} ({exc.strerror or exc})"
    return None


class LightScheduler:
    """Applies light_schedule's decision to a real lamp, on a timer.

    Constructed with the lamp rather than importing it, so every test below
    runs off the Pi. `light` needs `set_duty_cycle(int)` and `get_brightness()`
    — the surface app/sensors/light/light.py already exposes.

    `publish_state` is called with the Decision whenever the pair
    (brightness, source) CHANGES — not merely when the lamp moves, and that is
    the T-527.6 half. "The light is at 100%" and "the light is at 100% because
    the schedule says so" are different statements, and the second is what
    tells a human whether an override is still holding. So an override that
    happens to match the schedule's brightness, and its later expiry, are both
    published even though the pin never moves.

    The other direction still holds: the state topics are retained, so
    republishing an unchanged pair every 30 s would be pure broker traffic. A
    raise from the callable is caught — the broker being unreachable must never
    be able to stop the photoperiod.
    """

    def __init__(
        self,
        light,
        publish_state=None,
        *,
        publish_heartbeat=None,
        config_path=CONFIG_PATH,
        state_path=None,
        tick_seconds=TICK_SECONDS,
        heartbeat_seconds=HEARTBEAT_SECONDS,
        now=datetime.now,
        clock_probe=read_clock_state,
        uptime=seconds_since_boot,
        never_synced_hold_seconds=NEVER_SYNCED_HOLD_SECONDS,
        monotonic_clock=monotonic,
        heartbeat_clock=monotonic,
        sleeper=sleep,
    ):
        self._light = light
        self._publish_state = publish_state
        self._publish_heartbeat = publish_heartbeat
        self._config_path = config_path
        self._state_path = state_path if state_path is not None else default_state_path()
        self._tick_seconds = tick_seconds
        self._heartbeat_seconds = heartbeat_seconds
        # SEPARATE FROM self._monotonic ON PURPOSE, for the same reason
        # run_forever uses the module-level monotonic(). self._monotonic exists
        # for exactly one job — the never-synced hold's fallback anchor — and
        # tests inject FINITE iterators into it. Driving the heartbeat cadence
        # from the same callable would exhaust them, turning a heartbeat test
        # into a StopIteration in an unrelated branch.
        self._heartbeat_clock = heartbeat_clock
        self._now = now
        self._clock_probe = clock_probe
        self._uptime = uptime
        self._never_synced_hold_seconds = never_synced_hold_seconds
        self._monotonic = monotonic_clock
        self._sleeper = sleeper

        # THE ONE LOCK, and it is held across the whole of tick() rather than
        # around the override slot alone (T-527.20). Everything below is either
        # read inside a tick or written by one, and three separate defects came
        # from letting a second thread interleave with one: a command landing
        # mid-tick was reverted and mis-persisted, a publish_now() racing a tick
        # pinned the owner topic to the wrong writer indefinitely, and a failed
        # pigpio write stranded Home Assistant's retained copy permanently.
        #
        # THE COST IS REAL AND IS BOUGHT DELIBERATELY. tick() spawns
        # `timedatectl` (0.169 s median here, NTP_QUERY_TIMEOUT_SECONDS worst
        # case), so an MQTT command or a button press arriving mid-tick waits
        # that long before it is applied. That wait is the FIX, not a side
        # effect: the alternative is the command being applied and then reverted
        # by the tick already in flight, which is what shipped before.
        #
        # There is no RLock and no re-entrancy anywhere. Every public method
        # takes the lock exactly once and calls a `_locked` helper; the helpers
        # never take it. override_now() is the case that would otherwise nest,
        # and it is written as one acquisition covering both halves — which it
        # has to be anyway, or the override could expire between being recorded
        # and being applied.
        #
        # PAHO'S NETWORK THREAD CAN NOW BLOCK ON THIS LOCK, WHICH IT COULD NOT
        # BEFORE, and that was checked against paho 2.0.0's source rather than
        # assumed. `_packet_queue` acquires paho's own `_in_callback_mutex`
        # NON-blocking (`acquire(False)`) and the socket is non-blocking, so
        # there is no lock-order inversion and `client.publish()` under this
        # lock cannot block on the network. The wait is bounded by
        # NTP_QUERY_TIMEOUT_SECONDS plus pigpio round-trips, against a
        # KEEP_ALIVE_INTERVAL of 60 s, so a 10-20 s stall is inside budget.
        #
        # The residual, named rather than hidden: a WEDGED pigpiod. Its socket
        # round-trip has no timeout, so it would pin this lock indefinitely.
        # That exposure is widened here rather than created —
        # announce_to_home_assistant() already calls light.get_brightness() on
        # paho's thread before it reaches publish_now().
        self._lock = threading.Lock()
        self._override = None
        self._stop = threading.Event()
        # Has this PROCESS ever seen a genuinely synchronised clock? Once true,
        # never false again — see _clock_verdict for why that latch is the whole
        # T-527.19 fix. Not persisted: it is a statement about this process's
        # own evidence, and a restart has none.
        self._ever_synced = False
        # Anchor for the never-synced hold when /proc/uptime is unreadable.
        self._started_monotonic = self._monotonic()
        # Did the last _apply() leave the lamp where the decision said? A failed
        # drive must not be recorded as published — see _tick_locked.
        self._last_apply_ok = True
        # Last brightness this scheduler COMMANDED, used only to decide whether
        # a log line is news. Authority over what the lamp is actually at stays
        # with the hardware read in _apply() — this is not a shadow variable
        # standing in for the lamp, and nothing decides anything from it.
        self._last_logged_target = None
        # The last (brightness, source) handed to publish_state, so an
        # unchanged pair is not republished on every tick.
        self._last_published = None
        # The last Decision reached, so the MQTT reconnect path can refresh
        # HA's retained copy without waiting for the next tick.
        self._last_decision = None
        # Notes already reported, keyed by category, so a config file that is
        # missing for a week produces one ERROR rather than 20,160 of them.
        self._reported = {}
        # How many heartbeats this process has sent, and when the last one went.
        # None, not 0, so the FIRST completed tick always publishes one: a
        # restart is exactly when Home Assistant most needs to be told the
        # scheduler is deciding again, and waiting HEARTBEAT_SECONDS for that
        # would leave a window in which a fresh process is indistinguishable
        # from a dead one.
        self._heartbeat_count = 0
        self._last_heartbeat = None
        # Which thread is the scheduler loop, once one is running. None until
        # run_forever starts, and re-stamped on every entry so it cannot go
        # stale. See _heartbeat_locked: this is what stops a FUTURE caller of
        # tick() from another thread claiming the loop is alive, the way
        # override_now() did before the call was moved out of _tick_locked.
        self._scheduler_ident = None

    # ------------------------------------------------------------- override

    def set_override(self, brightness):
        """Record a manual command. Does NOT drive the lamp — see override_now.

        `applied_at` is stamped from the SAME clock decide() is given, which is
        what makes "has the next boundary passed" answerable even while that
        clock is wrong — see Override's docstring in light_schedule.py.
        """
        with self._lock:
            self._set_override_locked(brightness)

    def _set_override_locked(self, brightness):
        self._override = Override(brightness, self._now())

    def override_now(self, brightness):
        """Take the lamp off schedule and move it in the same breath.

        THE POINT IS THAT THERE IS ONE PATH TO THE PIN. mqtt.py's command
        handlers used to call light.set_duty_cycle() and publish_light_state()
        themselves, which was correct while nothing else owned the lamp; with a
        scheduler running it would mean two writers, differing in what they
        persist and what they publish, and a manual command that never reached
        the state file would be silently forgotten by the unsynced-clock hold.
        Routing through tick() means an override is applied, persisted and
        published by exactly the code that does it for the schedule.

        It costs one `timedatectl` on the command path (0.169 s median on this
        Pi), spent on paho's network thread. That is a real cost and it is
        bought deliberately: the alternative is a second, subtly different
        implementation of "put the lamp here", which is the shape this file
        exists to remove. The same thread already spends a synchronous pigpio
        round-trip inside announce_to_home_assistant().

        ONE ACQUISITION COVERS BOTH HALVES (T-527.20). Recording the override
        and applying it used to be two separate lock acquisitions with a tick
        able to run in between, so a command landing inside a tick was applied,
        reverted by that tick, published as `schedule`, and PERSISTED at the
        scheduled brightness — after which a power cut restored the reverted
        value rather than the commanded one. Holding the lock across both means
        the person wins, which is what a person pressing a button expects.
        """
        with self._lock:
            self._set_override_locked(brightness)
            return self._tick_locked()

    def clear_override(self):
        """Hand the lamp back to the schedule immediately."""
        with self._lock:
            self._override = None

    @property
    def override(self):
        with self._lock:
            return self._override

    @property
    def last_decision(self):
        """The most recent Decision, or None before the first tick."""
        with self._lock:
            return self._last_decision

    def publish_now(self):
        """Re-publish the last decision unconditionally.

        For the MQTT reconnect path: a retained message is delivered once per
        subscribe, so when Home Assistant comes back and re-announces, its copy
        of who-owns-the-lamp has to be re-sent or it sits at whatever the broker
        still holds. Unconditional, because the whole point is that the
        SUBSCRIBER changed, not the value.

        UNDER THE LOCK, AND THE PUBLISH IS INSIDE IT (T-527.20). This runs on
        paho's network thread from announce_to_home_assistant() while tick()
        runs on the scheduler thread, and the two used to interleave: this
        method read the decision, wrote _last_published, and then landed its
        publish LAST, after a tick that had already published a newer one. The
        broker's retained copy was left naming the wrong writer — `schedule`
        while a person held the lamp — and the dedupe then suppressed every
        correction until the override expired. The T-527.9 obedience automation
        is specified to condition on exactly that topic.
        """
        with self._lock:
            decision = self._last_decision
            if decision is None:
                return
            self._record_published_locked(decision, self._publish(decision))

    # ----------------------------------------------------------------- tick

    def tick(self):
        """One pass. Returns the Decision, so a test can assert on it.

        THE HEARTBEAT IS EMITTED HERE AND NOT IN _tick_locked, and that
        placement is the whole of its meaning (T-527.22, corrected after
        review). _tick_locked is ALSO reached from override_now(), which
        mqtt.py's apply_light_override() calls on paho's network thread and
        toggle_light() calls from the physical-button thread. Both of those
        threads are alive in exactly the failure this heartbeat detects — a
        dead scheduler thread under a live broker connection — so a beat from
        either would report liveness for the thread that stopped using a thread
        that did not.

        The consequence when it was wired into _tick_locked, confirmed by a
        probe with three gating controls: with the scheduler thread genuinely
        gone, a person tapping the light in Home Assistant emitted a heartbeat
        and reset the staleness alarm for a full threshold. That is the worst
        possible shape — tapping the light is precisely what somebody does on
        noticing the garden is wrong, so the diagnostic action suppressed the
        diagnostic, and repeating it suppressed it indefinitely.

        The guard below is the second half. Moving the call here excludes the
        two callers that exist today; the ident check excludes a future one,
        because "a tick completed" and "the scheduler loop completed a pass"
        are different claims and only the second is worth publishing.

        IT ALSO SITS AFTER _tick_locked RETURNS, so a heartbeat means the pass
        ran to completion rather than that one was attempted. What can raise
        before it: `self._now()`, which is why
        test_a_tick_that_raises_leaves_no_heartbeat injects a raising `now`;
        `decide()`; and `override_is_live()` -> `next_boundary_after()`. NOT
        `load_schedule`, which an earlier version of this comment named — its
        own docstring says "NOTHING here raises" and the two sentences
        contradicted each other. run_forever catches whatever does escape, and
        correctly leaves no heartbeat behind.

        Note "completed" is not "the lamp was driven". `_apply` swallows a
        drive failure on purpose and test_a_failed_drive_still_publishes_a_
        heartbeat pins that: a pigpio drop means the lamp is wrong, not that
        the scheduler stopped deciding, and reporting it as dead would point
        the alarm at the wrong subsystem.
        """
        with self._lock:
            decision = self._tick_locked()
            self._heartbeat_locked()
            return decision

    def _clock_verdict(self, state):
        """Should we drive off the wall clock? Returns (trust_it, note).

        THE LATCH IS THE T-527.19 FIX, and the defect it repairs was in the
        QUESTION, not in the code that asked it. `NTPSynchronized` reports
        whether NTP has checked in recently, not whether the clock is right —
        see read_clock_state — so it flips to `no` about 8.9 h into ANY network
        outage on a clock that is perfectly accurate. decide() then held the
        last applied phase for the rest of the outage, which for a garden that
        tipped over after 19:00 is darkness until the network returns. The
        mechanism built to prevent a dark garden produced one.

        So: once this process has seen a real synchronisation, trust the clock
        from then on. That is sound rather than merely convenient. The gate
        exists for a boot with no network, where timesyncd restores a
        time-of-day from /var/lib/systemd/timesync/clock that is stale by
        however long the Pi was powered off — an error measured in days. A
        clock that DID sync this boot and has since free-run is out by the
        crystal's drift, which is seconds. Those are different quantities and
        only the first is worth holding a lamp for.

        A FAILED QUERY DOES NOT LATCH, and that is the reason read_clock_state
        returns three states instead of a bool. It still drives — the mistakes
        are asymmetric, and believing a good clock costs a plant nothing while
        disbelieving one costs it the photoperiod — but it must not be recorded
        as evidence of a sync, or a permanently broken `timedatectl` would pin
        the gate open on the strength of its own breakage.
        """
        if state == CLOCK_SYNCED:
            if not self._ever_synced:
                self._ever_synced = True
                logger.info("Clock synchronised; the schedule is trusted from here on")
            return True, None
        if self._ever_synced:
            # Latched. Not a note: this is the steady state during any outage
            # longer than ~8.9 h, and it is correct, so it is not news.
            return True, None
        if state == CLOCK_UNKNOWN:
            return True, None

        # Never synchronised in this process's life, and the kernel says so.
        # This is the scenario the gate is for — but it is bounded, because an
        # unbounded hold of a persisted 0 is the dark garden all over again.
        if self._elapsed_since_boot() < self._never_synced_hold_seconds:
            return False, None
        # NO ELAPSED TIME IN THIS STRING, and that is not a style choice.
        # _report dedupes on the message TEXT, so interpolating a value that
        # moves defeats it completely: `{elapsed / 3600:.1f}` changes every six
        # minutes, which measured 246 distinct ERROR lines a day into an
        # unrotated gardyn.log on an SD card — against the "one ERROR rather
        # than 20,160" this design claims. The condition holds for the whole
        # outage, so the message must be constant for the whole outage. The
        # journal's own timestamps say when it started; uptime says how long.
        return True, (
            "the clock has never synchronised and the host has been up longer "
            "than the hold ceiling; following the schedule anyway rather than "
            "holding the lamp indefinitely"
        )

    def _elapsed_since_boot(self):
        """Seconds since boot, falling back to this process's own age.

        THE FALLBACK UNDER-REPORTS ACROSS A RESTART, WHICH IS THE DANGEROUS
        DIRECTION, and an earlier version of this docstring called it the safe
        one — flatly contradicting UPTIME_PATH's comment two hundred lines up,
        which is the one that is right. Under-reporting LENGTHENS the hold, and
        an unbounded hold is the dark garden NEVER_SYNCED_HOLD_SECONDS exists
        to close; ending the hold early only costs a photoperiod at the wrong
        hour, which is this ticket's cheap mistake throughout.

        It is tolerable only because of where it can fire: /proc/uptime is
        always present on the deploy target, so this branch is reachable on the
        laptop the tests run on and essentially nowhere else. If that ever
        stops being true, this is a real hole rather than a convenience.
        """
        value = self._uptime()
        if value is None:
            return self._monotonic() - self._started_monotonic
        return value

    def _tick_locked(self):
        """One pass, with self._lock already held. See __init__ on the lock."""
        schedule, note = load_schedule(self._config_path)
        self._report("config", note)
        state, note = self._clock_probe()
        self._report("ntp", note)
        synced, note = self._clock_verdict(state)
        self._report("clock-hold", note)

        now = self._now()

        override = self._override
        if override is not None and not override_is_live(schedule, override, now):
            self._override = None
            override = None
            logger.info("Schedule override expired; the schedule owns the lamp again")

        decision = decide(
            schedule,
            now,
            synced,
            last_applied=read_last_applied(self._state_path),
            override=override,
        )
        applied = self._apply(decision)
        self._last_decision = decision
        self._last_apply_ok = applied
        # Publish on a change of OWNER as well as of brightness. An override
        # set to the brightness the schedule already wanted moves no pin, and
        # is still the difference between "the schedule is running" and "a
        # person has taken it".
        #
        # AND ALWAYS PUBLISH AFTER A FAILED DRIVE (T-527.20). The dedupe key is
        # the INTENDED pair; the payload is the OBSERVED brightness, because
        # publish_light_decision() re-reads the lamp. So a tick whose
        # set_duty_cycle() raised — a transient pigpio drop, which the suite's
        # own fake already models — used to record the intended pair as
        # published while sending the hardware's stale value, and every later
        # tick then deduped against it. Home Assistant showed the grow light
        # OFF while it was on, until the next boundary or an MQTT reconnect.
        #
        # The accepted cost: a PERMANENTLY failing pigpio now republishes every
        # tick rather than once. That is broker traffic on retained topics, and
        # it is dwarfed by _apply's own logger.exception traceback per tick,
        # which is pre-existing. Suppressing it would reinstate the strand.
        pair = (decision.brightness, decision.source)
        if pair != self._last_published or not applied:
            self._record_published_locked(decision, self._publish(decision))
        return decision

    def _heartbeat_locked(self):
        """Publish the liveness counter if HEARTBEAT_SECONDS have elapsed.

        CALLED ONLY FROM tick(), never from _tick_locked — see tick()'s
        docstring for why, and for what the other placement cost. override_now()
        and publish_now() are the two methods reachable from paho's network
        thread, and neither can reach this.

        THE PAYLOAD CARRIES NO TIME, deliberately. A timestamp payload would be
        read on the Home Assistant side as `now() - as_datetime(state)`, which
        makes the check depend on THIS host's clock — the one thing T-527.19
        established cannot be trusted, since a Pi with no RTC and no network
        restores a time-of-day from /var/lib/systemd/timesync/clock that can be
        days out. A clock running AHEAD would report the heartbeat as
        permanently fresh: a false all-clear on the exact check that exists to
        catch a silent failure.

        STALENESS IS DETECTED BY HOME ASSISTANT'S `expire_after`, NOT by
        arithmetic on `last_changed`. An earlier version of this docstring
        argued a counter was sufficient because `last_changed` moves whenever
        the state string moves. Review showed that reasoning inverted the whole
        feature: `last_changed` is ALSO reset by an availability flap, because
        `unavailable` -> value is itself a state change. Measured on the
        identically-shaped sibling sensor.gardyn_light_schedule_source, whose
        `last_changed` was 26.3 h NEWER than its last real value change after
        two flaps in ten days. The fatal case is mqtt.service in its
        Restart=always / RestartSec=10 crash loop, which this host has already
        lived through: every restart is an `unavailable` -> value round trip, so
        `last_changed` would move every ten seconds while the counter stayed
        frozen and the lamp stayed dark — a permanent all-clear during a
        permanent outage, from the check built to catch silent failure.

        `expire_after`'s RUNTIME timer counts received messages and nothing
        else, so the flap defect cannot reach it while HA stays up. Home
        Assistant's MQTT sensor documentation is explicit that the state payload
        should not be retained when using it — "it is not recommended to retain
        the sensor's state payload at the MQTT broker" — and that HA will
        "store and restore the sensor's state for you and calculate the
        remaining time to retain the sensor's state before it becomes
        unavailable".

        ONE RESIDUE, NAMED BECAUSE AN EARLIER VERSION OF THIS PARAGRAPH SAID
        "and nothing else" FLATLY AND THAT WAS FALSE. The restore path in HA's
        own mqtt/sensor.py computes
        `expiration_at = last_state.last_changed + timedelta(seconds=expire_after)`
        — so it inherits the very field this design moved away from. A flap that
        bumps `last_changed` before an HA restart extends the expiry by that
        much. Bounded at one threshold (a flap AFTER expiry finds the state
        already `unavailable` and the restore is skipped), and it needs a flap,
        a dead scheduler and an HA restart to coincide. Recorded rather than
        engineered around; the runtime path is the one that matters.

        The counter survives the change and still earns its place: it keeps the
        payload free of any clock, and a reset says the process restarted.

        Best-effort like every other publish here. The broker being down is the
        premise of T-527 and must never stop the photoperiod, so a raise is
        logged and swallowed — and the clock is NOT advanced on failure, so the
        next tick retries immediately rather than waiting out the interval.
        """
        if self._publish_heartbeat is None:
            return
        # Once run_forever is running, IT is the only caller entitled to speak
        # for the scheduler's liveness. Before then there is no loop to speak
        # for, so a direct tick() — which is every test in this file — may beat.
        #
        # THIS IS AN IDENTITY CHECK, NOT A LIVENESS CHECK, and the difference is
        # real on this platform: CPython reuses thread idents aggressively —
        # measured, 60 sequential short-lived threads produced ONE distinct
        # ident. The button path arms a threading.Timer per press, which is
        # exactly such a thread. So once the scheduler thread dies its ident is
        # immediately available for reuse, and a future caller of tick() that
        # inherited it would pass this guard. Latent today: tick() has no caller
        # but run_forever. The structural exclusion in tick() is the load-bearing
        # half; this is the backstop, and it is a partial one.
        if (
            self._scheduler_ident is not None
            and threading.get_ident() != self._scheduler_ident
        ):
            return
        stamp = self._heartbeat_clock()
        if (
            self._last_heartbeat is not None
            and stamp - self._last_heartbeat < self._heartbeat_seconds
        ):
            return
        count = self._heartbeat_count + 1
        try:
            self._publish_heartbeat(count)
        except Exception as exc:
            # _report, NOT logger.exception, and that is the difference between
            # one line and a traceback every two minutes for the length of a
            # broker outage, on an unrotated log on an SD card. _report dedupes
            # on the message text and announces the recovery, which is exactly
            # the shape this needs — so the text must stay CONSTANT for the
            # duration of the fault.
            #
            # THE EXCEPTION'S CLASS, NOT ITS MESSAGE. An earlier version
            # interpolated `{exc}`, whose text carries paho's return code — and
            # that code MOVES within a single outage: measured against real paho
            # 2.0.0, killing the socket under a connected client gives
            # MQTT_ERR_CONN_LOST (7) on the first beat and MQTT_ERR_NO_CONN (4)
            # thereafter. Two lines per clean outage, and driving the real
            # scheduler through a flapping link (7,4,7,4,…) defeated the dedupe
            # completely: 8 of 8 beats logged. That is the same shape as the
            # `{elapsed/3600:.1f}` defect this module already paid for.
            #
            # The class name is constant per cause and still discriminates the
            # thing worth discriminating — a RuntimeError from the return-code
            # check versus, say, an AttributeError from a stubbed paho.
            self._report(
                "heartbeat",
                f"cannot publish the schedule heartbeat ({type(exc).__name__})")
            return
        self._report("heartbeat", None)
        self._heartbeat_count = count
        self._last_heartbeat = stamp

    def _record_published_locked(self, decision, published_ok):
        """Remember what was published, and ONLY if it really was.

        Both halves have to hold. A failed DRIVE means the payload carried the
        hardware's stale value rather than the decision's, and a failed PUBLISH
        means nothing left the process at all — either way the broker's retained
        copy does not describe the lamp, so recording the pair would let the
        dedupe suppress every later correction. None forces the next tick to
        republish.
        """
        if self._last_apply_ok and published_ok:
            self._last_published = (decision.brightness, decision.source)
        else:
            self._last_published = None

    def _apply(self, decision):
        """Move the lamp if it is not already where the decision says.

        Returns True if the lamp is now where the decision says, False if the
        drive raised. The caller needs that answer because the state it
        publishes is READ BACK from the hardware, so a failed drive must force
        a republish rather than be recorded as done — see _tick_locked.

        THE COMPARISON IS AGAINST THE HARDWARE, not against what we last sent.
        mqtt.py's toggle_light() and publish_light_state() both read the lamp
        for the same reason: a shadow variable disagrees with the world the
        first time anything else writes to the pin — the physical button, a
        flash_lights() burst, or the MQTT command handlers.

        A residual worth checking on the host at deploy time rather than
        reasoning about here: PWM quantisation means get_brightness() need not
        return exactly what was commanded. If a steady-state 1% disagreement
        exists, this re-writes the pin every tick.

        THAT IS NOT FREE, AND AN EARLIER VERSION OF THIS DOCSTRING SAID IT WAS.
        The rounding below silences THIS module's log line only.
        app/sensors/light/light.py's set_duty_cycle() logs unconditionally on a
        logger it raises to INFO at import, so a steady mismatch writes ~2,880
        lines a day into an unrotated gardyn.log AND rewrites the SD card every
        30 s through write_last_applied(). Neither cost is visible from here.
        One command on the Pi settles whether it happens at all, and it is
        owed BEFORE the deploy, not during it:

            python3 -c "from gpiozero import PWMLED; \\
              from gpiozero.pins.pigpio import PiGPIOFactory; \\
              l=PWMLED(18,pin_factory=PiGPIOFactory()); l.value=0.5; \\
              print(l.value*100)"

        Anything other than 50.0 ± 0.5 means the rewrite loop is real.
        """
        actual = self._read_actual()
        target = decision.brightness
        if actual is not None and actual == target:
            self._last_logged_target = target
            return True

        try:
            self._light.set_duty_cycle(target)
        except Exception:
            logger.exception("Schedule could not drive the light to %s%%", target)
            return False

        if target != self._last_logged_target:
            logger.info(
                "Schedule set the light to %s%% (source=%s)", target, decision.source
            )
        else:
            logger.debug(
                "Schedule re-asserted %s%% (source=%s)", target, decision.source
            )
        self._last_logged_target = target

        note = write_last_applied(self._state_path, target)
        self._report("state", note)
        return True

    def _read_actual(self):
        """The lamp's real duty cycle as a whole percent, or None if unreadable.

        None means "apply anyway": a pigpio round-trip that failed is not
        evidence the lamp is already right.
        """
        try:
            return int(round(self._light.get_brightness()))
        except Exception:
            logger.exception("Schedule could not read the light's current brightness")
            return None

    def _publish(self, decision):
        """Publish the decision. Returns True if it actually went out.

        THE RETURN VALUE IS THE POINT, and its absence was a defect one line
        away from the one f631652 went to real trouble to close. That commit
        stopped a failed DRIVE being recorded as published; this stops a failed
        PUBLISH being recorded as published. Both strand Home Assistant on a
        retained value that no longer describes the lamp, and the dedupe then
        suppresses every later correction.

        It is not hypothetical: publish_light_decision() begins with
        light.get_brightness(), a pigpio round-trip — exactly the call
        _read_actual() exists to wrap because it can raise. One blip there at a
        boundary used to strand HA until the next boundary, up to 8 h.

        The exception is still swallowed. The broker being down is the PREMISE
        of T-527 and must never stop the photoperiod; what changes is that we
        no longer remember a publish that did not happen.
        """
        if self._publish_state is None:
            # No subscriber configured. Nothing failed, so the dedupe should
            # behave normally rather than republishing into the void forever.
            return True
        try:
            self._publish_state(decision)
        except Exception:
            # The broker is allowed to be down. That is the premise of T-527.
            logger.exception("Schedule could not publish the light's state")
            return False
        return True

    def _report(self, category, note):
        """Log a note once per distinct message, not once per tick."""
        if note is None:
            if self._reported.pop(category, None) is not None:
                logger.warning("Resolved: the previous %s problem is gone", category)
            return
        if self._reported.get(category) == note:
            return
        self._reported[category] = note
        logger.error("Schedule: %s", note)

    # ------------------------------------------------------------------ run

    def run_forever(self):
        """Tick until stop() is called. Never propagates an exception.

        The catch-all is the point, not laziness. An exception escaping here
        kills this thread and leaves the process alive with exit status 0, so
        Restart=always never fires and the lamp silently stops following its
        schedule. Everything in tick() is already written to return notes
        rather than raise; this is the backstop for the case that isn't.

        The sleep is measured from the START of the tick, so a slow tick eats
        into the gap rather than adding to it and the cadence does not drift.

        IT USES THE MODULE-LEVEL monotonic(), NOT self._monotonic, and the
        split is deliberate rather than an oversight. `self._monotonic` exists
        for ONE purpose — the never-synced hold's fallback anchor — and tests
        inject finite iterators into it; driving this loop from the same
        callable would exhaust them. Nothing here needs to be steerable,
        because the cadence is already injectable through `sleeper`.
        """
        # Stamped here rather than in start(), so it names the thread actually
        # running the loop even if this is called directly, and so it cannot go
        # stale across a restart. See _heartbeat_locked.
        self._scheduler_ident = threading.get_ident()
        while not self._stop.is_set():
            started = monotonic()
            try:
                self.tick()
            except Exception:
                logger.exception("Schedule tick failed; the next tick will retry")
            remaining = self._tick_seconds - (monotonic() - started)
            self._sleeper(remaining if remaining > 0 else 0)

    def stop(self):
        self._stop.set()

    def start(self):
        """Run the loop on a daemon thread and return it.

        Daemon, so a shutdown is never held up by a sleeping scheduler; the
        lamp keeps whatever duty cycle it had, which is the correct outcome
        for a process that is going away.
        """
        thread = threading.Thread(
            target=self.run_forever, name="light-schedule", daemon=True
        )
        thread.start()
        logger.warning(
            "Local light schedule started (tick %ss, config %s, state %s)",
            self._tick_seconds,
            self._config_path,
            self._state_path,
        )
        return thread
