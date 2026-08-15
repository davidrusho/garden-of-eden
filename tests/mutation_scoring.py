"""The scoring rule every mutation battery in this repo is judged by.

WHY THIS IS A MODULE AND NOT A PARAGRAPH REPEATED IN EACH HARNESS (T-527.32).

A battery scores a mutant by whether the test run FAILED, so a broken scorer
reports EVERY mutant caught - the most reassuring output available, where every
other broken instrument returns nothing and reads as clean. That defect was
found and fixed in tests/mutate_connack_refusal.py under T-527.11, found again
in tests/mutate_light_schedule.py and tests/mutate_payload_scanner.py, and then
found a THIRD time, in tests/mutate_retired_entities.py,
tests/mutate_camera_quality.py and tests/mutate_light_logging.py, which were
still reporting 29/29, 17/17 and 14/14 on a rule they did not implement.

Three times in one repo is a copy-paste problem, not a thinking problem. The
rule now has one implementation and the harnesses that import it cannot get a
half-applied fix. tests/test_mutation_scoring.py drives this file directly, on
inputs written for it, rather than restating the rule - a rule copied into its
own test scores itself.

THE CONSOLIDATION IS FINISHED FOR `ran_count` AS OF T-550, and this docstring
twice claimed the wrong thing about it - first that a fix here could not be
half-applied (it could, and was), then that only THREE harnesses imported this
module (five did by 85b86d6). So: state what is true and where the remaining
seams are, rather than restating a scope.

Every harness in this repo that computes a ran-count now imports it from here:

    mutate_camera_quality.py    mutate_light_logging.py    mutate_log_hygiene.py
    mutate_retired_entities.py  mutate_mutation_scoring.py mutate_connack_refusal.py
    mutate_light_scheduler.py   mutate_light_schedule.py   mutate_payload_scanner.py
    mutate_health_log.py        mutate_netwatch.py

WHAT IS STILL NOT SHARED, named so the next reader does not mistake the list
above for more than it says:

  * mutate_payload_scanner.py keeps its own `score` and `named_failures`. Its
    `named_failures` returns the failing test's NAME rather than the whole
    line, because its report prints `-> test_name`; its `score` compares
    `ran < clean_ran` (a DROP) where `score_run` uses `!=`.
  * mutate_health_log.py and mutate_netwatch.py have NO ran-count comparison
    at all. They score by return code, so `ran_count` there is a display
    label on an already-green CONTROL A run, and they remain blind to the
    ImportError family that `score_run` exists to separate out.
  * mutate_light_scheduler.py and mutate_light_schedule.py spell the
    no-verdict decision inline in their own main(), where it also drives
    CONTROL C reporting; only the count comes from here.

  * THREE INLINE COMPILE GATES SURVIVE, and this list said otherwise until a
    review of T-550 enumerated them. `compile_gate` was adopted by
    mutate_connack_refusal.py and mutate_payload_scanner.py in that same change
    and NOT by:
      - mutate_light_schedule.py, whose inline gate prints a message
        byte-identical to compile_gate's - so it is a pure duplicate, in a file
        T-550 was already editing.
      - mutate_light_scheduler.py, whose gate DIVERGES: it returns
        `f"mutant is not valid Python: {exc}"` and is guarded by
        `if path.suffix == ".py"`.
      - mutate_ha_birth_message.py, which imports nothing from here at all.
    Left alone deliberately: swapping a divergent gate changes output, which is
    a behaviour change rather than a consolidation, and T-550's acceptance was
    scoped to the ran-count rule. Named here so the next reader does not read
    "connack and payload_scanner now use compile_gate" as "compile_gate is
    consolidated."

  * purge_pycache and sha have local copies too: purge_pycache in
    mutate_connack_refusal.py, mutate_light_schedule.py and
    mutate_payload_scanner.py; sha in mutate_connack_refusal.py and
    mutate_payload_scanner.py. The shared purge_pycache takes a `repo`
    argument and two of those locals take none, so these are not drop-in
    swaps. Two of the three locals also still carry the old
    `if ".git" in root: continue` form that the shared version's comment
    documents as going inert for any checkout whose ancestor path contains
    `.git`.

  BLAST RADIUS OF THE IMPORT ITSELF. Eleven harnesses now die at import if this
  module is broken or `tests/__init__.py` disappears, where five did before
  T-550, and tests/test_suite_isolation.py exec_module()s every harness in its
  interrupt-restore probe. That is the intended trade of one shared rule
  against a wider single point of failure, and it is the reason this module's
  own battery (mutate_mutation_scoring.py) matters more than its size suggests.

WAS ANY OF THEM EXPOSED TO A LIVE FORGERY? No, and this was measured rather
than assumed (T-550), by a probe that forced EVERY test in each scored suite
to FAIL - which makes unittest print every test's first docstring line at
column 0, a far wider forgery surface than any single mutant produces - and
compared the two rules over that output. Anchored equalled unanchored
equalled the collected count in all nine suites the six harnesses score
(test_connack_refusal, test_ha_birth_message, test_retired_entities,
test_water_interlock, test_light_scheduler, test_light_schedule,
test_health_log, test_netwatch, test_setup_units), against a positive
control that DID report the `Ran 175 tests ... OK` shape when one was
planted. The two `Ran 175 tests` strings in tests/test_connack_refusal.py
are unprintable for a stronger reason than "a docstring": one is in the
docstring BODY of the module-level helper `_payload_sinks`, which is not a
test at all, and the other is a `#` comment.

EXACTLY ONE SUITE IN THIS REPO CARRIES A LIVE FORGERY, and it is this rule's
own: tests/test_mutation_scoring.py, whose `test_it_reads_the_SINGULAR_form`
opens its docstring `unittest writes 'Ran 1 test', not 'Ran 1 tests' - ...`.
Forced red, that suite reads 38 unanchored against 36 anchored. It is scored
only by tests/mutate_mutation_scoring.py, which already imports from here -
so the anchoring that T-527.36 added is load-bearing today, and the six
harnesses converted in T-550 were hygiene rather than a bug fix. Verdicts
were byte-identical across all eleven batteries before and after the
conversion, and the nine batteries that can report NO VERDICT reported none
on either side.

WHAT THE RULE IS, and why each half exists:

  * `compile_gate` runs BEFORE the write, so invalid Python never reaches disk.
    A mutant that does not compile makes every suite die at collection and
    scores KILLED while the behaviour it was written for never ran. Gating
    after the write leaves broken source on disk for the length of a call, in
    the exact window the restore handlers exist to close.

  * `score_run` returns a THIRD verdict. `compile()` gates SYNTAX, which is a
    narrower promise than it reads as: a mutant can compile perfectly and then
    die at IMPORT (a missing import, a bad attribute at module scope, an
    exception in a top-level call). Two tells, and neither covers the other:

      - ZERO named failing cases. unittest never got as far as collecting a
        case, so nothing printed a FAIL:/ERROR: line.
      - A COLLAPSED RAN-COUNT. This is the half the zero-named-cases tell does
        NOT cover, and believing it did is what T-527.18 corrected: unittest
        wraps an unimportable module in `unittest.loader._FailedTest` and
        reports it as an ordinary NAMED ERROR, so `fails` is non-empty and the
        mutant scored as a kill. No honest mutant changes how many tests are
        COLLECTED, only how many pass.

A red run with no verdict is the same class of result as a mutant that would
not apply: no information about the suite, and it must not be counted for or
against it.

WHAT THIS MODULE DELIBERATELY DOES NOT DO. It does not run suites, restore
files, or hold any state. Each harness owns its own targets, its own restore
paths and its own CONTROL A/B/C anchors, because those are properties of the
code under mutation and cannot be shared. Only the verdict rule is common.
"""
import hashlib
import os
import re
import shutil

