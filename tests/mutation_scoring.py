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

THE CONSOLIDATION IS NOT FINISHED, and this docstring used to claim otherwise
("a fix applied here cannot be half-applied across the fleet"). Only THREE
harnesses import this module - mutate_camera_quality.py, mutate_light_logging.py
and mutate_retired_entities.py. Five still carry their own copy:

    mutate_connack_refusal.py   ran_count + score_run + literal verdict strings
    mutate_light_scheduler.py   ran_count
    mutate_light_schedule.py    ran_count
    mutate_payload_scanner.py   ran_count + named_failures
    mutate_health_log.py        first-match `re.search`, a different rule again
    mutate_netwatch.py          first-match `re.search`

T-527.36 made that concrete rather than theoretical: `ran_count` was anchored
here (see below) and those copies are still unanchored, so the divergence the
sentence promised was impossible now exists. Whether any of them is exposed to
a live forgery was NOT established - the two `Ran 175 tests` strings in
tests/test_connack_refusal.py sit in a docstring body and a standalone comment,
neither of which unittest prints. Do not read the absence of a confirmed
exploit as coverage.

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
