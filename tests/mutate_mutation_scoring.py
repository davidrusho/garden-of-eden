#!/usr/bin/env python3
"""Mutation battery for tests/mutation_scoring.py (T-527.36).

WHY THIS FILE EXISTS AT ALL. tests/mutation_scoring.py holds the verdict rule
every other battery in this repo is judged by, and until now it was the only
module in tests/ with no battery of its own. That is the worst place in the
tree for the gap to sit: its failure mode is not a wrong answer but a
CONFIDENT one - a broken scorer reports every mutant caught, which is the most
reassuring output available, where every other broken instrument returns
nothing and reads as clean.

The gap has already cost something concrete. The T-527.31 review found
test_it_leaves_the_git_directory_alone was true for free: it asserted that a
plain FILE under .git survives a function that only ever deletes DIRECTORIES
named __pycache__, so deleting the guard outright and switching os.walk to
topdown=False both survived. A reviewer caught it, one round late, and the
fixture now carries a __pycache__ inside .git. The four `purge_pycache` and
`sha` mutants at the end of MUTANTS are that hole, kept as regression
coverage.

REFER TO MUTANTS BY WHAT THEY DO, NEVER BY INDEX. Two comments here carried
positional references ("mutants 11 and 12"), and adding two mutants in the
middle of the list silently made both wrong - the harness prints `[i/N]`, so
a reader following the comment lands on someone else's mutant. Found by
review, 2026-08-14.

WHY IT IS SANDBOXED RATHER THAN IN-PLACE. Three harnesses import this module
at their own module scope. A battery rewriting it in the working tree is a
concurrent writer on the one file every other battery depends on, and a
concurrent session running any of them during that window gets verdicts from a
deliberately broken scorer with nothing to say so.

THE ONE THING THAT LOOKS CIRCULAR AND IS NOT. This harness scores mutants of
`score_run` by calling `score_run`. That is safe, and it is safe for a reason
worth stating rather than trusting: the import below resolves against REPO,
the LIVE tree, which this harness never writes. Every mutation lands inside
the copytree sandbox and is read only by the `python -m unittest` subprocess,
which runs with cwd inside the copy. The instrument and the specimen are two
different files on disk for the whole run. Nothing here may reimplement the
rule locally - it was fixed in place three separate times before it was made a
module (T-527.32), and a fourth copy would restart that.

CONTROLS. A (clean sandbox must be GREEN), B (a wholesale break must score
KILLED), C (compiles, dies at import, must score NO VERDICT). Any control
landing wrong aborts with NO DATA rather than printing a score.

Run:  python3 tests/mutate_mutation_scoring.py
"""
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from tests.mutation_scoring import (  # noqa: E402
    KILLED, NO_VERDICT, SURVIVED, compile_gate, format_verdict, purge_pycache,
    ran_count, score_run, sha)

TARGET_REL = os.path.join("tests", "mutation_scoring.py")

# Only the suite that drives this module. Every other suite in the repo is
# indifferent to the scoring rule, so adding them buys no kill and multiplies
# the runtime by the size of the tree.
SUITES = ["tests.test_mutation_scoring"]

