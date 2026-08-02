#!/bin/bash
#
# Reviewed: 2026-08-02 against 9286e18 (T-493)
#
# Prove that what is DEPLOYED on this host is what a given commit says it is.
#
# Why this exists. `git status` in the checkout answers a narrower question than
# anyone reads it as answering: it describes the checkout's own pointer and the
# files under it, and says NOTHING about the copies this project installs
# elsewhere. The systemd units under /etc/systemd/system are copies made by
# bin/install-systemd-units.sh; they can be edited in place, half-written by an
# interrupted run, replaced by `systemctl mask`, or simply left behind by a
# rollback, and every one of those states reports a clean `git status`. T-485
# spent a session disproving a drift claim that `git status` could neither
# confirm nor deny. The answer is a hash compared against the commit.
#
# TWO CLASSES OF ARTIFACT, TWO MECHANISMS, and the distinction is the whole
# point of the file:
#
#   Class A - the checkout (/home/gardyn/garden-of-eden). Git can see these, so
#             git is the instrument: a content diff of the working tree against
#             the revision. That IS a hash comparison - git compares blob
#             object ids - and it covers every tracked file without a list.
#
#   Class B - the installed copies (/etc/systemd/system/*.service, *.timer).
#             Git cannot see these at all. Each one is hashed and compared to
#             the blob at the same revision.
#
# THE UNIT LIST IS DERIVED FROM THE SOURCE DIRECTORY, never hand-maintained,
# for the same reason bin/install-systemd-units.sh derives its own: a
# hand-maintained list is how the health sampler and the network watchdog came
# to be deployed by hand and drift with no git signal. A new unit file is
# verified by the next run with no edit here.
#
# FAIL CLOSED, IN EVERY DIRECTION. "Could not look" and "looked and found
# nothing wrong" produce the same empty result, and only one of them is an
# all-clear:
#
#   - A run that verifies ZERO artifacts exits non-zero. A relocated source
#     directory would otherwise produce a confident, instant, meaningless pass.
#   - `git show <rev>:<path>` on a path absent from that revision prints
#     `fatal: path ... does not exist` to stderr, EMPTY stdout and rc 128. Piped
#     straight into a hasher that becomes e3b0c442...b855, the SHA-256 of the
#     empty string - which compares equal to any other failed read, so a
#     hash-compare that silently hashes nothing agrees with itself perfectly
#     across every input. That constant is checked for by name below.
#   - A deployed unit that is a SYMLINK is refused rather than hashed.
#     `systemctl mask` links a unit to /dev/null, and hashing that link follows
#     it: measured, `shasum -a 256` on a link to /dev/null returns the
#     empty-string hash with rc 0. A masked unit - the grow-light controller
#     switched off at the systemd level - would read as a clean, quiet PASS.
#
# READ-ONLY AND UNPRIVILEGED, deliberately and testably. It writes nothing,
# runs no `sudo`, and never restarts anything, so it is safe to run on the live
# host at any time - including as the before-and-after check around a change
# somebody else is making. Unit files are 0644 and /etc/systemd/system is 0755,
# so an unprivileged read is sufficient. `tests/test_deploy_verify.py` asserts
# the no-sudo property rather than trusting this paragraph.
#
# WHAT IT CANNOT TELL YOU: whether the RUNNING process is executing the code on
# disk. Those are different questions and only the second one is answerable
# from files. The installer records the revision it last restarted mqtt.service
# at, in .gardyn-source-revision beside the units, and this script REPORTS that
# comparison - it never writes that file, which has exactly one writer.
#
# Environment seams, both defaulted for real use and overridden only by tests:
#   SYSTEMD_UNIT_DIR      where units are installed  (default /etc/systemd/system)
#   GARDYN_UNIT_SRC_DIR   where they are read from   (default <repo>/services/...)

# No `set -u`: macOS ships bash 3.2, where `${#arr[@]}` on an empty array is an
# unbound-variable error. Failures are handled explicitly instead.
set -o pipefail

GRN="\033[32m"
RED="\033[31m"
YLW="\033[33m"
GRY="\033[90m"
LGY="\033[37m"
RST="\033[0m"

function log_error { echo -e "[${RED}FAIL${RST}]: $*" >&2; }
function log_warn  { echo -e "[${YLW}WARN${RST}]: $*" >&2; }
function log_pass  { echo -e "[${GRN} OK ${RST}]: $*"; }
function log_info  { echo -e "[${GRY}INFO${RST}]: ${LGY}$*${RST}" >&2; }

