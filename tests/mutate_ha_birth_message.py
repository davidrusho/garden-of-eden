#!/usr/bin/env python3
"""Mutation battery for the homeassistant/status re-announce (T-527.1).

Scores tests/test_ha_birth_message.py and tests/test_retired_entities.py. The
second is in the list because T-527.1 REFACTORED on_connect - the announce
sequence moved into announce_to_home_assistant() - and the existing connect-path
suite is what proves that refactor took nothing with it.

WHY THIS BATTERY EXISTS IN THIS SHAPE

The change adds a path that fires when Home Assistant restarts. Nothing about
its failure mode is loud: the Pi keeps publishing, keeps answering ICMP, keeps
its MQTT session, and HA simply has no entities. So the suite is the only place
the behaviour is exercised, and a suite that cannot fail is worth nothing here.

The mutants are chosen against three questions rather than by walking the diff:

  1. WHAT DOES THE CHANGE MAKE POSSIBLE THAT WAS NOT POSSIBLE BEFORE? A code
     path that republishes retained discovery, reachable by anyone who can
     publish to homeassistant/status. Mutants 9-11 attack it: the announce
     called too often, called with the wrong things in it, or shadowing the
     command chain it now sits in front of.
  2. WHAT IS THE IRREVERSIBLE ACTION? Leaking publisher threads. start_publisher
     _threads() spawns PCB and camera loops with no check for existing ones, so
     one call per HA restart is an unbounded leak on a 512 MB Zero W with no
     console and no physical recovery path (T-258's hard constraint). Mutant 12
     plants it inside the shared announce; mutant 13 removes it from the connect
     path, which is the over-correction in the other direction.
  3. WHAT DOES THE CHANGED LINE ASSUME? That on_message decodes and strips
     before dispatch, that its catch-all still wraps the new branch, and that
     announce_to_home_assistant() is idempotent. Mutants 7, 8 and 16 attack
     those rather than the lines the diff touched.

Run:  python3 tests/mutate_ha_birth_message.py

TWO CONTROLS GATE EVERY RESULT AND BOTH MUST HOLD BEFORE ANY VERDICT IS READ.
A battery scores a mutant by whether the test run FAILED, so a broken scorer
reports every mutant caught - the most reassuring output available, and the one
that goes straight into a summary as proof of rigour:

  CONTROL A  clean tree                -> must be GREEN
  CONTROL B  deliberately broken code  -> must be RED
             (A alone is worthless: it is scored by the same path that may be
             broken, so only B proves the scorer can tell pass from fail.)

Mechanics handled, each of which has bitten this repo or is prescribed by
~/.claude/rules/test-and-review-code.md:
  * __pycache__ purged before every run and PYTHONDONTWRITEBYTECODE=1 set. .pyc
    validity keys on (mtime-seconds, size), so a mutation applied and reverted
    inside one second can silently re-run the previous bytecode.
  * stderr merged into stdout - unittest reports there.
  * every anchor required to match EXACTLY once, and the mutated file compared
    against the original, because a replacement matching nothing exits happily
    and is indistinguishable from one that changed nothing.
  * restore registered on atexit AND on SIGTERM/SIGINT, not only try/finally.
    A finally clause does not run on SIGTERM, and a stranded mutant is a
    PLAUSIBLE edit sitting in the working tree of the file that runs the garden.
  * the tree asserted byte-identical at the end. Read that line before believing
    any score.
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

SUITES = ["tests.test_ha_birth_message", "tests.test_retired_entities"]

MUTANTS = [
    # --- 1-5: the device never hears the birth message -----------------------
    # The original defect, in five spellings. Every one of them leaves the
    # handler below perfectly correct and permanently unreachable, which is
    # exactly the state mqtt.py was in before T-527.1.
    ("delete the HA status subscription - the original 2026-08-05 defect",
     "    client.subscribe(LIFECYCLE_SUBSCRIPTIONS)\n",
     ""),

    ("subscribe to an EMPTY lifecycle list",
     "    client.subscribe(LIFECYCLE_SUBSCRIPTIONS)",
     "    client.subscribe([])"),

    ("downgrade the HA status subscription to QoS 0",
     "LIFECYCLE_SUBSCRIPTIONS = [(HA_STATUS_TOPIC, 1)]",
     "LIFECYCLE_SUBSCRIPTIONS = [(HA_STATUS_TOPIC, 0)]"),

    ("point the subscription at the WRONG topic",
     'HA_STATUS_TOPIC = "homeassistant/status"',
     'HA_STATUS_TOPIC = "homeassistant/state"'),

    ("replace the command subscribe with the lifecycle one - silences commands",
     "    client.subscribe(COMMAND_SUBSCRIPTIONS)\n",
     ""),

    # --- 6-8: the payload check ---------------------------------------------
    ("re-announce on ANY payload - HA's own LWT becomes a trigger",
     "            if payload.lower() == HA_BIRTH_PAYLOAD:",
     "            if True:"),

    ("INVERT the birth check - announces on offline, ignores online",
     "            if payload.lower() == HA_BIRTH_PAYLOAD:",
     "            if payload.lower() != HA_BIRTH_PAYLOAD:"),

    ("drop .lower() - a cosmetic difference becomes a silent no-op",
     "            if payload.lower() == HA_BIRTH_PAYLOAD:",
     "            if payload == HA_BIRTH_PAYLOAD:"),

    # --- 9-11: the new branch's placement in on_message ----------------------
    ("delete the birth branch entirely - falls through, nothing announced",
     "        if msg.topic == HA_STATUS_TOPIC:",
     "        if False:"),

    ("match by SUBSTRING - any topic ending in the right characters triggers",
     "        if msg.topic == HA_STATUS_TOPIC:",
     "        if HA_STATUS_TOPIC in msg.topic:"),

    ("announce on EVERY message, not only the birth topic",
     "    try:\n        # === Home Assistant lifecycle ===",
     "    try:\n"
     "        announce_to_home_assistant(client)\n"
     "        # === Home Assistant lifecycle ==="),

    # --- 12-13: the irreversible action, mutated in BOTH directions ----------
    # Thread leak on a 512 MB host with no console. This is the worst outcome
    # available in the change and it is mutated explicitly rather than left to
    # the kill count to imply.
    ("LEAK publisher threads: start them inside the shared announce",
     "    publish_light_state(client)\n\n\ndef on_connect",
     "    publish_light_state(client)\n"
     "    start_publisher_threads(client)\n\n\ndef on_connect"),

    ("over-correct: drop start_publisher_threads from the connect path",
     "    start_publisher_threads(client)\n",
     ""),

    ("re-subscribe on every birth message - pointless traffic on one antenna",
     "                announce_to_home_assistant(client)",
     "                client.subscribe(LIFECYCLE_SUBSCRIPTIONS)\n"
     "                announce_to_home_assistant(client)"),

    # --- 15-18: the announce sequence itself ---------------------------------
    # Each of these leaves HA with a partially-rebuilt device, which is the
    # failure that looks most like success: entities appear, and are wrong.
    ("drop the discovery publish from the announce",
     "    send_discovery_messages(client)\n",
     ""),

    ("drop the availability re-assert - four entities announced UNAVAILABLE",
     '    client.publish(STATUS_TOPIC, "online", qos=1, retain=True)\n',
     ""),

    ("drop the light-state republish - HA rebuilds the entity at `unknown`",
     "    publish_light_state(client)\n\n\ndef on_connect",
     "\n\ndef on_connect"),

    ("drop the retired-entity clear from the announce",
     "    clear_retired_entities(client)\n",
     ""),

    ("reorder: clear AFTER discovery, so HA races the two",
     "    clear_retired_entities(client)\n"
     '    # Clear the retained "offline" the broker may have left from the last death.\n'
     "    # Publish before discovery so HA never sees an entity announced unavailable.\n"
     '    client.publish(STATUS_TOPIC, "online", qos=1, retain=True)\n'
     "    send_discovery_messages(client)",
     '    client.publish(STATUS_TOPIC, "online", qos=1, retain=True)\n'
     "    send_discovery_messages(client)\n"
     "    clear_retired_entities(client)"),

    # --- 20: the invariant the change RELIES on ------------------------------
    # The new branch sits INSIDE on_message's catch-all. Moving it out means an
    # exception on a topic anyone can publish to kills paho's network loop
    # thread, and with it every inbound command - strictly worse than the bug
    # being fixed.
    ("move the birth branch OUTSIDE the catch-all",
     "    try:\n        # === Home Assistant lifecycle ===",
     "    if msg.topic == HA_STATUS_TOPIC and payload.lower() == HA_BIRTH_PAYLOAD:\n"
     "        announce_to_home_assistant(client)\n"
     "        return\n\n"
     "    try:\n        # === Home Assistant lifecycle ==="),
]

# Control B. A deliberately broken tree that MUST score RED. Without it, Control
# A is scored by the same path that may itself be broken.
CONTROL_B = ("CONTROL B: deliberately broken - announce publishes nothing",
             "    send_discovery_messages(client)\n",
             "    pass\n")

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
    """Put mqtt.py back, whatever happened.

    Registered on atexit AND on SIGTERM/SIGINT. try/finally covers an exception
    and NOT a signal, and a battery is most likely to die by signal - a tool-call
    timeout sends SIGTERM. A stranded mutant is a plausible-looking edit in the
    working tree of the file that runs the garden, and reads as ordinary
    uncommitted work rather than as damage.
    """
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
    with open(MQTT, "w") as fh:
        fh.write(mutated)

    # A mutant MUST be valid Python, and this gate is not paranoia - it caught a
    # real false kill in this file's own first run. Mutant 11 inserted a line at
    # the wrong indent, mqtt.py failed to import, both suites died at collection,
    # and the battery scored it KILLED. The behaviour it was written to test was
    # never exercised. A syntax error reddens everything, and "everything red"
    # is indistinguishable from "the assertion fired" if you only read the
    # colour - so refuse the mutant here rather than scoring it.
    #
    # The tell in the output was `killed (0 failing case(s))`: a real kill names
    # the cases it broke. That is why the runner prints the count.
    try:
        compile(mutated, MQTT, "exec")
    except SyntaxError as exc:
        print(f"  MUTANT IS NOT VALID PYTHON ({exc.msg}, line {exc.lineno}) - "
              f"no verdict; a syntax error reddens the suite for the wrong reason")
        return False
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
    """Three restore paths, and all three are needed - none subsumes another.

      try/finally      an exception propagating out of the run, including the
                       KeyboardInterrupt that tests/test_suite_isolation.py's
                       probe injects. Restores SYNCHRONOUSLY, before the
                       exception leaves this frame.
      atexit           a clean exit down a path that skips the finally.
      SIGTERM/SIGINT   a signal, which runs NEITHER of the above. This is the
                       one that matters in practice: a tool-call timeout sends
                       SIGTERM, and that is how a battery usually dies.

    The first was missing in this file's first draft, on the reasoning that
    atexit was the stronger guard. It is stronger against signals and weaker
    here: MutationHarnessRestoreTests caught it immediately, because atexit had
    not run yet at the moment the probe fingerprinted the tree. A stranded
    mutant is a plausible-looking edit in the file that runs the garden.
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
    print("CONTROL A - clean tree must be GREEN")
    print("=" * 70)
    ok, out = run_suites()
    if not ok:
        print(out)
        print("\nCONTROL A FAILED - the suite does not pass on a clean tree.")
        print("This is NO DATA, not a score. Fix the suite before reading anything.")
        return 1
    print("  GREEN - suites pass on the clean tree\n")

    print("=" * 70)
    print("CONTROL B - a deliberately broken tree must be RED")
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
            # whole suite tested the harness, not the behaviour.
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
