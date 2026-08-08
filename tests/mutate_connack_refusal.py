#!/usr/bin/env python3
"""Mutation battery for the refused-CONNACK gate and the escaped decode line
(T-527.11).

Scores tests/test_connack_refusal.py, tests/test_ha_birth_message.py and
tests/test_retired_entities.py. The last two are in the list because the change
inserts a return in front of everything they exercise: an over-correction that
gated a HEALTHY connect would leave this ticket's own suite perfectly green
while silencing the device, which is the 2026-08-05 outage again.

WHY THESE MUTANTS

Chosen against the four questions ~/.claude/rules/test-and-review-code.md
prescribes, rather than by walking the diff:

  1. WHAT DOES THE CHANGE MAKE POSSIBLE? An early return in the one callback
     that starts everything. Mutants 1-8 attack the gate's placement, its
     polarity and its logging - including three that move it PAST work it was
     added to skip, since a gate in the wrong place is indistinguishable from a
     correct one by reading the diff.

  2. WHAT IS THE IRREVERSIBLE ACTION? Burning start_publisher_threads()'s
     once-only flag. It is per-PROCESS and nothing retries it: the refused pass
     spawns both loops against a client that is not connected, their first
     publish is lost, and the connect that succeeds finds the flag set and
     correctly declines to re-start them - so the PCB temperature entity is
     `unknown` for up to 30 minutes and the camera images for up to an hour
     (the loops have different periods), on a host nobody can reach by hand.
     It gets mutants in both directions: 24 stops the flag ever being set, 25
     removes the early return that makes it once-only. Neither line is in the
     diff. They are here because the fix's entire rationale rests on them, and
     a rationale nothing pins is a comment.

  3. WHAT DOES THE CHANGED LINE ASSUME? That `rc` is a paho ReasonCode whose
     `is_failure` is the right question. Mutants 9-17 attack _connack_refused()
     directly, including two that swap `is_failure` for `rc != 0`. Those two
     matter more than they look: for CONNACK the two spellings agree on every
     reachable input, so nothing about a real connection distinguishes them and
     the simplification is the natural edit for a future reader to make.

  4. WHAT DOES THE CHANGE CLAIM IN PROSE THAT NOTHING ASSERTS? On a device
     whose failure mode is defined as silence, the log is the only evidence
     either fix ever fired. Mutants 6-8 and 21 delete or downgrade log lines.

A SECOND PAYLOAD SINK was found by review AFTER the first version of this
battery scored 25/25: `logger.error(f"Invalid water low cm value: {payload}")`
in the water/low/cm/set handler, still raw. No mutant could have caught it -
there was nothing to perturb, because the guard was simply absent from that
line, and a kill count cannot go down for code nobody wrote a mutant against.
Mutant 22 covers it now, and mutant 23 covers the source-level rule, which is
the only thing able to notice a THIRD sink being added raw.

WHAT IS DELIBERATELY NOT HERE, and this is a finding rather than an omission:
`{payload!r}` -> `{payload!a}` is not scored. ascii() escapes control characters
exactly as repr() does, so for the property under test - a remote payload
cannot put a raw newline, CR or ESC into gardyn.log - the two are equivalent
code, and an equivalent mutant surviving says nothing about the suite. The
difference between them is non-ASCII handling, which this change is not about.

Run:  python3 tests/mutate_connack_refusal.py

TWO CONTROLS GATE EVERY RESULT AND BOTH MUST HOLD BEFORE ANY VERDICT IS READ.
A battery scores a mutant by whether the test run FAILED, so a broken scorer
reports every mutant caught - the most reassuring output available, and the one
that goes straight into a summary as proof of rigour:

  CONTROL A  clean tree                -> must be GREEN (positive control, and
             it runs FIRST as a gate: a red clean tree is NO DATA, not a score)
  CONTROL B  deliberately broken code  -> must be RED (negative control)
             A alone is worthless: it is scored by the same path that may be
             broken, so only B proves the scorer can tell pass from fail.

Mechanics, each of which has bitten this repo:
  * every mutant gated on compile() BEFORE the write. A previous battery in
    this repo scored an IndentationError as a kill: the mutant broke collection,
    every suite reddened, and the behaviour under test never ran. Compiling the
    candidate string also keeps invalid Python off disk entirely.
  * the count of NAMED failing cases printed per mutant. That count is the only
    tell for the above - a real kill names the cases it broke, and the giveaway
    on the earlier run was `killed (0 failing case(s))`.
  * every anchor required to match EXACTLY once, and the mutated text compared
    against the original, because a replacement matching nothing exits happily
    and is indistinguishable from one that changed nothing.
  * __pycache__ purged before every run and PYTHONDONTWRITEBYTECODE=1 set. .pyc
    validity keys on (mtime-seconds, size), so a mutation applied and reverted
    inside one second can silently re-run the previous bytecode - and this
    battery's whole run takes under a minute.
  * stderr merged into stdout - unittest reports there.
  * restore registered on atexit AND on SIGTERM/SIGINT, not only try/finally.
    A finally clause does not run on SIGTERM, and a tool-call timeout sends
    one. A stranded mutant is a PLAUSIBLE edit in the working tree of the file
    that runs the garden, and reads as ordinary uncommitted work.
  * the tree asserted byte-identical at the end. Read that line before
    believing any score above it.
"""
import atexit
import hashlib
import os
import shutil
import signal
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MQTT = os.path.join(REPO, "mqtt.py")

