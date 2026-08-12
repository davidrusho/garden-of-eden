#!/usr/bin/env python3
"""Mutation battery for the refused-CONNACK gate and the escaped decode line
(T-527.11).

Scores tests/test_connack_refusal.py, tests/test_ha_birth_message.py,
tests/test_retired_entities.py and tests/test_water_interlock.py. The middle two
are in the list because the change inserts a return in front of everything they
exercise: an over-correction that gated a HEALTHY connect would leave this
ticket's own suite perfectly green while silencing the device, which is the
2026-08-05 outage again. The last was added in T-527.11 remediation - see the
note on SUITES, including a measurement of what it does and does not catch.

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

     The largest instance of this was found by review after the second version
     of this battery scored 27/27, and it was not in the diff at all: the
     CALLBACK SIGNATURES. paho 2.0.0 calls a VERSION2 on_connect and
     on_disconnect with five positional arguments and never omits the fifth,
     while every call site in this repo's suite passed four - so `properties`
     could be deleted from either signature with everything green and the device
     in a permanent crash loop. Mutants 28-30. on_message was checked in the
     same pass and is not exposed: paho passes it three arguments under every
     callback API version, so there is no divergence to be caught out by. It is
     pinned anyway, because "we checked" ages worse than an assertion.

A SECOND PAYLOAD SINK was found by review AFTER the first version of this
battery scored 25/25: `logger.error(f"Invalid water low cm value: {payload}")`
in the water/low/cm/set handler, still raw. No mutant could have caught it -
there was nothing to perturb, because the guard was simply absent from that
line, and a kill count cannot go down for code nobody wrote a mutant against.
Mutant 22 covers it now, and mutant 23 covers the source-level rule, which is
the only thing able to notice a THIRD sink being added raw.

A THIRD BLIND SPOT was found by review after the second version scored 27/27:
mqtt.py's threshold-rejection sink is written across two lines (the call on one,
the f-string on the next), and the source-level rule test filtered for
'logger.' and '{payload}' on the SAME PHYSICAL LINE. A raw payload could be
planted there with the whole suite staying green at 23 tests, OK - measured. The
rule test is now an ast scan of the call rather than a line filter, and mutants
31-34 are the four shapes that defeated the old one: the multi-line f-string,
%-style lazy logging, str.format(), and `!s`.

`{payload!a}` IS scored now (mutant 33) and the reasoning for previously
excluding it still stands and is worth keeping straight: ascii() escapes \\n,
\\r and \\x1b exactly as repr() does, so `!a` is NOT a forgery path, and while
the only tests were safety tests it was a genuinely equivalent mutant whose
survival said nothing. It is killed by a CANONICAL-SPELLING test, not a safety
one, and the two are deliberately separate assertions with separate messages.
Scoring it as though it were a security hole would be the same kind of error the
"more dangerous of the two" comment was.

Run:  python3 tests/mutate_connack_refusal.py

THREE CONTROLS GATE EVERY RESULT AND ALL MUST HOLD BEFORE ANY VERDICT IS READ.
A battery scores a mutant by whether the test run FAILED, so a broken scorer
reports every mutant caught - the most reassuring output available, and the one
that goes straight into a summary as proof of rigour:

  CONTROL A  clean tree                -> must be GREEN (positive control, and
             it runs FIRST as a gate: a red clean tree is NO DATA, not a score)
  CONTROL B  deliberately broken code  -> must be RED (negative control)
             A alone is worthless: it is scored by the same path that may be
             broken, so only B proves the scorer can tell pass from fail.
  CONTROL C  compiles, dies at import  -> must score NO VERDICT (positive
             control for the third verdict; see score_run()). Without it, "0
             no-verdict mutants" is equally consistent with the rule working
             and with the rule being unreachable.

Mechanics, each of which has bitten this repo:
  * every mutant gated on compile() BEFORE the write. A previous battery in
    this repo scored an IndentationError as a kill: the mutant broke collection,
    every suite reddened, and the behaviour under test never ran. Compiling the
    candidate string also keeps invalid Python off disk entirely.
  * the count of NAMED failing cases DECIDES the verdict, rather than being
    printed beside one. compile() stops syntax errors; it does nothing about a
    mutant that compiles and dies at IMPORT, which reddens every suite with zero
    named cases. This file used to print `killed (0 failing case(s))` - the
    documented tell - and then count it as a kill anyway, so the tell corrected
    nothing. A zero-named-case red is now 'no verdict', reported in its own list
    and non-zero on exit.
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
import re
import shutil
import signal
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MQTT = os.path.join(REPO, "mqtt.py")

# tests.test_water_interlock was added in T-527.11 remediation. It asserts the
# decode line's literal source (test_inbound_decode_is_recorded) and mqtt.py's
# logger level, so it guards lines this battery mutates and belongs in the
# blast radius.
#
# MEASURED, because the reason it was added overstated what it does: of mutants
# 18-21 it catches only 21, the one that DELETES the decode line. Its assertion
# matches the source up to `{topic!r}` and stops, so every mutant that changes
# only the PAYLOAD's conversion (18, 19, 20) leaves it green, as does 22 and the
# multi-line sink mutants. (Since T-527.12 the literal it matches includes the
# topic's own `!r`, so it does now catch a topic un-escaping at that line - a
# side effect of the spelling rather than a coverage decision, and the mutants
# below are what cover topics on purpose.) It therefore adds no kill this
# battery did not
# already have. What it adds is an independent pin on the decode line's
# existence, held by a suite with different stubs and a different reason to
# exist - which is worth one extra subprocess per mutant and is not worth
# describing as coverage it does not provide.
SUITES = ["tests.test_connack_refusal", "tests.test_ha_birth_message",
          "tests.test_retired_entities", "tests.test_water_interlock"]

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
     '        logger.info(f"Decoded payload on {topic!r}: {payload!r}")',
     "        logger.info(f\"Decoded payload on {topic!r}: '{payload}'\")"),

    ("interpolate raw and unquoted",
     '        logger.info(f"Decoded payload on {topic!r}: {payload!r}")',
     '        logger.info(f"Decoded payload on {topic!r}: {payload}")'),

    ("!r -> !s - str() escapes nothing",
     '        logger.info(f"Decoded payload on {topic!r}: {payload!r}")',
     '        logger.info(f"Decoded payload on {topic!r}: {payload!s}")'),

    ("delete the decode line - no record of what arrived at all",
     '        logger.info(f"Decoded payload on {topic!r}: {payload!r}")\n',
     ""),

    ("un-escape the water threshold rejection - the sink review found raw",
     '                logger.error(f"Invalid water low cm value: {payload!r}")',
     '                logger.error(f"Invalid water low cm value: {payload}")'),

    # --- the TOPIC sinks (T-527.12) -----------------------------------------
    #
    # Added after review. The T-527.12 commit escaped three topic sinks in
    # mqtt.py and re-anchored six existing mutants onto the new spelling -
    # which left the production change with **no mutant of its own in any
    # battery**. All four batteries scored 100% and none of them was asking the
    # question the commit had just answered. A re-anchored mutant scores an
    # unchanged question; only these three perturb the change itself.
    #
    # One per sink rather than one for the set, because each is a separate line
    # a maintainer could un-escape independently, and because the third is the
    # only one reachable from the handler path.
    ("un-escape the topic on the decode line - the T-527.12 defect, restored",
     '        logger.info(f"Decoded payload on {topic!r}: {payload!r}")',
     '        logger.info(f"Decoded payload on {topic}: {payload!r}")'),

    ("un-escape the topic on the undecodable-payload line",
     '        logger.error(f"Failed to decode message on topic {topic!r}. '
     'Likely binary.")',
     '        logger.error(f"Failed to decode message on topic {topic}. '
     'Likely binary.")'),

    ("un-escape the topic on the catch-all handler",
     '        logger.exception(f"Error handling message on topic {topic!r}: '
     '{e!r}")',
     '        logger.exception(f"Error handling message on topic {topic}: '
     '{e!r}")'),

    # --- the undecodable-topic guard (T-527.12, from review) -----------------
    #
    # The defect this restores killed the process: `msg.topic` re-decodes on
    # every access, so reading it inside the handler for its own
    # UnicodeDecodeError raised out of on_message, out of loop_forever(), into
    # a permanent ten-second Restart=always loop with the light off.
    #
    # Both directions, and the second is the one that matters. Deleting a guard
    # is the mutation no maintainer makes; re-reading `msg.topic` inside the
    # guard's OWN handler is the shape somebody writes back while tidying (it
    # reads as "log the topic that failed"), and it reintroduces the crash
    # exactly.
    #
    # Note what is NOT here and why, so the absence is not read as coverage:
    # putting `msg.topic` back in the PAYLOAD handler below is harmless now.
    # The guard returns early on an undecodable topic, so that line can never
    # see one, and a mutant there would survive honestly.
    ("delete the decode-once guard - an undecodable topic kills the process",
     "    try:\n        topic = msg.topic\n    except (UnicodeDecodeError, AttributeError):",
     "    topic = msg.topic\n    if False:"),

    # Narrowing, not deleting - the mutation a maintainer plausibly makes.
    # AttributeError was added to the guard on review: the property is
    # `self._topic.decode(...)`, so a `_topic` holding anything but bytes
    # raises there and exits the process by the same route.
    ("narrow the guard back to UnicodeDecodeError - AttributeError exits again",
     "    except (UnicodeDecodeError, AttributeError):",
     "    except UnicodeDecodeError:"),

    ("the guard's own handler reads msg.topic again - the original re-raise",
     '                     "decoded: %r", getattr(msg, "_topic", None))',
     '                     "decoded: %r", msg.topic)'),

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

    # --- 28-30: the callback ARITY contract ---------------------------------
    # paho 2.0.0 calls a VERSION2 on_connect / on_disconnect with FIVE
    # positional arguments and never omits the fifth. Every call site this
    # repo's suite had before T-527.11 passed FOUR, so each of these used to
    # SURVIVE while producing a TypeError inside the callback on every connect
    # in production - re-raised by paho (suppress_exceptions is False), out of
    # loop_forever(), process exit, and Restart=always turns that into a
    # permanent crash loop with the grow light off.
    ("drop `properties` from on_connect - TypeError on every real CONNACK",
     "def on_connect(client, userdata, flags, rc, properties=None):",
     "def on_connect(client, userdata, flags, rc):"),

    ("drop `flags` from on_connect - the 5-arg call binds rc to the wrong slot",
     "def on_connect(client, userdata, flags, rc, properties=None):",
     "def on_connect(client, userdata, rc, properties=None):"),

    ("drop `properties` from on_disconnect - nothing called it at all before",
     "def on_disconnect(client, userdata, flags, rc, properties=None):",
     "def on_disconnect(client, userdata, flags, rc):"),

    # --- 31-34: the payload sink the LINE-BASED guard could not see ---------
    # mqtt.py's threshold-rejection sink puts the call on one line and the
    # f-string on the next. The pre-remediation guard filtered for 'logger.'
    # and '{payload}' on the same physical line, so all four of these SURVIVED
    # and the suite stayed green at 23 tests, OK. Measured, not assumed.
    ("raw payload in the MULTI-LINE sink - invisible to a line filter",
     '                        f"Rejecting water low threshold {payload!r} - "',
     '                        f"Rejecting water low threshold {payload} - "'),

    ("!s in the multi-line sink - reads right, escapes nothing",
     '                        f"Rejecting water low threshold {payload!r} - "',
     '                        f"Rejecting water low threshold {payload!s} - "'),

    # !a is NOT a forgery path - ascii() escapes \n, \r and \x1b exactly as
    # repr() does - so this is scored by the CANONICAL-SPELLING test, not by a
    # safety one. Kept separate from the mutants above on purpose: conflating
    # "safe but spelled differently" with "a remote client can forge log lines"
    # is how a rule stops meaning anything.
    ("!a in the multi-line sink - safe, but not the spelling the rule states",
     '                        f"Rejecting water low threshold {payload!r} - "',
     '                        f"Rejecting water low threshold {payload!a} - "'),

    ("%-style lazy logging in the multi-line sink - logging interpolates it raw",
     "                    logger.error(\n"
     '                        f"Rejecting water low threshold {payload!r} - "\n'
     '                        f"must be 0 (disabled) or within "\n'
     '                        f"{WATER_VALID_MIN_CM:.2f}-{WATER_VALID_MAX_CM:.2f}cm"\n'
     "                    )",
     "                    logger.error(\n"
     '                        "Rejecting water low threshold %s - "\n'
     '                        "must be 0 (disabled) or within "\n'
     '                        f"{WATER_VALID_MIN_CM:.2f}-{WATER_VALID_MAX_CM:.2f}cm",\n'
     "                        payload\n"
     "                    )"),

    ("str.format() in the single-line sink - the third shape a line filter misses",
     '                logger.error(f"Invalid water low cm value: {payload!r}")',
     '                logger.error("Invalid water low cm value: {}".format(payload))'),
]

# Control B. A deliberately broken tree that MUST score RED. Kept distinct from
# every scored mutant above so that a failure here is unambiguous - it is the
# scorer being tested, not one of the mutants.
CONTROL_B = ("CONTROL B: deliberately broken - the refusal gate never fires",
             "    if _connack_refused(rc):",
             "    if False and _connack_refused(rc):")

# Control C. The gap the compile() gate does NOT close, and the reason
# score_run() below refuses to call a zero-named-case red a kill.
#
# `threadingg.Lock()` is valid Python - compile() passes it, so it reaches disk
# - and then dies at IMPORT time with a NameError. Every suite exits 1 with no
# FAIL: or ERROR: line anywhere in its output, because unittest never got as far
# as collecting a case. Before T-527.11 this scored as `killed (0 failing
# case(s))` and was appended to `killed` regardless, so the headline count went
# UP for a mutant whose behaviour was never exercised.
#
# This control fires that shape deliberately and asserts the harness classifies
# it as NO VERDICT. It is a POSITIVE control for the scoring rule: without it,
# "0 no-verdict mutants" is equally consistent with the rule working and with
# the rule never being reachable.
# A MISSING IMPORT, NOT A TYPO'D NAME, and the change is deliberate
# (T-527.18). This control previously read `threadingg.Lock()`, which raises
# only because that particular assignment happens to execute at module scope —
# move the line inside a function and the control silently stops testing
# anything, while still looking correct. An import statement cannot be moved
# out of the import. It is also the exact shape the scoring rule was WRONG
# about: unittest reports an unimportable module as a named ERROR via
# `unittest.loader._FailedTest`, so the zero-named-cases tell never fired and
# this control was passing for a reason it did not state.
CONTROL_C = ("CONTROL C: compiles, dies at import - must score NO VERDICT",
             "_publisher_threads_lock = threading.Lock()",
             "_publisher_threads_lock = threading.Lock()\n"
             "import a_module_that_certainly_does_not_exist")

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


def ran_count(out):
    """How many tests actually RAN, summed across the suites in `out`."""
    return sum(int(n) for n in re.findall(r"Ran (\d+) tests?", out))


def score_run(ok, out, clean_ran=None):
    """Turn one suite run into a verdict: 'survived', 'killed' or 'no-verdict'.

    THE THIRD VERDICT IS THE POINT. A battery scores by colour, so anything that
    reddens the run reads as a kill - including a mutant that never let the
    behaviour under test execute at all. The compile() gate keeps SYNTAX errors
    off disk; it does nothing about a mutant that compiles and then dies at
    import (a misspelled name, a bad attribute at module scope, an exception in
    a top-level call). Those redden every suite with ZERO named failing cases,
    which is the documented tell - and until T-527.11 this file printed that tell
    and then appended the mutant to `killed` anyway, so the count it was meant to
    correct never moved.

    A red run with no named case is therefore NOT a verdict. It is the same
    class of result as a mutant that would not apply: no information about the
    suite, and it must not be counted for or against it.
    """
    if ok:
        return "survived", []
    fails = [line for line in out.splitlines()
             if line.startswith(("FAIL:", "ERROR:"))]
    # THE ZERO-NAMED-CASES TELL DOES NOT COVER THE ImportError FAMILY, which is
    # the half this rule was believed to cover and did not (T-527.18). unittest
    # wraps an unimportable module in `unittest.loader._FailedTest` and reports
    # it as an ordinary NAMED ERROR, so `fails` is non-empty and the mutant
    # scored as a kill while the behaviour under test never executed. The
    # ran-count catches it: no honest mutant changes how many tests are
    # COLLECTED, only how many pass. Passed in rather than recomputed so the
    # comparison is always against THIS run's own clean baseline.
    if clean_ran is not None and ran_count(out) != clean_ran:
        return "no-verdict", fails
    return ("killed" if fails else "no-verdict"), fails


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
    clean_ran = ran_count(out)
    if clean_ran == 0:
        print("\nCONTROL A FAILED - a green run that collected NO tests. NO DATA.")
        return 1
    print(f"  GREEN - suites pass on the clean tree ({clean_ran} tests)\n")

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
    print("CONTROL C (positive, for the no-verdict rule) - an import-time break")
    print("that COMPILES must be classified NO VERDICT, never a kill")
    print("=" * 70)
    label, anchor, replacement = CONTROL_C
    if not apply_mutation(anchor, replacement):
        print("\nCONTROL C could not be applied. NO DATA.")
        restore()
        return 1
    ok, out = run_suites()
    restore()
    verdict, fails = score_run(ok, out, clean_ran)
    if verdict != "no-verdict":
        print(f"  scored '{verdict}' with {len(fails)} named failing case(s), "
              f"but it MUST score 'no-verdict'.")
        print("\nCONTROL C FAILED - either the scoring rule is not doing its job,")
        print("or this mutant no longer reproduces the shape it was written for.")
        print("Either way the no-verdict path is unproven. NO DATA.")
        return 1
    print("  NO VERDICT - an import-time break is not counted as a kill\n")

    print("=" * 70)
    print(f"{len(MUTANTS)} MUTANTS")
    print("=" * 70)
    killed, survived, unapplied, no_verdict = [], [], [], []
    for i, (label, anchor, replacement) in enumerate(MUTANTS, 1):
        print(f"[{i}/{len(MUTANTS)}] {label}")
        if not apply_mutation(anchor, replacement):
            unapplied.append(label)
            restore()
            continue
        ok, out = run_suites()
        restore()
        # Read WHY it died, not just the colour - see score_run().
        verdict, fails = score_run(ok, out, clean_ran)
        if verdict == "survived":
            print("  SURVIVED - no test noticed")
            survived.append(label)
        elif verdict == "no-verdict":
            print("  NO VERDICT - the suites went red with ZERO named failing "
                  "cases, so the behaviour under test never ran")
            no_verdict.append(label)
        else:
            print(f"  killed ({len(fails)} failing case(s))")
            for line in fails[:3]:
                print(f"      {line}")
            killed.append(label)

    print("\n" + "=" * 70)
    print(f"RESULT: {len(killed)} killed, {len(survived)} survived, "
          f"{len(no_verdict)} no verdict, {len(unapplied)} not applied, "
          f"of {len(MUTANTS)}")
    print("=" * 70)
    for label in survived:
        print(f"  SURVIVED  {label}")
    for label in no_verdict:
        print(f"  NO VERDICT  {label}")
    for label in unapplied:
        print(f"  NOT APPLIED  {label}")

    # The byte-identity assertion. Read this line before believing any score
    # above it - a run that exited is not a run whose cleanup ran.
    if sha(MQTT) == original_sha:
        print("\nTREE RESTORED: mqtt.py is byte-identical to its pre-run state.")
    else:
        print("\n*** TREE NOT RESTORED - mqtt.py DIFFERS. Fix before committing. ***")
        return 1
    return 0 if not survived and not unapplied and not no_verdict else 1


if __name__ == "__main__":
    sys.exit(main())
