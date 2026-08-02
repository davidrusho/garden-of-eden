#!/usr/bin/env python3
"""Mutation battery for the deploy path (T-493).

Prove that tests/test_deploy_verify.py can FAIL. A green suite over a verifier
is worth nothing until you know the suite notices when the verifier stops
verifying - and that is the whole hazard here, because a broken verifier's
output is indistinguishable from a clean host.

SANDBOXED BY CONSTRUCTION. Every mutant is applied to a `shutil.copytree(REPO)`
copy, never to the working tree. A battery killed by SIGTERM skips `finally`
outright, and SIGINT never reaches a background child, so an in-place harness
leaves a plausible-looking edit sitting in the repository afterwards - it
happened twice in one session on this repo. There is nothing to restore here
because nothing in the working tree is ever written.

WHAT IS MUTATED, and why these. The rule is to list the actions that destroy or
overwrite something first and confirm each has a mutant, because the irreversible
action is the hardest to reach and its absence is invisible in a kill count. On
this deploy path they are:

  * `git reset --hard` in the rollback           -> D-ROLLBACK-*  (4 mutants)
  * `git merge` moving the checkout              -> D-FF-ONLY, D-NO-UPSTREAM
  * arming a unit that can REBOOT the host       -> D-NETWATCH-*  (2 mutants)
  * installing on top of unverified drift        -> D-PREFLIGHT
  * reporting a deploy as verified when it isn't -> D-VERIFY-*, D-FOLD-* (4)
  * every way the verifier can report a false
    all-clear                                    -> V-* (14 mutants)

Several are REINTRODUCTION mutants - they add code back rather than breaking
code that is present - because a suite that merely tolerates an absence will not
notice the thing returning. V-NAIVE-PIPELINE restores `git show | sha256sum`,
whose swallowed exit code is exactly how a failed read becomes the SHA-256 of
the empty string and compares equal to everything.

Two are COMBINATION mutants, added after the first full run. Three singles
survived, and in each case the reason was that the guard they break is a second
line of defence masked by an earlier one - the "this code is redundant" reading
of a survivor rather than the "this test is weak" reading. Removing both halves
at once is the only mutant shape that can tell those apart, and both combinations
are caught. See COMBOS below.

Both controls are gated BEFORE any verdict is read, because a mutation battery
inverts the usual false-all-clear: a mutant is scored by whether the test run
FAILED, so a broken scorer reports every mutant caught - the most reassuring
output available.

  Control A  clean tree must score GREEN (survived)
  Control B  a deliberately broken assertion must score RED (caught)

Control A alone is worthless: it is scored by the same path. stderr is merged,
because unittest reports there and `2>/dev/null` would blank the text being
matched. Each mutated file is diffed against its pristine copy to prove the edit
landed, and every anchor must match EXACTLY ONCE in the file.

Usage:
    python3 tests/mutate_deploy_verify.py              # everything
    python3 tests/mutate_deploy_verify.py --list
    python3 tests/mutate_deploy_verify.py --only V-SYMLINK D-FF-ONLY
    python3 tests/mutate_deploy_verify.py --jobs 1
"""
# Reviewed: 2026-08-02 against 9286e18 (T-493)
import argparse
import concurrent.futures
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUITE = "tests.test_deploy_verify"

VERIFIER = "bin/verify-deployed-artifacts.sh"
DEPLOY = "bin/deploy.sh"
TESTS = "tests/test_deploy_verify.py"


