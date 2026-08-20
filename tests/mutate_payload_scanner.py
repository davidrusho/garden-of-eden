#!/usr/bin/env python3
"""Mutation battery for the payload-sink SCANNER itself (T-527.17).

WHY A SEPARATE HARNESS FROM mutate_connack_refusal.py

That battery mutates `mqtt.py` - the production file - and asks whether the
suite notices a forgery sink appearing in it. It cannot ask the question this
one asks, because the thing under test here lives in the TEST file:
`_payload_sinks()` and the eighty-odd lines around it in
tests/test_connack_refusal.py.

The distinction is the whole reason T-527.17 existed. That scanner was the only
thing standing between a hostile MQTT payload and a forged line in gardyn.log,
and it had two confirmed escapes and four false accusations - all of them
invisible to a battery that only ever perturbs mqtt.py, because a mutant on the
module cannot narrow the rule that reads it. The repo has paid for this lesson
once already (2026-08-08, in this ticket's Improvements Log): "mutate the test
file's own constants, not only the module's. A guard on an architectural
promise is exactly the kind of thing that gets quietly narrowed, and no mutant
on the module can see it."

WHAT EACH MUTANT DOES

Each one restores a specific piece of the pre-T-527.17 scanner - the narrow
seed, the receiver-keyed sink, the missing propagation, the absent format
parsing - and asserts that a named test goes red. A survivor means the widening
it undoes is not actually pinned by anything, which is the same state the
scanner shipped in.

RUNS IN A shutil.copytree SANDBOX. The working tree is never written to, which
is what makes this safe to run beside a live session; the byte-identity line at
the end is the evidence, and it should be read rather than assumed - a wait
loop proves a process exited, not that its cleanup ran.

    python3 tests/mutate_payload_scanner.py
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# `ran_count` and the compile gate are IMPORTED, not restated (T-550). The
# `ran_count` copy that lived here was still the unanchored
# `re.findall(r"Ran (\d+) tests?")` form after the shared rule was anchored in
# T-527.36.
#
# `score` and `named_failures` below are NOT replaced by the shared
# `score_run` / `named_failures`, and the reason is behavioural rather than
# stylistic: this harness's `named_failures` returns the failing test's NAME
# (`^(?:FAIL|ERROR): (\S+)`) because its per-mutant report prints
# `-> test_name` and its MUTANTS table names the test that must notice, while
# the shared helper returns whole lines. Its `score` is also deliberately
# one-sided - `ran < clean_ran`, a DROP - where the shared rule uses `!=`.
# Swapping either would change this battery's output format and its verdict
# rule in one step, which is a different change from consolidating the count.
from tests.mutation_scoring import compile_gate, ran_count  # noqa: E402

TARGET_REL = os.path.join("tests", "test_connack_refusal.py")

# Only the suite that holds the scanner. The sibling suites in
# mutate_connack_refusal.py's SUITES list read mqtt.py, not this scanner, so
# adding them would triple the runtime for no additional kill. Stated rather
# than left implicit, because "which suites ran" is exactly the kind of scope
# that reads as broader than it is.
SUITES = ["tests.test_connack_refusal"]

# (label, anchor, replacement, the test that must notice)
MUTANTS = [
    ("seed narrowed back to the bare name - msg.payload stops being a source",
     "    return ((isinstance(node, ast.Name) and node.id in _TAINT_SEEDS)\n"
     "            or (isinstance(node, ast.Attribute) and node.attr in _TAINT_SEEDS))",
     "    return (isinstance(node, ast.Name) and node.id in _TAINT_SEEDS)",
     "reports_the_shapes_that_defeated_the_old_one"),

    # T-527.12. Per NAME, for the same reason the sink-method mutants below are
    # per name: the defect this guards is a seed going MISSING from the set,
    # which is a one-word edit, not the set being emptied.
    #
    # The expected killer is the POSITIVE CONTROL, deliberately, not
    # test_the_seed_set_is_pinned_per_name - that one also goes red and would
    # be a circular kill, since it asserts the very constant this mutates.
    # `expect` is matched against unittest's `FAIL: <method>` names, which stop
    # at the first space and therefore never contain the class, so it has to be
    # a method-name fragment rather than the suite's name.
    ("the topic seed dropped - msg.topic stops being a source again",
     '_TAINT_SEEDS = frozenset({"payload", "topic"})',
     '_TAINT_SEEDS = frozenset({"payload"})',
     "a_raw_topic_is_reported"),

    ("taint propagation removed - H1, a bound intermediate, escapes again",
     "    names = set()\n    changed = True",
     "    return frozenset()\n    names = set()\n    changed = True",
     "reports_the_shapes_that_defeated_the_old_one"),

    ("propagation made single-pass - a three-hop chain outruns it",
     "    changed = True\n    while changed:",
     "    changed = True\n    for _once in range(1):",
     "reports_the_shapes_that_defeated_the_old_one"),

    ("sinks keyed on the receiver again - H2, self.logger and logging escape",
     "            if isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS:",
     "            if (isinstance(func, ast.Attribute)\n"
     "                    and func.attr in _LOG_METHODS\n"
     "                    and isinstance(func.value, ast.Name)\n"
     '                    and func.value.id == "logger"):',
     "reports_the_shapes_that_defeated_the_old_one"),

    # Per-NAME, because emptying the set is not the same test as narrowing it,
    # and narrowing is what actually happened: `fatal` was simply absent.
    # `debug` and `fatal` stand for the two halves a review found unpinned -
    # an everyday level, and a deprecated alias.
    ("a single sink-method name dropped - `debug` stops being a sink",
     '    "debug", "info", "warning", "warn", "error", "exception", "critical",',
     '    "info", "warning", "warn", "error", "exception", "critical",',
     "sink_method_set_is_pinned_AS_A_SET"),

    ("the alias a review found missing goes missing again - `fatal`",
     '    "fatal", "log",',
     '    "log",',
     "sink_method_set_is_pinned_AS_A_SET"),

    ("print() dropped as a sink - stdout reaches the journal unwatched",
     '_BARE_CALL_SINKS = frozenset({"print"})',
     "_BARE_CALL_SINKS = frozenset()",
     "reports_the_shapes_that_defeated_the_old_one"),

    ("raised exceptions dropped as a sink",
     '        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):\n'
     '            yield "raise", node.exc.args, [kw.value for kw in node.exc.keywords]',
     "        elif False:\n"
     "            pass",
     "reports_the_shapes_that_defeated_the_old_one"),

    ("keyword arguments stop being scanned - extra={'raw': payload} escapes",
     "                yield func.attr, node.args, [kw.value for kw in node.keywords]",
     "                yield func.attr, node.args, []",
     "reports_the_shapes_that_defeated_the_old_one"),

    ("the sanitiser set emptied - correct shipping code is accused",
     '_SANITISERS = frozenset({"int", "float", "bool", "len", "round", "abs", "ord"})',
     "_SANITISERS = frozenset()",
     "taint_stops_at_a_numeric_conversion"),

    ("%r / %a stop counting as escaping - the M2 false alarms return",
     '_SAFE_PERCENT = {"r": "%r", "a": "%a"}',
     "_SAFE_PERCENT = {}",
     "the_other_two_formatting_syntaxes_escape_too"),

    ("{!r} / {!a} stop counting as escaping",
     '_SAFE_FORMAT = {"r": "{!r}", "a": "{!a}"}',
     "_SAFE_FORMAT = {}",
     "the_other_two_formatting_syntaxes_escape_too"),

    ("lazy-logging specifiers matched by PRESENCE, not position",
     "                for conv, value in zip(convs, values):\n"
     "                    if _carries_taint(value, names):\n"
     "                        claim(value, value.lineno,\n"
     "                              _SAFE_PERCENT.get(conv, \"RAW\"))",
     "                for conv, value in zip(convs, values):\n"
     "                    if _carries_taint(value, names):\n"
     "                        claim(value, value.lineno,\n"
     "                              _SAFE_PERCENT.get(convs[0], \"RAW\"))",
     "matched_by_POSITION_not_by_presence"),

    ("the control's own fixtures handed the scanner its seed name back",
     "        def bound_intermediate(msg, sink):",
     "        def bound_intermediate(payload, sink):",
     "control_fixtures_do_not_hand_the_scanner_the_names_it_reads"),

    # The BODY has to move, not just the signature. The first version of this
    # mutant renamed only the parameter and survived honestly: the fixture
    # still called `self_like.logger.error(...)`, so no receiver changed and
    # the property under test was never perturbed. A mutant that does not
    # reintroduce the defect it names is a mutant written wrong, which is the
    # second of the readings a survivor deserves.
    ("the control's own fixtures reach the sink through `logger` again",
     "        def module_level_logging(msg, logging_mod):\n"
     '            logging_mod.error("through the logging module: %s", msg.payload)',
     "        def module_level_logging(msg, logger):\n"
     '            logger.error("through the logging module: %s", msg.payload)',
     "control_fixtures_do_not_hand_the_scanner_the_names_it_reads"),
]

# DECLARED ABSENCES. Stated rather than left to be inferred from a clean
# score, because an unexplained gap reads as coverage.
#
# 1. The mapping-key bail in _percent_conversions() has NO mutant, and it is
#    not an oversight. It was written as one - turning `return None` into
#    `continue` - and it survived, correctly: with `continue`, a
#    `"%(name)s" % {...}` string yields an empty specifier list against one
#    argument, the arity check downstream refuses the mismatch, and the value
#    still falls through to RAW. The two spellings are behaviour-identical for
#    every input `%`-formatting actually accepts, since mixing mapping and
#    positional forms in one string is a TypeError at runtime. The bail is
#    kept anyway - it says what it means, and it would stop being redundant
#    the moment the arity check changed - but it is redundant TODAY and a
#    mutant on it can only ever survive. This is the third reading of a
#    survivor from test-and-review-code.md: the code under mutation is
#    redundant, not the test weak.
#
# 2. WITHDRAWN, and it was wrong in the way that matters. It read: "Nothing
#    mutates the sink-method NAMES individually (`debug` vs `warning` vs
#    `exception`). One mutant emptying _BARE_CALL_SINKS covers the shape."
#    _BARE_CALL_SINKS is {"print"} - those three names live in _LOG_METHODS,
#    which no mutant touched at all. A review swept the set per name and found
#    SIX of eight pinned by nothing, and the same gap had already let
#    `logger.fatal` ship missing from the set entirely: a real forgery path,
#    one word wide. The sentence is exactly what stopped anyone asking whether
#    the set was COMPLETE. Per-name mutants are below.
#
# 3. Nothing mutates the WIDENING direction - a defect of the form "something
#    is wrongly called safe". A review confirmed several survive
#    (_SANITISERS gaining "str", _SAFE_FORMAT gaining "s"). Those are guards
#    against a future edit rather than a present hole, since str(payload) is
#    correctly RAW today. Declared rather than fixed: the mutant set below
#    covers the narrowing direction, which is where the shipped defects were.

# Control B. A deliberately broken scanner that MUST score RED, kept distinct
# from every scored mutant so a failure here is unambiguously the scorer.
CONTROL_B = ("CONTROL B: deliberately broken - the scanner reports nothing at all",
             "    found = []\n    for method, args, kwargs in _sink_calls(tree):",
             "    return []\n    found = []\n    for method, args, kwargs in _sink_calls(tree):")

# Control C. Compiles, dies at import - must score NO VERDICT rather than
# KILLED. A missing import rather than a typo'd name, per T-527.18: a name
# typo only raises while that line happens to execute at module scope, so
# moving it into a function retires the control silently.
CONTROL_C = ("CONTROL C: compiles, dies at import - must score NO VERDICT",
             "import textwrap\nimport unittest",
             "import textwrap\nimport unittest\n"
             "import a_module_that_certainly_does_not_exist")


def purge_pycache(root):
    for base, dirs, _ in os.walk(root):
        if ".git" in base:
            continue
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(base, d), ignore_errors=True)


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def run_suites(root):
    purge_pycache(root)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    out = []
    ok = True
    for suite in SUITES:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", suite],
            cwd=root, env=env, capture_output=True, text=True, timeout=600,
        )
        out.append(proc.stdout + proc.stderr)
        ok = ok and proc.returncode == 0
    return ok, "\n".join(out)


def named_failures(out):
    return re.findall(r"^(?:FAIL|ERROR): (\S+)", out, re.M)


def score(ok, out, clean_ran):
    """green / killed / no-verdict, and WHY - never just the colour.

    Two independent guards against scoring a mutant that never ran the code it
    was written for. The ran-count is the one nothing can fake: no honest
    mutant changes how many tests are COLLECTED, so a drop means the module
    died at import and the red is the wrong red. The zero-named-cases rule is
    kept beside it because unittest wraps an unimportable module in
    _FailedTest and reports it as one ordinary NAMED error, so the count is
    what catches that family and the names are what catch the rest.
    """
    fails = named_failures(out)
    ran = ran_count(out)
    if ok:
        return "green", fails, ran
    if clean_ran is not None and ran < clean_ran:
        return "no-verdict", fails, ran
    if not fails:
        return "no-verdict", fails, ran
    return "killed", fails, ran


def apply_mutation(path, anchor, replacement):
    src = open(path).read()
    count = src.count(anchor)
    if count != 1:
        print(f"  ANCHOR MATCHED {count} TIMES - not applied, no verdict")
        return False
    mutated = src.replace(anchor, replacement)
    if mutated == src:
        print("  replacement changed nothing - not applied, no verdict")
        return False
    refusal = compile_gate(mutated, path)
    if refusal:
        print(f"  {refusal}")
        return False
    with open(path, "w") as fh:
        fh.write(mutated)
    return True


def main():
    before = sha(os.path.join(REPO, TARGET_REL))
    sandbox = tempfile.mkdtemp(prefix="mutate-payload-scanner-")
    root = os.path.join(sandbox, "repo")
    try:
        # THE CALL BELOW STAYS ON ONE LINE. test_suite_isolation.py's
        # test_a_sandboxed_harness_still_works_on_a_copy greps for the opening
        # of that call as a source literal, and it is the sole evidence
        # entitling this harness to sit in SANDBOXED rather than IN_PLACE.
        # Wrapped across two lines the assertion fails, and the harness keeps
        # its exemption from the interrupted-battery check while the test
        # earning it is red.
        #
        # This comment deliberately does NOT reproduce the literal. It did
        # until 2026-08-11, which put two copies in the file and meant a
        # re-wrapped call stayed green on the comment alone. The assertion now
        # strips comments before matching, so both halves of that are fixed —
        # but a comment that restates the string it guards is the defect, not
        # merely the thing that exposed one.
        shutil.copytree(REPO, root,
                        ignore=shutil.ignore_patterns(
                            ".git", "__pycache__", "venv", "*.pyc"))
        target = os.path.join(root, TARGET_REL)
        pristine = open(target).read()

        print("CONTROL A: the clean tree must be GREEN")
        ok, out = run_suites(root)
        clean_ran = ran_count(out)
        if not ok:
            print(f"  *** CONTROL A FAILED ({clean_ran} ran) - NO DATA. "
                  f"Fix the suite before reading any score. ***")
            print("\n".join(named_failures(out)))
            return 1
        # THE ZERO ABORT, which three sibling harnesses have and this one
        # did not (T-554). A green run that collected NOTHING is NO DATA,
        # not a pass - and the omission is silent rather than loud here,
        # because score() guards with `ran < clean_ran`: at clean_ran == 0
        # that can never fire, so the ran-count tell is simply inert while
        # the line below still prints a measurement-shaped "0 tests ran".
        # T-550 moved this harness onto the shared anchored rule, which has
        # strictly MORE ways to return 0 than the local one it replaced.
        if clean_ran == 0:
            print("  *** CONTROL A FAILED - a GREEN run that collected NO "
                  "tests. This is NO DATA, not a score. ***")
            return 1
        print(f"  clean: GREEN, {clean_ran} tests ran\n")

        for label, anchor, replacement in (CONTROL_B, CONTROL_C):
            open(target, "w").write(pristine)
            print(label)
            if not apply_mutation(target, anchor, replacement):
                print("  *** control could not be applied - NO DATA ***")
                return 1
            ok, out = run_suites(root)
            verdict, fails, ran = score(ok, out, clean_ran)
            expected = "killed" if label.startswith("CONTROL B") else "no-verdict"
            print(f"  {verdict} ({len(fails)} named case(s), {ran} ran) - "
                  f"expected {expected}")
            if verdict != expected:
                print("  *** CONTROL WRONG - the scorer is broken, NO DATA ***")
                return 1
        print()

        killed, survived, unapplied = [], [], []
        print(f"{len(MUTANTS)} MUTANTS")
        for i, (label, anchor, replacement, expect) in enumerate(MUTANTS, 1):
            open(target, "w").write(pristine)
            print(f"[{i}/{len(MUTANTS)}] {label}")
            if not apply_mutation(target, anchor, replacement):
                unapplied.append(label)
                continue
            ok, out = run_suites(root)
            verdict, fails, ran = score(ok, out, clean_ran)
            if verdict == "killed":
                right = [f for f in fails if expect in f]
                print(f"  killed ({len(fails)} named case(s), {ran} ran)"
                      f" -> {', '.join(sorted(set(fails))[:3])}")
                if not right:
                    print(f"  *** killed, but NOT by the test named for it "
                          f"({expect}). Read why before counting it. ***")
                killed.append(label)
            elif verdict == "green":
                print(f"  SURVIVED ({ran} ran) - nothing pins this widening")
                survived.append(label)
            else:
                print(f"  NO VERDICT ({len(fails)} named, {ran} ran vs "
                      f"{clean_ran} clean)")
                unapplied.append(label)

        print(f"\n{len(killed)} killed, {len(survived)} survived, "
              f"{len(unapplied)} no-verdict, of {len(MUTANTS)}")
        for label in survived:
            print(f"  SURVIVOR: {label}")
        for label in unapplied:
            print(f"  NO VERDICT: {label}")
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

    after = sha(os.path.join(REPO, TARGET_REL))
    if before == after:
        print(f"\nTREE UNTOUCHED: {TARGET_REL} is byte-identical to its "
              f"pre-run state (sha {after[:12]}).")
    else:
        print(f"\n*** TREE MODIFIED - {TARGET_REL} DIFFERS. "
              f"Fix before committing. ***")
        return 1
    return 0 if not survived and not unapplied else 1


if __name__ == "__main__":
    sys.exit(main())
