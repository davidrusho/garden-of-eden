#!/bin/bash
#
# Reviewed: 2026-08-01 against 3e8374c and 92dd3fd (T-477)
#
# Install the systemd units this project ships.
#
# The units are TRACKED FILES under services/etc/systemd/system/. This script
# copies them into the systemd unit directory; it never writes to them.
#
# Why it exists. setup.sh used to GENERATE mqtt.service with a heredoc whose
# target was that tracked path, so every setup run overwrote the file it was
# meant to deploy. Three consequences, in order of how quietly they bite:
#
#   1. The working tree came back dirty from a run that only claimed to install.
#   2. The generated text dropped `StartLimitIntervalSec=0`, `RestartSec=10`
#      and the `network-online.target` ordering added under T-471 - the
#      directives that stop a boot-time broker outage parking the unit in
#      `failed` permanently, which is the "router reboot leaves the garden with
#      no controller" failure. A setup re-run reverted the fix silently.
#   3. It knew about exactly one unit. The health sampler and the network
#      watchdog reached the Pi by hand, so the repo copy could drift from the
#      deployed copy with no git signal, and a rebuild lost both - the two
#      things that exist to make the next outage answerable and recoverable.
#
# The unit list is DERIVED FROM THE DIRECTORY, not hand-maintained, because a
# hand-maintained list is how (3) happened: a new unit file dropped into the
# source directory is deployed by the next run with no edit here. What gets
# ENABLED is derived from the presence of an `[Install]` section, so the two
# Type=oneshot units driven by timers are installed but never enabled directly.
# (`systemctl enable` on a unit with no [Install] does not fail - per
# systemctl(1) it "shows a warning" - so this grep is what makes the intent
# explicit rather than what avoids an error.)
#
# NOTHING IS ENABLED THAT CANNOT RUN. A unit whose ExecStart names a path that
# does not exist on this host is installed but not enabled, and the run exits
# non-zero. That matters because this is a PUBLIC repository: gardyn-netwatch
# is a watchdog that can REBOOT the host and is hardcoded for one specific LAN,
# so a checkout somewhere else must not quietly end up with it armed. The gate
# is not sufficient for a fork that happens to match these paths - see the
# README - but it makes the common case safe.
#
# Safe to re-run. A unit whose deployed copy already matches is re-installed
# (cheap, idempotent) but NOT restarted, so routine setup runs do not bounce
# the grow-light controller.
#
# THE RESTART DECISION SURVIVES AN INTERRUPTED RUN. It compares the source file
# to the DEPLOYED FILE, not to what systemd currently has loaded, so a run that
# stops between the install and the restart would otherwise leave the new file
# on disk with the old definition running - and the next run would see no
# difference, issue `start` instead of `restart`, and report success over a
# stale service. A `.<unit>.needs-restart` marker is written beside the unit as
# soon as the file is in place and removed only once the unit has actually been
# restarted, so the pending state crosses runs. Handled failures are also
# collected rather than aborted on, so one bad unit does not strand the others.
# The residual window is a kill landing between the `install` and the `touch`.
#
# A UNIT DROPPED FROM THE REPO IS NOT DELETED BY DEFAULT. Deleting unit files on
# a live host is the one thing here that cannot be undone by re-running, so the
# default is to REPORT a deployed unit the repo no longer ships and leave it
# alone. `--remove-retired` acts on it. That path is fail-closed twice over: it
# will only touch a name recorded in `.gardyn-installed-units`, the manifest
# THIS script writes, so a host that has never run it has nothing to remove and
# a unit belonging to another package can never be selected; and it refuses a
# destination that is not a plain file. Reporting rather than deleting matters
# because the retired unit that is most likely to exist is gardyn-netwatch,
# which can reboot the host - leaving it armed after it has been deleted from
# the repo is the failure worth being loud about.
#
# A PULL THAT CHANGES ONLY PYTHON CHANGES NO UNIT FILE, so the restart decision
# above correctly finds nothing to do - and the run then prints a column of PASS
# lines over a service still executing the old code. The checkout's git revision
# is recorded beside the units at the moment mqtt.service is actually restarted,
# and a later run whose revision differs says so instead of reporting success.
# `--restart-on-code-change` restarts the service instead of asking. The
# recorded revision is NOT updated by a run that only warned - the warning has
# to survive until something has actually restarted, which is the same reason
# the .needs-restart markers exist.
#
# Environment seams, both defaulted for real use and overridden only by tests:
#   SYSTEMD_UNIT_DIR      where units are installed  (default /etc/systemd/system)
#   GARDYN_UNIT_SRC_DIR   where they are read from   (default <repo>/services/...)