# Exit codes are three-valued on purpose. A caller that cannot distinguish
# "verified, and they differ" from "could not verify" will eventually treat the
# second as the first.
EXIT_OK=0
EXIT_MISMATCH=1
EXIT_CANNOT_CHECK=2

# SHA-256 of the empty string. Any read that fails and is piped into a hasher
# produces this, so seeing it where a non-empty blob was expected means the
# instrument failed, not that the files agree.
EMPTY_SHA="e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

mismatches=()
function record_mismatch {
    log_error "$*"
    mismatches+=("$*")
}

function usage {
    cat <<'USAGE'
Usage: verify-deployed-artifacts.sh [--rev <rev>] [--quiet]

Compare every deployed artifact against a commit and say which ones differ.
Read-only: it writes nothing, runs no sudo, and restarts nothing.

  --rev <rev>   Commit to verify against (default HEAD - the checkout's own
                revision, i.e. "is what is on disk what this checkout claims").
  --quiet       Suppress the per-artifact OK lines; still reports failures.

Exit status:
  0  every artifact matches
  1  at least one artifact differs from the revision
  2  the check could not be carried out - treat as unverified, NOT as clean
USAGE
}

REV="HEAD"
QUIET=0
while [ $# -gt 0 ]; do
    case "$1" in
        --rev) shift
               [ $# -gt 0 ] || { usage >&2; echo "--rev needs a value" >&2; exit $EXIT_CANNOT_CHECK; }
               REV="$1" ;;
        --rev=*) REV="${1#--rev=}" ;;
        --quiet) QUIET=1 ;;
        -h|--help) usage; exit 0 ;;
        # A typo must not read as the flag being absent.
        *) usage >&2; echo "unknown option: $1" >&2; exit $EXIT_CANNOT_CHECK ;;
    esac
    shift
done

# Portable, and deliberately not `readlink -f` / `realpath`: this script is
# exercised by the test suite on macOS, where those are not the GNU versions.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
INSTALL_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)

UNIT_SRC_DIR="${GARDYN_UNIT_SRC_DIR:-$INSTALL_DIR/services/etc/systemd/system}"
UNIT_DEST_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"

# --- the hasher --------------------------------------------------------------
#
# Linux ships sha256sum, macOS ships shasum. Both print `<hash>  <path>`, and
# both answer a missing file with rc 1 and nothing on stdout - observed rather
# than assumed. Having NEITHER is "could not check", never "clean".
if command -v sha256sum >/dev/null 2>&1; then
    HASHER="sha256sum"
elif command -v shasum >/dev/null 2>&1; then
    HASHER="shasum -a 256"
else
    log_error "no sha256sum and no shasum on PATH - cannot verify anything"
    exit $EXIT_CANNOT_CHECK
fi

# Hash a file on disk. Prints the bare hash on success; prints nothing and
# returns non-zero on any failure, so a caller cannot mistake a failed read for
# a value.
function hash_file {
    local out
    out=$($HASHER "$1" 2>/dev/null) || return 1
    out="${out%% *}"
    [ -n "$out" ] || return 1
    printf '%s' "$out"
}

# Hash the blob a revision holds at a path. Deliberately NOT
# `git show rev:path | sha256sum`: a pipeline exits with the status of its LAST
# command, so a `git show` that failed with rc 128 would be reported by the
# pipeline as success and the hasher would happily digest the empty stream.
# `git cat-file -s` first gives both an existence check and the size the
# emptiness guard needs.
function hash_rev_blob {
    local rev="$1" path="$2" size tmp out
    size=$(git -C "$INSTALL_DIR" cat-file -s "$rev:$path" 2>/dev/null) || return 1
    tmp=$(mktemp "${TMPDIR:-/tmp}/gardyn-verify.XXXXXX") || return 1
    if ! git -C "$INSTALL_DIR" show "$rev:$path" > "$tmp" 2>/dev/null; then
        rm -f "$tmp"
        return 1
    fi
    out=$(hash_file "$tmp")
    rm -f "$tmp"
    [ -n "$out" ] || return 1
    # The instrument, not the artifact: a non-empty blob that hashed to the
    # empty-string digest means the read produced nothing.
    if [ "$out" = "$EMPTY_SHA" ] && [ "${size:-0}" -gt 0 ]; then
        return 1
    fi
    printf '%s' "$out"
}

