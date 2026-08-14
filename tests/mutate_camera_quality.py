#!/usr/bin/env python3
"""Mutation battery for the fswebcam JPEG quality setting (T-478).

The defect being fixed IS AN ABSENCE - fswebcam was invoked with no `--jpeg`
flag at all - which makes this suite unusually easy to write uselessly. A test
that checks "the capture succeeded" or "the resolution is in the argv" passes
perfectly with the flag deleted again, because deleting it produces no error,
no log line and a visually identical photo. It only costs 4.4x the bytes.

So mutant [1] is the whole point of this file: remove `--jpeg` and confirm a
test goes RED. Everything else is supporting fire.

Run:  python3 tests/mutate_camera_quality.py

THREE controls gate every result and ALL must hold before any verdict is read.
A battery scores a mutant by whether the test run FAILED, so a broken scorer
reports every mutant caught - the most reassuring output available:

  CONTROL A  clean tree               -> must be GREEN
  CONTROL B  deliberately broken code -> must be RED
             (A alone is worthless: it is scored by the same path that may be
             broken, so only B proves the scorer can tell pass from fail.)
  CONTROL C  compiles, dies at import -> must score NO VERDICT
             (a POSITIVE control for the scoring rule: without it, "0
             no-verdict mutants" is equally consistent with the rule working
             and with the rule never being reachable.)

CONTROL C AND THE SCORING RULE ARRIVED HERE UNDER T-527.32. Before that this
file treated ANY red run as a kill and reported 17/17 on that basis, which was
not evidence: a mutant that compiles and then dies at import reddens every
suite without the behaviour under test ever executing. The rule lives in
tests/mutation_scoring.py, because this was the third harness in this repo
found with the same defect, and it is pinned by tests/test_mutation_scoring.py.

__pycache__ is purged before every run and PYTHONDONTWRITEBYTECODE=1 is set;
stderr is merged into stdout because unittest reports there; every anchor must
match exactly once; every mutant is gated on compile() BEFORE the write, so
invalid Python never reaches disk; restore runs on try/finally AND atexit AND
SIGTERM/SIGINT, none of which subsumes another; and the tree is asserted
byte-identical at the end.
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

MQTT = os.path.join(REPO, "mqtt.py")
CONFIG = os.path.join(REPO, "config.py")

SUITES = ["tests.test_camera_quality", "tests.test_water_interlock",
          "tests.test_retired_entities"]

# (file, label, anchor, replacement)
MUTANTS = [
    (MQTT,
     "REMOVE --jpeg entirely - the original bug, and a silent one",
     "'fswebcam', '-d', device, '-r', resolution, '--jpeg', str(quality),",
     "'fswebcam', '-d', device, '-r', resolution,"),

    (MQTT,
     "drop only the VALUE, leaving a dangling --jpeg flag",
     "'-r', resolution, '--jpeg', str(quality),",
     "'-r', resolution, '--jpeg',"),

    (MQTT,
     "hardcode the quality, ignoring the setting",
     "'--jpeg', str(quality),",
     "'--jpeg', '85',"),

    (MQTT,
     "pass the quality as an int, which subprocess rejects",
     "'--jpeg', str(quality),",
     "'--jpeg', quality,"),

    (MQTT,
     "give both cameras the UPPER quality, defeating the per-camera override",
     "LOWER_CAMERA_RESOLUTION, LOWER_CAMERA_JPEG_QUALITY,",
     "LOWER_CAMERA_RESOLUTION, UPPER_CAMERA_JPEG_QUALITY,"),

    (MQTT,
     "swap resolution and quality at the call site",
     "UPPER_CAMERA_RESOLUTION, UPPER_CAMERA_JPEG_QUALITY,",
     "UPPER_CAMERA_JPEG_QUALITY, UPPER_CAMERA_RESOLUTION,"),

    (CONFIG,
     "change the default from 85 to the out-of-range 255",
     "_JPEG_QUALITY_DEFAULT = 85",
     "_JPEG_QUALITY_DEFAULT = 255"),

    (CONFIG,
     "drop the range check, so 255 is accepted from the environment",
     "    if not _JPEG_QUALITY_MIN <= value <= _JPEG_QUALITY_MAX:",
     "    if False:"),

    (CONFIG,
     "widen the ceiling past fswebcam's documented 95",
     "_JPEG_QUALITY_MAX = 95",
     "_JPEG_QUALITY_MAX = 100"),

    (CONFIG,
     "admit -1, the 'automatic' factor that IS the bug on ARM",
     "_JPEG_QUALITY_MIN = 0",
     "_JPEG_QUALITY_MIN = -1"),

    (CONFIG,
     "raise on an unparseable value instead of falling back (crash loop)",
     "    except ValueError:\n"
     "        logging.getLogger(__name__).error(\n"
     '            "Unparseable %s=%r; using %s", var, raw, default)\n'
     "        return default",
     "    except ValueError:\n"
     "        raise"),

    (CONFIG,
     "ignore the per-camera override, collapsing both to the shared value",
     '    "LOWER_CAMERA_JPEG_QUALITY", CAMERA_JPEG_QUALITY)',
     '    "CAMERA_JPEG_QUALITY", CAMERA_JPEG_QUALITY)'),

    # --- the per-camera isolation (T-491) -----------------------------------
    #
    # A battery is evidence only for the code it MUTATES, and every mutant
    # above is about --jpeg. _capture_and_publish's docstring makes a second,
    # load-bearing claim - that a failing camera cannot block the other's
    # publish - and nothing scored it: replacing both except bodies with a bare
    # `raise` left all three suites green.
    (MQTT,
     "let a subprocess failure propagate, taking the other camera down with it",
     '        logger.error(f"{label} camera capture failed ({device}): {e}")',
     "        raise"),

    (MQTT,
     "let an UNEXPECTED error propagate - the catch-all branch",
     '        logger.exception(f"Unexpected error during {label} image capture/publish")',
     "        raise"),

    (MQTT,
     "swallow a failing camera silently, so an outage leaves no record",
     '    except subprocess.CalledProcessError as e:\n'
     '        logger.error(f"{label} camera capture failed ({device}): {e}")',
     "    except subprocess.CalledProcessError as e:\n"
     "        pass"),

    (MQTT,
     "capture the flaky lower camera FIRST, so a failure there is the one that "
     "blocks the healthy one",
     '        _capture_and_publish(client, "upper", UPPER_CAMERA_DEVICE,',
     '        _capture_and_publish(client, "lower", UPPER_CAMERA_DEVICE,'),

    (MQTT,
     "publish an empty body rather than the captured frame",
     "            client.publish(BASE_TOPIC + topic, payload=f.read(), qos=0, retain=False)",
     "            client.publish(BASE_TOPIC + topic, payload=b'', qos=0, retain=False)"),
]


# (file, label, anchor, replacement) - same shape as MUTANTS, so the same
# apply/score path drives them and a control cannot drift from a mutant.
CONTROL_B = (MQTT, "CONTROL B: a deliberately broken tree - must be RED",
             "'--jpeg', str(quality),", "'--CONTROL-B-BROKEN', str(quality),")

# CONTROL C - the gap compile() does NOT close.
#
# A MISSING IMPORT, not a typo'd name: a typo raises only because that
# particular statement happens to execute at module scope, so moving it into a
# function would silently stop the control testing anything while it still
# looked correct. An import statement cannot be moved out of the import.
#
# Anchored on a module-scope assignment so the appended import runs at import
# time. This compiles - so it reaches disk past the compile gate - and then
# dies when unittest imports the module, which unittest reports as a NAMED
# error through `unittest.loader._FailedTest`. That is why the zero-named-cases
# tell alone cannot see this shape and score_run compares the COLLECTED count.
CONTROL_C = (MQTT,
             "CONTROL C: compiles, dies at import - must score NO VERDICT",
             "_publisher_threads_lock = threading.Lock()",
             "_publisher_threads_lock = threading.Lock()\n"
             "import a_module_that_certainly_does_not_exist")

_ORIGINALS = {}


def run_suites():
    purge_pycache(REPO)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    combined, ok = [], True
    for suite in SUITES:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", suite],
            cwd=REPO, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        )
        combined.append(f"--- {suite} (rc={proc.returncode}) ---\n{proc.stdout}")
        ok = ok and proc.returncode == 0
    return ok, "\n".join(combined)


def restore():
    """Put every mutated file back, whatever happened.

    Without this a battery killed part-way through - ^C, a timeout, an
    exception in the harness itself - leaves a mutant applied in the working
    tree, which is a silent and entirely plausible-looking change to shipping
    code. mqtt.py is the file that runs the garden.
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