# name, file, anchor, replacement, one-line description of the defect it restores
MUTANTS = [
    # ---- the verifier's false all-clears --------------------------------
    (
        "V-SYMLINK", VERIFIER,
        '    if [ -L "$dest" ]; then',
        '    if false; then',
        "a masked unit is hashed rather than refused, so /dev/null passes",
    ),
    (
        "V-EMPTY-SHA", VERIFIER,
        '    if [ "$out" = "$EMPTY_SHA" ] && [ "${size:-0}" -gt 0 ]; then',
        '    if false; then',
        "the empty-string digest is accepted as a real hash",
    ),
    (
        "V-NAIVE-PIPELINE", VERIFIER,
        '''    size=$(git -C "$INSTALL_DIR" cat-file -s "$rev:$path" 2>/dev/null) || return 1
    tmp=$(mktemp "${TMPDIR:-/tmp}/gardyn-verify.XXXXXX") || return 1
    if ! git -C "$INSTALL_DIR" show "$rev:$path" > "$tmp" 2>/dev/null; then
        rm -f "$tmp"
        return 1
    fi
    out=$(hash_file "$tmp")
    rm -f "$tmp"''',
        '''    size=1
    out=$(git -C "$INSTALL_DIR" show "$rev:$path" 2>/dev/null | $HASHER)
    out="${out%% *}"''',
        "REINTRODUCED: git show | hasher, whose pipeline swallows rc 128",
    ),
    (
        "V-ZERO-UNITS", VERIFIER,
        'if [ ${#units[@]} -eq 0 ]; then',
        'if false; then',
        "a relocated source directory verifies nothing and reports success",
    ),
    (
        "V-MISMATCH-EXIT", VERIFIER,
        '    exit $EXIT_MISMATCH',
        '    exit $EXIT_OK',
        "a mismatch is reported but exits 0, so callers see a pass",
    ),
    (
        "V-CANNOT-CHECK-CODE", VERIFIER,
        'EXIT_CANNOT_CHECK=2',
        'EXIT_CANNOT_CHECK=0',
        '"could not check" collapses into "clean"',
    ),
    (
        "V-MISSING-DEST", VERIFIER,
        '        record_mismatch "$u: not deployed - $dest does not exist"',
        '        log_info "$u: not deployed - $dest does not exist"',
        "a unit that was never installed is reported as informational",
    ),
    (
        "V-NOT-PLAIN-FILE", VERIFIER,
        '    if [ ! -f "$dest" ]; then',
        '    if false; then',
        "a directory where a unit should be is hashed instead of refused",
    ),
    (
        "V-BOTH-CLASSES", VERIFIER,
        'if [ "$checked_tracked" -eq 0 ] || [ "$checked_units" -eq 0 ]; then',
        'if false; then',
        "an entire class of artifact may go unchecked behind the other's count",
    ),
    (
        "V-REV-IGNORED", VERIFIER,
        '               REV="$1" ;;',
        '               REV="HEAD" ;;',
        "--rev is accepted and ignored, so a check runs against the wrong commit",
    ),
    (
        "V-UNKNOWN-OPT", VERIFIER,
        '        *) usage >&2; echo "unknown option: $1" >&2; exit $EXIT_CANNOT_CHECK ;;',
        '        *) ;;',
        "a mistyped --rev is silently dropped and HEAD is checked instead",
    ),
    (
        "V-SRC-OUTSIDE", VERIFIER,
        '''case "$SRC_PREFIX" in
    /*|"$UNIT_SRC_DIR")
        log_error "unit source directory is outside the checkout: $UNIT_SRC_DIR - no revision holds those files, so they cannot be verified"
        exit $EXIT_CANNOT_CHECK ;;
esac''',
        ':',
        "units with no blob at any revision are compared anyway",
    ),
    (
        "V-CHECKOUT-DIFF", VERIFIER,
        'if [ -n "$drifted" ]; then',
        'if false; then',
        "an edited tracked file in the checkout is not reported",
    ),
    (
        "V-NOT-A-CHECKOUT", VERIFIER,
        'if [ "$toplevel" != "$INSTALL_DIR" ]; then',
        'if false; then',
        "a sandbox inside another repository is verified against its revisions",
    ),

    # ---- deploy.sh: the destructive and irreversible half ----------------
    (
        "D-ROLLBACK-DIRTY", DEPLOY,
        '    if [ -n "$dirty" ] && [ "$FORCE" -ne 1 ]; then',
        '    if false; then',
        "git reset --hard silently discards uncommitted work",
    ),
    (
        "D-ROLLBACK-UNRESOLVED", DEPLOY,
        '    [ -n "$target" ] || fail "cannot resolve $ROLLBACK_TO - fetch first, or name a commit this checkout already has"',
        '    :',
        "a typo'd rollback target is passed to git reset --hard unresolved",
    ),
    (
        "D-ROLLBACK-NO-RESTART", DEPLOY,
        '''    case " ${installer_args[*]} " in
        *" --restart-on-code-change "*) ;;
        *) installer_args+=("--restart-on-code-change") ;;
    esac''',
        '    :',
        "a rollback of Python-only code leaves the old code running",
    ),
    (
        "D-ROLLBACK-VS-CHECK", DEPLOY,
        '    fail "--check and --rollback-to are contradictory"',
        '    :',
        "--check silently performs a git reset --hard",
    ),
    (
        "D-FF-ONLY", DEPLOY,
        '        git -C "$INSTALL_DIR" merge --ff-only "$target" \\',
        '        git -C "$INSTALL_DIR" merge "$target" \\',
        "a diverged checkout is merged, inventing a commit nobody promoted",
    ),
    (
        "D-NO-UPSTREAM", DEPLOY,
        '    [ -n "$upstream" ] || fail "this checkout tracks no upstream branch. Point it at the deploy branch first: git branch --set-upstream-to=origin/deploy"',
        '    :',
        "a checkout tracking nothing is deployed from an empty ref",
    ),
    (
        "D-NETWATCH-GATE", DEPLOY,
        '    if [ -n "$netwatch_delta" ] && [ "$NETWATCH_OK" -ne 1 ]; then',
        '    if false; then',
        "a change to the reboot-capable watchdog ships unacknowledged",
    ),
    (
        "D-NETWATCH-PY", DEPLOY,
        '        bin/gardyn-netwatch.py 2>/dev/null)',
        '        2>/dev/null)',
        "the gate watches the unit file but not the script that decides to reboot",
    ),
    (
        "D-PREFLIGHT", DEPLOY,
        '    if [ $pre_rc -ne 0 ] && [ "$FORCE" -ne 1 ]; then',
        '    if false; then',
        "a deploy lands on top of drift nobody has accounted for",
    ),
    (
        "D-VERIFY-GATED", DEPLOY,
        '''log_info "verifying every deployed artifact against the checkout"
run_verifier
verify_rc=$?''',
        '''verify_rc=0
if [ $install_rc -eq 0 ]; then
    log_info "verifying every deployed artifact against the checkout"
    run_verifier
    verify_rc=$?
fi''',
        "REINTRODUCED: verification is skipped exactly when the install failed",
    ),
    (
        "D-VERIFY-CHECK-ONLY", DEPLOY,
        '''if [ "$CHECK_ONLY" -eq 1 ]; then
    run_verifier
    exit $?
fi''',
        'if false; then :; fi',
        "--check falls through and performs a real deploy",
    ),
    (
        "D-FOLD-VERIFY", DEPLOY,
        '[ $verify_rc -eq 0 ] || rc=1',
        ':',
        "a failed verification is reported and then exits 0",
    ),
    (
        "D-FOLD-INSTALL", DEPLOY,
        '[ $install_rc -eq 0 ] || rc=1',
        ':',
        "a failed install is reported and then exits 0",
    ),
    (
        "D-UNKNOWN-OPT", DEPLOY,
        '        *) usage >&2; fail "unknown option: $1" ;;',
        '        *) ;;',
        "a mistyped --netwatch-change-ok reads as the flag being absent",
    ),
    (
        "D-ARGS-DROPPED", DEPLOY,
        '"$INSTALLER" ${installer_args[@]+"${installer_args[@]}"}',
        '"$INSTALLER"',
        "--restart-on-code-change never reaches the installer",
    ),
]

