#!/usr/bin/env python3
"""Mutation battery for light_schedule.py (T-527.4).

    python3 tests/mutate_light_schedule.py

Proves tests/test_light_schedule.py can actually FAIL. A green suite that
cannot go red is not evidence, and this engine decides the photoperiod of a
garden attached to a Pi with no console — so "the tests pass" has to mean
something before anything ships.

MUTATES IN PLACE, which is why the restore machinery below is three-deep. The
alternative, a shutil.copytree sandbox, buys nothing here: light_schedule.py
has no dependency on its surroundings and the suite resolves the repo root
from __file__, so a copy would only add a second place for the mutant to be
left behind.

TWO MUTANTS ARE DELIBERATELY ABSENT, and saying so is the honest report — a
kill count bounds only the mutants somebody thought to write, so an unexplained
gap reads as coverage. Both were run once, both survived, and both survivors
are questions about the CODE rather than about the suite:

  * Removing the `else: break` in phase_at() is behaviour-preserving by
    construction. The boundaries are sorted, so once one compares greater than
    the target every later one does too and the `if` cannot fire again. The
    break is a readability marker for the sortedness invariant, not logic.

  * Removing the `^` from _ENTRY_RE changes nothing today, because the call
    site uses .match(), which anchors at position 0 by itself. The `^` is
    dormant insurance against a later switch to .search(); nothing can observe
    it until that switch happens, so there is no honest assertion to write.
    See the comment on _ENTRY_RE in light_schedule.py.

  * Deleting `override is not None and` from decide()'s first gate changes
    nothing, because override_is_live() performs the same None check itself.
    The clause is kept so the attribute access on the next line is provably
    safe to a reader and a type checker without holding a fact from another
    function in your head — decide()'s own comment says so.

A SECOND, INDEPENDENT REVIEW WROTE 17 MUTANTS OVER CONSTRUCTS THIS BATTERY HAD
NONE FOR, AND SEVEN SURVIVED — five of them undeclared, including both clamps
in decide() that its docstring claimed were universal. Those are now covered
above. Read that as the standing caution about any score here: a kill count
bounds only the mutants somebody thought to write, and the ones you write are
biased toward the code you were already thinking about.
"""
import atexit
import hashlib
import os
import signal
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TARGET = os.path.join(REPO, "light_schedule.py")

SUITES = ["tests.test_light_schedule"]