# No `set -u`: macOS ships bash 3.2, where `${#arr[@]}` on an empty array is an
# unbound-variable error. Failures are handled explicitly instead.
set -o pipefail

# \033 rather than \e: bash 3.2's builtin echo does not expand \e, and this
# script is run by its shebang, so it gets whatever /bin/bash is.
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

# An unrecoverable problem: nothing has been installed and nothing can be.
function fail {
    log_error "$*"
    exit 1
}

# A problem that must not stop the remaining units being installed and armed.
# Aborting mid-sequence is what leaves a unit installed but not activated, so
# these are collected and reported together at the end.
failures=()
function record_failure {
    log_error "$*"
    failures+=("$*")
}

function in_list {
    local needle="$1"; shift
    local c
    for c in "$@"; do
        [ "$c" = "$needle" ] && return 0
    done
    return 1
}

function usage {
    cat <<'USAGE'
Usage: install-systemd-units.sh [--remove-retired] [--restart-on-code-change]

  --remove-retired           Disable and delete deployed units this script
                             installed that the repository no longer ships.
                             Without it they are reported and left alone.
  --restart-on-code-change   Restart the service unit when the checkout's git
                             revision has moved since it was last restarted,
                             even though no unit FILE changed. Without it the
                             run says so and does not report a clean success.
USAGE
}

REMOVE_RETIRED=0
RESTART_ON_CODE_CHANGE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --remove-retired) REMOVE_RETIRED=1 ;;
        --restart-on-code-change) RESTART_ON_CODE_CHANGE=1 ;;
        -h|--help) usage; exit 0 ;;
        # A typo must not read as the flag being absent. `--remove-retried`
        # doing nothing quietly is the safe direction for THIS flag and the
        # wrong direction for the other one, so refuse either way.
        *) usage >&2; fail "unknown option: $1" ;;
    esac
    shift
done

# A unit is installed before it is activated, so a run that fails in between
# leaves the new file on disk with the old definition still loaded - and the
# NEXT run sees the file as unchanged and issues `start` rather than `restart`,
# reporting success over a stale service. The marker carries that pending state
# across runs: written once the file is in place, removed only once the unit has
# actually been restarted.
function pending_marker {
    echo "$UNIT_DEST_DIR/.$1.needs-restart"
}

# The names this script has installed here, so `--remove-retired` can never
# select a unit belonging to anything else. Absent on a host that has not run
# this version, which is what makes removal fail closed rather than guess.
function manifest_path {
    echo "$UNIT_DEST_DIR/.gardyn-installed-units"
}

# The checkout revision that was live the last time the service unit was
# actually restarted. Not "the last revision installed" - see the header.
function revision_path {
    echo "$UNIT_DEST_DIR/.gardyn-source-revision"
}

# Same atomic-rename reasoning as the manifest: a truncated write here reads as
# a revision that was never deployed.
function record_revision {
    if printf '%s\n' "$1" | sudo tee "$(revision_path).new" >/dev/null; then
        sudo mv -f "$(revision_path).new" "$(revision_path)" \
            || log_warn "could not replace the deployed revision at $(revision_path)"
    else
        sudo rm -f "$(revision_path).new"
        log_warn "could not record the deployed revision at $(revision_path)"
    fi
}

# The one unit that runs this repository's Python continuously, so it is the
# one a pull can leave stale. The oneshots are re-executed by their timers and
# pick up new code on their own.
CODE_UNIT="mqtt.service"

