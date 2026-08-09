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
# one `timedatectl`, and on this Pi Zero W that is 0.169 s median over seven
# runs (min 0.165, max 0.184) — so 30 s costs ~0.56% of one core, against a
# camera path on the same host that spawns fswebcam twice per cycle. Half a
# minute of lateness on a photoperiod boundary is not a quantity a plant can
# measure; the boundary latency HA achieved was 4-7 s and nothing depended on
# it. Going much shorter buys nothing and multiplies the only recurring cost
# this feature has.
TICK_SECONDS = 30

# `timedatectl` talks to systemd-timedated over D-Bus, which is activated on
# demand, so the first call after boot is slower than the steady state. Ten
# seconds is far past anything measured and exists only so a wedged bus cannot
# stall the tick loop forever.
NTP_QUERY_TIMEOUT_SECONDS = 10

# systemd.exec(5) and org.freedesktop.timedate1(5): NTPSynchronized "shows
# whether the kernel reports the time as synchronized (c.f. adjtimex(3))".
# Read from the KERNEL, so it stays correct if this host is ever moved from
# systemd-timesyncd to chrony — which is why this is preferred over stat()ing
# /run/systemd/timesync/synchronized, a file systemd-timesyncd(8) documents as
# its own and which chrony would never create. Verified against the man pages
# on the host (systemd 252) rather than from recall.
NTP_QUERY = ("timedatectl", "show", "--property=NTPSynchronized", "--value")


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


def clock_is_synced(_run=subprocess.run, timeout=NTP_QUERY_TIMEOUT_SECONDS):
    """Does the kernel consider the clock synchronised? Returns (synced, note).

    WHEN THE QUERY ITSELF FAILS THIS ANSWERS "SYNCED", AND THAT IS THE
    DELIBERATE HALF. A failed query is evidence about `timedatectl`, not about
    the clock, so the question becomes which mistake is cheaper — the same test
    light_schedule.decide() applies to its own no-persisted-phase branch.

      believe a bad clock   the lamp follows a wrong-time photoperiod for the
                            duration. Bounded, self-correcting the moment NTP
                            lands, and a plant cannot measure it.

      disbelieve a good     the unsynced branch HOLDS the last applied
      clock                 brightness — forever, if the query is permanently
                            broken. If it was holding `off` at the time, that
                            is an indefinite dark garden, which is the exact
                            failure this ticket exists to remove.

    The scenario the gate is actually FOR — a boot with no network — does not
    go down this path at all: systemd-timedated is D-Bus activated and answers
    offline, reporting `no` correctly. A broken `timedatectl` is a different
    and far rarer event, so it must not inherit the careful branch.
    """
    try:
        proc = _run(list(NTP_QUERY), capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return True, f"cannot read NTP sync state ({exc}); assuming the clock is good"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip() or f"exit {proc.returncode}"
        return True, f"cannot read NTP sync state ({detail}); assuming the clock is good"
    answer = (proc.stdout or "").strip()
    if answer == "yes":
        return True, None
    if answer == "no":
        return False, None
    # A third answer is a contract change in systemd, not a clock fact. Same
    # reasoning as the branches above: say so, and do not stop driving.
    return True, f"NTPSynchronized answered {answer!r}, which is neither yes nor no; assuming the clock is good"


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
        config_path=CONFIG_PATH,
        state_path=None,
        tick_seconds=TICK_SECONDS,
        now=datetime.now,
        synced_probe=clock_is_synced,
        sleeper=sleep,
    ):
        self._light = light
        self._publish_state = publish_state
        self._config_path = config_path
        self._state_path = state_path if state_path is not None else default_state_path()
        self._tick_seconds = tick_seconds
        self._now = now
        self._synced_probe = synced_probe
        self._sleeper = sleeper

        self._lock = threading.Lock()
        self._override = None
        self._stop = threading.Event()
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

    # ------------------------------------------------------------- override

    def set_override(self, brightness):
        """Record a manual command. Does NOT drive the lamp — see override_now.

        `applied_at` is stamped from the SAME clock decide() is given, which is
        what makes "has the next boundary passed" answerable even while that
        clock is wrong — see Override's docstring in light_schedule.py.
        """
        with self._lock:
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
        """
        self.set_override(brightness)
        return self.tick()

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
        return self._last_decision

    def publish_now(self):
        """Re-publish the last decision unconditionally.

        For the MQTT reconnect path: a retained message is delivered once per
        subscribe, so when Home Assistant comes back and re-announces, its copy
        of who-owns-the-lamp has to be re-sent or it sits at whatever the broker
        still holds. Unconditional, because the whole point is that the
        SUBSCRIBER changed, not the value.
        """
        if self._last_decision is None:
            return
        self._last_published = (self._last_decision.brightness,
                                self._last_decision.source)
        self._publish(self._last_decision)

    # ----------------------------------------------------------------- tick

    def tick(self):
        """One pass. Returns the Decision, so a test can assert on it."""
        schedule, note = load_schedule(self._config_path)
        self._report("config", note)
        synced, note = self._synced_probe()
        self._report("ntp", note)

        now = self._now()

        with self._lock:
            override = self._override
            if override is not None and not override_is_live(schedule, override, now):
                self._override = None
                override = None
                expired = True
            else:
                expired = False
        if expired:
            logger.info("Schedule override expired; the schedule owns the lamp again")

        decision = decide(
            schedule,
            now,
            synced,
            last_applied=read_last_applied(self._state_path),
            override=override,
        )
        self._apply(decision)
        self._last_decision = decision
        # Publish on a change of OWNER as well as of brightness. An override
        # set to the brightness the schedule already wanted moves no pin, and
        # is still the difference between "the schedule is running" and "a
        # person has taken it".
        pair = (decision.brightness, decision.source)
        if pair != self._last_published:
            self._last_published = pair
            self._publish(decision)
        return decision

    def _apply(self, decision):
        """Move the lamp if it is not already where the decision says.

        THE COMPARISON IS AGAINST THE HARDWARE, not against what we last sent.
        mqtt.py's toggle_light() and publish_light_state() both read the lamp
        for the same reason: a shadow variable disagrees with the world the
        first time anything else writes to the pin — the physical button, a
        flash_lights() burst, or the MQTT command handlers.

        A residual worth checking on the host at T-527.7 rather than reasoning
        about here: PWM quantisation means get_brightness() need not return
        exactly what was commanded. If a steady-state 1% disagreement exists,
        this re-writes the pin every tick. That is cheap and harmless — the
        rounding below absorbs it for the LOG, so it cannot produce log spam —
        but `journalctl -u mqtt` after the deploy is what settles whether it
        happens at all.
        """
        actual = self._read_actual()
        target = decision.brightness
        if actual is not None and actual == target:
            self._last_logged_target = target
            return

        try:
            self._light.set_duty_cycle(target)
        except Exception:
            logger.exception("Schedule could not drive the light to %s%%", target)
            return

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
        if self._publish_state is None:
            return
        try:
            self._publish_state(decision)
        except Exception:
            # The broker is allowed to be down. That is the premise of T-527.
            logger.exception("Schedule could not publish the light's state")

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
        """
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
