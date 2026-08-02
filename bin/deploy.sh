#!/bin/bash
#
# Reviewed: 2026-08-02 against 9286e18 (T-493)
#
# The deploy. One entrypoint, so the hash verification is not something anyone
# has to remember.
#
# WHAT MOVES THE HOST FORWARD IS THE `deploy` BRANCH, NOT `main`. A pull from
# main that fixes one thing also advances mqtt.py - the grow-light and pump
# controller - with plants on the end of it, which is why T-479 deployed by
# copying files instead. But copying leaves the checkout describing only its own
# pointer and saying nothing true about what is running, and disproving that
# state cost a session (T-485). The coupling objection is a BRANCHING problem,
# not a transport problem: a branch the maintainer promotes deliberately gives
# the release control that file-copy was reaching for, and keeps an honest
# checkout. Promotion policy lives in docs/DEPLOY.md.
#
# THIS HOST HAS NO PHYSICAL RECOVERY PATH. Nobody is going to pull the SD card,
# so a deploy that leaves it unreachable is terminal rather than inconvenient.
# Three consequences are built into this script:
#
#   1. Verification runs on EVERY deploy and runs even when the install FAILED,
#      because a failed deploy is exactly when knowing what is on disk matters.
#   2. `--rollback-to <rev>` is a single remote command, and the objects it
#      needs are already local after any fetch - so a rollback does not depend
#      on the network being healthy, on GitHub, or on anyone force-pushing.
#   3. A change to gardyn-netwatch is GATED. That unit runs as root, reconnects
#      Wi-Fi and can REBOOT the host; it is the only artifact here whose failure
#      mode is "the host stops coming back". Deploying a change to it requires
#      saying so, and the instruction is to disarm its timer first so a bad
#      version cannot start rebooting before anyone can look at it.
#
# `--check` is read-only, unprivileged, and safe to run on the live host at any
# moment. It is the before-and-after instrument for any change, including one
# somebody else is making.
#
# DELIBERATELY NOT bin/setup.sh. setup.sh is the FIRST-RUN script: it runs
# `apt update`/`apt install`, rebuilds the venv, rewrites /boot/config.txt and
# /etc/modules, calls raspi-config, adds group memberships and offers a reboot.
# T-477 made it stop generating mqtt.service and call the installer instead, so
# it now forwards its arguments correctly - but that fixed the unit half, not
# the fact that it is a provisioning script. Running it to ship a Python change
# would put an apt transaction and a venv rebuild in front of a grow light.
#
# Environment seams, both defaulted for real use and overridden only by tests:
#   GARDYN_INSTALLER   the unit installer  (default <repo>/bin/install-systemd-units.sh)
#   GARDYN_VERIFIER    the hash verifier   (default <repo>/bin/verify-deployed-artifacts.sh)

# No `set -u`: macOS ships bash 3.2, where `${#arr[@]}` on an empty array is an
# unbound-variable error. Failures are handled explicitly instead.
set -o pipefail

GRN="\033[32m"
RED="\033[31m"
YLW="\033[33m"
GRY="\033[90m"
LGY="\033[37m"
RST="\033[0m"

function log_error { echo -e "[${RED}ERROR${RST}]: $*" >&2; }
function log_warn  { echo -e "[${YLW}WARN${RST}]: $*" >&2; }
function log_pass  { echo -e "[${GRN}PASS${RST}]: $*"; }
function log_info  { echo -e "[${GRY}INFO${RST}]: ${LGY}$*${RST}" >&2; }

function fail {
    log_error "$*"
    exit 2
}

function usage {
    cat <<'USAGE'
Usage: deploy.sh [--check] [--no-pull] [--rollback-to <rev>] [--force]
                 [--netwatch-change-ok]
                 [--restart-on-code-change] [--remove-retired]

Fast-forward the checkout to its upstream, install the systemd units, and
hash-verify every deployed artifact against the resulting commit.

  --check                  Verify only. Runs nothing else, writes nothing, uses
                           no sudo. Safe on the live host at any time.
  --no-pull                Install and verify without moving the checkout.
  --rollback-to <rev>      Reset the checkout to <rev> and redeploy it. Implies
                           --no-pull and --restart-on-code-change. This is the
                           remote recovery path; the objects are already local.
  --force                  Proceed past a failed pre-flight verification, or
                           discard local modifications during a rollback.
  --netwatch-change-ok     Acknowledge that this deploy changes gardyn-netwatch,
                           the unit that can reboot the host.

  --restart-on-code-change Forwarded to install-systemd-units.sh: restart
                           mqtt.service when the checkout moved but no unit file
                           did. This is the usual flag for a Python-only change.
  --remove-retired         Forwarded to install-systemd-units.sh.

Exit status:
  0  deployed and verified
  1  the install or the verification failed - the host is NOT known-good
  2  refused before changing anything
USAGE
}

