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
rule now has one implementation, the harnesses import it, and a fix applied here
cannot be half-applied across the fleet. tests/test_mutation_scoring.py drives
this file directly, on inputs written for it, rather than restating the rule -
a rule copied into its own test scores itself.

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
        # than testing `".git" in root`. The substring form matched any path
        # containing `.git` anywhere - a checkout under a directory with
        # `.git` in its name, or any `.github/` tree - while still descending
        # into `.git` itself, so it was neither the skip nor the guard it read
        # as. Harmless in effect (skipping a `__pycache__` deletion is safe),
        # but a check should be the check it says it is.
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
    """
    return sum(int(n) for n in re.findall(r"Ran (\d+) tests?", out))


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
