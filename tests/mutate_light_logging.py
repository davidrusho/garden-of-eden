#!/usr/bin/env python3
"""Mutation battery for test_light_logging.py.

The suite it scores exists to prove light commands are RECORDED. That is an
assertion about an absence being fixed, which is exactly where a dead test and
a real pass produce identical output - so the suite is worthless until it has
been shown capable of going red.

Run:  python3 tests/mutate_light_logging.py

Two controls gate every result, and BOTH must hold before any mutant verdict is
read. A battery scores a mutant by whether the test run FAILED, so a broken
scorer reports every mutant caught - the most reassuring output available:

  CONTROL A  clean tree            -> must be GREEN
  CONTROL B  broken assertion      -> must be RED
             (A alone is worthless; it is scored by the same path that may be
             broken, so only B proves the scorer can distinguish the two.)

Mechanics that have bitten this repo before, all handled here:
  * __pycache__ purged before every run, and PYTHONDONTWRITEBYTECODE=1 set.
    .pyc validity keys on (mtime-seconds, size), so a mutation applied and
    reverted inside one second can silently re-run the previous bytecode. This
    file loads its target via spec_from_file_location, where that bites hardest.
  * stderr merged into stdout - unittest reports there, and 2>/dev/null would
    blank the very output being grepped.
  * each mutated file diffed against a pristine copy, because a replacement
    that matches nothing is indistinguishable from one that changed nothing.
  * the tree is asserted byte-identical at the end.
"""
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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
     'logger.info(f"Decoded payload on {msg.topic}',
     'logger.debug(f"Decoded payload on {msg.topic}'),
    ("re-promote a periodic publisher, re-burying the command record",
     'logger.debug(f"Captured+published {label} camera',
     'logger.info(f"Captured+published {label} camera'),
]


def purge_pycache():
    for root, dirs, _ in os.walk(REPO):
        if ".git" in root:
            continue
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)


def run_suite(suite=SUITE):
    """Return (passed, combined_output). stderr merged - unittest writes there."""
    purge_pycache()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", suite],
        cwd=REPO, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    out = proc.stdout
    return proc.returncode == 0, out


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main():
    pristine = tempfile.mkdtemp(prefix="lightmut-")
    backup = os.path.join(pristine, "light.py")
    shutil.copyfile(LIGHT, backup)          # not copy2: do NOT preserve mtime
    original_sha = sha(LIGHT)
    original_src = open(LIGHT).read()

    print("=" * 68)
    print("CONTROL A - clean tree must be GREEN")
    ok, out = run_suite()
    print(f"  clean tree: {'GREEN' if ok else 'RED'}")
    if not ok:
        print(out[-2500:])
        print("\nABORT: clean tree is not green. No mutant verdict is readable.")
        return 2

    print("=" * 68)
    print("CONTROL B - a deliberately broken assertion must be RED")
    broken = original_src.replace(
        'logger.info("Turning light on")',
        'logger.info("THIS_STRING_IS_NOT_WHAT_THE_TEST_EXPECTS")')
    if broken == original_src:
        print("ABORT: control-B anchor did not match. Harness is broken.")
        return 2
    open(LIGHT, "w").write(broken)
    ok_b, _ = run_suite()
    open(LIGHT, "w").write(original_src)
    print(f"  broken assertion: {'GREEN' if ok_b else 'RED'}")
    if ok_b:
        print("\nABORT: the suite passed a deliberately broken tree.")
        print("The scorer cannot tell pass from fail; every verdict below would")
        print("be meaningless. Fix the harness before reading any result.")
        return 2

    print("=" * 68)
    print("BOTH CONTROLS OK - mutant verdicts are readable\n")

    killed, survived = 0, []
    for i, (label, old, new) in enumerate(MUTANTS, 1):
        count = original_src.count(old)
        if count != 1:
            print(f"  [{i}] HARNESS ERROR ({count} anchor matches): {label}")
            survived.append((label, f"anchor matched {count}x, not 1"))
            continue

        mutated = original_src.replace(old, new)
        open(LIGHT, "w").write(mutated)

        # Prove the edit actually landed - a no-op replace looks like a survivor.
        if open(LIGHT).read() == original_src:
            print(f"  [{i}] HARNESS ERROR (file unchanged): {label}")
            survived.append((label, "mutation did not apply"))
            open(LIGHT, "w").write(original_src)
            continue

        ok_m, _ = run_suite()
        open(LIGHT, "w").write(original_src)

        if ok_m:
            print(f"  [{i}] SURVIVED  {label}")
            survived.append((label, "suite stayed green"))
        else:
            killed += 1
            print(f"  [{i}] killed    {label}")

    # --- mqtt.py pass -------------------------------------------------------
    print("\n" + "=" * 68)
    print("mqtt.py policy mutants (scored by " + MQTT_SUITE + ")")
    mqtt_src = open(MQTT).read()
    mqtt_sha = sha(MQTT)

    ok_m, _ = run_suite(MQTT_SUITE)
    print(f"  CONTROL A - clean tree: {'GREEN' if ok_m else 'RED'}")
    if not ok_m:
        print("  ABORT: mqtt suite is not green on a clean tree; verdicts unreadable.")
        return 2
    broken_m = mqtt_src.replace("logger.setLevel(logging.INFO)",
                                "logger.setLevel(logging.CRITICAL)")
    assert broken_m != mqtt_src, "control-B anchor missed"
    open(MQTT, "w").write(broken_m)
    ok_mb, _ = run_suite(MQTT_SUITE)
    open(MQTT, "w").write(mqtt_src)
    print(f"  CONTROL B - broken policy: {'GREEN' if ok_mb else 'RED'}")
    if ok_mb:
        print("  ABORT: mqtt suite passed a deliberately broken policy.")
        return 2
    print()

    for j, (label, old_s, new_s) in enumerate(MQTT_MUTANTS, 1):
        count = mqtt_src.count(old_s)
        if count != 1:
            print(f"  [m{j}] HARNESS ERROR ({count} anchor matches): {label}")
            survived.append((label, f"anchor matched {count}x, not 1"))
            continue
        open(MQTT, "w").write(mqtt_src.replace(old_s, new_s, 1))
        if open(MQTT).read() == mqtt_src:
            print(f"  [m{j}] HARNESS ERROR (file unchanged): {label}")
            survived.append((label, "mutation did not apply"))
            open(MQTT, "w").write(mqtt_src)
            continue
        ok_mm, _ = run_suite(MQTT_SUITE)
        open(MQTT, "w").write(mqtt_src)
        if ok_mm:
            print(f"  [m{j}] SURVIVED  {label}")
            survived.append((label, "suite stayed green"))
        else:
            killed += 1
            print(f"  [m{j}] killed    {label}")

    total = len(MUTANTS) + len(MQTT_MUTANTS)
    print("\n" + "=" * 68)
    print(f"RESULT: {killed}/{total} killed")

    restored = sha(LIGHT) == original_sha and sha(MQTT) == mqtt_sha
    print(f"tree restored byte-identical: {restored}")
    shutil.rmtree(pristine, ignore_errors=True)

    if survived:
        print("\nSURVIVORS - a survivor is a question about the CODE and the")
        print("HARNESS, not only about the suite. Three explanations: the test")
        print("is weak, the mutation never applied, or the mutated code is")
        print("redundant and genuinely changes nothing.")
        for label, why in survived:
            print(f"  - {label}: {why}")

    return 0 if (killed == total and restored) else 1


if __name__ == "__main__":
    sys.exit(main())