# (label, anchor, replacement, the test whose name must appear in the kill)
#
# `expect` is a SUBSTRING searched against the whole `FAIL:`/`ERROR:` line,
# which unittest writes as `FAIL: test_x (pkg.mod.Class.test_x)` - the
# parenthesised dotted path included. So a class-name fragment would match
# too. A method-name fragment is used here because it is the most specific
# thing available, not because the class is unavailable.
#
# (This comment previously claimed the line "stops at the first space and so
# never contains the class". That is false - it describes unittest's SHORT
# description, not the FAIL: line - and it invited narrowing the match on a
# wrong premise. Found by review, 2026-08-14.)
MUTANTS = [
    # ---- score_run: the verdict rule itself ----------------------------
    ("score_run: drop the ran-count clause, keeping only the "
     "zero-named-cases tell (the pre-T-527.18 rule verbatim)",
     "    if clean_ran is not None and ran_count(out) != clean_ran:\n"
     "        return NO_VERDICT, fails\n",
     "",
     "IMPORT_DEATH_is_NO_VERDICT"),

    # The half-measure, which is the likelier edit: somebody reasons that a
    # count can only ever collapse and writes the comparison one-sided.
    ("score_run: compare the ran-count with > instead of !=, so a "
     "COLLAPSED count stops being a tell",
     "if clean_ran is not None and ran_count(out) != clean_ran:",
     "if clean_ran is not None and ran_count(out) > clean_ran:",
     "IMPORT_DEATH_is_NO_VERDICT"),

    ("score_run: score any red run with a named case as KILLED - the exact "
     "defect this module was extracted to stop",
     "    return (KILLED if fails else NO_VERDICT), fails",
     "    return KILLED, fails",
     "red_run_naming_NOTHING_is_NO_VERDICT"),

    # Review survivor, 2026-08-14. Only a baseline of ZERO separates the two
    # spellings, and every case passed None or 3 until a test was written for
    # it. A clean tree that collects nothing is precisely when the tell must
    # stay armed rather than silently switching off.
    ("score_run: gate the ran-count tell on truthiness instead of `is not "
     "None`, so a clean baseline of 0 silently disables it",
     "    if clean_ran is not None and ran_count(out) != clean_ran:",
     "    if clean_ran and ran_count(out) != clean_ran:",
     "clean_ran_of_ZERO_still_arms_the_tell"),

    # ---- ran_count -----------------------------------------------------
    ("ran_count: drop the singular form from the regex, so a one-test suite "
     "reads as zero collected",
     'return sum(int(n) for n in re.findall(r"^Ran (\\d+) tests?", out, re.M))',
     'return sum(int(n) for n in re.findall(r"^Ran (\\d+) tests", out, re.M))',
     "reads_the_SINGULAR_form"),

    ("ran_count: take the FIRST count rather than the sum, so a harness "
     "running several suites undercounts every time",
     'return sum(int(n) for n in re.findall(r"^Ran (\\d+) tests?", out, re.M))',
     'm = re.findall(r"^Ran (\\d+) tests?", out, re.M)\n'
     '    return int(m[0]) if m else 0',
     "sums_across_several_suites"),

    # T-527.36. The anchor, in both the ways it can be lost. Dropping `^` is
    # the deliberate simplification; dropping re.M is the accident, and it is
    # the nastier of the two because it still matches the FIRST summary in a
    # multi-suite output and so keeps working for any harness running one
    # suite. Without these two mutants the anchor is only prose.
    ("ran_count: unanchor the regex, so PROSE ABOUT a summary and a pasted "
     "capture are both counted as summaries",
     'return sum(int(n) for n in re.findall(r"^Ran (\\d+) tests?", out, re.M))',
     'return sum(int(n) for n in re.findall(r"Ran (\\d+) tests?", out))',
     "EMBEDDED_summary_line_is_not_counted"),

    ("ran_count: keep the ^ but drop re.M, so only the very first line of a "
     "concatenated multi-suite output can ever match",
     'return sum(int(n) for n in re.findall(r"^Ran (\\d+) tests?", out, re.M))',
     'return sum(int(n) for n in re.findall(r"^Ran (\\d+) tests?", out))',
     "sums_across_several_suites"),

    # ---- named_failures ------------------------------------------------
    ("named_failures: match FAIL:/ERROR: anywhere in the line instead of at "
     "the start, so a traceback quoting one is counted as a named case",
     'return [line for line in out.splitlines()\n'
     '            if line.startswith(("FAIL:", "ERROR:"))]',
     'return [line for line in out.splitlines()\n'
     '            if "FAIL:" in line or "ERROR:" in line]',
     "EMBEDDED_error_line_is_not_a_named_case"),

    ("named_failures: stop counting ERROR: lines, so an erroring case reads "
     "as a run that named nothing",
     '            if line.startswith(("FAIL:", "ERROR:"))]',
     '            if line.startswith("FAIL:")]',
     "takes_FAIL_and_ERROR_lines"),

    # Review survivor, 2026-08-14. The docstring promises "in order" and the
    # only ordering assertion ran on a ONE-element list, where every ordering
    # is the same ordering. Order carries attribution: a harness prints the
    # first few named cases as its evidence for a kill.
    ("named_failures: return the cases SORTED rather than in the order "
     "unittest printed them, so a kill's evidence is reordered under it",
     "    return [line for line in out.splitlines()\n"
     '            if line.startswith(("FAIL:", "ERROR:"))]',
     "    return sorted(line for line in out.splitlines()\n"
     '                  if line.startswith(("FAIL:", "ERROR:")))',
     "preserves_the_ORDER"),

    # ---- compile_gate --------------------------------------------------
    ("compile_gate: swallow the SyntaxError and report success, so invalid "
     "Python reaches disk and reddens the suite for the wrong reason",
     '        return (f"MUTANT IS NOT VALID PYTHON ({exc.msg}, line {exc.lineno}) - "\n'
     '                f"no verdict; a syntax error reddens the suite for the wrong "\n'
     '                f"reason")',
     "        return None",
     "refuses_a_syntax_error"),

    # ---- format_verdict ------------------------------------------------
    ("format_verdict: drop the failing-case count from a kill, retiring the "
     "only tell for a mutant that died broadly",
     'return f"{indent}killed ({len(fails)} failing case(s))"',
     'return f"{indent}killed"',
     "kill_reports_its_named_case_COUNT"),

    ("format_verdict: render NO VERDICT exactly like a SURVIVOR, so the two "
     "become indistinguishable in a battery's output",
     "    if verdict == NO_VERDICT:\n"
     '        return (f"{indent}NO VERDICT - the run went red without the behaviour "\n'
     '                f"under test ever executing ({len(fails)} named case(s), "\n'
     '                f"collected count moved or zero cases named)")',
     "    if verdict == NO_VERDICT:\n"
     '        return f"{indent}SURVIVED - no test noticed"',
     "verdict_formats_distinguishably"),

    # Review survivors, 2026-08-14. Both are about the parts of the line a
    # READER uses, which is why neither had a test: they look like formatting.
    ("format_verdict: drop the two-space default indent, which is what this "
     "module's own documented grep recipe anchors on",
     'def format_verdict(verdict, fails, indent="  "):',
     'def format_verdict(verdict, fails, indent=""):',
     "DEFAULT_indent_is_the_two_spaces"),

    ("format_verdict: hardcode a NO VERDICT's named-case count to 0, "
     "collapsing the two tells that produce that verdict into one",
     '                f"under test ever executing ({len(fails)} named case(s), "',
     '                f"under test ever executing (0 named case(s), "',
     "NO_VERDICT_also_reports_its_named_case_count"),

    # ---- sha -----------------------------------------------------------
    # The reviewer's mutant M. sha() had no coverage at all until the first
    # T-527.31 review, so this is regression coverage for a real survivor.
    ("sha: raise on OSError instead of returning None, losing a snapshot "
     "comparison to an exception out of a finally",
     "    try:\n"
     "        with open(path, \"rb\") as fh:\n"
     "            return hashlib.sha256(fh.read()).hexdigest()\n"
     "    except OSError:\n"
     "        return None",
     "    with open(path, \"rb\") as fh:\n"
     "        return hashlib.sha256(fh.read()).hexdigest()",
     "MISSING_file_returns_None"),

    # ---- purge_pycache -------------------------------------------------
    # The three below are the T-527.31 hole. All three survived the suite as it
    # stood before the review added a __pycache__ INSIDE .git to the fixture,
    # because without that mark the name-prune and the substring test agree:
    # the substring form only ever skips MORE, and skipping never deletes.
    #
    # Deliberately NOT written: `dirs.remove(name)` after the rmtree. The
    # directory is already gone, so pruning it from the walk changes nothing
    # observable - it is an equivalent mutant, and a survivor there is not a
    # gap in the suite.
    ("purge_pycache: the reviewer's mutant N - make it a silent no-op, so "
     "every battery downstream can read stale bytecode",
     "    for root, dirs, _ in os.walk(repo):",
     "    return\n    for root, dirs, _ in os.walk(repo):",
     "removes_every_pycache_it_should"),

    ("purge_pycache: go back to the `.git` SUBSTRING test, which also skips "
     "any .github tree and goes inert under a checkout path containing .git",
     '        dirs[:] = [d for d in dirs if d != ".git"]',
     '        if ".git" in root:\n            continue',
     "removes_every_pycache_it_should"),

    ("purge_pycache: walk bottom-up, which makes the .git prune inert "
     "because the assignment to dirs no longer steers the walk",
     "    for root, dirs, _ in os.walk(repo):",
     "    for root, dirs, _ in os.walk(repo, topdown=False):",
     "leaves_the_git_directory_alone"),
]

