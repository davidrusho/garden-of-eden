#!/usr/bin/env python3
"""Mutation battery for test_light_logging.py.

The suite it scores exists to prove light commands are RECORDED. That is an
assertion about an absence being fixed, which is exactly where a dead test and
a real pass produce identical output - so the suite is worthless until it has
been shown capable of going red.

Run:  python3 tests/mutate_light_logging.py

THREE controls gate every result, and ALL must hold before any mutant verdict
is read. A battery scores a mutant by whether the test run FAILED, so a broken
scorer reports every mutant caught - the most reassuring output available:

  CONTROL A  clean tree               -> must be GREEN
  CONTROL B  broken assertion         -> must be RED
             (A alone is worthless; it is scored by the same path that may be
             broken, so only B proves the scorer can distinguish the two.)
  CONTROL C  compiles, dies at import -> must score NO VERDICT
             (a POSITIVE control for the scoring rule: without it, "0
             no-verdict mutants" is equally consistent with the rule working
             and with the rule never being reachable.)

EACH PHASE CARRIES ITS OWN THREE. This file mutates two files against two
different suites, so a single set of controls at the top would leave the whole
mqtt.py half scored by a path nothing had proved - and its clean-tree ran-count
is a different number, which is the figure the no-verdict rule compares
against.

CONTROL C AND THE SCORING RULE ARRIVED UNDER T-527.32. Before that this file
treated ANY red run as a kill and reported 14/14 on that basis, which was not
evidence: a mutant that compiles and then dies at import reddens the suite
without the behaviour under test ever executing. The rule now lives in
tests/mutation_scoring.py - this was the third harness found with the same
defect - and is pinned by tests/test_mutation_scoring.py.

Mechanics that have bitten this repo before, all handled here:
  * every mutant gated on compile() BEFORE the write, so invalid Python never
    reaches disk - not after, which leaves broken source there for the length
    of a call, in the exact window the restore handlers exist to close.
  * __pycache__ purged before every run, and PYTHONDONTWRITEBYTECODE=1 set.
    .pyc validity keys on (mtime-seconds, size), so a mutation applied and
    reverted inside one second can silently re-run the previous bytecode. This
    file loads its target via spec_from_file_location, where that bites hardest.
  * stderr merged into stdout - unittest reports there, and 2>/dev/null would
    blank the very output being grepped.
  * each mutated file compared against the source captured before the run,
    because a replacement that matches nothing is indistinguishable from one
    that changed nothing.
  * restore on try/finally AND atexit AND SIGTERM/SIGINT. None subsumes
    another: `finally` covers an exception, atexit covers an exit down a path
    that skips it, and a signal runs neither.
  * the tree is asserted byte-identical at the end. Read that line before
    believing any score above it - a run that exited is not a run whose
    cleanup ran.
"""
import atexit
import hashlib
import os
import signal
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.mutation_scoring import (  # noqa: E402
    NO_VERDICT, SURVIVED, compile_gate, format_verdict, purge_pycache,
    ran_count, score_run, sha)

LIGHT = os.path.join(REPO, "app", "sensors", "light", "light.py")
MQTT = os.path.join(REPO, "mqtt.py")
SUITE = "tests.test_light_logging"
MQTT_SUITE = "tests.test_water_interlock"

# Each mutant reintroduces a real regression. Note the mix: some BREAK present
# code, one REINTRODUCES deleted code (the bare root call), and one applies the
# plausible-but-wrong fix (blanket INFO). A suite that only tolerates an absence
# will not notice the absent thing coming back.
MUTANTS = [
    ("drop the module-owned level",
     "logger.setLevel(logging.INFO)",
     "pass  # level removed"),
    ("revert 'turning on' to the bare root logger",
     'logger.info("Turning light on")',
     'logging.info("Turning light on")'),
    ("revert 'turning off' to the bare root logger",
     'logger.info("Turning light off")',
     'logging.info("Turning light off")'),
    ("revert the brightness record to the bare root logger",
     'logger.info(f"Setting light duty_cycle to {duty_cycle_percentage}%")',
     'logging.info(f"Setting light duty_cycle to {duty_cycle_percentage}%")'),
    ("revert the no-op re-assert record to the bare root logger",
     'logger.info("Light already on, skipping")',
     'logging.info("Light already on, skipping")'),
    ("the plausible-but-WRONG fix: blanket INFO on the root",
     "logger.setLevel(logging.INFO)",
     "logging.getLogger().setLevel(logging.INFO)"),
    ("firehose regression: read path back to INFO",
     'logger.debug(f"Light duty_cycle is {duty_cycle}%")',
     'logger.info(f"Light duty_cycle is {duty_cycle}%")'),
    ("silence the read path entirely (demoted too far)",
     'logger.debug(f"Light duty_cycle is {duty_cycle}%")',
     "pass  # read record removed"),
]


