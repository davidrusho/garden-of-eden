#!/bin/bash
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
# hand-maintained list is how (3) happened: a new *.service or *.timer dropped
# into the source directory is deployed by the next run with no edit here.
# What gets ENABLED is derived from the presence of an `[Install]` section, so
# the two Type=oneshot units driven by timers are installed but never enabled
# directly - `systemctl enable` fails on a unit with no [Install].
#
# Safe to re-run. A unit whose deployed copy already matches is re-installed
# (cheap, idempotent) but NOT restarted, so routine setup runs do not bounce
# the grow-light controller.
#
# Environment seams, both defaulted for real use and overridden only by tests:
#   SYSTEMD_UNIT_DIR      where units are installed  (default /etc/systemd/system)
#   GARDYN_UNIT_SRC_DIR   where they are read from   (default <repo>/services/...)

# No `set -u`: macOS ships bash 3.2, where `${#arr[@]}` on an empty array is an
# unbound-variable error. Failures are handled explicitly instead.
set -o pipefail

GRN="\e[32m"
RED="\e[31m"
YLW="\e[33m"
GRY="\e[90m"
LGY="\e[37m"
RST="\e[0m"

function log_error { echo -e "[${RED}ERROR${RST}]: $*" >&2; }
function log_warn  { echo -e "[${YLW}WARN${RST}]: $*" >&2; }
function log_pass  { echo -e "[${GRN}PASS${RST}]: $*"; }
function log_info  { echo -e "[${GRY}INFO${RST}]: ${LGY}$*${RST}" >&2; }

function fail {
    log_error "$*"
    exit 1
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
for f in "$UNIT_SRC_DIR"/*.service "$UNIT_SRC_DIR"/*.timer; do
    units+=("$(basename "$f")")
done
for f in "$UNIT_SRC_DIR"/*; do
    case "$f" in
        *.service|*.timer) ;;
        *) strays+=("$(basename "$f")") ;;
    esac
done
shopt -u nullglob

# An empty result must be loud. A glob that matches nothing exits 0, so a
# renamed or relocated source directory would otherwise produce a run that
# installs nothing and reports success - the deployment equivalent of a scan
# that cannot fail.
if [ ${#units[@]} -eq 0 ]; then
    fail "no *.service or *.timer files found in $UNIT_SRC_DIR - refusing to report success for a run that installed nothing"
fi

if [ ${#strays[@]} -gt 0 ]; then
    log_warn "not a unit file, NOT installed: ${strays[*]}"
fi

# --- preflight: do the units point at paths that exist on this host? ---------
#
# The shipped units carry absolute paths for the deployment they were written
# for. A checkout under a different user or directory installs cleanly and then
# fails at start time with nothing in the setup output to explain it, so name
# the mismatch here. A warning, not an error: the operator may be installing
# units for a path that is about to exist.
function warn_on_missing_exec_paths {
    local unit="$1"
    local line token
    # Word-split the ExecStart value but do NOT glob it: an unquoted expansion
    # would otherwise let a `*` in a unit file expand against the cwd.
    set -f
    while IFS= read -r line; do
        for token in ${line#ExecStart=}; do
            case "$token" in
                -/*|+/*|@/*|!/*) token="${token:1}" ;;
                /*) ;;
                *) continue ;;
            esac
            [ -e "$token" ] || log_warn "$unit: ExecStart path does not exist on this host: $token"
        done
    done < <(grep '^ExecStart=' "$UNIT_SRC_DIR/$unit")
    set +f
}

for u in "${units[@]}"; do
    warn_on_missing_exec_paths "$u"
done

# --- install -----------------------------------------------------------------
changed=()
for u in "${units[@]}"; do
    src="$UNIT_SRC_DIR/$u"
    dest="$UNIT_DEST_DIR/$u"

    if cmp -s "$src" "$dest"; then
        log_info "$u already matches the deployed copy"
    else
        changed+=("$u")
    fi

    # 0644 root:root, matching what is deployed. `install` under sudo already
    # produces root ownership, so -o/-g would be redundant - and would break
    # the test harness, which runs the real command unprivileged.
    sudo install -m 0644 "$src" "$dest" || fail "failed to install $u to $dest"
done

sudo systemctl daemon-reload || fail "systemctl daemon-reload failed"

function is_changed {
    local needle="$1" c
    for c in ${changed[@]+"${changed[@]}"}; do
        [ "$c" = "$needle" ] && return 0
    done
    return 1
}

# --- enable and start --------------------------------------------------------
for u in "${units[@]}"; do
    if ! grep -q '^\[Install\]' "$UNIT_SRC_DIR/$u"; then
        log_info "$u has no [Install] section - installed, not enabled (its timer starts it)"
        continue
    fi

    sudo systemctl enable "$u" || fail "systemctl enable $u failed"

    if is_changed "$u"; then
        # The unit file changed, so a plain `start` would leave the running
        # instance on the old definition.
        sudo systemctl restart "$u" || fail "systemctl restart $u failed"
        log_pass "$u installed (changed), enabled and restarted"
    else
        sudo systemctl start "$u" || fail "systemctl start $u failed"
        log_pass "$u enabled and running (unit file unchanged, not restarted)"
    fi
done

if [ ${#changed[@]} -eq 0 ]; then
    log_pass "${#units[@]} units installed; none changed"
else
    log_pass "${#units[@]} units installed; changed: ${changed[*]}"
fi