# CONTROL B. A wholesale break that must score KILLED. ran_count is the right
# place for it: it is read by score_run and by three test classes, so a
# suite that has stopped running at all cannot produce this red.
CONTROL_B = ("CONTROL B: ran_count always returns 0 - must score KILLED",
             'return sum(int(n) for n in re.findall(r"^Ran (\\d+) tests?", out, re.M))',
             "return 0")

# CONTROL C. Compiles, dies at IMPORT - must score NO VERDICT rather than
# KILLED. A missing import rather than a typo'd name, per T-527.18: a name
# typo only raises while that line happens to execute at module scope, so
# moving it into a function retires the control silently.
CONTROL_C = ("CONTROL C: compiles, dies at import - must score NO VERDICT",
             "import hashlib\nimport os",
             "import hashlib\nimport a_module_that_certainly_does_not_exist\n"
             "import os")


def run_suites(root):
    """Run SUITES inside the sandbox at `root`. Returns (ok: bool, out: str).

    The sandbox root is the FIRST POSITIONAL ARGUMENT on purpose.
    test_suite_isolation.py's restore probe replaces this function with a
    double and locates the sandbox from that argument; a runner that closed
    over the path instead would leave the probe unable to tell a real sandbox
    from a probe that mutated nothing anywhere.
    """
    purge_pycache(root)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    # Inherit the environment rather than rebuilding it. Overriding PATH here
    # swaps python3 from Homebrew 3.14 to the system 3.9.6 for anything that
    # resolves it by name, and the failure then surfaces in files the mutation
    # never touched.
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