# --- mqtt.py half -----------------------------------------------------------
#
# A battery is evidence only for the code it MUTATES. The list above scores
# light.py; every one of these four regressions previously left the suite GREEN,
# because the deployed policy in mqtt.py had no coverage at all. The first is
# the one that matters: `basicConfig(level=INFO)` is the blanket fix a person
# would actually write, and it lives HERE, not in light.py where the light
# battery tests for it.
MQTT_MUTANTS = [
    ("the REAL blanket-INFO fix, in the file someone would write it",
     "    level=logging.WARNING,",
     "    level=logging.INFO,"),
    ("revert the service logger to WARNING (the original bug)",
     "logger.setLevel(logging.INFO)",
     "logger.setLevel(logging.WARNING)"),
    ("raise the handlers above INFO, silencing everything invisibly",
     "    force=True,\n)",
     "    force=True,\n)\nfor _h in logging.getLogger().handlers:\n    _h.setLevel(logging.WARNING)"),
    ("disable logging globally - no logger or handler level reflects it",
     "    force=True,\n)",
     "    force=True,\n)\nlogging.disable(logging.INFO)"),
    ("re-bury the inbound decode that identifies a replayed command",
     'logger.info(f"Decoded payload on {topic!r}',
     'logger.debug(f"Decoded payload on {topic!r}'),
    ("re-promote a periodic publisher, re-burying the command record",
     'logger.debug(f"Captured+published {label} camera',
     'logger.info(f"Captured+published {label} camera'),
]


# (file, suite, label, anchor, replacement) for each phase's controls.
#
# CONTROL C is a MISSING IMPORT, not a typo'd name: a typo raises only because
# that particular statement happens to execute at module scope, so moving it
# into a function would silently stop the control testing anything while it
# still looked correct. An import statement cannot be moved out of the import.
# Each anchor is a module-scope statement, so the appended import runs at import
# time. Both compile - so they reach disk past the compile gate - and then die
# when unittest imports the module, which unittest reports as a NAMED error via
# `unittest.loader._FailedTest`. That is exactly why the zero-named-cases tell
# alone cannot see this shape, and why score_run compares the COLLECTED count.
CONTROL_B = (LIGHT, SUITE, "CONTROL B: a broken assertion - must be RED",
             'logger.info("Turning light on")',
             'logger.info("THIS_STRING_IS_NOT_WHAT_THE_TEST_EXPECTS")')
CONTROL_C = (LIGHT, SUITE,
             "CONTROL C: compiles, dies at import - must score NO VERDICT",
             "logger = logging.getLogger(__name__)",
             "logger = logging.getLogger(__name__)\n"
             "import a_module_that_certainly_does_not_exist")
MQTT_CONTROL_B = (MQTT, MQTT_SUITE,
                  "CONTROL B: a broken policy - must be RED",
                  "logger.setLevel(logging.INFO)",
                  "logger.setLevel(logging.CRITICAL)")
MQTT_CONTROL_C = (MQTT, MQTT_SUITE,
                  "CONTROL C: compiles, dies at import - must score NO VERDICT",
                  "_publisher_threads_lock = threading.Lock()",
                  "_publisher_threads_lock = threading.Lock()\n"
                  "import a_module_that_certainly_does_not_exist")

_ORIGINALS = {}


def run_suite(suite=SUITE):
    """Return (passed, combined_output). stderr merged - unittest writes there."""
    purge_pycache(REPO)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", suite],
        cwd=REPO, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    out = proc.stdout
    return proc.returncode == 0, out