# --- preflight ---------------------------------------------------------------
#
# `git rev-parse --show-toplevel` walks UP, so a checkout that is not this
# directory - a sandbox that happens to sit inside some other repository -
# would otherwise be verified against a revision that has nothing to do with
# the code deployed here.
toplevel=$(git -C "$INSTALL_DIR" rev-parse --show-toplevel 2>/dev/null)
if [ "$toplevel" != "$INSTALL_DIR" ]; then
    log_error "$INSTALL_DIR is not the root of a git checkout - cannot verify anything against a revision"
    exit $EXIT_CANNOT_CHECK
fi

RESOLVED_REV=$(git -C "$INSTALL_DIR" rev-parse --verify "$REV^{commit}" 2>/dev/null)
if [ -z "$RESOLVED_REV" ]; then
    log_error "cannot resolve revision: $REV"
    exit $EXIT_CANNOT_CHECK
fi

[ -d "$UNIT_SRC_DIR" ] || { log_error "unit source directory not found: $UNIT_SRC_DIR"; exit $EXIT_CANNOT_CHECK; }
[ -d "$UNIT_DEST_DIR" ] || { log_error "systemd unit directory not found: $UNIT_DEST_DIR"; exit $EXIT_CANNOT_CHECK; }

log_info "verifying against $RESOLVED_REV ($REV)"

# Counted separately, and reported separately, because they are answered by
# different instruments and a single total hides which half was actually
# exercised. A run that verifies the checkout and silently skips every
# installed copy is the failure this script exists to remove.
checked_tracked=0
checked_units=0

# --- Class A: the checkout ---------------------------------------------------
#
# Every tracked file, without naming any of them. `git diff --name-only <rev>`
# compares the WORKING TREE to the revision, which is the question being asked:
# not "is the index clean" but "is the file on disk the file that commit holds".
drifted=$(git -C "$INSTALL_DIR" diff --name-only "$RESOLVED_REV" -- 2>/dev/null)
rc=$?
if [ $rc -ne 0 ]; then
    log_error "could not diff the checkout against $RESOLVED_REV"
    exit $EXIT_CANNOT_CHECK
fi
tracked_count=$(git -C "$INSTALL_DIR" ls-tree -r --name-only "$RESOLVED_REV" | wc -l | tr -d ' ')
if [ "${tracked_count:-0}" -eq 0 ]; then
    log_error "$RESOLVED_REV holds no tracked files - refusing to report a verification that checked nothing"
    exit $EXIT_CANNOT_CHECK
fi
checked_tracked=$tracked_count

if [ -n "$drifted" ]; then
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        record_mismatch "checkout: $f differs from $REV"
    done <<EOF
$drifted
EOF
else
    [ "$QUIET" -eq 1 ] || log_pass "checkout: $tracked_count tracked files match $REV"
fi

# Untracked-and-not-ignored files are reported, not failed. The drift that
# started T-485 looked exactly like this - bin/gardyn-netwatch.py hand-copied
# onto the Pi and showing as untracked - but a stray log file is not a reason to
# refuse a deploy, so this is a signal rather than a gate.
untracked=$(git -C "$INSTALL_DIR" ls-files --others --exclude-standard 2>/dev/null)
if [ -n "$untracked" ]; then
    log_warn "checkout: untracked files present (not verified against any revision):"
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        log_warn "  - $f"
    done <<EOF
$untracked
EOF
fi

