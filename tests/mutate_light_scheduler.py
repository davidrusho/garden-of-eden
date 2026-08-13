#!/usr/bin/env python3
"""Mutation battery for the local photoperiod's I/O layer (T-527.5).

    python3 tests/mutate_light_scheduler.py

A green suite proves nothing until it has been shown capable of going red. Each
mutant below breaks ONE guarantee tests/test_light_scheduler.py claims to
enforce; a survivor means the corresponding test is decorative, OR the mutated
code is redundant, OR the corpus contains no case that reaches it — all three
are findings, and the output says enough to tell them apart.

RUNS IN A shutil.copytree(REPO) SANDBOX rather than mutating the working tree,
which is the design tests/test_suite_isolation.py prefers and the direction
T-527.13 is moving the sibling harness. Nothing here can strand a mutant in a
tree a concurrent session is committing from, and no restore path has to be
trusted — the sandbox is simply deleted.

THE FAMILY THAT IS DELIBERATELY OVER-REPRESENTED IS "the lamp stops moving".
This host has no console, no keyboard and no reimage; the failure that costs
most is not a crash but a scheduler that quietly holds the garden dark, so
every branch that can decide "do not drive the lamp" gets at least one mutant:
the NTP gate's three failure readings, the corrupt-state-file reading, the
already-at-target shortcut, and run_forever's catch-all.

THREADING IS NOW IN THE BATTERY, AND ITS ABSENCE WAS THIS FILE'S WORST BUG.
An earlier version of the list below named three gaps and did not mention
concurrency, so the largest untested surface in the module was the one this
preamble implied was covered — which is worse than an undeclared gap, because
an undeclared gap at least prompts the question. An independent 15-mutant pass
then left 10 survivors, `with self._lock:` -> `if True:` among them.

THERE ARE SEVEN `with self._lock:` STATEMENTS AND THREE OF THEM CARRY MUTANTS.
Counted, because the first version of this paragraph said "both lock
statements" — committing the exact error it had just been written to warn
about, one sentence after warning about it. The three that are mutated are the
ones with production callers: `tick`, `override_now`, `publish_now` (mqtt.py
calls only those three plus `start`). The four that are not are
`set_override`, `clear_override`, and the `override` and `last_decision`
properties; each was checked individually and all four can be removed with the
suite still GREEN. That is a real gap in the guard rather than a judgement that
it does not matter — it is unexercised API surface today, and the moment
anything calls it, it is untested. Do not read the 100% score as covering it.

The publish bookkeeping and the failed-drive republish do carry mutants, and
tests/test_light_scheduler.py's SerialisationTests is what kills them. The
standing lesson: when declaring absences, ENUMERATE the module's constructs —
`grep -c` them — rather than listing the ones already in mind.

CONSTRUCTS WITH NO MUTANT, DECLARED RATHER THAN LEFT AS AN UNEXPLAINED GAP —
because a kill count bounds only the mutants somebody thought to write, and an
undeclared absence reads as coverage:

  * `parse_env`'s quote-stripping and comment handling, beyond the two mutants
    below. tests/test_light_scheduler.py exercises those branches, and the
    identical function in bin/gardyn-netwatch.py is exercised again by
    tests/test_netwatch.py:1043-1054 — but NO battery in this repo mutates any
    of it, including tests/mutate_netwatch.py, which touches build_config and
    never parse_env. So this is a genuine gap rather than coverage borrowed
    from elsewhere, and the honest reason it is left is proportion: the two
    properties the schedule format actually depends on are `export` stripping
    and `partition`-not-`split` (a `=`-bearing value is the format here, not an
    edge case), and both have mutants.
  * The `logger.debug` re-assert branch in `_apply`. Its only observable is
    the absence of a log line at a level the test handler does capture, and a
    mutant promoting it to INFO is caught by
    test_a_persistent_config_problem_is_logged_once only by accident; a
    dedicated test would assert on log volume, which is not a property worth
    pinning at this cost.
  * The empty corpus case for CORRUPT_STATE_BODIES cannot be covered by any
    mutant on the module — emptying a subTest corpus leaves nothing to
    perturb. tests/test_light_scheduler.py carries
    test_the_corruption_corpus_has_not_been_quietly_narrowed for exactly that,
    and the last mutant below empties the tuple to prove that guard fires.
  * The ORDER of the two lock acquisitions cannot be perturbed into a deadlock
    by any single-anchor mutant, because there is only ever one lock and no
    nesting — that is the design rather than an oversight, and
    SerialisationTests' stress case asserts no thread hangs. A mutant that
    introduced a second lock would be writing new code, not perturbing this.
  * `_elapsed_since_boot`'s subtraction. The fallback branch has mutants on
    both of its inputs (`self._uptime()` forced to None, and the ceiling
    itself), and a mutant on the arithmetic would only restate them.

Exit 0 all killed, 1 survivors or unapplied or no-verdict, 2 a broken
instrument (either control failed).
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

SRC = "light_scheduler.py"
TESTS = "tests/test_light_scheduler.py"
MQTT = "mqtt.py"
UNIT = "services/etc/systemd/system/mqtt.service"

SUITE = "tests.test_light_scheduler"

# The one suite that can see any of these files. Kept explicit rather than
# running everything, so a slow unrelated suite cannot mask a survivor by
# reddening for its own reasons.
SUITES_FOR = {SRC: (SUITE,), TESTS: (SUITE,), MQTT: (SUITE,), UNIT: (SUITE,)}

# (label, file, old, new) — `old` must appear exactly once in `file`.
MUTANTS = [
    # ------------------------------------------------- the NTP gate's readings
    ("a broken timedatectl reads as an UNSYNCED clock (freezes the lamp)", SRC,
     '        return CLOCK_UNKNOWN, f"cannot read NTP sync state ({exc})"',
     '        return CLOCK_UNSYNCED, f"cannot read NTP sync state ({exc})"'),
    ("a non-zero timedatectl exit reads as an UNSYNCED clock", SRC,
     '        return CLOCK_UNKNOWN, f"cannot read NTP sync state ({detail})"',
     '        return CLOCK_UNSYNCED, f"cannot read NTP sync state ({detail})"'),
    ("an empty or unrecognised NTPSynchronized answer reads as `no`", SRC,
     '    if answer == "no":\n        return CLOCK_UNSYNCED, None',
     '    if answer != "yes":\n        return CLOCK_UNSYNCED, None'),
    ("a broken timedatectl is remembered as a SYNC, latching the gate open on "
     "the strength of its own breakage", SRC,
     "        if state == CLOCK_SYNCED:",
     "        if state in (CLOCK_SYNCED, CLOCK_UNKNOWN):"),
    ("capture_output is dropped, so stdout is None and every answer is unknown",
     SRC,
     "        proc = _run(list(NTP_QUERY), capture_output=True, text=True, timeout=timeout)",
     "        proc = _run(list(NTP_QUERY), text=True, timeout=timeout)"),
    ("text= is dropped, so the answer arrives as bytes and never equals 'yes'",
     SRC,
     "        proc = _run(list(NTP_QUERY), capture_output=True, text=True, timeout=timeout)",
     "        proc = _run(list(NTP_QUERY), capture_output=True, timeout=timeout)"),
    ("the probe asks whether a time SERVICE is enabled, not whether the "
     "kernel clock is synchronised", SRC,
     'NTP_QUERY = ("timedatectl", "show", "--property=NTPSynchronized", "--value")',
     'NTP_QUERY = ("timedatectl", "show", "--property=NTP", "--value")'),
    ("the subprocess is given no timeout, so a wedged bus stalls every tick", SRC,
     "        proc = _run(list(NTP_QUERY), capture_output=True, text=True, timeout=timeout)",
     "        proc = _run(list(NTP_QUERY), capture_output=True, text=True)"),

    # -------------------------------------------------------- the config read
    ("an absent config file propagates OSError out of the tick", SRC,
     '    except OSError as exc:\n        return DEFAULT_SCHEDULE, f"cannot read {path}',
     '    except NotADirectoryError as exc:\n        return DEFAULT_SCHEDULE, f"cannot read {path}'),
    ("a binary config file is reported as a CONTENT problem in a file that "
     "was never read", SRC,
     '    except UnicodeDecodeError:\n        return DEFAULT_SCHEDULE, f"{path} is not text; using the built-in photoperiod"',
     "    except UnicodeDecodeError:\n        raise"),
    ("a malformed schedule propagates ScheduleConfigError out of the tick", SRC,
     "    except ScheduleConfigError as exc:\n        return DEFAULT_SCHEDULE,",
     "    except NotImplementedError as exc:\n        return DEFAULT_SCHEDULE,"),
    ("the fallback happens silently, with nothing for the journal", SRC,
     '        return DEFAULT_SCHEDULE, f"{path} is not usable ({exc}); using the built-in photoperiod"',
     "        return DEFAULT_SCHEDULE, None"),

    # --------------------------------------------------- the persisted phase
    ("a corrupt state file reads as ZERO - the dark garden", SRC,
     "    try:\n        value = int(raw.strip())\n    except ValueError:\n        return None",
     "    try:\n        value = int(raw.strip())\n    except ValueError:\n        return 0"),
    ("an out-of-range persisted value is accepted instead of read as corruption",
     SRC,
     "    if not MIN_BRIGHTNESS <= value <= MAX_BRIGHTNESS:\n        return None",
     "    if False:\n        return None"),
    ("an unreadable state file raises instead of reading as absent", SRC,
     "    try:\n        with open(path) as handle:\n            raw = handle.read()\n"
     "    except (OSError, UnicodeDecodeError):\n        return None",
     "    try:\n        with open(path) as handle:\n            raw = handle.read()\n"
     "    except KeyError:\n        return None"),
    ("the persist is not atomic - a failed write destroys the previous value",
     SRC,
     '        with open(tmp, "w") as handle:\n            handle.write(f"{brightness}\\n")\n        os.replace(tmp, path)',
     '        with open(path, "w") as handle:\n            handle.write(f"{brightness}\\n")'),
    ("a failed persist raises instead of reporting a note", SRC,
     '        return f"cannot persist the applied brightness to {path} ({exc.strerror or exc})"',
     "        raise"),
    ("the state directory is not created, so the first boot never persists", SRC,
     "        if directory:\n            os.makedirs(directory, exist_ok=True)",
     "        if False:\n            os.makedirs(directory, exist_ok=True)"),
    ("a stranded .tmp is left behind after a failed write", SRC,
     "        try:\n            os.unlink(tmp)\n        except OSError:\n            pass",
     "        pass"),

    # ------------------------------------------------------ the state path
    ("$STATE_DIRECTORY is ignored, so systemd's directory is never used", SRC,
     '    first = (environ.get("STATE_DIRECTORY") or "").split(":")[0].strip()',
     '    first = ""'),
    ("a colon-separated $STATE_DIRECTORY is used whole, producing a path that "
     "cannot exist", SRC,
     '.split(":")[0].strip()',
     ".strip()"),

    # ------------------------------------------------------------ parse_env
    ("a leading `export` is left on the key", SRC,
     '        if line.startswith("export "):\n            line = line[len("export "):].lstrip()',
     "        if False:\n            pass"),
    ("KEY=value is split on every `=`, which truncates the schedule at its "
     "first boundary", SRC,
     '        key, sep, value = line.partition("=")',
     '        key, sep, value = (line.split("=") + ["", ""])[:3]'),

    # ------------------------------------------------------------- the tick
    ("the schedule is not re-read, so an edit needs a restart", SRC,
     "        schedule, note = load_schedule(self._config_path)",
     "        schedule, note = DEFAULT_SCHEDULE, None"),
    ("the persisted phase is never read, so an unsynced clock cannot hold", SRC,
     "            last_applied=read_last_applied(self._state_path),",
     "            last_applied=None,"),
    ("the override is never handed to decide()", SRC,
     "            override=override,\n        )",
     "            override=None,\n        )"),
    ("an expired override is left in place instead of being cleared", SRC,
     "            self._override = None\n            override = None\n            logger.info(",
     "            override = None\n            logger.info("),
    ("the override is stamped from the wall clock rather than the scheduler's",
     SRC,
     "        self._override = Override(brightness, self._now())",
     "        self._override = Override(brightness, datetime.now())"),

    # ------------------------------------------------------------- _apply
    ("the lamp is rewritten on every tick even when it is already there", SRC,
     "        if actual is not None and actual == target:",
     "        if False:"),
    ("an unreadable lamp is treated as already correct, so it never moves", SRC,
     "        if actual is not None and actual == target:",
     "        if actual is None or actual == target:"),
    ("the hardware read is not rounded, so PWM quantisation rewrites the pin "
     "every tick", SRC,
     "            return int(round(self._light.get_brightness()))",
     "            return self._light.get_brightness()"),
    ("the applied brightness is never persisted", SRC,
     "        note = write_last_applied(self._state_path, target)",
     "        note = None"),
    ("the state is never published, so HA never learns the lamp moved", SRC,
     "        if pair != self._last_published or not applied:\n"
     "            self._record_published_locked(decision, self._publish(decision))",
     "        self._last_published = pair"),

    # ---------------------------------------------------- who owns the lamp
    ("the publish fires on a brightness change only, so an override at the "
     "schedule's own brightness is invisible", SRC,
     "        pair = (decision.brightness, decision.source)",
     "        pair = (decision.brightness,)"),
    ("an override is recorded but never applied, so the lamp waits a whole "
     "tick for a command", SRC,
     "            self._set_override_locked(brightness)\n            return self._tick_locked()",
     "            self._set_override_locked(brightness)\n            return self._last_decision"),
    ("publish_now honours the dedupe, so a reconnecting HA is never re-told "
     "who owns the lamp", SRC,
     "                return\n"
     "            self._record_published_locked(decision, self._publish(decision))",
     "                return\n            pair = (decision.brightness, decision.source)\n"
     "            if pair != self._last_published:\n"
     "                self._record_published_locked(decision, self._publish(decision))"),
    ("publish_now publishes before any decision exists", SRC,
     "            if decision is None:\n                return",
     "            if False:\n                return"),
    ("a light that refuses to be driven takes the tick down with it", SRC,
     "        try:\n            self._light.set_duty_cycle(target)\n        except Exception:",
     "        try:\n            self._light.set_duty_cycle(target)\n        except KeyError:"),
    ("a publish failure takes the tick down with it - the broker being down "
     "is the PREMISE of this ticket", SRC,
     "        try:\n            self._publish_state(decision)\n        except Exception:",
     "        try:\n            self._publish_state(decision)\n        except KeyError:"),

    # ---------------------------------------------------------- the logging
    ("every tick re-logs the same problem, burying the four transitions a day "
     "the log exists to carry", SRC,
     "        if self._reported.get(category) == note:\n            return",
     "        if False:\n            return"),
    ("a resolved problem is never announced, so the journal's last word on it "
     "is the failure", SRC,
     '            if self._reported.pop(category, None) is not None:\n                logger.warning("Resolved:',
     '            if False:\n                logger.warning("Resolved:'),
    ("the module does not raise its own logger, so mqtt.py's root WARNING "
     "discards every transition", SRC,
     "logger.setLevel(logging.INFO)",
     "logger.setLevel(logging.WARNING)"),

    # ------------------------------------------------------- the run loop
    ("an exception in a tick kills the scheduler thread silently", SRC,
     "            try:\n                self.tick()\n            except Exception:",
     "            try:\n                self.tick()\n            except KeyboardInterrupt:"),
    ("the loop sleeps the full cadence regardless of how long the tick took", SRC,
     "            remaining = self._tick_seconds - (monotonic() - started)",
     "            remaining = self._tick_seconds"),
    ("a tick longer than the cadence sleeps a negative duration", SRC,
     "            self._sleeper(remaining if remaining > 0 else 0)",
     "            self._sleeper(remaining)"),
    ("the scheduler thread is non-daemon and can hold up a shutdown", SRC,
     '            target=self.run_forever, name="light-schedule", daemon=True',
     '            target=self.run_forever, name="light-schedule", daemon=False'),

    # -------------------------------------------------------- the heartbeat
    #
    # T-527.22. The family to over-represent here is "the heartbeat still
    # publishes but no longer means what it says", because a heartbeat that
    # stops is loud — the HA staleness check fires — while one that keeps
    # beating for the wrong reason is a permanent false all-clear on the ONLY
    # check covering a dead scheduler thread under a live broker connection.
    # GATES the existing call rather than adding a second one. The first
    # version of this mutant inserted an extra _heartbeat_locked() inside the
    # dedupe branch and left the unconditional one in place; it survived,
    # correctly, because the interval gate makes the extra call a no-op and the
    # real one still ran. An additive "mutant" tests nothing.
    ("the heartbeat is folded INTO the dedupe, so a healthy unchanging "
     "scheduler goes silent - the exact fault this ticket exists to fix", SRC,
     # Gates on whether the DEDUPE let a publish through, by reading
     # _last_published either side of the tick. An earlier draft compared
     # decision.source against _last_published (a str against a tuple), which
     # is unequal always and beats always - inert, the third time this file has
     # produced that shape.
     "            decision = self._tick_locked()\n            self._heartbeat_locked()",
     "            before = self._last_published\n"
     "            decision = self._tick_locked()\n"
     "            if self._last_published != before:\n"
     "                self._heartbeat_locked()"),
    # MOVES the call. The first version inserted one at the top of
    # _tick_locked and LEFT the real one in place - the additive shape this
    # file's own comment two entries up calls "tests nothing". It was killed,
    # but by collateral rather than by the guarantee its label names.
    ("the heartbeat fires BEFORE the decision, so it reports a tick that was "
     "merely attempted rather than one that completed", SRC,
     "            decision = self._tick_locked()\n            self._heartbeat_locked()\n            return decision",
     "            self._heartbeat_locked()\n            return self._tick_locked()"),
    # THE FINDING THIS COMMIT ANSWERS, planted at its real call site. Reaching
    # the heartbeat from override_now() is what a person tapping the light in
    # Home Assistant does, on paho's network thread, while the scheduler thread
    # is dead - so the diagnostic action silences the diagnostic. Written as an
    # addition to override_now rather than a move out of tick(), because that
    # is the single site where the defect actually lived.
    ("override_now() emits a heartbeat, so paho's network thread and the "
     "physical button both speak for a scheduler thread that may be dead", SRC,
     "            self._set_override_locked(brightness)\n            return self._tick_locked()",
     "            self._set_override_locked(brightness)\n"
     "            decision = self._tick_locked()\n"
     "            self._heartbeat_locked()\n            return decision"),
    ("the scheduler-thread guard is inert, so any future caller of tick() from "
     "another thread can speak for the loop's liveness", SRC,
     "        if (\n            self._scheduler_ident is not None\n"
     "            and threading.get_ident() != self._scheduler_ident\n        ):\n            return",
     "        if False:\n            return"),
    # RELABELLED after review. This was called "the REAL loop is refused and the
    # heartbeat never publishes at all", and it is not that: it perturbs the
    # PRE-LOOP permissiveness, and the test that names a silent total failure
    # (test_run_forever_claims_the_ident_on_the_thread_it_runs_on) passes under
    # it. Probed in a sandbox to confirm which case each arm actually reddens.
    # The direction the old label described had no mutant at all; it is the
    # next entry.
    ("the guard stops being inert before a loop runs, so a direct tick() is "
     "refused and every test in this file loses its heartbeat", SRC,
     "            self._scheduler_ident is not None\n"
     "            and threading.get_ident() != self._scheduler_ident",
     "            self._scheduler_ident is None\n"
     "            or threading.get_ident() != self._scheduler_ident"),
    ("the guard's comparison is flipped, so the REAL loop is refused and a "
     "non-scheduler thread is the only caller that can beat", SRC,
     "            and threading.get_ident() != self._scheduler_ident",
     "            and threading.get_ident() == self._scheduler_ident"),
    ("run_forever never claims the ident, so the guard can never reject "
     "anything", SRC,
     "        self._scheduler_ident = threading.get_ident()\n        while not self._stop.is_set():",
     "        while not self._stop.is_set():"),
    ("the cadence is derived from the tick, so retuning timedatectl cost "
     "silently moves what Home Assistant is watching", SRC,
     "HEARTBEAT_SECONDS = 120",
     "HEARTBEAT_SECONDS = 4 * TICK_SECONDS"),
    ("the cadence outruns Home Assistant's expire_after, so a HEALTHY "
     "scheduler flaps the sensor unavailable - a permanent false alarm", SRC,
     "HEARTBEAT_SECONDS = 120",
     "HEARTBEAT_SECONDS = 900"),
    ("the first tick after a restart waits out a whole interval, so a fresh "
     "process is indistinguishable from a dead one", SRC,
     "        self._last_heartbeat = None",
     "        self._last_heartbeat = 0.0"),
    ("the cadence boundary is exclusive, so the interval is one tick longer "
     "than the constant says", SRC,
     "            and stamp - self._last_heartbeat < self._heartbeat_seconds",
     "            and stamp - self._last_heartbeat <= self._heartbeat_seconds"),
    ("a failed heartbeat publish stamps the clock anyway, so a broker blip "
     "swallows a whole interval instead of retrying next tick", SRC,
     '            self._report("heartbeat", f"cannot publish the schedule heartbeat ({exc})")\n            return',
     '            self._report("heartbeat", f"cannot publish the schedule heartbeat ({exc})")\n            self._last_heartbeat = stamp\n            return'),
    # Sets the counter in the FAILURE branch. The first version reordered the
    # two success-path assignments and added `+ 0`, which is a no-op the
    # `except` arm returns before ever reaching — it survived because it was
    # not a mutation of anything.
    ("a failed heartbeat publish advances the counter, so the next successful "
     "beat skips a number and the sink's record disagrees with the scheduler's",
     SRC,
     '            self._report("heartbeat", f"cannot publish the schedule heartbeat ({exc})")\n            return',
     '            self._report("heartbeat", f"cannot publish the schedule heartbeat ({exc})")\n            self._heartbeat_count = count\n            return'),
    ("a broker that refuses the heartbeat takes the whole tick down - the "
     "observability feature killing the photoperiod it observes", SRC,
     "        try:\n            self._publish_heartbeat(count)\n        except Exception as exc:",
     "        try:\n            self._publish_heartbeat(count)\n        except KeyError as exc:"),
    ("the heartbeat shares the never-synced hold's clock, which tests inject "
     "finite iterators into", SRC,
     "        stamp = self._heartbeat_clock()",
     "        stamp = self._monotonic()"),
    ("publish_now() refreshes the heartbeat, so paho's live network thread "
     "reports liveness on behalf of the dead scheduler thread", SRC,
     "            self._record_published_locked(decision, self._publish(decision))\n\n    # ----------------------------------------------------------------- tick",
     "            self._record_published_locked(decision, self._publish(decision))\n            self._heartbeat_locked()\n\n    # ----------------------------------------------------------------- tick"),
    # RETAINING is the mutant now, not the fix. The first version had this
    # backwards: it treated retain=True as correct and the absence as the
    # defect. HA's MQTT sensor docs say the opposite for an expire_after
    # sensor, and a retained replay is what lets the entity come back
    # available with an expired state.
    ("the heartbeat is RETAINED, so a replay makes the sensor available with "
     "an expired state and an availability flap resets the staleness", MQTT,
     '    info = client.publish(LIGHT_HEARTBEAT_TOPIC, str(count))',
     '    info = client.publish(LIGHT_HEARTBEAT_TOPIC, str(count), retain=True)'),
    ("the publish return code is ignored, so a beat dropped by a disconnected "
     "broker is charged as sent - paho RETURNS rc=4, it does not raise", MQTT,
     "    if info.rc != mqtt.MQTT_ERR_SUCCESS:",
     "    if False:"),
    ("a failed heartbeat writes a traceback per beat instead of one deduped "
     "line, burying an unrotated log on an SD card for the length of an outage",
     SRC,
     '            self._report("heartbeat", f"cannot publish the schedule heartbeat ({exc})")',
     '            logger.exception("Schedule could not publish its heartbeat: %s", exc)'),
    ("a recovered heartbeat never says so, so the journal's last word on it is "
     "the failure", SRC,
     '        self._report("heartbeat", None)\n        self._heartbeat_count = count',
     '        self._heartbeat_count = count'),
    ("expire_after is dropped, so the sensor never goes unavailable and the "
     "staleness check has nothing to read", MQTT,
     '        "expire_after": 600,\n',
     ''),
    ("expire_after is shorter than the publish cadence, so a HEALTHY scheduler "
     "flaps the sensor unavailable between beats", MQTT,
     '        "expire_after": 600,',
     '        "expire_after": 60,'),
    ("the heartbeat is published onto the SOURCE topic, destroying the one "
     "property that makes that topic readable", MQTT,
     'LIGHT_HEARTBEAT_TOPIC = BASE_TOPIC + "/light/schedule/heartbeat"',
     'LIGHT_HEARTBEAT_TOPIC = LIGHT_SOURCE_TOPIC'),
    ("the heartbeat sensor is never announced, so Home Assistant has no "
     "entity to measure staleness on", MQTT,
     "    publish_config(HEARTBEAT_CONFIG_TOPIC, {",
     "    _unpublished_heartbeat_config = ({"),
    ("the heartbeat sink is never wired in, so the whole feature is inert "
     "with every unit test still green", MQTT,
     "        publish_heartbeat=lambda count: publish_light_heartbeat(client, count),\n",
     ""),

    # ----------------------------------------------------------- the wiring
    # Anchored on the call PLUS the connect that follows it, because the bare
    # call is no longer unique: publish_light_heartbeat's docstring now names
    # `light_scheduler.start()` while explaining the startup race. That is this
    # repo's recurring trap - a source anchor matching prose - and the harness
    # caught it as NOT APPLIED rather than mis-applying it.
    ("mqtt.py never starts the scheduler", MQTT,
     "    light_scheduler.start()\n\n    client.connect_async(",
     "    pass\n\n    client.connect_async("),
    ("the scheduler starts AFTER loop_forever(), which never returns", MQTT,
     # MOVED PAST loop_forever(), not merely past connect_async(). An earlier
     # rewrite of this anchor put start() after connect_async and SURVIVED,
     # correctly: connect_async does not block, so the scheduler still starts
     # before the loop and nothing about the guarantee changed. The mutation
     # has to cross the call that never returns.
     #
     # It also has to KEEP the start() call. The version this replaced deleted
     # it, so the kill it scored belonged to
     # test_mqtt_constructs_and_starts_the_scheduler — a different guarantee
     # from the one the label names.
     "    light_scheduler.start()\n\n    client.connect_async(BROKER, PORT, KEEP_ALIVE_INTERVAL)\n\n"
     "    # The periodic publishers are started from on_connect, not here — see\n"
     "    # start_publisher_threads().\n"
     "    client.loop_forever(retry_first_connection=True)",
     "    client.connect_async(BROKER, PORT, KEEP_ALIVE_INTERVAL)\n\n"
     "    # The periodic publishers are started from on_connect, not here — see\n"
     "    # start_publisher_threads().\n"
     "    client.loop_forever(retry_first_connection=True)\n"
     "    light_scheduler.start()"),
    ("the scheduler is started from on_connect, reintroducing the broker "
     "dependency this whole ticket removes", MQTT,
     "    start_publisher_threads(client)",
     "    start_publisher_threads(client)\n    LightScheduler(light, None).start()"),
    ("the unit stops creating the state directory", UNIT,
     "StateDirectory=gardyn\n",
     ""),

    # ----------------------------------------- the override wiring (T-527.6)
    ("an MQTT light command drives the pin directly, beside the scheduler",
     MQTT,
     "        elif topic_suffix == \"light/command\":\n            if payload.upper() == \"ON\":\n                apply_light_override(brightness)\n            elif payload.upper() == \"OFF\":\n                apply_light_override(0)",
     "        elif topic_suffix == \"light/command\":\n            if payload.upper() == \"ON\":\n                light.set_duty_cycle(brightness)\n            elif payload.upper() == \"OFF\":\n                light.off()"),
    ("a brightness command drives the pin directly, so nothing persists it "
     "and nothing publishes the new owner", MQTT,
     "            brightness = int(payload)\n            apply_light_override(brightness)",
     "            brightness = int(payload)\n            light.set_duty_cycle(brightness)"),
    ("the physical button is not an override, so a press is reverted within "
     "a tick", MQTT,
     "        logger.info(\"Toggling Light ON\")\n        apply_light_override(brightness)",
     "        logger.info(\"Toggling Light ON\")\n        light.set_duty_cycle(brightness)"),
    ("a reconnecting Home Assistant is never re-told who owns the lamp", MQTT,
     "    if light_scheduler is not None:\n        light_scheduler.publish_now()",
     "    if False:\n        light_scheduler.publish_now()"),
    ("the owner has no discovery entity, so it exists only as a raw topic",
     MQTT,
     "    publish_config(SOURCE_CONFIG_TOPIC, {",
     "    _unpublished = ({"),

    # ------------------------------------- serialisation (T-527.20) --------
    # THE LARGEST UNTESTED SURFACE IN THE PREVIOUS BATTERY, and it was the one
    # declared covered by omission: an independent 15-mutant pass left 10
    # survivors, `with self._lock:` -> `if True:` among them, because the file
    # had no concurrent test at all.
    ("the tick is not serialised, so a command landing inside one is reverted",
     SRC,
     # Re-anchored when tick() gained the heartbeat call (T-527.22). The old
     # anchor was `with self._lock:\n return self._tick_locked()`, which now
     # matches nothing - the harness reported it NOT APPLIED, which is that
     # gate working in the direction that costs nothing.
     "        with self._lock:\n            decision = self._tick_locked()",
     "        if True:\n            decision = self._tick_locked()"),
    ("recording an override and applying it are two acquisitions again, so a "
     "tick can run between them", SRC,
     "        with self._lock:\n            self._set_override_locked(brightness)\n"
     "            return self._tick_locked()",
     "        self._set_override_locked(brightness)\n        return self.tick()"),
    ("publish_now runs outside the lock, so its publish can land after a "
     "tick's while its bookkeeping lands before", SRC,
     "        with self._lock:\n            decision = self._last_decision",
     "        if True:\n            decision = self._last_decision"),
    ("a failed drive is recorded as published, stranding HA on the value the "
     "lamp never reached", SRC,
     "        if pair != self._last_published or not applied:",
     "        if pair != self._last_published:"),
    ("_apply reports success after the drive raised", SRC,
     '            logger.exception("Schedule could not drive the light to %s%%", target)\n'
     "            return False",
     '            logger.exception("Schedule could not drive the light to %s%%", target)\n'
     "            return True"),
    ("the published pair is remembered even when the lamp never got there",
     SRC,
     "        if self._last_apply_ok and published_ok:\n"
     "            self._last_published = (decision.brightness, decision.source)",
     "        if True:\n            self._last_published = (decision.brightness, decision.source)"),
    # The publish-path half, added with the fix that closed it. This mutant
    # REINTRODUCES the exact defect: a publish that raised recorded as sent.
    ("a failed PUBLISH is recorded as published, stranding HA on a retained "
     "value that never left the process", SRC,
     "        if self._last_apply_ok and published_ok:",
     "        if self._last_apply_ok:"),
    ("_publish reports success after the publish raised", SRC,
     '            logger.exception("Schedule could not publish the light\'s state")\n'
     "            return False",
     '            logger.exception("Schedule could not publish the light\'s state")\n'
     "            return True"),
    ("a scheduler with no publisher reports every tick as a FAILED publish, "
     "so the dedupe never engages", SRC,
     "            # No subscriber configured. Nothing failed, so the dedupe should\n"
     "            # behave normally rather than republishing into the void forever.\n"
     "            return True",
     "            return False"),

    # ------------------------------------- the clock gate (T-527.19) -------
    ("the latch never sets, so the 8.9-hour staleness trip freezes the lamp "
     "again", SRC,
     "                self._ever_synced = True",
     "                self._ever_synced = False"),
    ("the latch is never consulted, which is the shipped defect verbatim", SRC,
     "        if self._ever_synced:\n            # Latched.",
     "        if False:\n            # Latched."),
    ("the latch is set unconditionally, so a boot that never synced drives "
     "off a clock restored from disk", SRC,
     "        if state == CLOCK_UNKNOWN:\n            return True, None",
     "        self._ever_synced = True\n        if state == CLOCK_UNKNOWN:\n            return True, None"),
    ("the never-synced hold has no ceiling - a legitimate persisted 0 is a "
     "dark garden for the whole outage", SRC,
     "        if self._elapsed_since_boot() < self._never_synced_hold_seconds:\n"
     "            return False, None",
     "        if True:\n            return False, None"),
    ("the ceiling is inclusive, which is the off-by-one nobody would notice",
     SRC,
     "        if self._elapsed_since_boot() < self._never_synced_hold_seconds:",
     "        if self._elapsed_since_boot() <= self._never_synced_hold_seconds:"),
    # NOTE: the first version of this mutant was `None or f"" or f"the clock…"`,
    # which evaluates to the SAME string — it perturbed nothing and survived
    # honestly. A survivor is a question about the harness before it is a
    # question about the suite.
    ("ending the hold is silent, so nothing in the journal says the schedule "
     "is running on an uncorroborated clock", SRC,
     "        return True, (\n            \"the clock has never synchronised and the host has been up longer \"\n"
     "            \"than the hold ceiling; following the schedule anyway rather than \"\n"
     "            \"holding the lamp indefinitely\"\n        )",
     "        return True, None"),
    # A mutant must be able to REINTRODUCE removed code, not only break what is
    # present. This puts the elapsed value back into the note, which is what
    # defeated _report's text dedupe and wrote 246 ERROR lines a day.
    ("the hold note interpolates a value that moves, so _report's dedupe is "
     "defeated and every six minutes writes a fresh ERROR line", SRC,
     "        return True, (\n            \"the clock has never synchronised and the host has been up longer \"\n"
     "            \"than the hold ceiling; following the schedule anyway rather than \"\n"
     "            \"holding the lamp indefinitely\"\n        )",
     "        return True, (\n            f\"the clock has never synchronised and the host has been up \"\n"
     "            f\"{self._elapsed_since_boot() / 3600:.1f} h; following the schedule anyway \"\n"
     "            f\"rather than holding the lamp indefinitely\"\n        )"),
    ("the ceiling is measured from process start, so a crash loop grants a "
     "fresh window every ten seconds", SRC,
     "        value = self._uptime()\n        if value is None:",
     "        value = None\n        if value is None:"),
    ("an unreadable /proc/uptime reads as a host that just booted, extending "
     "the very hold the ceiling bounds", SRC,
     "    try:\n        with _open(path) as handle:\n            raw = handle.read()\n"
     "    except (OSError, UnicodeDecodeError):\n        return None",
     "    try:\n        with _open(path) as handle:\n            raw = handle.read()\n"
     "    except (OSError, UnicodeDecodeError):\n        return 0.0"),
    ("an unparseable /proc/uptime reads as zero rather than as unreadable",
     SRC,
     "    except (IndexError, ValueError):\n        return None",
     "    except (IndexError, ValueError):\n        return 0.0"),

    # --------------------------------- the module-level defaults -----------
    # Every test passes these explicitly, so before T-527.20 a mutant on any of
    # them was SILENT. A default nothing asserts is a default nothing protects.
    ("CONFIG_PATH is repointed at a path that does not exist", SRC,
     'CONFIG_PATH = "/etc/gardyn/light.env"',
     'CONFIG_PATH = "/etc/gardyn/light.conf"'),
    ("the state directory disagrees with StateDirectory= in the unit", SRC,
     'STATE_DIR_FALLBACK = "/var/lib/gardyn"',
     'STATE_DIR_FALLBACK = "/var/lib/gardyn-light"'),
    ("the tick cadence is raised to an hour, so a boundary is up to 60 minutes "
     "late", SRC,
     "TICK_SECONDS = 30",
     "TICK_SECONDS = 3600"),
    ("the NTP query timeout is cut to a value no fork+exec can meet", SRC,
     "NTP_QUERY_TIMEOUT_SECONDS = 10",
     "NTP_QUERY_TIMEOUT_SECONDS = 0.001"),
    ("the never-synced ceiling is widened to a week", SRC,
     "NEVER_SYNCED_HOLD_SECONDS = 2 * 60 * 60",
     "NEVER_SYNCED_HOLD_SECONDS = 7 * 24 * 60 * 60"),
    ("UPTIME_PATH is repointed away from procfs", SRC,
     'UPTIME_PATH = "/proc/uptime"',
     'UPTIME_PATH = "/proc/loadavg"'),
    # A hardcoded default that EQUALS its constant cannot be caught at runtime
    # — every assertion agrees while the single source of truth is gone, and
    # `is` agrees too, since identical literals in one module fold to one
    # object. The killing test is a SOURCE assertion, so the mutant has to be
    # one the source can see.
    ("the constructor hardcodes its own config path past the module constant",
     SRC,
     "        config_path=CONFIG_PATH,",
     '        config_path="/etc/gardyn/light.env",  # noqa'),
    ("the constructor hardcodes the tick cadence past the module constant",
     SRC,
     "        tick_seconds=TICK_SECONDS,",
     "        tick_seconds=30,"),
    ("the constructor hardcodes the never-synced ceiling past the module "
     "constant", SRC,
     "        never_synced_hold_seconds=NEVER_SYNCED_HOLD_SECONDS,",
     "        never_synced_hold_seconds=2 * 60 * 60,"),

    # ------------------------- the TEST FILE's own constants and controls ---
    # A guard on an architectural promise is exactly the thing that gets
    # quietly narrowed, and no mutant on the module can see it (T-527.4's
    # battery found this the hard way).
    ("the purity guard's forbidden set is emptied, making it vacuous", TESTS,
     '    FORBIDDEN = frozenset(\n        {"gpiozero", "pigpio", "paho", "flask", "dotenv", "mqtt", "app", "config"}\n    )\n\n    def _forbidden_names_pulled_by',
     "    FORBIDDEN = frozenset()\n\n    def _forbidden_names_pulled_by"),
    ("the state-file corruption corpus is emptied, making its subTest loop "
     "vacuous", TESTS,
     'CORRUPT_STATE_BODIES = ("", "   ", "\\x00\\x00", "5 0", "fifty", "50%", "50.0",\n                        "0x32", " 50 50", "50\\n60", "None", "-")',
     "CORRUPT_STATE_BODIES = ()"),
]


def apply_once(path: Path, old: str, new: str):
    """Replace `old` with `new`, exactly once. Returns a problem string or None.

    Gated on compile() for Python targets BEFORE the write. A mutant that is
    not valid Python makes the module die at import, every case errors, and the
    battery scores it KILLED while the behaviour it was written for never ran —
    which is a lie in the reassuring direction. Compiling first turns that into
    "not applied", which is a finding rather than a verdict.
    """
    text = path.read_text()
    hits = text.count(old)
    if hits != 1:
        return f"anchor appears {hits} times, expected exactly 1"
    mutated = text.replace(old, new)
    if mutated == text:
        return "replacement changed nothing"
    if path.suffix == ".py":
        try:
            compile(mutated, str(path), "exec")
        except SyntaxError as exc:
            return f"mutant is not valid Python: {exc}"
    path.write_text(mutated)
    return None


def ran_count(out: str) -> int:
    """How many tests actually RAN, summed across the suites in `out`.

    THE ONLY RELIABLE SIGNAL THAT A MUTANT DIED AT COLLECTION (T-527.18). The
    no-verdict rule below was written to stop "the module died at import" being
    scored as "the behaviour was noticed", and it looks for a red run with zero
    named FAIL:/ERROR: lines. That catches nothing in the ImportError family,
    because unittest wraps an unimportable module in `unittest.loader.
    _FailedTest` and reports it as a perfectly ordinary named ERROR:

        ERROR: test_light_scheduler (unittest.loader._FailedTest.…)
        Ran 1 test in 0.000s

    One named line, so the old rule scored it `killed (1 failing case(s))`
    while the behaviour under test never executed. Confirmed by planting
    `import a_module_that_does_not_exist` and reading the output.

    The ran-count cannot be fooled that way: no honest mutant changes how many
    tests are COLLECTED, only how many pass. A count that moved means the suite
    that ran is not the suite that was scored.
    """
    return sum(int(n) for n in re.findall(r"Ran (\d+) tests?", out))


def run_suites(root: Path, suites=(SUITE,)):
    """Return (all_passed, combined_output). stderr merged — unittest uses it."""
    for cache in root.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    combined, ok = [], True
    for suite in suites:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", suite],
            cwd=root,
            # Inherit the environment and add ONE variable. Rebuilding PATH
            # here would silently select a different python3 from the one this
            # repo runs under, and the resulting failures land in files the
            # mutant never touched — which reads as a kill.
            env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        combined.append(f"--- {suite} (rc={proc.returncode}) ---\n{proc.stdout}")
        ok = ok and proc.returncode == 0
    return ok, "\n".join(combined)


# Control B: a deliberately broken ASSERTION, not broken syntax. The scorer has
# to be shown able to distinguish pass from fail; a mutant that stops the suite
# importing proves only that the runner reports errors.
CONTROL_B = (
    TESTS,
    "        self.assertEqual(100, decision.brightness)\n"
    "        self.assertEqual(ls.SOURCE_SCHEDULE, decision.source)",
    "        self.assertEqual(-1, decision.brightness)\n"
    "        self.assertEqual(ls.SOURCE_SCHEDULE, decision.source)",
)

# CONTROL C — the positive control for the NO-VERDICT rule (T-527.15/.18).
#
# Without it, "0 no-verdict" is equally consistent with the rule working and
# with the rule being unreachable, and the second was TRUE for the whole
# ImportError family until 2026-08-09: unittest wraps an unimportable module in
# `unittest.loader._FailedTest` and reports one ordinary named ERROR, so the
# zero-named-lines rule could never fire and the mutant scored as a kill.
#
# Deliberately a MISSING IMPORT rather than a typo'd name. A name typo
# (`threadingg.Lock()`) is the shape a sibling harness used, and it happens to
# raise at module level too — but only because that particular line executes at
# import. Move it inside a function and the same control silently stops testing
# anything. An import statement cannot be moved out of the import.
CONTROL_C = (
    SRC,
    "import subprocess\n",
    "import subprocess\nimport a_module_that_certainly_does_not_exist\n",
)


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        shutil.copytree(REPO, root,
                        ignore=shutil.ignore_patterns("__pycache__", "venv",
                                                      ".git", "node_modules"))

        print("=" * 72)
        print("CONTROL A (positive, runs FIRST as a gate) - clean tree GREEN")
        print("=" * 72)
        ok, out = run_suites(root)
        if not ok:
            print(out[-4000:])
            print("\nCONTROL A FAILED - the suite does not pass on a clean copy.")
            print("This is NO DATA, not a score. Nothing below means anything.")
            return 2
        clean_ran = ran_count(out)
        if clean_ran == 0:
            print("\nCONTROL A FAILED - a green run that collected NO tests.")
            print("This is NO DATA, not a score. Nothing below means anything.")
            return 2
        print(f"  GREEN ({clean_ran} tests)\n")

        print("=" * 72)
        print("CONTROL B (negative) - a broken ASSERTION must score RED")
        print("=" * 72)
        ctl = root / CONTROL_B[0]
        pristine_ctl = ctl.read_text()
        problem = apply_once(ctl, CONTROL_B[1], CONTROL_B[2])
        if problem:
            print(f"  could not inject the broken assertion: {problem}")
            print("\nCONTROL B FAILED - the scorer is unproven. NO DATA.")
            return 2
        ok, _ = run_suites(root)
        ctl.write_text(pristine_ctl)
        if ok:
            print("  GREEN - but it MUST be RED.")
            print("\nCONTROL B FAILED - the scorer cannot tell pass from fail.")
            print("Every 'killed' below would be meaningless. NO DATA.")
            return 2
        print("  RED - the scorer can distinguish pass from fail\n")

        print("=" * 72)
        print("CONTROL C (positive, for the NO-VERDICT rule) - an import-time")
        print("break must score NO VERDICT, not a kill")
        print("=" * 72)
        ctl = root / CONTROL_C[0]
        pristine_ctl = ctl.read_text()
        problem = apply_once(ctl, CONTROL_C[1], CONTROL_C[2])
        if problem:
            print(f"  could not inject the import break: {problem}")
            print("\nCONTROL C FAILED - the no-verdict rule is unproven. NO DATA.")
            return 2
        ok, out = run_suites(root)
        ctl.write_text(pristine_ctl)
        c_ran = ran_count(out)
        c_fails = [ln for ln in out.splitlines()
                   if ln.startswith(("FAIL:", "ERROR:"))]
        if ok or c_ran == clean_ran:
            print(f"  the suite ran {c_ran} tests against the clean {clean_ran}"
                  f" and was {'GREEN' if ok else 'RED'}")
            print("\nCONTROL C FAILED - an import-time break was not detected as")
            print("a collection failure, so every 'killed' below may belong to a")
            print("mutant whose behaviour never ran. NO DATA.")
            return 2
        print(f"  NO VERDICT - {c_ran} tests ran against the clean {clean_ran}, "
              f"with {len(c_fails)} named failing case(s)")
        print("  (note the named lines: that is exactly why the zero-named-lines")
        print("   rule alone could not see this, and the count can)\n")

        print("=" * 72)
        print(f"{len(MUTANTS)} MUTANTS")
        print("=" * 72)
        pristine = {rel: (root / rel).read_text()
                    for rel in {m[1] for m in MUTANTS}}
        killed, survived, unapplied, broad = [], [], [], []
        for i, (label, rel, old, new) in enumerate(MUTANTS, 1):
            print(f"[{i}/{len(MUTANTS)}] {label}")
            target = root / rel
            problem = apply_once(target, old, new)
            if problem:
                print(f"  NOT APPLIED - {problem}")
                unapplied.append(label)
                target.write_text(pristine[rel])
                continue
            ok, out = run_suites(root, SUITES_FOR[rel])
            target.write_text(pristine[rel])
            if ok:
                print("  SURVIVED - no test noticed")
                survived.append(label)
                continue
            # Read WHY it died, on TWO independent signals, because each misses
            # what the other catches (T-527.18).
            #
            # First: did the same tests run at all? A mutant that compiles and
            # then dies at import is wrapped by unittest in
            # `unittest.loader._FailedTest` and reported as ONE ordinary named
            # ERROR line — so the zero-named-lines rule below cannot see it,
            # and it used to score as a kill. The ran-count can: no honest
            # mutant changes how many tests are COLLECTED.
            mutant_ran = ran_count(out)
            if mutant_ran != clean_ran:
                print(f"  NO VERDICT - {mutant_ran} tests ran against the clean")
                print(f"  tree's {clean_ran}, so the module died at collection")
                print("  rather than the behaviour being noticed. NOT a kill.")
                broad.append(label)
                continue
            # Second: a red run with no named failing case at all. Kept as its
            # own rule rather than folded into the count, because it catches
            # the shapes that redden without changing collection.
            fails = [ln for ln in out.splitlines()
                     if ln.startswith(("FAIL:", "ERROR:"))]
            if not fails:
                print("  NO VERDICT - red with no named failing case, so the")
                print("  module died at collection rather than the behaviour")
                print("  being noticed. This is NOT a kill.")
                broad.append(label)
                continue
            print(f"  killed ({len(fails)} failing case(s))")
            for line in fails[:2]:
                print(f"      {line}")
            killed.append(label)

        drifted = [rel for rel, text in pristine.items()
                   if (root / rel).read_text() != text]

        print("\n" + "=" * 72)
        print(f"RESULT: {len(killed)} killed, {len(survived)} survived, "
              f"{len(unapplied)} not applied, {len(broad)} no-verdict, "
              f"of {len(MUTANTS)}")
        if broad:
            print("\nNO VERDICT - treat as untested, not as killed:")
            for label in broad:
                print(f"  - {label}")
        if survived:
            print("\nSURVIVORS - ask in order: does any case in the corpus reach")
            print("this construct; is the code redundant; is the test weak.")
            for label in survived:
                print(f"  - {label}")
        if unapplied:
            print("\nNOT APPLIED - no verdict was reached for these:")
            for label in unapplied:
                print(f"  - {label}")
        if drifted:
            print(f"\nSANDBOX NOT RESTORED - still mutated: {drifted}")
        print("=" * 72)

        # The working tree is never touched by this harness — the sandbox is a
        # copytree and is deleted with the TemporaryDirectory. Said explicitly
        # because tests/test_suite_isolation.py splits harnesses into IN_PLACE
        # and SANDBOXED on exactly this property, and a reader has to be able
        # to check the claim rather than take the label.
        print(f"sandbox: {root} (deleted on exit); "
              f"{REPO} was never written to")
        return 0 if not (survived or unapplied or broad or drifted) else 1


if __name__ == "__main__":
    sys.exit(main())