# Verdicts, named so a harness cannot typo one into a silent miss.
SURVIVED = "survived"
KILLED = "killed"
NO_VERDICT = "no-verdict"


def purge_pycache(repo):
    """Delete every __pycache__ under `repo`, skipping .git.

    .pyc validity keys on (mtime-seconds, size), so a mutation applied and
    reverted inside one second can silently re-run the PREVIOUS bytecode and
    return a confident verdict for code that never executed. `-B` and
    PYTHONDONTWRITEBYTECODE suppress WRITING a cache; neither stops a stale one
    being READ, which is why this exists as well as the env var.
    """
    for root, dirs, _ in os.walk(repo):
        # Prune rather than `continue`, and match the DIRECTORY NAME rather
        # than testing `".git" in root`.
        #
        # Being precise about what the substring form did, because the first
        # version of this comment overstated it: it WAS a working .git guard.
        # `os.walk` descended into .git, but every root beneath .git also
        # contains the substring, so nothing under it was ever deleted. The
        # two real defects were that it was OVER-BROAD - it also skipped any
        # `.github/` tree, leaving stale bytecode there - and that it went
        # entirely INERT for a checkout whose ancestor path contains `.git`,
        # which is the dangerous one: a purge that silently does nothing is a
        # stale-bytecode condition, and stale bytecode makes a battery's
        # verdicts belong to the previous mutant. The wasted traversal is a
        # performance cost, not a correctness one.
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in list(dirs):
            if name == "__pycache__":
                shutil.rmtree(os.path.join(root, name), ignore_errors=True)
                dirs.remove(name)