CHECK_ONLY=0
NO_PULL=0
FORCE=0
NETWATCH_OK=0
ROLLBACK_TO=""
installer_args=()

while [ $# -gt 0 ]; do
    case "$1" in
        --check) CHECK_ONLY=1 ;;
        --no-pull) NO_PULL=1 ;;
        --force) FORCE=1 ;;
        --netwatch-change-ok) NETWATCH_OK=1 ;;
        --rollback-to) shift
                       [ $# -gt 0 ] || fail "--rollback-to needs a revision"
                       ROLLBACK_TO="$1" ;;
        --rollback-to=*) ROLLBACK_TO="${1#--rollback-to=}" ;;
        --restart-on-code-change|--remove-retired) installer_args+=("$1") ;;
        -h|--help) usage; exit 0 ;;
        # A typo must not read as the flag being absent - and here the flags
        # that would be silently dropped are the ones that gate a reboot-capable
        # unit and a file deletion.
        *) usage >&2; fail "unknown option: $1" ;;
    esac
    shift
done

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
INSTALL_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)

INSTALLER="${GARDYN_INSTALLER:-$SCRIPT_DIR/install-systemd-units.sh}"
VERIFIER="${GARDYN_VERIFIER:-$SCRIPT_DIR/verify-deployed-artifacts.sh}"

[ -x "$INSTALLER" ] || fail "installer not executable: $INSTALLER"
[ -x "$VERIFIER" ]  || fail "verifier not executable: $VERIFIER"

if [ -n "$ROLLBACK_TO" ] && [ "$CHECK_ONLY" -eq 1 ]; then
    fail "--check and --rollback-to are contradictory"
fi

# `git rev-parse --show-toplevel` walks UP, so without this a sandbox sitting
# inside some other repository would be fast-forwarded and verified against a
# revision that has nothing to do with the code deployed here.
toplevel=$(git -C "$INSTALL_DIR" rev-parse --show-toplevel 2>/dev/null)
[ "$toplevel" = "$INSTALL_DIR" ] || fail "$INSTALL_DIR is not the root of a git checkout"

function run_verifier {
    "$VERIFIER" "$@"
}

# --- --check: read-only, and that is the whole point -------------------------
if [ "$CHECK_ONLY" -eq 1 ]; then
    run_verifier
    exit $?
fi

# --- rollback ----------------------------------------------------------------
if [ -n "$ROLLBACK_TO" ]; then
    target=$(git -C "$INSTALL_DIR" rev-parse --verify "$ROLLBACK_TO^{commit}" 2>/dev/null)
    [ -n "$target" ] || fail "cannot resolve $ROLLBACK_TO - fetch first, or name a commit this checkout already has"

    # `reset --hard` discards uncommitted work silently, which is the wrong
    # default even here. Name what would be lost and make the operator say so;
    # it is one extra remote command, and rollback is not an emergency in which
    # a second keystroke matters.
    dirty=$(git -C "$INSTALL_DIR" diff --name-only HEAD -- 2>/dev/null)
    if [ -n "$dirty" ] && [ "$FORCE" -ne 1 ]; then
        log_error "the checkout has local modifications that --rollback-to would discard:"
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            log_error "  - $f"
        done <<EOF
$dirty
EOF
        fail "re-run with --force to discard them, or commit them first"
    fi

    log_warn "rolling back to $target"
    git -C "$INSTALL_DIR" reset --hard "$target" || fail "git reset --hard $target failed"
    NO_PULL=1
    # A rollback that leaves the old code running is not a rollback. No unit
    # file need have changed, so nothing else would restart the service.
    case " ${installer_args[*]} " in
        *" --restart-on-code-change "*) ;;
        *) installer_args+=("--restart-on-code-change") ;;
    esac
fi

