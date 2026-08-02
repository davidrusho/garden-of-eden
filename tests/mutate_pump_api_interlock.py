#!/usr/bin/env python3
"""Mutation battery for the pump-start routes removed from the REST API (T-489).

The change it scores is a DELETION, which is the case where a green suite says
least: every assertion in tests/test_pump_api_interlock.py is about an absence,
and an absence is what a file that never had the route produces for free. So
seven of the nine mutants below REINTRODUCE a way to energise the pump over
HTTP rather than break something that is present. Those are what this battery
is actually for.

The irreversible action here is "the pump energises", and it is mutated
explicitly rather than left to a kill count to imply. Mutants 1-7 each restore
a start path - by name, under a different name, under a different blueprint,
and by widening an existing read-only rule's `methods` - because a suite that
only knows the two paths that used to exist would pass every one of the others.

Run:  python3 tests/mutate_pump_api_interlock.py

Flask is required (as it is for tests/test_api.py). On a machine without it
every run scores RED for the wrong reason, which CONTROL A catches.

Two controls gate every result and BOTH must hold before any verdict is read. A
battery scores a mutant by whether the test run FAILED, so a broken scorer
reports every mutant caught - the most reassuring output available, and the one
that goes straight into a summary as proof of rigour:

  CONTROL A  clean tree               -> must be GREEN
  CONTROL B  deliberately broken code -> must be RED
             (A alone is worthless: it is scored by the same path that may be
             broken, so only B proves the scorer can tell pass from fail.)

Mechanics that have bitten this repo before, all handled:
  * __pycache__ purged before every run and PYTHONDONTWRITEBYTECODE=1 set. .pyc
    validity keys on (mtime-seconds, size), so a mutation applied and reverted
    inside one second can silently re-run the previous bytecode.
  * stderr merged into stdout - unittest reports there, so 2>/dev/null would
    blank the output being grepped.
  * every anchor required to match EXACTLY once, and the mutated file compared
    against the original, because a replacement that matches nothing exits
    successfully and is indistinguishable from one that changed nothing.
  * the tree asserted byte-identical at the end.
"""
import hashlib
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROUTES = os.path.join(REPO, "app", "sensors", "pump", "routes.py")

SUITES = ["tests.test_pump_api_interlock"]

# The route as it stood before T-489, verbatim. The central risk of a
# deletion-shaped change is not that the deletion fails, it is that the deleted
# thing comes back and nothing notices.
TURN_ON_RESTORED = '''@pump_blueprint.route('/on', methods=['POST'])
@check_sensor
def turn_on():
    pump_control.on()
    return jsonify(message="Pump turned on!"), 200

@pump_blueprint.route('/off', methods=['POST'])'''

ADJUST_SPEED_RESTORED = '''@pump_blueprint.route('/speed', methods=['POST'])
@check_sensor
def adjust_speed():
    data = request.get_json()
    speed_value = data.get('value', 30)
    try:
        pump_control.set_speed(speed_value)
        return jsonify(message=f"Pump adjusted to {speed_value}% speed!"), 200
    except ValueError as e:
        return jsonify(message=str(e)), 400

@pump_blueprint.route('/speed', methods=['GET'])'''

# Same capability, a name no test could have been written against.
RENAMED_START_ROUTE = '''@pump_blueprint.route('/start', methods=['POST'])
@check_sensor
def start_it():
    pump_control.on()
    return jsonify(message="Pump turned on!"), 200

@pump_blueprint.route('/off', methods=['POST'])'''

# The subtlest reintroduction: no new function and no new URL, just one more
# entry in an existing rule's methods list.
WIDENED_STATS_RULE = """@pump_blueprint.route('/stats', methods=['GET', 'POST'])
@check_sensor
def get_pump_data():
    from flask import request
    if request.method == 'POST':
        pump_control.set_speed(100)
    data = fetch_ina219_data()
    return jsonify(data)"""