SUITES = ["tests.test_connack_refusal", "tests.test_ha_birth_message",
          "tests.test_retired_entities"]

# The gate, quoted once so a mutant that moves it cannot drift from the one that
# deletes it.
GATE = (
    "    if _connack_refused(rc):\n"
    "        logger.error(\n"
    '            f"Connection REFUSED by broker: {rc}. Not subscribing, not "\n'
    '            f"announcing discovery, and NOT starting the publisher threads - "\n'
    '            f"their once-only guard must survive for the connect that succeeds. "\n'
    '            f"paho will retry with backoff."\n'
    "        )\n"
    "        return\n"
)
CONNECTED_LOG = '    logger.warning(f"Connected with result code {rc}")\n'

MUTANTS = [
    # --- 1-5: the gate's existence and placement ----------------------------
    # Reading the diff cannot distinguish a correct gate from one two lines too
    # late, so every position that still "has a gate" is mutated explicitly.
    ("delete the gate - the defect exactly as it was",
     GATE, ""),

    ("INVERT the gate - refuse every HEALTHY connect, silence the device",
     "    if _connack_refused(rc):",
     "    if not _connack_refused(rc):"),

    ("keep the log, drop the return - refusal announces into a dying socket",
     "        )\n        return\n    logger.warning(f\"Connected with result code {rc}\")",
     "        )\n    logger.warning(f\"Connected with result code {rc}\")"),

    ("gate AFTER the command subscribe - a refusal still asks for topics",
     GATE + CONNECTED_LOG
     + '    # Explicit topic list, not BASE_TOPIC + "/#" — see COMMAND_SUBSCRIPTIONS.\n'
     + "    client.subscribe(COMMAND_SUBSCRIPTIONS)\n",
     CONNECTED_LOG
     + '    # Explicit topic list, not BASE_TOPIC + "/#" — see COMMAND_SUBSCRIPTIONS.\n'
     + "    client.subscribe(COMMAND_SUBSCRIPTIONS)\n"
     + GATE),

    ("burn the publisher guard BEFORE the gate - the defect, gate and all",
     "    if _connack_refused(rc):\n        logger.error(",
     "    start_publisher_threads(client)\n"
     "    if _connack_refused(rc):\n        logger.error("),

    # --- 6-8: the refusal log, which is the only evidence it fired ----------
    ("downgrade the refusal to DEBUG - below the root level, so invisible",
     "    if _connack_refused(rc):\n        logger.error(",
     "    if _connack_refused(rc):\n        logger.debug("),

    ("delete the refusal log - the fix works and leaves no trace",
     "        logger.error(\n"
     '            f"Connection REFUSED by broker: {rc}. Not subscribing, not "\n'
     '            f"announcing discovery, and NOT starting the publisher threads - "\n'
     '            f"their once-only guard must survive for the connect that succeeds. "\n'
     '            f"paho will retry with backoff."\n'
     "        )\n"
     "        return\n",
     "        return\n"),

    ("drop the reason from the log - a bad password reads like a dead broker",
     '            f"Connection REFUSED by broker: {rc}. Not subscribing, not "',
     '            f"Connection REFUSED by broker. Not subscribing, not "'),

    # --- 9-17: what question _connack_refused() asks ------------------------
    ("always report NOT refused - the gate is present and inert",
     "    is_failure = getattr(rc, \"is_failure\", None)\n"
     "    if is_failure is None:\n"
     "        return rc != 0\n"
     "    return bool(is_failure)",
     "    return False"),

    ("always report refused - nothing ever connects",
     "    is_failure = getattr(rc, \"is_failure\", None)\n"
     "    if is_failure is None:\n"
     "        return rc != 0\n"
     "    return bool(is_failure)",
     "    return True"),

    ("swap is_failure for `rc != 0` - agrees on every REACHABLE connack",
     "    is_failure = getattr(rc, \"is_failure\", None)\n"
     "    if is_failure is None:\n"
     "        return rc != 0\n"
     "    return bool(is_failure)",
     "    return rc != 0"),

    ("typo the attribute name - falls back to `rc != 0` for every rc, silently",
     '    is_failure = getattr(rc, "is_failure", None)',
     '    is_failure = getattr(rc, "is_failed", None)'),

    ("negate the verdict",
     "    return bool(is_failure)",
     "    return not bool(is_failure)"),

    ("invert the type test - a ReasonCode takes the int branch and vice versa",
     "    if is_failure is None:",
     "    if is_failure is not None:"),

    ("default the getattr to False - a bare int rc can never be a refusal",
     '    is_failure = getattr(rc, "is_failure", None)',
     '    is_failure = getattr(rc, "is_failure", False)'),

    ("read rc.is_failure directly - AttributeError on paho's VERSION1 int rc",
     '    is_failure = getattr(rc, "is_failure", None)',
     "    is_failure = rc.is_failure"),

    ("accept any non-zero int - the int fallback stops classifying",
     "        return rc != 0",
     "        return False"),

    # --- 18-23: the payload sinks -------------------------------------------
    ("restore the raw interpolation - the second defect, exactly as it was",
     '        logger.info(f"Decoded payload on {msg.topic}: {payload!r}")',
     "        logger.info(f\"Decoded payload on {msg.topic}: '{payload}'\")"),

    ("interpolate raw and unquoted",
     '        logger.info(f"Decoded payload on {msg.topic}: {payload!r}")',
     '        logger.info(f"Decoded payload on {msg.topic}: {payload}")'),

    ("!r -> !s - str() escapes nothing",
     '        logger.info(f"Decoded payload on {msg.topic}: {payload!r}")',
     '        logger.info(f"Decoded payload on {msg.topic}: {payload!s}")'),

    ("delete the decode line - no record of what arrived at all",
     '        logger.info(f"Decoded payload on {msg.topic}: {payload!r}")\n',
     ""),

    ("un-escape the water threshold rejection - the sink review found raw",
     '                logger.error(f"Invalid water low cm value: {payload!r}")',
     '                logger.error(f"Invalid water low cm value: {payload}")'),

    ("escape it with !s instead - reads right, escapes nothing",
     '                logger.error(f"Invalid water low cm value: {payload!r}")',
     '                logger.error(f"Invalid water low cm value: {payload!s}")'),

    # --- 24-25: the invariant the fix RELIES on, in both directions ---------
    # Neither line is in the diff. The fix's whole argument is that the flag is
    # per-process and once-only; if it were neither, burning it would cost
    # nothing and this ticket would not exist.
    ("stop setting the once-only flag - burning it becomes harmless",
     "        _publisher_threads_started = True",
     "        pass"),

    ("remove the once-only early return - every reconnect leaks two threads",
     "        if _publisher_threads_started:\n            return",
     "        if False:\n            return"),

    # --- 26-27: the announce sequence the gate now stands in front of -------
    # An over-correction is not a smaller bug. These pin that the ACCEPTED path
    # still does its work, from the other side.
    ("drop the announce from the connect path entirely",
     "    announce_to_home_assistant(client)\n"
     "    # A water-state refresh stood here",
     "    # A water-state refresh stood here"),

    ("drop start_publisher_threads from the connect path",
     "    start_publisher_threads(client)\n",
     ""),
]