def main():
    """Three restore paths, none of which subsumes another.

      try/finally      an exception out of the run, including the
                       KeyboardInterrupt tests/test_suite_isolation.py injects.
                       Restores SYNCHRONOUSLY, before the exception leaves.
      atexit           a clean exit down a path that skips the finally.
      SIGTERM/SIGINT   a signal, which runs NEITHER of the above.
    """
    _ORIGINALS.update({MQTT: open(MQTT).read(), CONFIG: open(CONFIG).read()})
    atexit.register(restore)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    try:
        return _run()
    finally:
        restore()


def _run():
    shas = {p: sha(p) for p in _ORIGINALS}

    print("=" * 70)
    print("CONTROL A (positive, runs FIRST as a gate) - clean tree must be GREEN")
    print("=" * 70)
    ok, out = run_suites()
    if not ok:
        print(out[-3000:])
        print("\nCONTROL A FAILED - the suites do not pass on a clean tree.")
        print("This is NO DATA, not a score. Fix the suites before reading "
              "anything.")
        return 2
    clean_ran = ran_count(out)
    if clean_ran == 0:
        print("\nCONTROL A FAILED - a green run that collected NO tests. NO DATA.")
        return 2
    print(f"  GREEN - suites pass on the clean tree ({clean_ran} tests)\n")

    print("=" * 70)
    print("CONTROL B (negative) - a deliberately broken tree must be RED")
    print("=" * 70)
    path, _, anchor, replacement = CONTROL_B
    if not apply_mutation(path, anchor, replacement):
        print("\nCONTROL B could not be applied - the scorer is unproven. NO DATA.")
        restore()
        return 2
    ok_b, _ = run_suites()
    restore()
    if ok_b:
        print("  GREEN - but it MUST be RED.")
        print("\nCONTROL B FAILED - the scorer cannot tell pass from fail.")
        print("Every 'killed' below would be meaningless. NO DATA.")
        return 2
    print("  RED - the scorer can distinguish pass from fail\n")

    print("=" * 70)
    print("CONTROL C (positive, for the no-verdict rule) - an import-time break")
    print("that COMPILES must be classified NO VERDICT, never a kill")
    print("=" * 70)
    path, _, anchor, replacement = CONTROL_C
    if not apply_mutation(path, anchor, replacement):
        print("\nCONTROL C could not be applied. NO DATA.")
        restore()
        return 2
    ok_c, out_c = run_suites()
    restore()
    verdict, fails = score_run(ok_c, out_c, clean_ran)
    if verdict != NO_VERDICT:
        print(f"  scored '{verdict}' with {len(fails)} named failing case(s), "
              f"but it MUST score '{NO_VERDICT}'.")
        print("\nCONTROL C FAILED - either the scoring rule is not doing its "
              "job, or this mutant no longer reproduces the shape it was "
              "written for. Either way the no-verdict path is unproven. NO DATA.")
        return 2
    print("  NO VERDICT - an import-time break is not counted as a kill\n")

    print("=" * 70)
    print(f"{len(MUTANTS)} MUTANTS")
    print("=" * 70)
    killed, survived, unapplied, no_verdict = [], [], [], []
    for i, (path, label, old, new) in enumerate(MUTANTS, 1):
        print(f"[{i}/{len(MUTANTS)}] {label}")
        if not apply_mutation(path, old, new):
            unapplied.append(label)
            restore()
            continue
        ok_m, out_m = run_suites()
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

    print("\n" + "=" * 70)
    print(f"RESULT: {len(killed)} killed, {len(survived)} survived, "
          f"{len(no_verdict)} no verdict, {len(unapplied)} not applied, "
          f"of {len(MUTANTS)}")
    print("=" * 70)
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
    drifted = [p for p in _ORIGINALS if sha(p) != shas[p]]
    if not drifted:
        print("\nTREE RESTORED: mqtt.py and config.py are byte-identical to "
              "their pre-run state.")
    else:
        print(f"\n*** TREE NOT RESTORED - {', '.join(drifted)} DIFFER. "
              f"Fix before committing. ***")
        return 1
    return 0 if not (survived or unapplied or no_verdict) else 1


if __name__ == "__main__":
    sys.exit(main())
