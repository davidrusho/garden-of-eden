#!/usr/bin/env python3
"""Mutation battery for the T-475 entity withdrawal.

The suites it scores are tests/test_retired_entities.py and
tests/test_water_interlock.py, and it exists because the change they cover is
almost entirely a DELETION - the case where a green suite says least:

  * Nearly every assertion is about an ABSENCE, and an absence is what a
    function that raises before publishing anything produces for free. So the
    mutants below include ones that make code RAISE, not only ones that make it
    publish the wrong thing.
  * A suite that merely tolerates an absence will not notice the absent thing
    COMING BACK. Six of the mutants reintroduce deleted code verbatim - a
    retired discovery block, a retired state publish, a retired publisher loop.
    Those are the ones this battery is really for.
  * A battery is evidence only for the code it MUTATES, and the easiest code to
    mutate is not the dangerous code. The irreversible action here is the
    interlock refusing to run a pump dry, so both of its guards are mutated
    explicitly rather than left to the kill count to imply.

Run:  python3 tests/mutate_retired_entities.py

THREE controls gate every result and ALL must hold before any verdict is read.
A battery scores a mutant by whether the test run FAILED, so a broken scorer
reports every mutant caught - the most reassuring output available, and the one
that goes straight into a summary as proof of rigour:

  CONTROL A  clean tree               -> must be GREEN
  CONTROL B  deliberately broken code -> must be RED
             (A alone is worthless: it is scored by the same path that may be
             broken, so only B proves the scorer can tell pass from fail.)
  CONTROL C  compiles, dies at import -> must score NO VERDICT
             (a POSITIVE control for the scoring rule: without it, "0
             no-verdict mutants" is equally consistent with the rule working
             and with the rule never being reachable.)

CONTROL C AND THE SCORING RULE ARRIVED HERE UNDER T-527.32, and until then this
file did not have them. It treated ANY red run as a kill, and reported 29/29 on
that basis - a figure that was not evidence, because a mutant that compiles and
then dies at import reddens every suite without the behaviour under test ever
executing. That is the same defect T-527.11 fixed in mutate_connack_refusal.py
and T-527.13 fixed in mutate_light_schedule.py; finding it a third time is why
the rule now lives in tests/mutation_scoring.py rather than being restated in
each harness. It is pinned by tests/test_mutation_scoring.py.

Mechanics that have bitten this repo before, all handled:
  * every mutant gated on compile() BEFORE the write, so invalid Python never
    reaches disk - not after, which leaves broken source there for the length
    of a call, in the exact window the restore handlers exist to close.
  * __pycache__ purged before every run and PYTHONDONTWRITEBYTECODE=1 set. .pyc
    validity keys on (mtime-seconds, size), so a mutation applied and reverted
    inside one second can silently re-run the previous bytecode.
  * stderr merged into stdout - unittest reports there, so 2>/dev/null would
    blank the output being grepped.
  * every anchor required to match EXACTLY once, and the mutated file compared
    against the original, because a replacement that matches nothing exits
    successfully and is indistinguishable from one that changed nothing.
  * restore on try/finally AND atexit AND SIGTERM/SIGINT. None subsumes
    another: `finally` covers an exception, atexit covers an exit down a path
    that skips it, and a signal runs neither.
  * the tree asserted byte-identical at the end. Read that line before
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

MQTT = os.path.join(REPO, "mqtt.py")

SUITES = ["tests.test_retired_entities", "tests.test_water_interlock"]

# A verbatim copy of the Pump discovery block as it stood before T-475. Used by
# the first mutant: the central risk of a deletion-shaped change is not that the
# deletion fails, it is that the deleted thing comes back and nothing notices.
PUMP_BLOCK_RESTORED = '''    TEMP_CONFIG_TOPIC = "homeassistant/light/gardyn/"+IDENTIFIER+"_pump/config"
    temp_config_payload = {
        "name": "Pump",
        "unique_id": IDENTIFIER + "_pump",
        "platform": "mqtt",
        "state_topic": BASE_TOPIC + "/pump/state",
        "command_topic": BASE_TOPIC + "/pump/command",
        "icon": "mdi:water-pump",
        "qos": 1,
        "device": device_info
    }
    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload)
    # The Pump discovery block stood here (T-475).'''

TEMPERATURE_BLOCK_RESTORED = '''    TEMP_CONFIG_TOPIC = "homeassistant/sensor/gardyn/"+IDENTIFIER+"_temperature/config"
    temp_config_payload = {
        "name": "Temperature",
        "unique_id": IDENTIFIER + "_temperature",
        "state_topic": BASE_TOPIC + "/temperature",
        "unit_of_measurement": "\\u00b0C",
        "device_class": "temperature",
        "device": device_info
    }
    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload)
    # Discovery configuration for Camera A (image entity)'''

MUTANTS = [
    # --- publish_light_decision (T-527.6/.7) --------------------------------
    # ADDED because a reviewer proved this function was executed by NO TEST in
    # the repository: replacing its whole body left all 931 tests green. It is
    # the single writer of gardyn/light/state, .../brightness/state and
    # .../source now that T-527.6 removed the command handlers' own publishes,
    # so a silent break here means Home Assistant sits on a retained value from
    # before the deploy — showing the lamp ON all night — with nothing red.
    ("the lamp's ON/OFF and brightness state stop being published, so HA keeps "
     "whatever the broker still holds",
     "    publish_light_state(client)\n"
     "    client.publish(LIGHT_SOURCE_TOPIC, decision.source, retain=True)",
     "    client.publish(LIGHT_SOURCE_TOPIC, decision.source, retain=True)"),
    ("the owner topic is not retained, so a reconnecting HA is never told who "
     "holds the lamp",
     "    client.publish(LIGHT_SOURCE_TOPIC, decision.source, retain=True)",
     "    client.publish(LIGHT_SOURCE_TOPIC, decision.source)"),
    ("the owner is published to the wrong topic, which no subscriber reads",
     "    client.publish(LIGHT_SOURCE_TOPIC, decision.source, retain=True)",
     '    client.publish(LIGHT_SOURCE_TOPIC + "_x", decision.source, retain=True)'),
    ("the DECISION's brightness is published instead of the hardware's, so a "
     "failed drive reads as success",
     "    duty = light.get_brightness()",
     "    duty = 100.0"),

    # --- reintroduce deleted code (the central risk of this change) ---------
    ("REINTRODUCE the Pump discovery block",
     "    # The Pump discovery block stood here (T-475).",
     PUMP_BLOCK_RESTORED),

    ("REINTRODUCE the Temperature discovery block",
     "    # Discovery configuration for Camera A (image entity)",
     TEMPERATURE_BLOCK_RESTORED),

    ("REINTRODUCE a retained pump state publish on the command path",
     "                start_pump(speed, client)",
     '                start_pump(speed, client)\n'
     '                client.publish(BASE_TOPIC + "/pump/state", "ON", retain=True)'),

    ("REINTRODUCE a retired publisher loop, which would refill the cleared topic",
     "    for target in (publish_pcb_temperature, publish_images):",
     "    for target in (publish_pcb_temperature, publish_images, publish_images):"),

    ("REINTRODUCE a water level publish inside the read probe",
     '                logger.info(f"Reservoir probe: {distance:.2f}cm")',
     '                logger.info(f"Reservoir probe: {distance:.2f}cm")\n'
     '                client.publish(BASE_TOPIC + "/water/level",\n'
     '                               f"{distance:.2f}", qos=1, retain=True)'),

    ("REINTRODUCE the temperature/get subscription",
     '    (BASE_TOPIC + "/pcb/temperature/get", 0),',
     '    (BASE_TOPIC + "/pcb/temperature/get", 0),\n'
     '    (BASE_TOPIC + "/temperature/get", 0),'),

    # --- break the clear itself --------------------------------------------
    ("clear WITHOUT retain=True - deletes nothing, looks like it worked",
     '        client.publish(topic, "", retain=True)',
     '        client.publish(topic, "")'),

    ("clear with a NON-EMPTY payload - a normal publish, not a delete",
     '        client.publish(topic, "", retain=True)',
     '        client.publish(topic, "cleared", retain=True)'),

    ("drop a discovery topic from the clear list",
     '    f"homeassistant/sensor/gardyn/{IDENTIFIER}_water_low_mode/config",',
     "",),

    ("drop a retained state topic from the clear list",
     '    BASE_TOPIC + "/pump/speed/state",',
     "",),

    ("drop the water trust topic from the clear list",
     '    BASE_TOPIC + "/water/status",',
     "",),

    ("OVER-BROAD clear: withdraw a surviving entity too",
     '    BASE_TOPIC + "/pump/state",',
     '    BASE_TOPIC + "/pump/state",\n    BASE_TOPIC + "/light/state",'),

    # --- break the ordering / the connect path ------------------------------
    ("reorder: clear AFTER discovery instead of before",
     "    clear_retired_entities(client)\n"
     '    # Clear the retained "offline" the broker may have left from the last death.\n'
     "    # Publish before discovery so HA never sees an entity announced unavailable.\n"
     '    client.publish(STATUS_TOPIC, "online", qos=1, retain=True)\n'
     "    send_discovery_messages(client)",
     '    client.publish(STATUS_TOPIC, "online", qos=1, retain=True)\n'
     "    send_discovery_messages(client)\n"
     "    clear_retired_entities(client)"),

    ("drop the clear from on_connect entirely",
     "    clear_retired_entities(client)\n",
     ""),

    ("RAISE inside clear_retired_entities - takes the SURVIVORS down with it",
     "    for topic in RETIRED_DISCOVERY_TOPICS + RETIRED_STATE_TOPICS:",
     '    raise RuntimeError("clear exploded")\n'
     "    for topic in RETIRED_DISCOVERY_TOPICS + RETIRED_STATE_TOPICS:"),

    # Added after review: the first two SURVIVED the original battery, because
    # the suite asserted membership of COMMAND_SUBSCRIPTIONS - the module
    # agreeing with itself - while the fake client's subscribe() swallowed its
    # argument. Deleting the call silences every inbound command, including the
    # only runtime path to the interlock's threshold, and every
    # absence-assertion in the suite stayed green.
    ("delete client.subscribe() - silences every command, publishes nothing wrong",
     "    client.subscribe(COMMAND_SUBSCRIPTIONS)\n",
     ""),

    ("subscribe to an EMPTY list instead of the command topics",
     "    client.subscribe(COMMAND_SUBSCRIPTIONS)",
     "    client.subscribe([])"),

    ("drop the interlock's threshold topic from the subscriptions",
     '    (BASE_TOPIC + "/water/low/cm/set", 1),\n',
     ""),

    ("downgrade the commands to QoS 0, so the broker discards them when offline",
     '    (BASE_TOPIC + "/pump/command", 1),',
     '    (BASE_TOPIC + "/pump/command", 0),'),

    # STALE SINCE b95e263, AND THAT IS THE FINDING. T-527.6 inserted the light
    # SOURCE discovery block between this anchor's two halves, so the anchor
    # stopped matching and this mutant has been reported NOT APPLIED — i.e.
    # unverified — ever since, in the same commit that shipped four failing
    # tests. Re-anchored on the light's own publish_config call, which is what
    # the mutant is actually about and which no neighbouring insertion can
    # split.
    # Anchored on the light's UNIQUE topic rather than on its publish_config
    # call, because `publish_config(TEMP_CONFIG_TOPIC, temp_config_payload)`
    # appears four times — the file reuses those two names as scratch
    # variables for every entity, so any call-site anchor is ambiguous and any
    # surrounding-context anchor breaks the next time somebody inserts a block
    # nearby. Announcing under a topic Home Assistant does not read is
    # equivalent to deleting the entity, from HA's point of view.
    ("delete a SURVIVING discovery block (the light)",
     '    TEMP_CONFIG_TOPIC = "homeassistant/light/gardyn/"+IDENTIFIER+"_light/config"',
     '    TEMP_CONFIG_TOPIC = "homeassistant/light/gardyn/"+IDENTIFIER+"_gone/config"'),

    # --- the interlock: the irreversible action, mutated explicitly ---------
    ("INVERT the interlock's no-reading refusal (fail OPEN on unknown water)",
     "    if distance is None:\n"
     '        logger.warning("Refusing to start pump: no trustworthy reservoir reading")',
     "    if distance is not None and False:\n"
     '        logger.warning("Refusing to start pump: no trustworthy reservoir reading")'),

    ("INVERT the interlock's low-water comparison",
     "    if distance > WATER_LOW_CM:",
     "    if distance < WATER_LOW_CM:"),

    ("delete the interlock's low-water refusal outright",
     "    if distance > WATER_LOW_CM:",
     "    if False:"),

    ("disarm the threshold validation, so nan can fail the interlock OPEN",
     "                if not _threshold_is_acceptable(candidate):",
     "                if False:"),

    ("widen the plausibility band to swallow a dead sensor's 0.09cm",
     "    if not (WATER_VALID_MIN_CM <= distance <= WATER_VALID_MAX_CM):",
     "    if not (0 <= distance <= 100):"),
]

# Break something every suite depends on and nothing else could mask.
CONTROL_B = ("CONTROL B: a deliberately broken tree - must be RED",
             'client.publish(topic, "", retain=True)',
             'client.publish("gardyn/CONTROL_B_BROKEN", "", retain=True)')

# CONTROL C - the gap compile() does NOT close.
#
# A MISSING IMPORT, not a typo'd name. A typo raises only because that
# particular statement happens to execute at module scope; move it inside a
# function and the control silently stops testing anything while still looking
# correct. An import statement cannot be moved out of the import.
#
# The anchor is a module-scope assignment, so the appended import runs at
# import time. This compiles cleanly - so it reaches disk past the compile gate
# - and then dies when unittest imports the module, which unittest reports as a
# NAMED error through `unittest.loader._FailedTest`. That is why the
# zero-named-cases tell alone cannot see this shape, and why score_run compares
# the COLLECTED count against the clean baseline.
CONTROL_C = ("CONTROL C: compiles, dies at import - must score NO VERDICT",
             "_publisher_threads_lock = threading.Lock()",
             "_publisher_threads_lock = threading.Lock()\n"
             "import a_module_that_certainly_does_not_exist")

_ORIGINAL_SRC = None


def run_suites():
    """Return (all_passed, combined_output). stderr merged - unittest uses it."""
    purge_pycache(REPO)
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


def restore():
    """Put mqtt.py back, whatever happened.

    Without this a battery killed part-way through - ^C, a timeout, an
    exception in the harness itself - leaves a mutant applied in the working
    tree, which is a silent and entirely plausible-looking change to the file
    that runs the garden.
    """
    if _ORIGINAL_SRC is None:
        return
    if sha(MQTT) != hashlib.sha256(_ORIGINAL_SRC.encode()).hexdigest():
        with open(MQTT, "w") as fh:
            fh.write(_ORIGINAL_SRC)


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
    mutated = src.replace(anchor, replacement, 1)
    if mutated == src:
        print("  replacement changed nothing - mutation NOT applied, no verdict")
        return False
    refusal = compile_gate(mutated, MQTT)
    if refusal:
        print(f"  {refusal}")
        return False
    with open(MQTT, "w") as fh:
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
    original_sha = sha(MQTT)

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
    _, anchor, replacement = CONTROL_B
    if not apply_mutation(anchor, replacement):
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
    _, anchor, replacement = CONTROL_C
    if not apply_mutation(anchor, replacement):
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
    for i, (label, old, new) in enumerate(MUTANTS, 1):
        print(f"[{i}/{len(MUTANTS)}] {label}")
        if not apply_mutation(old, new):
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
    if original_sha is not None and sha(MQTT) == original_sha:
        print("\nTREE RESTORED: mqtt.py is byte-identical to its pre-run state.")
    else:
        print("\n*** TREE NOT RESTORED - mqtt.py DIFFERS. Fix before committing. ***")
        return 1
    return 0 if not (survived or unapplied or no_verdict) else 1


if __name__ == "__main__":
    sys.exit(main())
