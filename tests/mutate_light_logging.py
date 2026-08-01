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
SUITE = "tests.test_light_logging"

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


def purge_pycache():
    for root, dirs, _ in os.walk(REPO):
        if ".git" in root:
            continue
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)


def run_suite():
    """Return (passed, combined_output). stderr merged - unittest writes there."""
    purge_pycache()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", SUITE],
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

    print("\n" + "=" * 68)
    print(f"RESULT: {killed}/{len(MUTANTS)} killed")

    restored = sha(LIGHT) == original_sha
    print(f"tree restored byte-identical: {restored}")
    shutil.rmtree(pristine, ignore_errors=True)

    if survived:
        print("\nSURVIVORS - a survivor is a question about the CODE and the")
        print("HARNESS, not only about the suite. Three explanations: the test")
        print("is weak, the mutation never applied, or the mutated code is")
        print("redundant and genuinely changes nothing.")
        for label, why in survived:
            print(f"  - {label}: {why}")

    return 0 if (killed == len(MUTANTS) and restored) else 1


if __name__ == "__main__":
    sys.exit(main())