# --- pre-flight: what is on this host RIGHT NOW ------------------------------
#
# Deploying on top of unknown drift is how a host stops being describable. If
# the current state does not match the current commit, say so and stop - the
# remedy is a rollback or an explicit --force, both of which are remote.
if [ -z "$ROLLBACK_TO" ]; then
    log_info "pre-flight: verifying the host against its current checkout"
    run_verifier --quiet
    pre_rc=$?
    if [ $pre_rc -ne 0 ] && [ "$FORCE" -ne 1 ]; then
        log_error "pre-flight verification exited $pre_rc - this host does not match its own checkout."
        log_error "Deploying on top of that would make the result unattributable. Roll back with"
        log_error "  ./bin/deploy.sh --rollback-to <last known good>"
        log_error "or re-run with --force if you know why it differs."
        exit 2
    fi
    [ $pre_rc -eq 0 ] || log_warn "pre-flight verification exited $pre_rc; --force given, continuing anyway"
fi

# --- fast-forward to the deploy branch ---------------------------------------
if [ "$NO_PULL" -ne 1 ]; then
    upstream=$(git -C "$INSTALL_DIR" rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null)
    [ -n "$upstream" ] || fail "this checkout tracks no upstream branch. Point it at the deploy branch first: git branch --set-upstream-to=origin/deploy"

    remote="${upstream%%/*}"
    log_info "fetching $remote"
    git -C "$INSTALL_DIR" fetch "$remote" || fail "git fetch $remote failed"

    target=$(git -C "$INSTALL_DIR" rev-parse --verify "$upstream" 2>/dev/null)
    [ -n "$target" ] || fail "cannot resolve $upstream after fetch"

    # Checked BEFORE the merge, so a refusal leaves the tree untouched.
    #
    # gardyn-netwatch is the one artifact whose bad version can take the host
    # away permanently: it runs as root and its escalation path ends in
    # `reboot`. Its own consecutive-reboot cap is the only thing standing
    # between a bad deploy and a loop, and that cap is part of what a deploy
    # can change. So a netwatch change has to be acknowledged, and the
    # acknowledgement carries the ordering advice with it.
    netwatch_delta=$(git -C "$INSTALL_DIR" diff --name-only HEAD "$target" -- \
        services/etc/systemd/system/gardyn-netwatch.service \
        services/etc/systemd/system/gardyn-netwatch.timer \
        bin/gardyn-netwatch.py 2>/dev/null)
    if [ -n "$netwatch_delta" ] && [ "$NETWATCH_OK" -ne 1 ]; then
        log_error "this deploy changes the network watchdog, which runs as root and can REBOOT this host:"
        while IFS= read -r f; do
            [ -n "$f" ] || continue
            log_error "  - $f"
        done <<EOF
$netwatch_delta
EOF
        log_error "There is no physical recovery path for this machine. Disarm the watchdog first:"
        log_error "  sudo systemctl disable --now gardyn-netwatch.timer"
        log_error "then re-run with --netwatch-change-ok, confirm the new version by hand, and re-enable it:"
        log_error "  sudo systemctl enable --now gardyn-netwatch.timer"
        exit 2
    fi

    if [ "$target" = "$(git -C "$INSTALL_DIR" rev-parse HEAD)" ]; then
        log_info "already at $target - nothing to fast-forward"
    else
        log_info "fast-forwarding to $target ($upstream)"
        # --ff-only, never a merge: the deploy branch is promoted by the
        # maintainer and this host is a follower. A checkout that cannot
        # fast-forward has diverged - usually because a rollback moved it back -
        # and merging that would invent a commit nobody reviewed.
        git -C "$INSTALL_DIR" merge --ff-only "$target" \
            || fail "cannot fast-forward to $upstream. The checkout has diverged (a rollback does this deliberately). Re-promote the deploy branch, or reset to it explicitly."
    fi
fi

# --- install -----------------------------------------------------------------
log_info "running $INSTALLER ${installer_args[*]}"
"$INSTALLER" ${installer_args[@]+"${installer_args[@]}"}
install_rc=$?
[ $install_rc -eq 0 ] || log_error "the unit installer exited $install_rc"

# --- verify, ALWAYS ----------------------------------------------------------
#
# Deliberately not gated on the install succeeding. A failed install is exactly
# the run whose aftermath nobody can describe: some units copied, some not, the
# service maybe restarted. Skipping verification there would withhold the answer
# at the only moment it is needed.
log_info "verifying every deployed artifact against the checkout"
run_verifier
verify_rc=$?

rc=0
[ $install_rc -eq 0 ] || rc=1
[ $verify_rc -eq 0 ] || rc=1

if [ $rc -eq 0 ]; then
    log_pass "deploy complete and verified at $(git -C "$INSTALL_DIR" rev-parse HEAD)"
else
    log_error "DEPLOY NOT VERIFIED (installer $install_rc, verifier $verify_rc)."
    log_error "Roll back with: ./bin/deploy.sh --rollback-to <last known good>"
fi
exit $rc