# --- combination mutants -----------------------------------------------------
#
# Three single mutants SURVIVED the first full run, and the reason was the same
# in each case: the guard they break is a SECOND line of defence, masked by an
# earlier one. That is the "the code is redundant" reading of a survivor rather
# than the "the test is weak" reading, and the two need separating, because only
# one of them means anything is wrong.
#
#   V-EMPTY-SHA      is masked by `git cat-file -s` + `if ! git show`, which
#                    already refuse a path that is absent from the revision.
#   V-NAIVE-PIPELINE is masked by the EMPTY_SHA guard, which catches the empty
#                    digest that the swallowed rc 128 produces.
#   V-BOTH-CLASSES   is masked by the zero-tracked-files and zero-units guards,
#                    which refuse before either counter can reach the report.
#
# So each pair is genuinely belt-and-braces. Removing BOTH halves at once is
# what proves the braces are load-bearing rather than decorative - and it is the
# only mutant shape that can, since either half alone leaves the other standing.
COMBOS = [
    (
        "V-EMPTY-SHA+NAIVE",
        ["V-NAIVE-PIPELINE", "V-EMPTY-SHA"],
        "both halves of the failed-read defence removed: git show | hasher AND "
        "the empty-digest check, so a path absent from the revision hashes to "
        "e3b0c442... and compares equal to any other failed read",
    ),
    (
        "V-BOTH-CLASSES+ZERO-UNITS",
        ["V-ZERO-UNITS", "V-BOTH-CLASSES"],
        "both zero-artifact refusals removed, so a relocated source directory "
        "verifies the checkout, skips every installed copy, and reports success",
    ),
]