def main():
    """Three restore paths, none of which subsumes another.

      try/finally      an exception out of the run, including the
                       KeyboardInterrupt tests/test_suite_isolation.py injects.
                       Restores SYNCHRONOUSLY, before the exception leaves.
      atexit           a clean exit down a path that skips the finally.
      SIGTERM/SIGINT   a signal, which runs NEITHER of the above.
    """
    _ORIGINALS.update({LIGHT: open(LIGHT).read(), MQTT: open(MQTT).read()})
    atexit.register(restore)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    try:
        return _run()
    finally:
        restore()


def _on_signal(signum, _frame):
    restore()
    sys.exit(128 + signum)


def apply_mutation(path, anchor, replacement):
    """Return True if applied. Refuses anything that is not an exact single hit."""
    src = open(path).read()
    count = src.count(anchor)
    if count != 1:
        print(f"  ANCHOR MATCHED {count} TIMES in {os.path.basename(path)} - "
              f"mutation NOT applied, no verdict")
        return False
    mutated = src.replace(anchor, replacement, 1)
    if mutated == src:
        print("  replacement changed nothing - mutation NOT applied, no verdict")
        return False
    refusal = compile_gate(mutated, path)
    if refusal:
        print(f"  {refusal}")
        return False
    with open(path, "w") as fh:
        fh.write(mutated)
    return True


def _controls(path, suite, control_b, control_c, phase):
    """Run this phase's three controls. Returns the clean ran-count, or None.

    None means NO DATA for the phase - the caller must abort rather than score
    anything, because every verdict below a failed control is meaningless.
    """
    print("=" * 68)
    print(f"{phase} CONTROL A (positive, runs FIRST as a gate) - clean tree "
          f"must be GREEN")
    ok, out = run_suite(suite)
    if not ok:
        print(out[-2500:])
        print(f"\n{phase} CONTROL A FAILED - {suite} does not pass on a clean "
              f"tree. This is NO DATA, not a score.")
        return None
    clean_ran = ran_count(out)
    if clean_ran == 0:
        print(f"\n{phase} CONTROL A FAILED - a green run that collected NO "
              f"tests. NO DATA.")
        return None
    print(f"  GREEN - {suite} passes on the clean tree ({clean_ran} tests)")

    print(f"{phase} CONTROL B (negative) - a deliberately broken tree must be RED")
    if not apply_mutation(path, control_b[3], control_b[4]):
        print(f"\n{phase} CONTROL B could not be applied - the scorer is "
              f"unproven. NO DATA.")
        restore()
        return None
    ok_b, _ = run_suite(suite)
    restore()
    if ok_b:
        print(f"\n{phase} CONTROL B FAILED - the suite passed a deliberately "
              f"broken tree, so the scorer cannot tell pass from fail. Every "
              f"'killed' below would be meaningless. NO DATA.")
        return None
    print("  RED - the scorer can distinguish pass from fail")

    print(f"{phase} CONTROL C (positive, for the no-verdict rule) - an "
          f"import-time break that COMPILES must score NO VERDICT")
    if not apply_mutation(path, control_c[3], control_c[4]):
        print(f"\n{phase} CONTROL C could not be applied. NO DATA.")
        restore()
        return None
    ok_c, out_c = run_suite(suite)
    restore()
    verdict, fails = score_run(ok_c, out_c, clean_ran)
    if verdict != NO_VERDICT:
        print(f"  scored '{verdict}' with {len(fails)} named failing case(s), "
              f"but it MUST score '{NO_VERDICT}'.")
        print(f"\n{phase} CONTROL C FAILED - either the scoring rule is not "
              f"doing its job, or this mutant no longer reproduces the shape "
              f"it was written for. NO DATA.")
        return None
    print("  NO VERDICT - an import-time break is not counted as a kill\n")
    return clean_ran