def sha(path):
    """sha256 of a file's bytes, or None if it is not there.

    Returns None rather than raising so a caller comparing against a snapshot
    can treat "missing" as "differs" instead of losing the comparison to an
    OSError raised out of a `finally`.
    """
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None


def compile_gate(source, path):
    """None if `source` is valid Python, else a human-readable refusal.

    Returns a MESSAGE rather than raising, because the caller's response is to
    record the mutant as NOT APPLIED and carry on - an exception here would
    abort a battery over one bad anchor.
    """
    try:
        compile(source, path, "exec")
    except SyntaxError as exc:
        return (f"MUTANT IS NOT VALID PYTHON ({exc.msg}, line {exc.lineno}) - "
                f"no verdict; a syntax error reddens the suite for the wrong "
                f"reason")
    return None


def ran_count(out):
    """How many tests actually RAN, summed across every suite in `out`.

    Summed rather than taken from one match because a harness may run several
    suites per verdict and concatenate their output.

    ANCHORED AT THE START OF A LINE, for the same reason `named_failures` is
    (T-527.36). unittest prints its summary at column 0 and nothing else does,
    so an unanchored match counts TEXT ABOUT a summary as a summary: a
    docstring quoting "Ran 1 test", or - the shape this repo produces
    routinely - a subprocess's captured output pasted into an assertion
    message. Both only appear when a run goes RED, so the inflation lands
    exclusively on the runs a battery is trying to score, and `score_run` then
    reads the moved count as an import death and returns NO VERDICT for a
    genuine kill. A battery over this very module hit it: at HEAD it aborts at
    its own CONTROL B, scoring a deliberately-broken scorer NO VERDICT (28 ran
    against a 26 clean baseline) instead of KILLED.

    BE PRECISE ABOUT THE SCALE, because the first version of this paragraph
    was not. unittest prints the docstring of each test that FAILED, so the
    forgery is emitted PER FAILING TEST, not per red run - which means the
    inflation depends on WHICH tests a mutant reddens, not merely that one
    did. Measured over the 16 mutants in tests/mutate_mutation_scoring.py,
    anchored against unanchored: 2 changed verdict (KILLED -> NO VERDICT),
    14 were identical. Mutant 3's red output carries no forgery at all. The
    mechanism and the failure direction below are general; "every red run"
    was a one-instance measurement written up as a property.

    Note the failure direction, because it decides how urgent this is: the
    inflation cannot manufacture a KILL, only suppress one. It reads as "no
    information" rather than as a false pass - but a battery that reports no
    information about every mutant is indistinguishable from one whose suite
    is broken, which is how it goes unread.

    MATCH THE WHOLE SUMMARY SHAPE, not just its opening words. The anchor
    alone is NECESSARY BUT NOT SUFFICIENT, and that was measured rather than
    reasoned: unittest prints a failing test's docstring first line on its own
    line AT COLUMN 0, so a docstring that BEGINS "Ran 99 tests ..." is
    counted by an anchored-but-loose pattern exactly as a real summary is.
    Measured on real output - three collected tests, anchored sum 102, and
    the forged line sorts FIRST so even a single `re.search` returns 99.

    Requiring ` in <float>s` closes it, because unittest's own format string
    is fixed ("Ran %d test%s in %.3fs") while a docstring reproducing the
    whole shape is no longer something anyone writes by accident. It is not
    a proof - a docstring beginning "Ran 99 tests in 0.5s" still defeats it -
    and tests/test_mutation_scoring.py pins that residual as a KNOWN
    LIMITATION rather than leaving it to be rediscovered.

    TWO MISS SHAPES THE LOOSE FORM DID NOT HAVE, neither reachable here.
    `$` under re.M matches only immediately before `\\n`, so a CRLF line
    ending or a trailing space defeats it and the summary is MISSED - which
    collapses the count and suppresses every kill, the same silent direction
    this rule exists to remove. Both are unreachable today: every harness in
    this repo captures with `text=True`, which universal-newlines a CRLF
    away, and unittest emits no trailing space. Stated because the failure
    would be silent if either premise ever stopped holding, and because a
    reader tightening this further should know which way the risk runs.

    A MEASURED WAY TO CLOSE THE RESIDUAL, not taken here: unittest prints
    `separator2` (70 dashes) immediately before the summary, while a failing
    test's docstring is printed between `separator1` and `separator2`, so
    requiring the preceding line distinguishes them completely. Verified by
    review against six real suites plus the concatenated multi-suite and
    import-death shapes, with identical counts everywhere and the whole-shape
    forgery correctly excluded. NOT adopted yet because it interacts with
    tests/test_suite_isolation.py's restore probe, whose canned doubles carry
    no separator line - they would read as zero collected, which turns that
    probe's CONTROL C into a false failure. Doing it means updating the
    doubles in the same change and re-running every harness.
    """
    return sum(int(n) for n in
               re.findall(r"^Ran (\d+) tests? in [\d.]+s$", out, re.M))