def apply_mutation(path, anchor, replacement):
    """Write the mutant, or explain why it was not applied. Never raises.

    compile_gate runs BEFORE the write, so invalid Python never reaches disk.
    """
    with open(path) as fh:
        src = fh.read()
    count = src.count(anchor)
    if count != 1:
        print(f"  ANCHOR MATCHED {count} TIMES - not applied, no verdict")
        return False
    mutated = src.replace(anchor, replacement, 1)
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


def _battery():
    """Run the whole battery. Returns an exit code; never touches the live tree.

    Split out of main() so the byte-identity assertion below runs on EVERY
    path, including the four control aborts. It used to sit after the `try`,
    so a `return 1` from inside skipped it - and an abnormal exit is exactly
    when you most want to know whether the working tree was left alone.
    """
    sandbox = tempfile.mkdtemp(prefix="mutate-mutation-scoring-")
    root = os.path.join(sandbox, "repo")
    survived, no_verdict, not_applied, killed, misattributed = [], [], [], [], []
    try:
        shutil.copytree(REPO, root, ignore=shutil.ignore_patterns(
            ".git", "__pycache__", "venv", "*.pyc"))
        target = os.path.join(root, TARGET_REL)
        with open(target) as fh:
            pristine = fh.read()

        print("CONTROL A: the clean sandbox must be GREEN")
        ok, out = run_suites(root)
        clean_ran = ran_count(out)
        if not ok:
            print(f"  *** CONTROL A FAILED ({clean_ran} ran) - NO DATA. "
                  f"Fix the suite before reading any score. ***")
            print(out[-2500:])
            return 1
        print(f"  clean: GREEN, {clean_ran} tests ran\n")

        for label, anchor, replacement in (CONTROL_B, CONTROL_C):
            with open(target, "w") as fh:
                fh.write(pristine)
            print(label)
            if not apply_mutation(target, anchor, replacement):
                print("  *** control could not be applied - NO DATA ***")
                return 1
            ok, out = run_suites(root)
            verdict, fails = score_run(ok, out, clean_ran)
            ran = ran_count(out)
            expected = KILLED if label.startswith("CONTROL B") else NO_VERDICT
            print(f"  {format_verdict(verdict, fails).strip()} "
                  f"({ran} ran) - expected {expected}")
            if verdict != expected:
                print("  *** CONTROL WRONG - the scorer is broken, NO DATA ***")
                return 1
            # CONTROL C MUST BE THE SHAPE IT CLAIMS, not merely the verdict.
            #
            # NO_VERDICT has two producers - zero named cases, and a moved
            # ran-count - and asserting the verdict alone accepts either. A
            # `SystemExit` or `KeyboardInterrupt` at module scope also scores
            # NO VERDICT, with ran=0 and nothing named, and would keep this
            # control printing a healthy line while no longer exercising the
            # T-527.18 family it exists for. That family is specifically:
            # unittest wraps the unimportable module in `_FailedTest`, NAMES
            # it, and the collapsed count is the only tell. So assert both
            # halves. Found by review, 2026-08-14.
            if label.startswith("CONTROL C"):
                if ran != 1 or not any("_FailedTest" in f for f in fails):
                    print(f"  *** CONTROL C IS NOT AN IMPORT DEATH "
                          f"(ran={ran}, expected 1; named={fails}). It scored "
                          f"NO VERDICT for the wrong reason, so it no longer "
                          f"exercises the shape it was written for. "
                          f"NO DATA ***")
                    return 1
        print()

        print(f"{len(MUTANTS)} MUTANTS")
        for i, (label, anchor, replacement, expect) in enumerate(MUTANTS, 1):
            with open(target, "w") as fh:
                fh.write(pristine)
            print(f"[{i}/{len(MUTANTS)}] {label}")
            # NOT APPLIED and NO VERDICT are different results and were being
            # summed under one heading. An anchor that stopped matching is a
            # decayed harness; a red run that collected the wrong number of
            # tests is a mutant that told us nothing. Both mean "no data about
            # the suite", and they send you to different files.
            if not apply_mutation(target, anchor, replacement):
                not_applied.append(label)
                continue
            ok, out = run_suites(root)
            verdict, fails = score_run(ok, out, clean_ran)
            print(f"{format_verdict(verdict, fails)} ({ran_count(out)} ran "
                  f"vs {clean_ran} clean)")
            if verdict == KILLED:
                # ATTRIBUTION IS PART OF THE SCORE, not a note beside it.
                # A mutant killed only by collateral damage is evidence about
                # some other test, and counting it toward "16 killed" inflates
                # the figure in exactly the way a survivor never can. This used
                # to print and then append to `killed` anyway, leaving the run
                # exit 0. Found by review, 2026-08-14.
                if not [f for f in fails if expect in f]:
                    print(f"  *** killed, but NOT by the test named for it "
                          f"({expect}). Read why before counting it. ***")
                    misattributed.append(label)
                print(f"      {', '.join(sorted(set(fails))[:3])}")
                killed.append(label)
            elif verdict == SURVIVED:
                survived.append(label)
            else:
                no_verdict.append(label)

        print(f"\n{len(killed)} killed, {len(survived)} survived, "
              f"{len(no_verdict)} no verdict, {len(not_applied)} not applied, "
              f"of {len(MUTANTS)}")
        for label in survived:
            print(f"  SURVIVOR: {label}")
        for label in no_verdict:
            print(f"  NO VERDICT: {label}")
        for label in not_applied:
            print(f"  NOT APPLIED: {label}")
        for label in misattributed:
            print(f"  MISATTRIBUTED (killed by another test): {label}")
        if survived or no_verdict or not_applied or misattributed:
            return 1
        return 0
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)