def _score_phase(path, suite, mutants, clean_ran, tag, buckets):
    """Apply and score one phase's mutants into the shared buckets."""
    killed, survived, unapplied, no_verdict = buckets
    for i, (label, old, new) in enumerate(mutants, 1):
        print(f"[{tag}{i}/{len(mutants)}] {label}")
        if not apply_mutation(path, old, new):
            unapplied.append(label)
            restore()
            continue
        ok_m, out_m = run_suite(suite)
        restore()
        # Read WHY it died, not just the colour.
        verdict, fails = score_run(ok_m, out_m, clean_ran)
        print(format_verdict(verdict, fails))
        if verdict == SURVIVED:
            survived.append(label)
        elif verdict == NO_VERDICT:
            no_verdict.append(label)
        else:
            for line in fails[:3]:
                print(f"      {line}")
            killed.append(label)


def restore():
    """Put every mutated file back, whatever happened.

    Without this a battery killed part-way through - ^C, a timeout, an
    exception in the harness itself - leaves a mutant applied in the working
    tree, which is a silent and plausible-looking change to shipping code.
    Both files here run the garden: light.py drives the grow light and mqtt.py
    is the controller.
    """
    errors = []
    for path, text in _ORIGINALS.items():
        # Each file independently: one loop abandons the rest the moment one
        # raises, and it raises out of a `finally`, so the second file stays
        # mutated with nothing reporting it.
        if sha(path) == hashlib.sha256(text.encode()).hexdigest():
            continue
        try:
            with open(path, "w") as fh:
                fh.write(text)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    if errors:
        raise OSError("could not restore: " + "; ".join(errors))


def _run():
    """Two phases, each with its own three controls and its own baseline.

    The phases target DIFFERENT files against DIFFERENT suites, so a single
    clean ran-count would be the wrong comparison for one of them - and the
    ran-count is what the no-verdict rule reads. A phase whose controls do not
    hold aborts the whole run rather than being scored, because a battery that
    reports part of a score reads as a complete one.
    """
    shas = {p: sha(p) for p in _ORIGINALS}

    clean_ran = _controls(LIGHT, SUITE, CONTROL_B, CONTROL_C, "[light.py]")
    if clean_ran is None:
        return 2

    print("=" * 68)
    print(f"{len(MUTANTS)} light.py MUTANTS (scored by {SUITE})")
    print("=" * 68)
    buckets = ([], [], [], [])
    _score_phase(LIGHT, SUITE, MUTANTS, clean_ran, "", buckets)

    mqtt_clean_ran = _controls(MQTT, MQTT_SUITE, MQTT_CONTROL_B, MQTT_CONTROL_C,
                               "[mqtt.py]")
    if mqtt_clean_ran is None:
        return 2

    print("=" * 68)
    print(f"{len(MQTT_MUTANTS)} mqtt.py policy MUTANTS (scored by {MQTT_SUITE})")
    print("=" * 68)
    _score_phase(MQTT, MQTT_SUITE, MQTT_MUTANTS, mqtt_clean_ran, "m", buckets)

    killed, survived, unapplied, no_verdict = buckets
    total = len(MUTANTS) + len(MQTT_MUTANTS)
    print("\n" + "=" * 68)
    print(f"RESULT: {len(killed)} killed, {len(survived)} survived, "
          f"{len(no_verdict)} no verdict, {len(unapplied)} not applied, "
          f"of {total}")
    print("=" * 68)
    if survived:
        print("\nSURVIVORS - a survivor is a question about the CODE, the "
              "CORPUS and the\nHARNESS, not only about the suite: the test may "
              "be weak, the mutation may\nnever have applied, the mutated code "
              "may be redundant, or the construct may\nnot exist in the corpus "
              "the suites actually read.")
    for label in survived:
        print(f"  SURVIVED  {label}")
    for label in no_verdict:
        print(f"  NO VERDICT  {label}")
    for label in unapplied:
        print(f"  NOT APPLIED  {label}")

    # The byte-identity assertion. Read this line before believing any score
    # above it - a run that exited is not a run whose cleanup ran.
    drifted = [p for p in _ORIGINALS
               if shas[p] is None or sha(p) != shas[p]]
    if not drifted:
        print("\nTREE RESTORED: light.py and mqtt.py are byte-identical to "
              "their pre-run state.")
    else:
        print(f"\n*** TREE NOT RESTORED - {', '.join(drifted)} DIFFER. "
              f"Fix before committing. ***")
        return 1
    return 0 if not (survived or unapplied or no_verdict) else 1


if __name__ == "__main__":
    sys.exit(main())