# (label, anchor, replacement). Every anchor must match EXACTLY ONCE in the
# file; apply_mutation refuses anything else rather than guessing, because a
# two-hit anchor mutates something nobody chose.
MUTANTS = [
    (
        "phase_at: a boundary no longer owns its own instant",
        "if boundary.at <= when:",
        "if boundary.at < when:",
    ),
    (
        "phase_at: the wrap picks the FIRST boundary instead of the last",
        "active = schedule.boundaries[-1]",
        "active = schedule.boundaries[0]",
    ),
    (
        "next_boundary_after: not strictly after, so a boundary expires itself",
        "if candidate > when",
        "if candidate >= when",
    ),
    (
        "next_boundary_after: takes the LAST boundary today, not the next one",
        "        return min(later)",
        "        return max(later)",
    ),
    (
        "next_boundary_after: wraps to today rather than tomorrow",
        "timedelta(days=1)",
        "timedelta(days=0)",
    ),
    (
        "override_is_live: expires inclusively, one instant late",
        "return now < next_boundary_after(schedule, override.applied_at)",
        "return now <= next_boundary_after(schedule, override.applied_at)",
    ),
    (
        "override_is_live: a missing override reads as live",
        "    if override is None:\n        return False",
        "    if override is None:\n        return True",
    ),
    (
        "decide: the override gate never fires",
        "if override is not None and override_is_live(schedule, override, now):",
        "if False and override is not None and override_is_live(schedule, override, now):",
    ),
    (
        "decide: the unsynced-clock gate never fires",
        "    if not clock_synced:",
        "    if clock_synced and not clock_synced:",
    ),
    (
        "decide: a persisted brightness of 0 reads as no history at all",
        "if last_applied is None:",
        "if not last_applied:",
    ),
    (
        "decide: a hold is reported to HA as if the schedule decided it",
        "return Decision(_clamped(last_applied), SOURCE_HOLD)",
        "return Decision(_clamped(last_applied), SOURCE_SCHEDULE)",
    ),
    # (The old single "returns the value unclamped" mutant is gone: _clamped's
    # min/max one-liner was replaced by explicit bound checks during the second
    # remediation, and those have a mutant each — which is strictly better,
    # since one anchor could only ever test whichever bound it happened to hit
    # first.)
    (
        "_clamped: a non-numeric value raises instead of degrading",
        "    except (TypeError, ValueError):\n        return MIN_BRIGHTNESS",
        "    except (TypeError, ValueError):\n        raise",
    ),
    (
        "Schedule.of: duplicate boundary times accepted",
        "            if at in seen:",
        "            if False:",
    ),
    (
        "Schedule.of: the sub-minute guard drops its microsecond half",
        "if at.second or at.microsecond:",
        "if at.second:",
    ),
    (
        "Schedule.of: boundaries left in the order given",
        "        built.sort(key=lambda b: b.at)",
        "        built = built",
    ),
    (
        "Schedule.of: an empty schedule is accepted",
        "        if not pairs:",
        "        if False:",
    ),
    (
        "_validated_brightness: the upper bound is dropped",
        "    if not MIN_BRIGHTNESS <= number <= MAX_BRIGHTNESS:",
        "    if not MIN_BRIGHTNESS <= number:",
    ),
    (
        "parse_schedule: a trailing comma is skipped instead of refused",
        '            raise ScheduleConfigError(f"{KEY_SCHEDULE} has an empty entry")',
        "            continue",
    ),
    (
        "parse_schedule: unpadded hours and minutes accepted",
        r'_ENTRY_RE = re.compile(r"^([0-9]{2}):([0-9]{2})=([0-9]{1,3})\Z")',
        r'_ENTRY_RE = re.compile(r"^([0-9]+):([0-9]+)=([0-9]{1,3})\Z")',
    ),
    (
        "parse_schedule: the minute bound is not checked",
        "        if hour > 23 or minute > 59:",
        "        if hour > 23:",
    ),
    (
        "parse_schedule: an empty fallback becomes 0 instead of the default",
        '    if fallback is None or str(fallback).strip() == "":',
        "    if fallback is None:",
    ),
    (
        "parse_schedule: a missing schedule key is silently substituted",
        '        raise ScheduleConfigError(f"{KEY_SCHEDULE} is missing or empty")',
        '        raw = "03:00=50"',
    ),
    (
        "DEFAULT_SCHEDULE: the unsynced fallback darkens the garden",
        "    unsynced_fallback=100,",
        "    unsynced_fallback=0,",
    ),
    (
        "MAX_BRIGHTNESS widened past what Light.set_duty_cycle accepts",
        "MAX_BRIGHTNESS = 100",
        "MAX_BRIGHTNESS = 255",
    ),
    # --- Added after review. Every one of these SURVIVED an independent
    # adversarial pass over the same code, which is the point of building a
    # remediation battery from somebody else's survivors rather than from your
    # own list: a kill count bounds only the mutants you thought of.
    (
        "_clamped: OverflowError escapes, so a huge int kills the tick thread",
        "    except OverflowError:",
        "    except ZeroDivisionError:",
    ),
    (
        "_clamped: an overflowing magnitude collapses to darkness instead of "
        "clamping, reintroducing the discontinuity at the float limit",
        "        return MAX_BRIGHTNESS if positive else MIN_BRIGHTNESS",
        "        return MIN_BRIGHTNESS",
    ),
    (
        "_clamped: the inner comparison guard re-raises",
        "            return MIN_BRIGHTNESS\n        return MAX_BRIGHTNESS if positive",
        "            raise\n        return MAX_BRIGHTNESS if positive",
    ),
    (
        "_clamped: ValueError escapes, so float('abc') kills the tick thread",
        "    except (TypeError, ValueError):\n        return MIN_BRIGHTNESS",
        "    except (TypeError,):\n        return MIN_BRIGHTNESS",
    ),
    # --- Added after the SECOND review, from the mutants it wrote and found
    # surviving. Its supplementary battery ran 17 over constructs this one had
    # none for; seven survived. That is the whole argument for building a
    # remediation battery from somebody else's list.
    (
        "_clamped: converts through int(), so '55.0' becomes a dark garden",
        "        number = float(value)",
        "        number = int(value)",
    ),
    (
        "_clamped: NaN is not caught, so it reaches int() and raises",
        "    if number != number:  # NaN, which compares false against everything",
        "    if False:  # NaN, which compares false against everything",
    ),
    (
        "_clamped: the lower bound is dropped",
        "    if number < MIN_BRIGHTNESS:\n        return MIN_BRIGHTNESS",
        "    if False:\n        return MIN_BRIGHTNESS",
    ),
    (
        "_clamped: the upper bound is dropped",
        "    if number > MAX_BRIGHTNESS:\n        return MAX_BRIGHTNESS",
        "    if False:\n        return MAX_BRIGHTNESS",
    ),
    (
        "decide: the SCHEDULE branch stops clamping (a Schedule built through "
        "the bare NamedTuple constructor carries whatever it was given)",
        "    return Decision(_clamped(phase_at(schedule, now.time())), SOURCE_SCHEDULE)",
        "    return Decision(phase_at(schedule, now.time()), SOURCE_SCHEDULE)",
    ),
    (
        "decide: the FALLBACK branch stops clamping",
        "            return Decision(_clamped(schedule.unsynced_fallback), SOURCE_FALLBACK)",
        "            return Decision(schedule.unsynced_fallback, SOURCE_FALLBACK)",
    ),
    (
        "Schedule.of: a non-iterable raises TypeError, escaping the "
        "ScheduleConfigError contract the fallback instruction depends on",
        "        except TypeError:\n            raise ScheduleConfigError(",
        "        except ZeroDivisionError:\n            raise ScheduleConfigError(",
    ),
    (
        "parse_schedule: the end anchor weakens to $, which matches before a "
        "trailing newline",
        r'_ENTRY_RE = re.compile(r"^([0-9]{2}):([0-9]{2})=([0-9]{1,3})\Z")',
        r'_ENTRY_RE = re.compile(r"^([0-9]{2}):([0-9]{2})=([0-9]{1,3})$")',
    ),
    (
        "PurityTests: the forbidden-import set quietly drops dotenv, the "
        "transitive tell for config.py",
        '    FORBIDDEN = frozenset(\n'
        '        {"gpiozero", "pigpio", "paho", "flask", "dotenv", "mqtt", "app", "config"}\n'
        "    )",
        '    FORBIDDEN = frozenset(\n'
        '        {"gpiozero", "pigpio", "paho", "flask", "mqtt", "app", "config"}\n'
        "    )",
    ),
    (
        "parse_schedule: the entry pattern loses its end anchor",
        r'_ENTRY_RE = re.compile(r"^([0-9]{2}):([0-9]{2})=([0-9]{1,3})\Z")',
        r'_ENTRY_RE = re.compile(r"^([0-9]{2}):([0-9]{2})=([0-9]{1,3})")',
    ),
    (
        "parse_schedule: digits widen to Unicode decimals again",
        r'_ENTRY_RE = re.compile(r"^([0-9]{2}):([0-9]{2})=([0-9]{1,3})\Z")',
        r'_ENTRY_RE = re.compile(r"^(\d{2}):(\d{2})=(\d{1,3})\Z")',
    ),
    (
        "parse_schedule: the missing-key guard is dropped (the empty-entry "
        "check still raises, so only the MESSAGE distinguishes them)",
        '        raise ScheduleConfigError(f"{KEY_SCHEDULE} is missing or empty")',
        "        pass",
    ),
    (
        "next_boundary_after: candidate loses the minute, so every off-the-hour "
        "boundary collapses to the top of its hour",
        "today.replace(hour=b.at.hour, minute=b.at.minute)",
        "today.replace(hour=b.at.hour)",
    ),
    (
        "Schedule.of: pairs is not materialised, so an exhausted iterator "
        "builds an empty schedule",
        "            pairs = tuple(pairs)",
        "            pairs = pairs",
    ),
    (
        "PurityTests: the probe is redirected at an innocuous module",
        '        hits, pulled = self._forbidden_names_pulled_by("import light_schedule")',
        '        hits, pulled = self._forbidden_names_pulled_by("import json")',
    ),
    (
        "PurityTests: the forbidden-import set is emptied",
        '    FORBIDDEN = frozenset(\n'
        '        {"gpiozero", "pigpio", "paho", "flask", "dotenv", "mqtt", "app", "config"}\n'
        "    )",
        "    FORBIDDEN = frozenset(\n        set()\n    )",
    ),
    (
        "PurityTests: the forbidden-import set quietly drops the GPIO driver",
        '    FORBIDDEN = frozenset(\n'
        '        {"gpiozero", "pigpio", "paho", "flask", "dotenv", "mqtt", "app", "config"}\n'
        "    )",
        '    FORBIDDEN = frozenset(\n'
        '        {"gpiozero", "paho", "flask", "dotenv", "mqtt", "app", "config"}\n'
        "    )",
    ),
]

