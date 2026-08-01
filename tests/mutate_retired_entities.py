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

    ("delete a SURVIVING discovery block (the light)",
     "    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload)\n\n"
     "    # The Pump discovery block stood here (T-475).",
     "\n    # The Pump discovery block stood here (T-475)."),

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
    original_src = open(MQTT).read()
    original_sha = sha(MQTT)

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
    # Break something every suite depends on and nothing else could mask.
    broken = original_src.replace(
        'client.publish(topic, "", retain=True)',
        'client.publish("gardyn/CONTROL_B_BROKEN", "", retain=True)')
    if broken == original_src:
        print("ABORT: control-B anchor did not match. Harness is broken.")
        return 2
    open(MQTT, "w").write(broken)
    ok_b, _ = run_suites()
    open(MQTT, "w").write(original_src)
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
        open(MQTT, "w").write(mutated)

        # Prove the edit landed. A no-op replacement looks exactly like a
        # survivor, and it is the failure mode a kill count cannot show.
        if open(MQTT).read() == original_src:
            print(f"  [{i:2}] HARNESS ERROR (file unchanged): {label}")
            survived.append((label, "mutation did not apply"))
            open(MQTT, "w").write(original_src)
            continue

        ok_m, _ = run_suites()
        open(MQTT, "w").write(original_src)

        if ok_m:
            print(f"  [{i:2}] SURVIVED  {label}")
            survived.append((label, "suites stayed green"))
        else:
            killed += 1
            print(f"  [{i:2}] killed    {label}")

    print("\n" + "=" * 70)
    print(f"RESULT: {killed}/{len(MUTANTS)} killed")

    restored = sha(MQTT) == original_sha
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