# Control B. A deliberately broken tree that MUST score RED. Kept distinct from
# every scored mutant above so that a failure here is unambiguous - it is the
# scorer being tested, not one of the mutants.
CONTROL_B = ("CONTROL B: deliberately broken - the refusal gate never fires",
             "    if _connack_refused(rc):",
             "    if False and _connack_refused(rc):")

_ORIGINAL_SRC = None


def purge_pycache():
    for root, dirs, _ in os.walk(REPO):
        if ".git" in root:
            continue
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def restore():
    """Put mqtt.py back, whatever happened."""
    if _ORIGINAL_SRC is None:
        return
    if sha(MQTT) != hashlib.sha256(_ORIGINAL_SRC.encode()).hexdigest():
        with open(MQTT, "w") as fh:
            fh.write(_ORIGINAL_SRC)
        print("  [restore] mqtt.py put back")


def _on_signal(signum, _frame):
    restore()
    sys.exit(128 + signum)


def apply_mutation(anchor, replacement):
    """Return True if applied. Refuses anything that is not an exact single hit."""
    src = open(MQTT).read()
    count = src.count(anchor)
    if count != 1:
        print(f"  ANCHOR MATCHED {count} TIMES - mutation NOT applied, no verdict")
        return False
    mutated = src.replace(anchor, replacement)
    if mutated == src:
        print("  replacement changed nothing - mutation NOT applied, no verdict")
        return False

    # Checked BEFORE the write, so invalid Python never reaches disk. A mutant
    # that does not compile makes every suite die at collection and scores as
    # KILLED while the behaviour it was written for never runs.
    try:
        compile(mutated, MQTT, "exec")
    except SyntaxError as exc:
        print(f"  MUTANT IS NOT VALID PYTHON ({exc.msg}, line {exc.lineno}) - "
              f"no verdict; a syntax error reddens the suite for the wrong reason")
        return False

    with open(MQTT, "w") as fh:
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
            cwd=REPO, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        )
        combined.append(f"--- {suite} (rc={proc.returncode}) ---\n{proc.stdout}")
        ok = ok and proc.returncode == 0
    return ok, "\n".join(combined)