# The negative control. Must score RED or the scorer cannot tell pass from
# fail and every "killed" above is meaningless.
CONTROL_B = (
    "CONTROL B: the schedule branch always returns darkness",
    "    return Decision(_clamped(phase_at(schedule, now.time())), SOURCE_SCHEDULE)",
    "    return Decision(0, SOURCE_SCHEDULE)",
)

# The one mutant that lives in the TEST file rather than the module: see
# MUTANTS' last entry. Kept in the same battery because PurityTests is the only
# guard on the module's single architectural promise, so a battery that could
# not perturb it would certify that promise on the strength of a test nobody
# had shown could fail.
TEST_FILE = os.path.join(REPO, "tests", "test_light_schedule.py")

_ORIGINALS = {}


def purge_pycache():
    """CPython validates a .pyc on (mtime-seconds, size).

    A mutant applied and reverted inside one second, preserving size, would
    otherwise re-run the PREVIOUS bytecode and return a confident verdict for
    code that never executed. -B suppresses writing a cache, not reading a
    stale one.
    """
    for root, dirs, _ in os.walk(REPO):
        if ".git" in root:
            continue
        for name in list(dirs):
            if name == "__pycache__":
                for entry in os.scandir(os.path.join(root, name)):
                    os.unlink(entry.path)


