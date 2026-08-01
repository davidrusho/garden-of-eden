#!/usr/bin/env python3
"""Mutation battery for the netwatch-heartbeat half of gardyn-health-log.py (T-479).

A green suite proves nothing until it has been shown capable of going red.
Each mutation below breaks ONE guarantee the new tests claim to enforce; every
one of them must produce at least one failure. A mutation that survives means
the corresponding test is decorative.

Runs entirely inside a disposable copy of the repo - the real working tree is
never mutated, so a crash here cannot damage uncommitted work.

Run from the repo root:

    python3 tests/mutate_health_log.py

Exit 0 means every mutation was caught. Exit 1 names the survivors, each of
which is a test that passes for a reason other than the one it claims.
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC = "bin/gardyn-health-log.py"
TESTS = "tests/test_health_log.py"

# (name, file, old, new) — `old` must appear exactly once, or the mutation
# silently did nothing and the "survived" verdict would be meaningless.
MUTATIONS = [
    # The allowlist is the ONLY thing standing between a masked/disabled timer
    # and a green monitor. Widening it is the realistic way that goes wrong.
    ("masked accepted as a running state", SRC,
     'if enabled not in ("enabled", "enabled-runtime"):',
     'if enabled not in ("enabled", "enabled-runtime", "masked", "disabled"):'),
    ("freshness bound widened past any real staleness", SRC,
     "NETWATCH_MAX_AGE_S = 420.0", "NETWATCH_MAX_AGE_S = 999999.0"),
    ("never_triggered downgraded to healthy", SRC,
     'return {**verdict, "ok": False, "reason": "never_triggered"}',
     'return {**verdict, "ok": True, "reason": "never_triggered"}'),
    ("probe failure treated as healthy instead of unknown", SRC,
     'return {**verdict, "ok": None, "reason": f"probe_{probe_error}"}',
     'return {**verdict, "ok": True, "reason": f"probe_{probe_error}"}'),
    ("absent unit no longer detected", SRC,
     'if not enabled:\n        return {**verdict, "ok": False, "reason": "timer_absent"}',
     'if False:\n        return {**verdict, "ok": False, "reason": "timer_absent"}'),
    ("failed run collapsed into the healthy path", SRC,
     'if result and result != "success":', 'if False:'),
    ("min and ms units swapped", SRC,
     '"h": 3600.0, "min": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6,',
     '"h": 3600.0, "min": 1e-3, "s": 1.0, "ms": 60.0, "us": 1e-6,'),
    ("timespan parser made lenient about trailing junk", SRC,
     "    if pos == 0 or text[pos:].strip():\n        return None",
     "    if pos == 0:\n        return None"),
    ("baked-in template query no longer stripped", SRC,
     'base = url.partition("?")[0]', "base = url"),
    ("push body no longer inspected (trusts HTTP 200)", SRC,
     '        if json.loads(body).get("ok") is True:\n            return "ok"',
     '        if body is not None:\n            return "ok"'),
    ("exception detail leaks the URL and its token", SRC,
     'return f"failed_{type(exc).__name__}"', 'return f"failed_{exc}"'),
    ("fault pushed as up", SRC,
     'status = "up" if verdict["ok"] else "down"', 'status = "up"'),
    ("unmeasurable verdict pushed instead of skipped", SRC,
     '    if verdict.get("ok") is None:\n        return None',
     '    if verdict.get("ok") is None:\n        verdict = {**verdict, "ok": True}'),
    ("blank UnitFileState dropped by the property parser", SRC,
     "        if sep:\n            out[key.strip()] = value.strip()",
     "        if sep and value.strip():\n            out[key.strip()] = value.strip()"),
]


def run_suite(root: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "tests.test_health_log"],
        cwd=root, capture_output=True, text=True)
    return proc.returncode, proc.stderr


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        shutil.copytree(REPO, root, ignore=shutil.ignore_patterns(".git", "__pycache__"))

        rc, err = run_suite(root)
        if rc != 0:
            print("BASELINE IS RED - the battery cannot mean anything. Output:\n" + err)
            return 2
        count = re.search(r"Ran (\d+) tests", err)
        print("baseline: GREEN (%s tests)\n" % (count.group(1) if count else "?"))

        survived = []
        for name, relpath, old, new in MUTATIONS:
            target = root / relpath
            original = target.read_text()
            if original.count(old) != 1:
                print(f"  SKIP  {name}: anchor appears {original.count(old)}x, not 1")
                survived.append(name + " (anchor not unique)")
                continue
            target.write_text(original.replace(old, new))
            rc, err = run_suite(root)
            target.write_text(original)

            failures = re.search(r"FAILED \((.*)\)", err)
            if rc == 0:
                print(f"  SURVIVED  {name}")
                survived.append(name)
            else:
                print(f"  killed    {name}  [{failures.group(1) if failures else 'error'}]")

        print()
        if survived:
            print(f"{len(survived)} MUTATION(S) SURVIVED - those tests prove nothing:")
            for name in survived:
                print(f"  - {name}")
            return 1
        print(f"all {len(MUTATIONS)} mutations killed")
        return 0


if __name__ == "__main__":
    sys.exit(main())