def main():
    """Three restore paths, none of which subsumes another.

      try/finally      an exception out of the run, including the
                       KeyboardInterrupt tests/test_suite_isolation.py injects.
                       Restores SYNCHRONOUSLY, before the exception leaves.
      atexit           a clean exit down a path that skips the finally.
      SIGTERM/SIGINT   a signal, which runs NEITHER of the above.
    """
    global _ORIGINAL_SRC
    _ORIGINAL_SRC = open(MQTT).read()
    atexit.register(restore)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    try:
        return _run()
    finally:
        restore()


def _run():
    original_sha = hashlib.sha256(_ORIGINAL_SRC.encode()).hexdigest()

    print("=" * 70)
    print("CONTROL A (positive, runs FIRST as a gate) - clean tree must be GREEN")
    print("=" * 70)
    ok, out = run_suites()
    if not ok:
        print(out)
        print("\nCONTROL A FAILED - the suite does not pass on a clean tree.")
        print("This is NO DATA, not a score. Fix the suite before reading anything.")
        return 1
    print("  GREEN - suites pass on the clean tree\n")

    print("=" * 70)
    print("CONTROL B (negative) - a deliberately broken tree must be RED")
    print("=" * 70)
    label, anchor, replacement = CONTROL_B
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

    print("=" * 70)
    print(f"{len(MUTANTS)} MUTANTS")
    print("=" * 70)
    killed, survived, unapplied = [], [], []
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
        else:
            # Read WHY it died, not just the colour. A mutant that reddens the
            # whole suite tested the harness, not the behaviour - and `killed
            # (0 failing case(s))` is the tell for exactly that.
            fails = [l for l in out.splitlines()
                     if l.startswith(("FAIL:", "ERROR:"))]
            print(f"  killed ({len(fails)} failing case(s))")
            for line in fails[:3]:
                print(f"      {line}")
            killed.append(label)

    print("\n" + "=" * 70)
    print(f"RESULT: {len(killed)} killed, {len(survived)} survived, "
          f"{len(unapplied)} not applied, of {len(MUTANTS)}")
    print("=" * 70)
    for label in survived:
        print(f"  SURVIVED  {label}")
    for label in unapplied:
        print(f"  NOT APPLIED  {label}")

    # The byte-identity assertion. Read this line before believing any score
    # above it - a run that exited is not a run whose cleanup ran.
    if sha(MQTT) == original_sha:
        print("\nTREE RESTORED: mqtt.py is byte-identical to its pre-run state.")
    else:
        print("\n*** TREE NOT RESTORED - mqtt.py DIFFERS. Fix before committing. ***")
        return 1
    return 0 if not survived and not unapplied else 1


if __name__ == "__main__":
    sys.exit(main())