def main():
    """Run the battery, then assert the working tree was left alone.

    The assertion runs whatever `_battery()` did - a clean sweep, a survivor,
    or an abort at CONTROL A. Read the byte-identity line before believing
    any score above it: a wait loop proves the process exited, not that its
    cleanup ran, so the absence of this line is the absence of evidence.
    """
    target = os.path.join(REPO, TARGET_REL)
    before = sha(target)
    # Pessimistic default: if _battery() dies before assigning, the run has
    # produced no score and must not exit 0.
    rc = 1
    try:
        rc = _battery()
    finally:
        # ASSIGN, NEVER RETURN, FROM THIS BLOCK. A `return` inside `finally`
        # discards any in-flight exception, so a crashing battery would exit
        # 1 with no traceback - indistinguishable from an honest "a mutant
        # survived", and the more misleading of the two because it reads as a
        # result rather than as a failure to produce one.
        after = sha(target)
        if before != after:
            print(f"\n*** WORKING TREE MODIFIED - {TARGET_REL} DIFFERS from "
                  f"its pre-run state. This harness is SANDBOXED and must "
                  f"never write here. Fix before committing. ***")
            # A stranded mutation outranks any score the battery produced.
            rc = 1
        else:
            # Names the one file it measured, because "TREE UNTOUCHED" is a
            # wider claim than the check makes. The rest of the tree is
            # covered by the harness never writing outside the sandbox, which
            # test_suite_isolation.py's restore probe measures independently.
            print(f"\n{TARGET_REL}: byte-identical to its pre-run state "
                  f"(sha {after[:12]}). No other path is written by this "
                  f"harness.")
    return rc


if __name__ == "__main__":
    sys.exit(main())