def _path_for(anchor):
    """Which file an anchor belongs to.

    Only the PurityTests mutants live in the test file; everything else is in
    the module. Keyed on names that appear nowhere in light_schedule.py rather
    than on a hand-kept index, so adding a mutant cannot silently point at the
    wrong file. A wrong route is not silent anyway - the anchor then matches
    zero times and apply_mutation refuses with no verdict - but a refusal
    reads as a harness bug at the moment you least want to debug one.
    """
    test_only = ("FORBIDDEN = frozenset", "_forbidden_names_pulled_by")
    return TEST_FILE if any(name in anchor for name in test_only) else TARGET


def restore():
    """Put every mutated file back, content AND mode, whatever happened."""
    for path, (src, mode) in _ORIGINALS.items():
        try:
            with open(path, "r") as fh:
                if fh.read() == src:
                    os.chmod(path, mode)
                    continue
        except OSError:
            pass
        with open(path, "w") as fh:
            fh.write(src)
        os.chmod(path, mode)


def _on_signal(signum, _frame):
    # try/finally does not cover a signal and atexit does not either.
    restore()
    sys.exit(128 + signum)


def apply_mutation(anchor, replacement):
    """Return True if applied. Refuses anything that is not an exact single hit."""
    path = _path_for(anchor)
    src = open(path).read()
    count = src.count(anchor)
    if count != 1:
        print(f"  ANCHOR MATCHED {count} TIMES - mutation NOT applied, no verdict")
        return False
    mutated = src.replace(anchor, replacement)
    if mutated == src:
        print("  replacement changed nothing - mutation NOT applied, no verdict")
        return False

    # Compiled BEFORE the write, so invalid Python never reaches disk. A mutant
    # that does not compile makes the suite die at collection and would score
    # as KILLED while the behaviour it was written for never ran.
    try:
        compile(mutated, path, "exec")
    except SyntaxError as exc:
        print(
            f"  MUTANT IS NOT VALID PYTHON ({exc.msg}, line {exc.lineno}) - no "
            f"verdict; a syntax error reddens the suite for the wrong reason"
        )
        return False

    with open(path, "w") as fh:
        fh.write(mutated)
    return True


def run_suites():
    """Return (all_passed, combined_output). stderr merged - unittest uses it."""
    purge_pycache()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    combined = []
    ok = True
    for suite in SUITES:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", suite],
            cwd=REPO,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        combined.append(f"--- {suite} (rc={proc.returncode}) ---\n{proc.stdout}")
        ok = ok and proc.returncode == 0
    return ok, "\n".join(combined)


def main():
    """Three restore paths, none of which subsumes another.

      try/finally      an exception out of the run, including the
                       KeyboardInterrupt tests/test_suite_isolation.py injects.
      atexit           a clean exit down a path that skips the finally.
      SIGTERM/SIGINT   a signal, which runs NEITHER of the above.
    """
    for path in (TARGET, TEST_FILE):
        _ORIGINALS[path] = (open(path).read(), os.stat(path).st_mode)
    atexit.register(restore)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    try:
        return _run()
    finally:
        restore()