# --- Class B: the installed copies -------------------------------------------
units=()
shopt -s nullglob
for f in "$UNIT_SRC_DIR"/*.service "$UNIT_SRC_DIR"/*.timer \
         "$UNIT_SRC_DIR"/*.socket "$UNIT_SRC_DIR"/*.path \
         "$UNIT_SRC_DIR"/*.target "$UNIT_SRC_DIR"/*.mount \
         "$UNIT_SRC_DIR"/*.slice; do
    units+=("$(basename "$f")")
done
shopt -u nullglob

# A glob that matches nothing exits 0, so a renamed or relocated source
# directory would otherwise verify the checkout, skip every installed copy, and
# report a clean run - the exact class of false all-clear this file exists to
# remove.
if [ ${#units[@]} -eq 0 ]; then
    log_error "no unit files found in $UNIT_SRC_DIR - refusing to report success for a run that verified no installed copy"
    exit $EXIT_CANNOT_CHECK
fi

# The source path a unit is deployed FROM, relative to the repository root, so
# the blob can be looked up at the revision. Derived from the configured source
# directory rather than hardcoded, so the test seam and production agree.
#
# A source directory OUTSIDE the checkout has no blob at any revision, so there
# is nothing to compare against. Refuse rather than hand `git show` an absolute
# path and let it fail one unit at a time - the whole run is unverifiable.
SRC_PREFIX="${UNIT_SRC_DIR#$INSTALL_DIR/}"
case "$SRC_PREFIX" in
    /*|"$UNIT_SRC_DIR")
        log_error "unit source directory is outside the checkout: $UNIT_SRC_DIR - no revision holds those files, so they cannot be verified"
        exit $EXIT_CANNOT_CHECK ;;
esac

for u in "${units[@]}"; do
    dest="$UNIT_DEST_DIR/$u"
    relsrc="$SRC_PREFIX/$u"

    # Refused, not followed. `systemctl mask` points a unit at /dev/null, and
    # hashing the link follows it to the empty-string digest with rc 0 - a
    # masked grow-light controller reading as a quiet PASS.
    if [ -L "$dest" ]; then
        record_mismatch "$u: deployed path is a symlink (masked?): $dest -> $(readlink "$dest")"
        checked_units=$((checked_units + 1))
        continue
    fi
    if [ ! -e "$dest" ]; then
        record_mismatch "$u: not deployed - $dest does not exist"
        checked_units=$((checked_units + 1))
        continue
    fi
    if [ ! -f "$dest" ]; then
        record_mismatch "$u: deployed path is not a plain file: $dest"
        checked_units=$((checked_units + 1))
        continue
    fi

    want=$(hash_rev_blob "$RESOLVED_REV" "$relsrc")
    if [ -z "$want" ]; then
        log_error "$u: cannot read $relsrc at $REV - the check could not be carried out"
        exit $EXIT_CANNOT_CHECK
    fi
    got=$(hash_file "$dest")
    if [ -z "$got" ]; then
        log_error "$u: cannot read $dest - the check could not be carried out"
        exit $EXIT_CANNOT_CHECK
    fi

    checked_units=$((checked_units + 1))
    if [ "$want" = "$got" ]; then
        [ "$QUIET" -eq 1 ] || log_pass "$u matches $REV ($want)"
    else
        record_mismatch "$u: DEPLOYED COPY DIFFERS from $REV - $dest has $got, $relsrc has $want"
    fi
done

# --- what the installer last restarted at ------------------------------------
#
# Reported, never written. bin/install-systemd-units.sh is the single writer of
# this file; duplicating that here would put two writers on one fact. It answers
# the question files cannot: whether the RUNNING service was started from the
# code now on disk.
revision_file="$UNIT_DEST_DIR/.gardyn-source-revision"
if [ -f "$revision_file" ]; then
    recorded=$(cat "$revision_file" 2>/dev/null)
    head_rev=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null)
    if [ -z "$recorded" ]; then
        log_warn "$revision_file is empty - nothing says what revision mqtt.service was started from"
    elif [ "$recorded" = "$head_rev" ]; then
        [ "$QUIET" -eq 1 ] || log_pass "mqtt.service was last restarted at $recorded, which is this checkout's HEAD"
    else
        log_warn "mqtt.service was last restarted at $recorded but this checkout is at $head_rev - the RUNNING code is not the code on disk. Re-run the deploy with --restart-on-code-change."
    fi
else
    log_warn "$revision_file does not exist - nothing on this host says what revision mqtt.service is running. The first install-systemd-units.sh run records one."
fi

# --- report ------------------------------------------------------------------
#
# Both halves must be non-zero. Either one at zero means an entire class of
# artifact went unexamined, and a total would hide it behind the other half's
# count.
if [ "$checked_tracked" -eq 0 ] || [ "$checked_units" -eq 0 ]; then
    log_error "verified $checked_tracked tracked files and $checked_units installed units - refusing to report success with a class unchecked"
    exit $EXIT_CANNOT_CHECK
fi

checked=$((checked_tracked + checked_units))

if [ ${#mismatches[@]} -gt 0 ]; then
    log_error "${#mismatches[@]} of $checked artifacts do NOT match $REV:"
    for m in "${mismatches[@]}"; do
        log_error "  - $m"
    done
    log_error "This host is not running what $REV says it is. Do not treat the last deploy as having taken effect."
    exit $EXIT_MISMATCH
fi

log_pass "$checked artifacts verified against $RESOLVED_REV ($checked_tracked tracked files, $checked_units installed units)"
exit $EXIT_OK