def named_failures(out):
    """The FAIL:/ERROR: lines unittest printed, in order."""
    return [line for line in out.splitlines()
            if line.startswith(("FAIL:", "ERROR:"))]


def score_run(ok, out, clean_ran=None):
    """Turn one suite run into (verdict, named_failure_lines).

    `clean_ran` is THIS run's own clean-tree baseline, passed in rather than
    recomputed so the comparison is never against a figure from some other
    tree. Omit it and the ran-count tell is disabled - which is a real loss,
    not a default, so a harness that omits it is choosing to be blind to the
    ImportError family.
    """
    if ok:
        return SURVIVED, []
    fails = named_failures(out)
    if clean_ran is not None and ran_count(out) != clean_ran:
        return NO_VERDICT, fails
    return (KILLED if fails else NO_VERDICT), fails


def format_verdict(verdict, fails, indent="  "):
    r"""The one-line report for a scored mutant.

    The named-case COUNT is printed for every kill, because it is the only tell
    for a mutant that died broadly: a real kill names one or two cases, and
    `killed (0 failing case(s))` is what a module that stopped importing looks
    like.

    GREP FOR THE WHOLE LINE, NOT FOR THE FRAGMENT. The full line is safe - it
    is not a substring of `killed (10 failing case(s))` or any other count. The
    BARE fragment `0 failing case(s)` is the unsafe one, because it matches
    every count ending in zero, and reaching for it is how a clean run gets
    read as having three kills that tested nothing. Either match the whole
    field (`grep -E "^  killed \( *0 failing"`) or read the column.
    """
    if verdict == SURVIVED:
        return f"{indent}SURVIVED - no test noticed"
    if verdict == NO_VERDICT:
        return (f"{indent}NO VERDICT - the run went red without the behaviour "
                f"under test ever executing ({len(fails)} named case(s), "
                f"collected count moved or zero cases named)")
    return f"{indent}killed ({len(fails)} failing case(s))"