def _run():
    print("=" * 72)
    print("CONTROL A (positive, runs FIRST as a gate) - clean tree must be GREEN")
    print("=" * 72)
    ok, out = run_suites()
    if not ok:
        print(out)
        print("\nCONTROL A FAILED - the suite does not pass on a clean tree.")
        print("This is NO DATA, not a score. Fix the suite before reading anything.")
        return 1
    print("  GREEN - the suite passes on the clean tree\n")

    print("=" * 72)
    print("CONTROL B (negative) - a deliberately broken tree must be RED")
    print("=" * 72)
    label, anchor, replacement = CONTROL_B
    print(f"  {label}")
    if not apply_mutation(anchor, replacement):
        print("\nCONTROL B could not be applied - the scorer is unproven. NO DATA.")
        restore()
        return 1
    ok, _ = run_suites()
    restore()
    if ok:
        print("  GREEN - but it MUST be RED.")
        print("\nCONTROL B FAILED - the scorer cannot tell pass from fail.")
        print("Every 'killed' below would be meaningless. NO DATA.")
        return 1
    print("  RED - the scorer can distinguish pass from fail\n")

    print("=" * 72)
    print(f"{len(MUTANTS)} MUTANTS")
    print("=" * 72)
    killed, survived, unapplied, broad = [], [], [], []
    for i, (label, anchor, replacement) in enumerate(MUTANTS, 1):
        print(f"[{i}/{len(MUTANTS)}] {label}")
        if not apply_mutation(anchor, replacement):
            unapplied.append(label)
            restore()
            continue
        ok, out = run_suites()
        restore()
        if ok:
            print("  SURVIVED - no test noticed")
            survived.append(label)
            continue

        # Read WHY it died, not just the colour, and ACT on the reading. A
        # review of this harness's sibling found the tell being printed and
        # then ignored: a mutant that compiles but dies at IMPORT reddens every
        # suite with zero named failing cases, and was still appended to
        # `killed`, so the headline count never moved. The compile() gate above
        # cannot catch that class - the mutant is valid Python - so the check
        # has to live here.
        fails = [
            line for line in out.splitlines() if line.startswith(("FAIL:", "ERROR:"))
        ]
        if not fails:
            print("  NO VERDICT - the suite went red with no named failing case,")
            print("  which means the module died at collection or import rather")
            print("  than the behaviour being noticed. This is NOT a kill.")
            broad.append(label)
            continue
        print(f"  killed ({len(fails)} failing case(s))")
        for line in fails[:3]:
            print(f"      {line}")
        killed.append(label)

    print("\n" + "=" * 72)
    print(
        f"RESULT: {len(killed)} killed, {len(survived)} survived, "
        f"{len(unapplied)} not applied, {len(broad)} no-verdict, of {len(MUTANTS)}"
    )
    if broad:
        print("\nNO VERDICT - red for a broad reason, so the behaviour these were")
        print("written for was never exercised. Treat as untested, not as killed:")
        for label in broad:
            print(f"  - {label}")
    if survived:
        print("\nSURVIVORS - each is a question about the CORPUS and the CODE, not")
        print("only about the assertion. Ask in order: does any real input reach")
        print("this construct; is the code redundant; is the test weak.")
        for label in survived:
            print(f"  - {label}")
    if unapplied:
        print("\nNOT APPLIED - no verdict was reached for these:")
        for label in unapplied:
            print(f"  - {label}")
    print("=" * 72)

    # Byte-identity, asserted rather than assumed. A wait loop proves a process
    # exited, not that its cleanup ran, so this line is the evidence.
    clean = True
    for path, (src, mode) in _ORIGINALS.items():
        on_disk = open(path).read()
        same = hashlib.sha256(on_disk.encode()).hexdigest() == hashlib.sha256(
            src.encode()
        ).hexdigest()
        mode_ok = os.stat(path).st_mode == mode
        print(
            f"RESTORED {os.path.relpath(path, REPO)}: "
            f"content {'identical' if same else 'DIFFERS'}, "
            f"mode {'identical' if mode_ok else 'DIFFERS'}"
        )
        clean = clean and same and mode_ok

    if not clean:
        print("TREE NOT RESTORED - fix before trusting any score above.")
        return 1
    return 0 if not survived and not unapplied and not broad else 1


if __name__ == "__main__":
    sys.exit(main())
