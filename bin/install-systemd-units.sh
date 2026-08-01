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

# A unit is installed before it is activated, so a run that fails in between
# leaves the new file on disk with the old definition still loaded - and the
# NEXT run sees the file as unchanged and issues `start` rather than `restart`,
# reporting success over a stale service. The marker carries that pending state
# across runs: written once the file is in place, removed only once the unit has
# actually been restarted.
function pending_marker {
    echo "$UNIT_DEST_DIR/.$1.needs-restart"
}

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

sudo systemctl daemon-reload || record_failure "systemctl daemon-reload failed"

# --- enable ------------------------------------------------------------------
#
# Every enable happens before any start. Interleaving them meant a failure to
# start the first unit aborted before the later units were ever enabled - and
# the later units are the health sampler and the network watchdog, the two this
# script exists to stop losing. Enablement is what survives a reboot, so it is
# the half to secure first.
enableable=()
for u in ${installed[@]+"${installed[@]}"}; do
    grep -q '^\[Install\]' "$UNIT_SRC_DIR/$u"
    case $? in
        0) ;;
        1) log_info "$u has no [Install] section - installed, not enabled (its timer starts it)"
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
for u in ${enableable[@]+"${enableable[@]}"}; do
    if in_list "$u" ${changed[@]+"${changed[@]}"}; then
        # The unit file changed, so a plain `start` would leave the running
        # instance on the old definition.
        if sudo systemctl restart "$u"; then
            sudo rm -f "$(pending_marker "$u")"
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

# --- report ------------------------------------------------------------------
if [ ${#failures[@]} -gt 0 ]; then
    report_and_exit
fi

if [ ${#changed[@]} -eq 0 ]; then
    log_pass "${#units[@]} units installed; none changed"
else
    log_pass "${#units[@]} units installed; changed: ${changed[*]}"
fi
report_and_exit