# Control B: a deliberately broken assertion in the suite itself. It must be
# CAUGHT, or the scorer is not reading the test result at all.
CONTROL_B = (
    "CONTROL-B", TESTS,
    "        self.assertEqual(proc.returncode, OK, self.all_output(proc))\n"
    "        self.assertIn(\"deploy complete and verified\", self.all_output(proc))",
    "        self.assertEqual(proc.returncode, 99, self.all_output(proc))\n"
    "        self.assertIn(\"deploy complete and verified\", self.all_output(proc))",
    "deliberately broken assertion - the scorer must report this as CAUGHT",
)


def ignore(_dir, names):
    return [n for n in names if n in (".git", "venv", "__pycache__", ".mypy_cache")]


def fingerprint(root):
    """content-hash + mtime of every file, so 'the tree was untouched' is proven."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in (".git", "venv", "__pycache__", ".mypy_cache")]
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                st = os.stat(p)
                with open(p, "rb") as fh:
                    out[os.path.relpath(p, root)] = (st.st_mtime_ns, st.st_size,
                                                     hashlib.sha256(fh.read()).hexdigest())
            except OSError:
                out[os.path.relpath(p, root)] = "<unreadable>"
    return out


def edits_for(spec):
    """A spec's edits as a list of (relpath, anchor, replacement).

    A plain mutant carries one; a COMBO names other mutants and carries theirs.
    """
    name = spec[0]
    if name == "CONTROL-A":
        return []
    if len(spec) == 3:                      # a COMBO: (name, [member, ...], why)
        by_name = {m[0]: m for m in MUTANTS}
        return [(by_name[n][1], by_name[n][2], by_name[n][3]) for n in spec[1]]
    return [(spec[1], spec[2], spec[3])]


def run_one(spec):
    """Apply one mutant in a fresh copy and score it. Returns (name, verdict, detail)."""
    name = spec[0]
    sandbox = tempfile.mkdtemp(prefix="mutate-t493-")
    try:
        dst = os.path.join(sandbox, "repo")
        shutil.copytree(REPO, dst, ignore=ignore, symlinks=True)

        for relpath, anchor, replacement in edits_for(spec):
            target = os.path.join(dst, relpath)
            with open(target) as fh:
                original = fh.read()

            # The anchor must match EXACTLY ONCE, checked in Python rather than
            # with grep -F, which treats each line of a multi-line pattern as a
            # separate alternative and would report a false unique match.
            hits = original.count(anchor)
            if hits != 1:
                return name, "HARNESS-BROKEN", f"anchor in {relpath} matched {hits} times, expected 1"
            with open(target, "w") as fh:
                fh.write(original.replace(anchor, replacement, 1))

            # Prove the edit landed. A replacement that matched nothing leaves
            # the file identical and the run then scores the CLEAN tree.
            with open(target) as fh:
                on_disk = fh.read()
            if on_disk == original:
                return name, "HARNESS-BROKEN", f"{relpath} is identical to the original"
            if replacement not in on_disk:
                return name, "HARNESS-BROKEN", f"the replacement text is not in {relpath}"

        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
        env.pop("GIT_DIR", None)
        env.pop("GIT_INDEX_FILE", None)
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", SUITE],
            cwd=dst, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env,
        )
        # stderr is merged above: unittest reports there, and 2>/dev/null would
        # blank the very text being matched.
        passed = proc.returncode == 0
        tail = proc.stdout[-600:]
        return name, ("SURVIVED" if passed else "CAUGHT"), tail
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--only", nargs="*", default=None)
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args()

    everything = MUTANTS + COMBOS

    if args.list:
        for spec in everything:
            print(f"{spec[0]:26s} {spec[-1]}")
        return 0

    before = fingerprint(REPO)

    # --- the two controls, gated before any verdict is read ------------------
    print("=" * 72, flush=True)
    print("CONTROL A - clean tree must be GREEN (scored SURVIVED)", flush=True)
    a_name, a_verdict, a_detail = run_one(("CONTROL-A", VERIFIER, "", "", ""))
    print(f"  {a_verdict}", flush=True)
    if a_verdict != "SURVIVED":
        print("  the clean tree does not pass; every result below is meaningless")
        print(a_detail)
        return 2

    print("CONTROL B - a deliberately broken assertion must be RED (scored CAUGHT)",
          flush=True)
    b_name, b_verdict, b_detail = run_one(CONTROL_B)
    print(f"  {b_verdict}", flush=True)
    if b_verdict != "CAUGHT":
        print("  the scorer does not notice a failing suite; every result below")
        print("  would read as CAUGHT for the wrong reason")
        print(b_detail)
        return 2
    print("both controls pass - results below are readable", flush=True)
    print("=" * 72, flush=True)

    selected = everything
    if args.only:
        wanted = set(args.only)
        selected = [m for m in everything if m[0] in wanted]
        missing = wanted - {m[0] for m in selected}
        if missing:
            print(f"no such mutant: {sorted(missing)}")
            return 2

    skipped = [m[0] for m in everything if m not in selected]

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for res in pool.map(run_one, selected):
            results.append(res)
            print(f"  {res[1]:16s} {res[0]}", flush=True)

    caught = [r for r in results if r[1] == "CAUGHT"]
    survived = [r for r in results if r[1] == "SURVIVED"]
    broken = [r for r in results if r[1] == "HARNESS-BROKEN"]

    print("=" * 72)
    print(f"{len(caught)}/{len(selected)} killed")
    if skipped:
        print(f"NOT RUN ({len(skipped)}): {', '.join(skipped)}")
    for name, _v, detail in survived:
        why = next(m[-1] for m in everything if m[0] == name)
        print(f"\nSURVIVED {name}: {why}")
        print(detail)
    for name, _v, detail in broken:
        print(f"\nHARNESS-BROKEN {name}: {detail}")

    after = fingerprint(REPO)
    if before != after:
        changed = sorted(set(before) ^ set(after)) + \
            sorted(k for k in set(before) & set(after) if before[k] != after[k])
        print(f"\nWORKING TREE CHANGED - this harness is supposed to be sandboxed: {changed}")
        return 2
    print("\nworking tree unchanged in content AND mtime across the run")

    return 0 if not survived and not broken else 1


if __name__ == "__main__":
    sys.exit(main())