function report_and_exit {
    if [ ${#failures[@]} -gt 0 ]; then
        log_error "${#failures[@]} problem(s):"
        for f in "${failures[@]}"; do
            log_error "  - $f"
        done
        log_error "A unit may be installed on disk without the running service having picked it up. Re-run this script; the units left in that state are remembered and will be restarted."
        exit 1
    fi
    exit 0
}

# Portable, and deliberately not `readlink -f` / `realpath`: this script is
# exercised by the test suite on macOS, where those are not the GNU versions.
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
INSTALL_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd -P)

UNIT_SRC_DIR="${GARDYN_UNIT_SRC_DIR:-$INSTALL_DIR/services/etc/systemd/system}"
UNIT_DEST_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"

[ -d "$UNIT_SRC_DIR" ] || fail "unit source directory not found: $UNIT_SRC_DIR"
[ -d "$UNIT_DEST_DIR" ] || fail "systemd unit directory not found: $UNIT_DEST_DIR"

# --- discover the units ------------------------------------------------------
units=()
strays=()
shopt -s nullglob
for f in "$UNIT_SRC_DIR"/*.service "$UNIT_SRC_DIR"/*.timer \
         "$UNIT_SRC_DIR"/*.socket "$UNIT_SRC_DIR"/*.path \
         "$UNIT_SRC_DIR"/*.target "$UNIT_SRC_DIR"/*.mount \
         "$UNIT_SRC_DIR"/*.slice; do
    units+=("$(basename "$f")")
done
for f in "$UNIT_SRC_DIR"/*; do
    case "$f" in
        *.service|*.timer|*.socket|*.path|*.target|*.mount|*.slice) ;;
        # A drop-in directory is a real override mechanism, and this script has
        # no way to deploy one. Silently leaving it behind is the exact class of
        # bug this script was written to remove, so refuse instead.
        *.d) [ -d "$f" ] && fail "drop-in directory not supported by this installer: $(basename "$f")"
             strays+=("$(basename "$f")") ;;
        *) strays+=("$(basename "$f")") ;;
    esac
done
shopt -u nullglob

# An empty result must be loud. A glob that matches nothing exits 0, so a
# renamed or relocated source directory would otherwise produce a run that
# installs nothing and reports success - the deployment equivalent of a scan
# that cannot fail.
if [ ${#units[@]} -eq 0 ]; then
    fail "no unit files found in $UNIT_SRC_DIR - refusing to report success for a run that installed nothing"
fi

if [ ${#strays[@]} -gt 0 ]; then
    log_warn "not a systemd unit file, NOT installed: ${strays[*]}"
fi

# --- can this unit actually run on this host? --------------------------------
#
# Returns 0 when every absolute path named by an ExecStart line exists. The
# shipped units carry absolute paths for the deployment they were written for,
# so this is what distinguishes "installing onto the host these were written
# for" from "installing onto some other machine".
#
# IT FAILS CLOSED. Every way of finding nothing to check - a .service with no
# ExecStart at all, a unit file that cannot be read - refuses the unit rather
# than passing it, because "found no problem" and "could not look" produce the
# same empty result and only one of them is an all-clear.
#
# Known gaps, all in the direction of NOT warning: a backslash continuation
# line, and a relative executable resolved from $PATH.
function unit_can_run {
    local unit="$1" line token ok=0 seen=0 lines rc

    # A .timer has no ExecStart of its own - it arms the .service of the same
    # name. Judging it on its own contents would pass every timer, which is
    # exactly backwards for gardyn-netwatch.timer: the timer is what arms the
    # watchdog, and the unrunnable thing is the service behind it.
    case "$unit" in
        *.timer)
            if [ -f "$UNIT_SRC_DIR/${unit%.timer}.service" ]; then
                unit_can_run "${unit%.timer}.service" || return 1
            fi
            ;;
    esac

    # systemd.syntax(7): "Whitespace immediately before or after the '=' is
    # ignored", so the separator has to be matched loosely.
    lines=$(grep -E '^[[:space:]]*ExecStart[[:space:]]*=' "$UNIT_SRC_DIR/$unit")
    rc=$?
    if [ $rc -gt 1 ]; then
        log_warn "$unit: cannot read $UNIT_SRC_DIR/$unit to check its ExecStart"
        return 1
    fi

    # Word-split the ExecStart value but do NOT glob it: an unquoted expansion
    # would otherwise let a `*` in a unit file expand against the cwd.
    set -f
    while IFS= read -r line; do
        [ -n "$line" ] || continue
        seen=1
        for token in ${line#*=}; do
            # systemd allows a run of prefix characters before the path.
            while [ -n "$token" ]; do
                case "$token" in
                    [-+!@:]*) token="${token:1}" ;;
                    *) break ;;
                esac
            done
            token="${token#\"}"; token="${token%\"}"
            token="${token#\'}"; token="${token%\'}"
            case "$token" in
                /*) ;;
                *) continue ;;
            esac
            if [ ! -e "$token" ]; then
                log_warn "$unit: ExecStart path does not exist on this host: $token"
                ok=1
            fi
        done
    done <<EOF
$lines
EOF
    set +f

    # A .service with no ExecStart line is not something this script can judge,
    # so it does not get enabled. Other unit types legitimately have none.
    case "$unit" in
        *.service)
            if [ $seen -eq 0 ]; then
                log_warn "$unit: no ExecStart line found - cannot tell whether it can run here"
                ok=1
            fi
            ;;
    esac
    return $ok
}

# --- install -----------------------------------------------------------------
changed=()
installed=()
for u in "${units[@]}"; do
    src="$UNIT_SRC_DIR/$u"
    dest="$UNIT_DEST_DIR/$u"

    # A symlink here is almost always `systemctl mask` (a link to /dev/null),
    # and GNU install unlinks the destination before writing - so installing
    # over it silently unmasks the unit. Refuse and say so.
    if [ -L "$dest" ]; then
        record_failure "$u: destination is a symlink (masked?): $dest -> $(readlink "$dest")"
        continue
    fi
    if [ -d "$dest" ]; then
        # `install SOURCE DIRECTORY` is a valid second form and exits 0, copying
        # INTO the directory. The `|| fail` guard cannot see that.
        record_failure "$u: destination is a directory, not a unit file: $dest"
        continue
    fi

    if [ ! -e "$dest" ]; then
        # The ordinary first-install case. `cmp` reports this as rc 2, the same
        # code it uses for "could not read", so checking first keeps a routine
        # install from emitting an alarming warning.
        changed+=("$u")
    else
        cmp -s "$src" "$dest"
        case $? in
            0) if [ -e "$(pending_marker "$u")" ]; then
                   log_info "$u matches the deployed copy but a previous run did not restart it"
                   changed+=("$u")
               else
                   log_info "$u already matches the deployed copy"
               fi ;;
            1) changed+=("$u") ;;
            *) log_warn "$u: cannot compare with $dest - treating as changed"
               changed+=("$u") ;;
        esac
    fi

    # 0644 root:root, matching what is deployed. `install` under sudo already
    # produces root ownership, so -o/-g would be redundant - and would break
    # the test harness, which runs the real command unprivileged.
    if sudo install -m 0644 "$src" "$dest"; then
        installed+=("$u")
        if in_list "$u" ${changed[@]+"${changed[@]}"}; then
            sudo touch "$(pending_marker "$u")" \
                || log_warn "$u: could not record that it still needs a restart"
        fi
    else
        record_failure "failed to install $u to $dest"
    fi
done

if [ ${#installed[@]} -eq 0 ]; then
    record_failure "no unit was installed"
    report_and_exit
fi

reload_ok=1
sudo systemctl daemon-reload || {
    reload_ok=0
    record_failure "systemctl daemon-reload failed"
}

# --- enable ------------------------------------------------------------------
#
# Every enable happens before any start. Interleaving them meant a failure to
# start the first unit aborted before the later units were ever enabled - and
# the later units are the health sampler and the network watchdog, the two this
# script exists to stop losing. Enablement is what survives a reboot, so it is
# the half to secure first.
enableable=()
no_install=()
for u in ${installed[@]+"${installed[@]}"}; do
    grep -q '^\[Install\]' "$UNIT_SRC_DIR/$u"
    case $? in
        0) ;;
        1) log_info "$u has no [Install] section - installed, not enabled (its timer starts it)"
           no_install+=("$u")
           continue ;;
        *) record_failure "$u: could not read $UNIT_SRC_DIR/$u to look for an [Install] section"
           continue ;;
    esac

    if ! unit_can_run "$u"; then
        record_failure "$u: NOT enabled - it names a path that does not exist on this host. These units are written for one specific deployment; see the README before running this elsewhere."
        continue
    fi

    # A timer whose service was refused (masked destination, failed install) must
    # not be armed - it would fire against whatever is deployed instead.
    case "$u" in
        *.timer)
            sib="${u%.timer}.service"
            if [ -f "$UNIT_SRC_DIR/$sib" ] \
               && ! in_list "$sib" ${installed[@]+"${installed[@]}"}; then
                record_failure "$u: NOT enabled - $sib was not installed"
                continue
            fi
            ;;
    esac

    if sudo systemctl enable "$u"; then
        enableable+=("$u")
    else
        record_failure "systemctl enable $u failed"
    fi
done

# --- start / restart ---------------------------------------------------------
restarted=()
for u in ${enableable[@]+"${enableable[@]}"}; do
    if in_list "$u" ${changed[@]+"${changed[@]}"}; then
        # The unit file changed, so a plain `start` would leave the running
        # instance on the old definition.
        if sudo systemctl restart "$u"; then
            sudo rm -f "$(pending_marker "$u")"
            restarted+=("$u")
            log_pass "$u installed (changed), enabled and restarted"
        else
            record_failure "systemctl restart $u failed"
        fi
    else
        if sudo systemctl start "$u"; then
            log_pass "$u enabled and running (unit file unchanged, not restarted)"
        else
            record_failure "systemctl start $u failed"
        fi
    fi
done

# --- clear the pending marker for units that are never restarted -------------
#
# A unit with no [Install] section is skipped by both loops above, so it never
# reaches the `rm -f` that clears its marker. Left alone the marker is
# PERMANENT: every later run sees it, calls the unit changed, and the
# "none changed" summary becomes unreachable - the file is deployed and correct
# while the run reports otherwise, which is the same class of lie the marker was
# added to remove. For these units the reload IS the pickup: they do not run
# continuously, and systemd starts the new definition the next time their timer
# fires. So the marker is cleared once daemon-reload has succeeded, and only
# then - a failed reload means systemd is still holding the old definition.
for u in ${no_install[@]+"${no_install[@]}"}; do
    in_list "$u" ${changed[@]+"${changed[@]}"} || continue
    if [ "$reload_ok" -eq 1 ]; then
        sudo rm -f "$(pending_marker "$u")"
        log_pass "$u installed (changed); its timer runs the new definition"
    else
        log_warn "$u installed (changed) but daemon-reload failed - still pending"
    fi
done

# --- units the repository no longer ships ------------------------------------
#
# Nothing above can notice a unit DELETED from the source directory: the loops
# are driven by what is there now, so a deployed unit simply stops being
# mentioned and stays enabled forever. That is the deployment twin of a retained
# MQTT message outliving the code that published it, and the unit it is most
# likely to happen to is the watchdog that can reboot the host.
retired=()
prev_manifest=()
if [ -f "$(manifest_path)" ]; then
    # `|| [ -n "$line" ]` so a file with no trailing newline does not silently
    # drop its LAST entry - which would un-claim a unit permanently, and in the
    # reassuring direction: nothing is deleted, the warning simply stops.
    while IFS= read -r line || [ -n "$line" ]; do
        [ -n "$line" ] || continue
        prev_manifest+=("$line")
        in_list "$line" "${units[@]}" || retired+=("$line")
    done < "$(manifest_path)"
fi

removed_any=0
for u in ${retired[@]+"${retired[@]}"}; do
    dest="$UNIT_DEST_DIR/$u"
    if [ ! -e "$dest" ]; then
        log_info "$u is no longer shipped and is already gone from $UNIT_DEST_DIR"
        continue
    fi
    if [ "$REMOVE_RETIRED" -ne 1 ]; then
        log_warn "$u is deployed but the repository no longer ships it - still enabled and running. Re-run with --remove-retired to disable and delete it."
        continue
    fi
    # Removal is the one irreversible thing here, so it does not happen on a run
    # that has already gone wrong. A failed daemon-reload means systemd is still
    # holding definitions this run could not refresh, and an earlier failure
    # means the operator has something to fix first - `disable --now` against
    # either state is acting on a picture that is known to be out of date.
    if [ "$reload_ok" -ne 1 ] || [ ${#failures[@]} -gt 0 ]; then
        log_warn "$u is no longer shipped, but removal is deferred: this run already failed. Fix the failures above and re-run with --remove-retired."
        continue
    fi
    # Fail closed on anything that is not the plain file this script wrote. A
    # symlink here is `systemctl mask`, and a directory is not ours to delete.
    if [ -L "$dest" ] || [ ! -f "$dest" ]; then
        record_failure "$u: refusing to remove $dest - not a plain file"
        continue
    fi
    if sudo systemctl disable --now "$u"; then
        if sudo rm -f "$dest"; then
            sudo rm -f "$(pending_marker "$u")"
            removed_any=1
            log_pass "$u removed - the repository no longer ships it"
        else
            record_failure "failed to remove $dest"
        fi
    else
        record_failure "systemctl disable --now $u failed - $dest left in place"
    fi
done

# systemd is still holding a definition whose file has just been deleted.
if [ "$removed_any" -eq 1 ]; then
    sudo systemctl daemon-reload \
        || record_failure "systemctl daemon-reload after removal failed"
fi

# Claim ownership of what this run actually INSTALLED - not of what the source
# directory happens to hold. A unit whose `install` failed is still in `units`,
# and claiming it would let a later --remove-retired run disable and delete a
# file this script provably never wrote. That is the one guarantee the whole
# removal path rests on.
#
# Retired units that are still deployed keep being claimed, so their warning
# repeats until they are dealt with rather than appearing exactly once.
manifest=()
for u in ${installed[@]+"${installed[@]}"}; do
    manifest+=("$u")
done
for u in ${prev_manifest[@]+"${prev_manifest[@]}"}; do
    # A previously-owned unit that is still shipped but failed to install this
    # run: keep the claim rather than dropping it over a transient error.
    in_list "$u" ${manifest[@]+"${manifest[@]}"} && continue
    if in_list "$u" "${units[@]}" || [ -e "$UNIT_DEST_DIR/$u" ]; then
        manifest+=("$u")
    fi
done
# Written through a temporary file: a `tee` truncated by ENOSPC or a kill leaves
# a SHORT manifest, and a short manifest silently un-claims whatever fell off
# the end. Rename is atomic on the same filesystem, so a reader sees the old
# file or the new one and never a half-written one.
if printf '%s\n' ${manifest[@]+"${manifest[@]}"} \
       | sudo tee "$(manifest_path).new" >/dev/null; then
    sudo mv -f "$(manifest_path).new" "$(manifest_path)" \
        || log_warn "could not replace the installed-unit manifest at $(manifest_path)"
else
    sudo rm -f "$(manifest_path).new"
    log_warn "could not record the installed-unit manifest at $(manifest_path)"
fi

# --- did the CODE move without any unit file moving? -------------------------
#
# `git pull && ./bin/install-systemd-units.sh` is the deploy, and a pull that
# touches only Python changes no unit file - so every check above passes, every
# line says PASS, and mqtt.service goes on running the previous revision. The
# recorded revision is written only when that service is actually restarted, so
# a run that merely warned leaves the warning standing for the next one.
#
# Advisory, and deliberately fail-open: a checkout with no git available is not
# a reason to refuse a deploy, so the check is skipped and says so.
if in_list "$CODE_UNIT" "${units[@]}"; then
    # --show-toplevel first: `git rev-parse` walks UP, so a checkout that is not
    # this directory - a sandbox that happens to sit inside some other repo -
    # would otherwise hand back a revision that has nothing to do with the code
    # being deployed, and the advisory would fire on every run forever.
    current_rev=""
    if [ "$(git -C "$INSTALL_DIR" rev-parse --show-toplevel 2>/dev/null)" \
         = "$INSTALL_DIR" ]; then
        current_rev=$(git -C "$INSTALL_DIR" rev-parse HEAD 2>/dev/null)
    fi
    recorded_rev=""
    [ -f "$(revision_path)" ] && recorded_rev=$(cat "$(revision_path)" 2>/dev/null)

    if [ -z "$current_rev" ]; then
        log_info "not a git checkout (or git unavailable) - cannot tell whether the code moved since $CODE_UNIT was last restarted"
    elif in_list "$CODE_UNIT" ${restarted[@]+"${restarted[@]}"}; then
        record_revision "$current_rev"
    elif [ "$recorded_rev" = "$current_rev" ]; then
        : # the service was restarted at this revision and nothing has moved
    elif [ "$RESTART_ON_CODE_CHANGE" -eq 1 ]; then
        # Checked BEFORE the no-revision case, so the flag can also seed a host
        # that has never recorded one. Ordered the other way the empty-revision
        # branch swallows every run, the restart is unreachable, and the whole
        # check stays dormant on exactly the machine it was written for.
        if sudo systemctl restart "$CODE_UNIT"; then
            record_revision "$current_rev"
            log_pass "$CODE_UNIT restarted - it is now running $current_rev"
        else
            record_failure "systemctl restart $CODE_UNIT failed after a code change"
        fi
    elif [ -z "$recorded_rev" ]; then
        # First run of a version of this script that records anything. Nothing
        # on the host says what the running service was built from, and refusing
        # the deploy over that would fail every upgrade. Take the current
        # revision as the baseline and SAY SO - the alternative is what the
        # review found: no revision is ever recorded, because one is only
        # written when a unit FILE changes, which on a settled host is never.
        record_revision "$current_rev"
        log_warn "no revision was recorded for $CODE_UNIT before this run; taking $current_rev as the baseline. If the service is actually running older code, restart it once with --restart-on-code-change."
    else
        code_stale=1
    fi
fi

# --- report ------------------------------------------------------------------
if [ ${#failures[@]} -gt 0 ]; then
    report_and_exit
fi

# Not a `failures` entry, because that list's advice is "re-run this script" and
# re-running will not fix this one. The units are all correct; the SERVICE is
# behind the checkout. Reporting success here is the whole defect - a deploy
# that changed nothing printing a column of PASS lines.
if [ "${code_stale:-0}" -eq 1 ]; then
    log_error "$CODE_UNIT is still running $recorded_rev; the checkout is at $current_rev."
    log_error "No unit file changed, so nothing was restarted and this deploy has NOT taken effect."
    # Deliberately NOT "or restart it yourself". A restart issued out of band is
    # invisible here - the revision is recorded only by the run that performs
    # the restart - so that advice would leave the deploy permanently red while
    # the service was in fact current. Re-running with the flag restarts and
    # records in one step; on an already-current service the extra bounce is
    # seconds, which is cheaper than a check nobody can clear.
    log_error "Re-run with --restart-on-code-change; a restart done by hand is not visible to this script and will not clear this."
    exit 1
fi

if [ ${#changed[@]} -eq 0 ]; then
    log_pass "${#units[@]} units installed; none changed"
else
    log_pass "${#units[@]} units installed; changed: ${changed[*]}"
fi
report_and_exit
