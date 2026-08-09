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
     '        return True, f"cannot read NTP sync state ({exc}); assuming the clock is good"',
     '        return False, f"cannot read NTP sync state ({exc}); assuming the clock is good"'),
    ("a non-zero timedatectl exit reads as an UNSYNCED clock", SRC,
     '        return True, f"cannot read NTP sync state ({detail}); assuming the clock is good"',
     '        return False, f"cannot read NTP sync state ({detail}); assuming the clock is good"'),
    ("an empty or unrecognised NTPSynchronized answer reads as `no`", SRC,
     '    if answer == "no":\n        return False, None',
     '    if answer != "yes":\n        return False, None'),
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
     "    except (OSError, UnicodeDecodeError):\n        return None",
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
     "                self._override = None\n                override = None\n                expired = True",
     "                override = None\n                expired = True"),
    ("the override is stamped from the wall clock rather than the scheduler's",
     SRC,
     "            self._override = Override(brightness, self._now())",
     "            self._override = Override(brightness, datetime.now())"),

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
     "        self._publish()",
     "        pass"),
    ("a light that refuses to be driven takes the tick down with it", SRC,
     "        try:\n            self._light.set_duty_cycle(target)\n        except Exception:",
     "        try:\n            self._light.set_duty_cycle(target)\n        except KeyError:"),
    ("a publish failure takes the tick down with it - the broker being down "
     "is the PREMISE of this ticket", SRC,
     "        try:\n            self._publish_state()\n        except Exception:",
     "        try:\n            self._publish_state()\n        except KeyError:"),

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

    # ----------------------------------------------------------- the wiring
    ("mqtt.py never starts the scheduler", MQTT,
     "    light_scheduler.start()",
     "    pass"),
    ("the scheduler starts AFTER loop_forever(), which never returns", MQTT,
     "    light_scheduler = LightScheduler(light, lambda: publish_light_state(client))\n    light_scheduler.start()\n\n    client.connect_async(BROKER, PORT, KEEP_ALIVE_INTERVAL)",
     "    client.connect_async(BROKER, PORT, KEEP_ALIVE_INTERVAL)\n    light_scheduler = LightScheduler(light, lambda: publish_light_state(client))"),
    ("the scheduler is started from on_connect, reintroducing the broker "
     "dependency this whole ticket removes", MQTT,
     "    start_publisher_threads(client)",
     "    start_publisher_threads(client)\n    LightScheduler(light, lambda: publish_light_state(client)).start()"),
    ("the unit stops creating the state directory", UNIT,
     "StateDirectory=gardyn\n",
     ""),

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
        ran = re.search(r"Ran (\d+) tests", out)
        print(f"  GREEN ({ran.group(1) if ran else '?'} tests)\n")

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
            # Read WHY it died. A mutant that compiles but dies at import
            # reddens the whole suite with zero named failing cases, and
            # scoring that as a kill means the behaviour was never exercised.
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
