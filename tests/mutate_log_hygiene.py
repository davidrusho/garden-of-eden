#!/usr/bin/env python3
"""Mutation battery for log_hygiene.py (T-527.28).

The module exists to stop a forged record reaching gardyn.log, and gardyn.log
is the only incident record a console-less Pi has. Every mutant below is a
plausible maintainer edit - a narrowing, a simplification, a "surely tabs
should be escaped too" - never a wholesale delete, because deleting a scrub
reddens any test that mentions it including one that tests nothing.

SANDBOXED via copytree: log_hygiene.py is imported by mqtt.py at module scope,
so mutating it in the working tree would hand any concurrent session a broken
log formatter with nothing to say so.

Run:  python3 tests/mutate_log_hygiene.py
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

TARGET_REL = "log_hygiene.py"
SUITES = ["tests.test_log_hygiene"]

# (label, anchor, replacement, the test whose name must appear in the kill)
MUTANTS = [
    ("scrub: stop indenting continuation lines - the forged record can begin "
     "a line again, which is the entire defect",
     '    return first + sep + "\\n".join(CONTINUATION_INDENT + line\n'
     '                                   for line in rest.split("\\n"))',
     "    return escaped",
     "exc_info_route_is_closed"),

    ("scrub: indent only the SECOND line, so a forgery on the third survives",
     '    return first + sep + "\\n".join(CONTINUATION_INDENT + line\n'
     '                                   for line in rest.split("\\n"))',
     '    tail = rest.split("\\n")\n'
     '    return first + sep + "\\n".join(\n'
     '        [CONTINUATION_INDENT + tail[0]] + tail[1:])',
     "exc_info_route_is_closed"),

    ("scrub: skip the escaping and only indent - \\r and \\x1b reach the log",
     "    escaped = text.translate(_TABLE)",
     "    escaped = text",
     "carriage_returns_and_escapes_do_not_survive"),

    ("CONTINUATION_INDENT emptied - indenting by nothing is not indenting",
     'CONTINUATION_INDENT = "  | "',
     'CONTINUATION_INDENT = ""',
     "exc_info_route_is_closed"),

    # The escape table, per exclusion. These are one-word edits and each
    # removes a different guarantee, so they are separate mutants rather than
    # one "break the table" mutant.
    ("_ESCAPE: stop excluding \\n, so newlines are escaped and a traceback "
     "collapses onto one unreadable line",
     "           if c not in (0x0A, 0x09)}",
     "           if c not in (0x09,)}",
     "tabs_and_newlines_are_KEPT"),

    ("_ESCAPE: stop excluding \\t, losing traceback indentation",
     "           if c not in (0x0A, 0x09)}",
     "           if c not in (0x0A,)}",
     "tabs_and_newlines_are_KEPT"),

    ("_ESCAPE: drop DEL (0x7f), a control character that renders as nothing",
     "_ESCAPE = {c: f\"\\\\x{c:02x}\" for c in list(range(0x20)) + [0x7F]",
     "_ESCAPE = {c: f\"\\\\x{c:02x}\" for c in list(range(0x20))",
     "carriage_returns_and_escapes_do_not_survive"),

    ("_ESCAPE: narrow the range to 0x00-0x0f, so \\x1b (ESC) survives and an "
     "ANSI sequence reaches the terminal reading the log",
     "for c in list(range(0x20)) + [0x7F]",
     "for c in list(range(0x10)) + [0x7F]",
     "carriage_returns_and_escapes_do_not_survive"),

    # The Formatter hook. This is the one that looks most like a tidy-up and
    # is the whole reason the module is not a message-level filter.
    ("ControlCharEscapingFormatter: scrub formatMessage instead of format, "
     "which leaves the exc_info route - the ticket's actual defect - open",
     "    def format(self, record):\n"
     "        return scrub(super().format(record))",
     "    def formatMessage(self, record):\n"
     "        return scrub(super().formatMessage(record))",
     "exc_info_route_is_closed"),

    ("install: set the formatter on the FIRST handler only, so the file gets "
     "it and the console does not (or the reverse)",
     "    for handler in root.handlers:\n"
     "        handler.setFormatter(formatter)",
     "    for handler in root.handlers[:1]:\n"
     "        handler.setFormatter(formatter)",
     "install_sets_the_formatter_on_every_handler"),

    ("install: return a constant instead of the handler count, so a no-op "
     "install reports success",
     "    return len(root.handlers)",
     "    return 1",
     "reports_ZERO_rather_than_silently_doing_nothing"),

    # The three a review ran and watched SURVIVE the first version of this
    # suite. Each is here because the test written for it did not exist, not
    # because the mutant is clever - which is the point: they mark real holes.
    ("install: the DEFAULT root is no longer the root logger - the shipped "
     "call passes no root, so gardyn.log silently loses all escaping",
     "    root = logging.getLogger() if root is None else root",
     '    root = logging.getLogger("not.the.root") if root is None else root',
     "importing_mqtt_leaves_every_root_handler_scrubbing"),

    ("install: ignore the fmt argument and hardcode a bare message format, "
     "discarding the record layout basicConfig was given",
     "    formatter = ControlCharEscapingFormatter(fmt)",
     '    formatter = ControlCharEscapingFormatter("%(message)s")',
     "shipped_format_is_the_single_sourced_one"),

    ("_ESCAPE: drop U+2028, which str.splitlines() treats as a line break "
     "while str.split(chr(10)) does not - a forgery for every Python reader",
     '_ESCAPE.update({0x85: "\\\\x85", 0x2028: "\\\\u2028", 0x2029: "\\\\u2029"})',
     '_ESCAPE.update({0x85: "\\\\x85", 0x2029: "\\\\u2029"})',
     "unicode_line_separators_cannot_forge_a_line"),
]

CONTROL_B = ("CONTROL B: scrub becomes the identity - must score KILLED",
             "    escaped = text.translate(_TABLE)",
             "    return text\n    escaped = text.translate(_TABLE)")

CONTROL_C = ("CONTROL C: compiles, dies at import - must score NO VERDICT",
             "import logging",
             "import logging\nimport a_module_that_certainly_does_not_exist")


def run_suites(root):
    """(ok, out). Sandbox root is the first positional arg - the restore probe
    in tests/test_suite_isolation.py locates the copy through it."""
    purge_pycache(root)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    out, ok = [], True
    for suite in SUITES:
        proc = subprocess.run([sys.executable, "-m", "unittest", suite],
                              cwd=root, env=env, capture_output=True,
                              text=True, timeout=600)
        out.append(proc.stdout + proc.stderr)
        ok = ok and proc.returncode == 0
    return ok, "\n".join(out)


def apply_mutation(path, anchor, replacement):
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
    sandbox = tempfile.mkdtemp(prefix="mutate-log-hygiene-")
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
            print(f"  *** CONTROL A FAILED ({clean_ran} ran) - NO DATA ***")
            print(out[-2500:])
            return 1
        # THE ZERO ABORT (T-554). A green run that collected NOTHING is NO
        # DATA, not a pass, and the omission is SILENT rather than loud:
        # `score_run` arms its ran-count tell with `ran < clean_ran`, which
        # at clean_ran == 0 can never fire, so the tell is inert while the
        # line below still prints a measurement-shaped "0 tests ran".
        if clean_ran == 0:
            print("  *** CONTROL A FAILED - a GREEN run that collected NO "
                  "tests. This is NO DATA, not a score. ***")
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
            print(f"  {format_verdict(verdict, fails).strip()} ({ran} ran) - "
                  f"expected {expected}")
            if verdict != expected:
                print("  *** CONTROL WRONG - the scorer is broken, NO DATA ***")
                return 1
            # Same shape check as mutate_mutation_scoring.py: NO_VERDICT has
            # two producers, and a control asserting only the verdict accepts
            # a SystemExit at module scope as an "import death".
            if label.startswith("CONTROL C") and (
                    ran != 1 or not any("_FailedTest" in f for f in fails)):
                print(f"  *** CONTROL C IS NOT AN IMPORT DEATH (ran={ran}, "
                      f"named={fails}) - NO DATA ***")
                return 1
        print()

        print(f"{len(MUTANTS)} MUTANTS")
        for i, (label, anchor, replacement, expect) in enumerate(MUTANTS, 1):
            with open(target, "w") as fh:
                fh.write(pristine)
            print(f"[{i}/{len(MUTANTS)}] {label}")
            if not apply_mutation(target, anchor, replacement):
                not_applied.append(label)
                continue
            ok, out = run_suites(root)
            verdict, fails = score_run(ok, out, clean_ran)
            print(f"{format_verdict(verdict, fails)} ({ran_count(out)} ran vs "
                  f"{clean_ran} clean)")
            if verdict == KILLED:
                if not [f for f in fails if expect in f]:
                    print(f"  *** killed, but NOT by the test named for it "
                          f"({expect}) ***")
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
        return 1 if (survived or no_verdict or not_applied
                     or misattributed) else 0
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def main():
    target = os.path.join(REPO, TARGET_REL)
    before = sha(target)
    rc = 1
    try:
        rc = _battery()
    finally:
        # Assign, never return, from this block: a `return` here would discard
        # an in-flight exception and a crash would read as an honest survivor.
        after = sha(target)
        if before != after:
            print(f"\n*** WORKING TREE MODIFIED - {TARGET_REL} DIFFERS. This "
                  f"harness is SANDBOXED and must never write here. ***")
            rc = 1
        else:
            print(f"\n{TARGET_REL}: byte-identical to its pre-run state "
                  f"(sha {after[:12]}).")
    return rc


if __name__ == "__main__":
    sys.exit(main())