MUTANTS = [
    # --- the irreversible action: every mutant here energises the pump ------
    ("RESTORE POST /pump/on verbatim (the un-interlocked start route)",
     "@pump_blueprint.route('/off', methods=['POST'])",
     TURN_ON_RESTORED),

    ("RESTORE POST /pump/speed verbatim (the other un-interlocked start)",
     "@pump_blueprint.route('/speed', methods=['GET'])",
     ADJUST_SPEED_RESTORED),

    ("reintroduce a start route under a NEW name (/pump/start)",
     "@pump_blueprint.route('/off', methods=['POST'])",
     RENAMED_START_ROUTE),

    ("widen the read-only /pump/stats rule to accept POST and start the pump",
     "@pump_blueprint.route('/stats', methods=['GET'])\n"
     "@check_sensor\n"
     "def get_pump_data():\n"
     "    data = fetch_ina219_data()\n"
     "    return jsonify(data)",
     WIDENED_STATS_RULE),

    ("turn the STOP route into a start route",
     "    pump_control.off()\n"
     '    return jsonify(message="Pump turned off!"), 200',
     "    pump_control.on()\n"
     '    return jsonify(message="Pump turned off!"), 200'),

    ("make the stop route start the pump at speed as well as stopping it",
     "    pump_control.off()\n",
     "    pump_control.off()\n    pump_control.set_speed(100)\n"),

    ("energise via set_duty_cycle, the method neither deleted route used",
     "    pump_control.off()\n",
     "    pump_control.set_duty_cycle(100)\n    pump_control.off()\n"),

    # --- over-deletion: the failure that looks like success -----------------
    ("delete the STOP route, so nothing can turn the pump off",
     "@pump_blueprint.route('/off', methods=['POST'])\n"
     "@check_sensor\n"
     "def turn_off():\n"
     "    pump_control.off()\n"
     '    return jsonify(message="Pump turned off!"), 200\n',
     ""),

    ("delete the read-only speed endpoint",
     "@pump_blueprint.route('/speed', methods=['GET'])\n"
     "@check_sensor\n"
     "def get_speed():\n"
     "    current_speed = pump_control.get_speed()\n"
     "    return jsonify(value=current_speed), 200\n",
     ""),
]


def purge_pycache():
    for root, dirs, _ in os.walk(REPO):
        if ".git" in root:
            continue
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)


def run_suites():
    """Return (all_passed, combined_output). stderr merged - unittest uses it."""
    purge_pycache()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    combined = []
    ok = True
    for suite in SUITES:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", suite],
            cwd=REPO, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        )
        combined.append(f"--- {suite} (rc={proc.returncode}) ---\n{proc.stdout}")
        ok = ok and proc.returncode == 0
    return ok, "\n".join(combined)


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main():
    original_src = open(ROUTES).read()
    original_sha = sha(ROUTES)
    # Every write below is undone on its own happy path, which covers nothing
    # if the run dies in between -- a Ctrl-C or a wrapper's timeout then leaves
    # a mutant sitting in the working tree, where the next commit picks it up.
    # tests/test_suite_isolation.py drives this harness and asserts the tree
    # comes back, so the guarantee is checked rather than asserted in a comment.
    try:
        return _battery(original_src, original_sha)
    finally:
        if open(ROUTES).read() != original_src:
            open(ROUTES, "w").write(original_src)
            print(f"\nrestored {ROUTES} after an interrupted run")


def _battery(original_src, original_sha):
    print("=" * 70)
    print("CONTROL A - clean tree must be GREEN")
    ok, out = run_suites()
    print(f"  clean tree: {'GREEN' if ok else 'RED'}")
    if not ok:
        print(out[-3000:])
        print("\nABORT: clean tree is not green. No mutant verdict is readable.")
        return 2

    print("=" * 70)
    print("CONTROL B - a deliberately broken assertion must be RED")
    # Break the surviving stop route, which every remaining assertion about a
    # working endpoint depends on and nothing else could mask.
    broken = original_src.replace(
        '    return jsonify(message="Pump turned off!"), 200',
        '    return jsonify(message="CONTROL_B_BROKEN"), 200')
    if broken == original_src:
        print("ABORT: control-B anchor did not match. Harness is broken.")
        return 2
    open(ROUTES, "w").write(broken)
    ok_b, _ = run_suites()
    open(ROUTES, "w").write(original_src)
    print(f"  broken tree: {'GREEN' if ok_b else 'RED'}")
    if ok_b:
        print("\nABORT: the suites passed a deliberately broken tree.")
        print("The scorer cannot tell pass from fail; every verdict below")
        print("would be meaningless. Fix the harness before reading any result.")
        return 2

    print("=" * 70)
    print("BOTH CONTROLS OK - mutant verdicts are readable\n")

    killed, survived = 0, []
    for i, (label, old, new) in enumerate(MUTANTS, 1):
        count = original_src.count(old)
        if count != 1:
            print(f"  [{i:2}] HARNESS ERROR ({count} anchor matches): {label}")
            survived.append((label, f"anchor matched {count}x, not 1"))
            continue

        mutated = original_src.replace(old, new, 1)
        open(ROUTES, "w").write(mutated)

        # Prove the edit landed. A no-op replacement looks exactly like a
        # survivor, and it is the failure mode a kill count cannot show.
        if open(ROUTES).read() == original_src:
            print(f"  [{i:2}] HARNESS ERROR (file unchanged): {label}")
            survived.append((label, "mutation did not apply"))
            open(ROUTES, "w").write(original_src)
            continue

        ok_m, _ = run_suites()
        open(ROUTES, "w").write(original_src)

        if ok_m:
            print(f"  [{i:2}] SURVIVED  {label}")
            survived.append((label, "suites stayed green"))
        else:
            killed += 1
            print(f"  [{i:2}] killed    {label}")

    print("\n" + "=" * 70)
    print(f"RESULT: {killed}/{len(MUTANTS)} killed")

    restored = sha(ROUTES) == original_sha
    print(f"tree restored byte-identical: {restored}")

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
