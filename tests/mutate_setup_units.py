#!/usr/bin/env python3
"""Mutation battery for test_setup_units.py.

The suite it scores asserts that setup.sh no longer generates a unit file and
that all five shipped units are installed. Both are assertions about an
ABSENCE - no heredoc, no missing unit - which is exactly the shape where a dead
test and a real pass produce identical output. The suite is worthless until it
has been shown capable of going red.

Run:  python3 tests/mutate_setup_units.py

Two controls gate every result, and BOTH must hold before any mutant verdict is
read. A battery scores a mutant by whether the test run FAILED, so a broken
scorer reports every mutant caught - the most reassuring output available:

  CONTROL A  clean tree                 -> must be GREEN
  CONTROL B  deliberately broken code   -> must be RED
             (A alone is worthless; it is scored by the same path that may be
             broken, so only B proves the scorer can distinguish the two.)

A battery is evidence only for the code it MUTATES, and the actions worth
covering are the ones that DESTROY or OVERWRITE something. All three of this
change's have at least one mutant:

  * the `cat >` over a git-tracked unit file   -> mutant [s1], which puts it back
  * the copy into /etc/systemd/system          -> mutants [i1] [i3]
  * the service restart                        -> mutants [i4] [i5]

Mechanics that have bitten this repo before, all handled here:
  * __pycache__ purged before every run, and PYTHONDONTWRITEBYTECODE=1 set.
  * stderr merged into stdout - unittest reports there.
  * every mutation is proved to have landed; a replacement matching nothing is
    indistinguishable from one that changed nothing.
  * the tree is asserted byte-identical at the end, including the file the
    deletion mutant removes.
"""
# Reviewed: 2026-08-02 against 27a8165 (T-494) — read end to end; every anchor
# re-verified to match exactly once against the merged installer.
# Reviewed: 2026-08-01 against 3e8374c and 92dd3fd (T-477)
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(REPO, "bin", "install-systemd-units.sh")
SETUP = os.path.join(REPO, "bin", "setup.sh")
MQTT_UNIT = os.path.join(REPO, "services", "etc", "systemd", "system",
                         "mqtt.service")
NETWATCH_TIMER = os.path.join(REPO, "services", "etc", "systemd", "system",
                              "gardyn-netwatch.timer")
SUITE = "tests.test_setup_units"

# The heredoc exactly as setup.sh carried it before this change - a mutant that
# REINTRODUCES deleted code, not merely one that breaks present code. A suite
# that only tolerates an absence will not notice the absent thing coming back.
OLD_GENERATOR = '''function install_systemd_units {
    local service_file="$INSTALL_DIR/services/etc/systemd/system/mqtt.service"

    cat > $service_file <<EOF
[Unit]
Description=MQTT Service
Requires=pigpiod.service
After=network.target pigpiod.service

[Service]
User=$USER
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/venv/bin/python $INSTALL_DIR/mqtt.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

    sudo cp $service_file /etc/systemd/system/
    sudo systemctl daemon-reload
    sudo systemctl enable mqtt.service
    sudo systemctl start mqtt.service
'''

# (tag, label, path, old, new)
MUTANTS = [
    ("i1", "make the install a no-op - no unit ever reaches systemd",
     INSTALLER,
     '    if sudo install -m 0644 "$src" "$dest"; then',
     '    if true; then'),

    ("i2", "swallow an install failure instead of recording it",
     INSTALLER,
     '        record_failure "failed to install $u to $dest"',
     '        :'),

    ("i3", "drop the mode, letting units land at whatever umask gives",
     INSTALLER,
     'sudo install -m 0644 "$src" "$dest"',
     'sudo install "$src" "$dest"'),

    ("i4", "restart on every run, bouncing the controller during a no-op setup",
     INSTALLER,
     # Anchored on the newline: the marker branch in the install loop is the
     # same text at deeper indentation, so a bare match hits twice.
     '\n    if in_list "$u" ${changed[@]+"${changed[@]}"}; then',
     '\n    if true; then'),

    ("i5", "never restart, so a changed unit file is installed but not applied",
     INSTALLER,
     '        if sudo systemctl restart "$u"; then',
     '        if sudo systemctl start "$u"; then'),

    ("i6", "drop the daemon-reload",
     INSTALLER,
     'sudo systemctl daemon-reload || {\n    reload_ok=0',
     'true || {\n    reload_ok=0'),

    ("i7", "enable everything, including the two units with no [Install]",
     INSTALLER,
     "    grep -q '^\\[Install\\]' \"$UNIT_SRC_DIR/$u\"\n    case $? in\n        0) ;;",
     "    grep -q '^\\[Install\\]' \"$UNIT_SRC_DIR/$u\"\n    case 0 in\n        0) ;;"),

    ("i8", "remove the empty-directory guard (the clean-zero failure)",
     INSTALLER,
     'if [ ${#units[@]} -eq 0 ]; then',
     'if false; then'),

    ("i9", "drop the unit-source-directory check",
     INSTALLER,
     '[ -d "$UNIT_SRC_DIR" ] || fail "unit source directory not found: $UNIT_SRC_DIR"',
     ':'),

    ("i10", "drop the destination-directory check",
     INSTALLER,
     '[ -d "$UNIT_DEST_DIR" ] || fail "systemd unit directory not found: $UNIT_DEST_DIR"',
     ':'),

    ("i11", "swallow an enable failure",
     INSTALLER,
     '        record_failure "systemctl enable $u failed"',
     '        :'),

    ("i12", "install everything in the directory, unit file or not",
     INSTALLER,
     '         "$UNIT_SRC_DIR"/*.slice; do',
     '         "$UNIT_SRC_DIR"/*; do'),

    ("i24", "narrow the glob back, silently dropping other unit types",
     INSTALLER,
     '         "$UNIT_SRC_DIR"/*.socket "$UNIT_SRC_DIR"/*.path \\\n',
     ''),

    ("i25", "let the ExecStart split glob against the cwd",
     INSTALLER,
     '    set -f\n    while IFS= read -r line; do',
     '    while IFS= read -r line; do'),

    ("i13", "drop the ExecStart-path check inside the can-this-run gate",
     INSTALLER,
     '            if [ ! -e "$token" ]; then',
     '            if false; then'),

    ("i14", "drop the mask (symlink destination) guard",
     INSTALLER,
     '    if [ -L "$dest" ]; then',
     '    if false; then'),

    ("i15", "drop the directory-destination guard",
     INSTALLER,
     '    if [ -d "$dest" ]; then',
     '    if false; then'),

    ("i16", "drop the can-this-unit-run gate, arming a watchdog that cannot run",
     INSTALLER,
     '    if ! unit_can_run "$u"; then',
     '    if false; then'),

    ("i17", "judge a .timer on itself, so every timer passes the gate",
     INSTALLER,
     '                unit_can_run "${unit%.timer}.service" || return 1',
     '                :'),

    ("i18", "abort on the first enable failure, stranding the later units",
     INSTALLER,
     '        record_failure "systemctl enable $u failed"',
     '        fail "systemctl enable $u failed"'),

    ("i19", "abort on the first restart failure, stranding the later units",
     INSTALLER,
     '            record_failure "systemctl restart $u failed"',
     '            fail "systemctl restart $u failed"'),

    ("i20", "drop the drop-in directory refusal",
     INSTALLER,
     '        *.d) [ -d "$f" ] && fail "drop-in directory not supported by this installer: $(basename "$f")"',
     '        *.d) [ -d "$f" ] && :'),

    ("i21", "collect the failures and then exit 0 anyway",
     INSTALLER,
     '    if [ ${#failures[@]} -gt 0 ]; then\n        log_error "${#failures[@]} problem(s):"',
     '    if false; then\n        log_error "${#failures[@]} problem(s):"'),

    ("i22", "drop the nothing-was-installed guard",
     INSTALLER,
     'if [ ${#installed[@]} -eq 0 ]; then',
     'if false; then'),

    ("i23", "report an ordinary first install as an unreadable destination",
     INSTALLER,
     '    if [ ! -e "$dest" ]; then\n        # The ordinary first-install case.',
     '    if false; then\n        # The ordinary first-install case.'),

    ("i26", "drop the pending-restart marker, losing an interrupted run's state",
     INSTALLER,
     '            sudo touch "$(pending_marker "$u")" \\\n'
     '                || log_warn "$u: could not record that it still needs a restart"',
     '            :'),

    ("i27", "clear the pending marker before the restart has happened",
     INSTALLER,
     '        if sudo systemctl restart "$u"; then\n'
     '            sudo rm -f "$(pending_marker "$u")"',
     '        sudo rm -f "$(pending_marker "$u")"\n'
     '        if sudo systemctl restart "$u"; then\n'
     '            :'),

    ("i28", "ignore the pending marker when deciding whether to restart",
     INSTALLER,
     '            0) if [ -e "$(pending_marker "$u")" ]; then',
     '            0) if false; then'),

    ("i29", "let a .service with no ExecStart pass the gate (fail open)",
     INSTALLER,
     '            if [ $seen -eq 0 ]; then',
     '            if false; then'),

    ("i30", "treat an unreadable unit file as an all-clear",
     INSTALLER,
     '    if [ $rc -gt 1 ]; then',
     '    if false; then'),

    ("i31", "require ExecStart= with no whitespace, missing a legal unit",
     INSTALLER,
     "    lines=$(grep -E '^[[:space:]]*ExecStart[[:space:]]*=' \"$UNIT_SRC_DIR/$unit\")",
     "    lines=$(grep -E '^ExecStart=' \"$UNIT_SRC_DIR/$unit\")"),

    ("i32", "stop stripping quotes, so a quoted path is never checked",
     INSTALLER,
     '            token="${token#\\"}"; token="${token%\\"}"',
     '            :'),

    ("i33", "arm a timer whose service was refused",
     INSTALLER,
     '               && ! in_list "$sib" ${installed[@]+"${installed[@]}"}; then',
     '               && false; then'),

    ("i34", "print a PASS line on a run that had failures",
     INSTALLER,
     'if [ ${#failures[@]} -gt 0 ] || [ "${code_stale:-0}" -eq 1 ]; then\n'
     '    report_and_exit\nfi',
     ':'),

    ("i35", "use \\\\e escapes bash 3.2 cannot expand",
     INSTALLER,
     'GRN="\\033[32m"',
     'GRN="\\e[32m"'),

    ("s1", "REINTRODUCE the heredoc that overwrites the tracked unit file",
     SETUP,
     'function install_systemd_units {\n    if ! "$BIN_DIR/install-systemd-units.sh" "$@"; then\n        log_error "systemd unit installation failed."\n        return 1\n',
     OLD_GENERATOR),

    ("s2", "stop calling the installer from main",
     SETUP,
     '\ninstall_systemd_units "$@" || exit 1\n',
     '\n#install_systemd_units "$@" || exit 1\n'),

    ("s3", "let setup.sh swallow the installer's failure",
     SETUP,
     'install_systemd_units "$@" || exit 1',
     'install_systemd_units "$@"'),

    ("s4", "return success from a failed unit install",
     SETUP,
     '        log_error "systemd unit installation failed."\n        return 1',
     '        log_error "systemd unit installation failed."\n        return 0'),

    ("m1", "drop StartLimitIntervalSec=0 (half the T-471 boot fix)",
     MQTT_UNIT,
     'StartLimitIntervalSec=0\n',
     ''),

    ("m2", "drop RestartSec=10 (the other half)",
     MQTT_UNIT,
     'Restart=always\nRestartSec=10\n',
     'Restart=always\n'),

    ("m3", "downgrade network-online.target back to network.target",
     MQTT_UNIT,
     'Wants=network-online.target\nAfter=network-online.target pigpiod.service',
     'After=network.target pigpiod.service'),

    # --- the production default (T-491) -------------------------------------
    #
    # Covered by nothing before this: every case overrides SYSTEMD_UNIT_DIR, so
    # the suite stayed 48/48 green with the default pointed at /tmp.
    ("p1", "point the production default at the wrong directory",
     INSTALLER,
     'UNIT_DEST_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"',
     'UNIT_DEST_DIR="${SYSTEMD_UNIT_DIR:-/tmp/WRONG-UNIT-DIR}"'),

    ("p2", "shift the production default by one character",
     INSTALLER,
     'UNIT_DEST_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system}"',
     'UNIT_DEST_DIR="${SYSTEMD_UNIT_DIR:-/etc/systemd/system-old}"'),

    ("p3", "point the source default at the wrong directory",
     INSTALLER,
     'UNIT_SRC_DIR="${GARDYN_UNIT_SRC_DIR:-$INSTALL_DIR/services/etc/systemd/system}"',
     'UNIT_SRC_DIR="${GARDYN_UNIT_SRC_DIR:-$INSTALL_DIR/services/systemd}"'),

    # --- the pending marker for units that are never restarted (T-491) ------
    ("k1", "leak the marker again for the two units with no [Install]",
     INSTALLER,
     '    in_list "$u" ${changed[@]+"${changed[@]}"} || continue\n'
     '    if [ "$reload_ok" -eq 1 ]; then',
     '    in_list "$u" ${changed[@]+"${changed[@]}"} || continue\n'
     '    if false; then'),

    ("k2", "clear the marker even when daemon-reload failed",
     INSTALLER,
     '    if [ "$reload_ok" -eq 1 ]; then\n        sudo rm -f "$(pending_marker "$u")"',
     '    if true; then\n        sudo rm -f "$(pending_marker "$u")"'),

    ("k3", "start the oneshots while clearing their marker",
     INSTALLER,
     '        sudo rm -f "$(pending_marker "$u")"\n'
     '        log_pass "$u installed (changed); its timer runs the new definition"',
     '        sudo rm -f "$(pending_marker "$u")"\n'
     '        sudo systemctl start "$u"\n'
     '        log_pass "$u installed (changed); its timer runs the new definition"'),

    # --- retired-unit removal (T-491) ---------------------------------------
    #
    # DESTRUCTIVE. r1 and r2 are the two ways this deletes a file that should
    # have been left alone, and they are the mutants that matter most in this
    # list: the guard they remove is the only thing between --remove-retired
    # and a unit belonging to some other package.
    ("r1", "remove without consulting the manifest - delete anything the "
           "source directory no longer holds",
     INSTALLER,
     '        prev_manifest+=("$line")\n'
     '        in_list "$line" "${units[@]}" || retired+=("$line")',
     '        prev_manifest+=("$line")\n'
     '        retired+=("$line")'),

    ("r2", "drop the not-a-plain-file guard, so a masked unit is deleted",
     INSTALLER,
     '    if [ -L "$dest" ] || [ ! -f "$dest" ]; then',
     '    if false; then'),

    ("r3", "remove by default, with no flag asked for",
     INSTALLER,
     '    if [ "$REMOVE_RETIRED" -ne 1 ]; then',
     '    if false; then'),

    ("r4", "delete the file even when disable --now failed",
     INSTALLER,
     '    if sudo systemctl disable --now "$u"; then\n        if sudo rm -f "$dest"; then',
     '    if true; then\n        if sudo rm -f "$dest"; then'),

    ("r5", "stop claiming a retired unit, so its warning appears exactly once",
     INSTALLER,
     '    if in_list "$u" "${units[@]}" || [ -e "$UNIT_DEST_DIR/$u" ]; then\n'
     '        manifest+=("$u")\n'
     '    fi\n',
     '    :\n'),

    ("r6", "claim what the repo SHIPS rather than what was installed, so a "
           "unit whose install failed becomes removable",
     INSTALLER,
     'manifest=()\nfor u in ${installed[@]+"${installed[@]}"}; do',
     'manifest=()\nfor u in "${units[@]}"; do'),

    ("r8", "remove even when the run has already failed",
     INSTALLER,
     '    if [ "$reload_ok" -ne 1 ] || [ ${#failures[@]} -gt 0 ]; then\n'
     '        log_warn "$u is no longer shipped, but removal is deferred',
     '    if false; then\n'
     '        log_warn "$u is no longer shipped, but removal is deferred'),

    ("r9", "skip the daemon-reload after deleting a unit file",
     INSTALLER,
     'if [ "$removed_any" -eq 1 ]; then\n    sudo systemctl daemon-reload',
     'if false; then\n    sudo systemctl daemon-reload'),

    ("r10", "drop the last manifest entry when the file has no trailing newline",
     INSTALLER,
     'while IFS= read -r line || [ -n "$line" ]; do',
     'while IFS= read -r line; do'),

    ("r11", "write the manifest in place, so a truncated write is what readers see",
     INSTALLER,
     'if printf \'%s\\n\' ${manifest[@]+"${manifest[@]}"} \\\n'
     '       | sudo tee "$(manifest_path).new" >/dev/null; then\n'
     '    sudo mv -f "$(manifest_path).new" "$(manifest_path)" \\\n',
     'if printf \'%s\\n\' ${manifest[@]+"${manifest[@]}"} \\\n'
     '       | sudo tee "$(manifest_path)" >/dev/null; then\n'
     '    true \\\n'),

    ("r7", "never write the manifest, so removal can never become possible",
     INSTALLER,
     'if printf \'%s\\n\' ${manifest[@]+"${manifest[@]}"} \\\n'
     '       | sudo tee "$(manifest_path).new" >/dev/null; then',
     'if true; then'),

    # --- the code-moved advisory (T-491) ------------------------------------
    #
    # DESTRUCTIVE at c3: that one restarts the live controller.
    ("c1", "report success over a service still running the old revision",
     INSTALLER,
     '    else\n        code_stale=1\n    fi',
     '    else\n        :\n    fi'),

    ("c2", "advance the recorded revision on a run that only warned",
     INSTALLER,
     '    else\n        code_stale=1\n    fi',
     '    else\n        record_revision "$current_rev"\n        code_stale=1\n    fi'),

    ("c3", "restart the controller on a code change with no flag asked for",
     INSTALLER,
     '    elif [ "$RESTART_ON_CODE_CHANGE" -eq 1 ]; then',
     '    elif true; then'),

    ("c7", "test the empty-revision case BEFORE the flag, so the flag can "
           "never seed a host that has no revision recorded",
     INSTALLER,
     '    elif [ "$RESTART_ON_CODE_CHANGE" -eq 1 ]; then\n'
     '        # Checked BEFORE the no-revision case',
     '    elif [ -z "$recorded_rev" ]; then\n'
     '        record_revision "$current_rev"\n'
     '    elif [ "$RESTART_ON_CODE_CHANGE" -eq 1 ]; then\n'
     '        # Checked BEFORE the no-revision case'),

    ("c8", "leave a host with no recorded revision dormant forever, instead "
           "of seeding a baseline on the first run",
     INSTALLER,
     '        record_revision "$current_rev"\n'
     '        log_warn "no revision was recorded for $CODE_UNIT before this run;',
     '        log_warn "no revision was recorded for $CODE_UNIT before this run;'),

    ("c9", "seed the baseline silently, so an operator cannot tell that the "
           "running code was never confirmed",
     INSTALLER,
     '        log_warn "no revision was recorded for $CODE_UNIT before this run;',
     '        : "no revision was recorded for $CODE_UNIT before this run;'),

    ("c10", "prescribe a manual restart, which this script cannot observe and "
            "which therefore leaves the deploy permanently red",
     INSTALLER,
     '    log_error "Re-run with --restart-on-code-change; a restart done by hand '
     'is not visible to this script and will not clear this."',
     '    log_error "Run \'sudo systemctl restart $CODE_UNIT\'."'),

    ("c11", "write the revision in place rather than through a rename",
     INSTALLER,
     '    if printf \'%s\\n\' "$1" | sudo tee "$(revision_path).new" >/dev/null; then\n'
     '        sudo mv -f "$(revision_path).new" "$(revision_path)" \\\n',
     '    if printf \'%s\\n\' "$1" | sudo tee "$(revision_path)" >/dev/null; then\n'
     '        true \\\n'),

    ("c4", "record the revision on install rather than on restart, so the "
           "warning is silenced by the run that should have raised it",
     INSTALLER,
     '    elif in_list "$CODE_UNIT" ${restarted[@]+"${restarted[@]}"}; then',
     '    elif true; then'),

    ("c5", "read an enclosing repository's HEAD",
     INSTALLER,
     '    if [ "$(git -C "$INSTALL_DIR" rev-parse --show-toplevel 2>/dev/null)" \\\n'
     '         = "$INSTALL_DIR" ]; then',
     '    if true; then'),

    ("c6", "skip the check silently instead of saying it skipped",
     INSTALLER,
     '        log_info "not a git checkout (or git unavailable) - cannot tell '
     'whether the code moved since $CODE_UNIT was last restarted"',
     '        :'),

    # --- both failure conditions at once ------------------------------------
    #
    # The netwatch-config refusal and the code-moved advisory were written
    # independently and can hold in the same run. x1 restores the arrangement
    # that had, where the failure list exited on its own and the stale-deploy
    # report below it was unreachable - a masking defect that reads as a
    # perfectly ordinary early exit.
    ("x1", "exit on the failure list, so a missing config MASKS a stale deploy",
     INSTALLER,
     '        log_error "A unit may be installed on disk without the running '
     'service having picked it up. Re-run this script; the units left in that '
     'state are remembered and will be restarted."\n        rc=1',
     '        log_error "A unit may be installed on disk without the running '
     'service having picked it up. Re-run this script; the units left in that '
     'state are remembered and will be restarted."\n        exit 1'),

    ("x2", "never report the stale deploy at all",
     INSTALLER,
     '    report_code_stale || rc=1',
     '    :'),

    ("x3", "report the stale deploy but exit 0 over it",
     INSTALLER,
     '    report_code_stale || rc=1',
     '    report_code_stale || true'),

    ("x4", "reach the report only when the failure list is empty",
     INSTALLER,
     'if [ ${#failures[@]} -gt 0 ] || [ "${code_stale:-0}" -eq 1 ]; then\n'
     '    report_and_exit\nfi',
     'if [ "${code_stale:-0}" -eq 1 ]; then\n    report_and_exit\nfi'),

    # --- the watchdog config-usability gate (T-494) --------------------------
    #
    # This gate decides whether gardyn-netwatch - the unit that can REBOOT a
    # host with no physical recovery path - gets armed. n3 is the one that
    # matters most and the one a suite fed only bad configs cannot catch: it
    # installs the NAIVE placeholder test, which refuses every correctly
    # filled config forever because the template's own prose mentions the
    # token. It is killed only by the positive control.
    ("n1", "drop the directory check - a dir at the config path arms it",
     INSTALLER,
     '    if [ -d "$NETWATCH_CONFIG" ]; then',
     '    if false; then'),

    ("n2", "drop the placeholder check - an unedited template arms it",
     INSTALLER,
     '    grep -qE "^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=.*$NETWATCH_PLACEHOLDER" \\\n'
     '        "$NETWATCH_CONFIG"\n'
     '    case $? in',
     '    grep -qE "THIS-PATTERN-MATCHES-NOTHING-EVER" \\\n'
     '        "$NETWATCH_CONFIG"\n'
     '    case $? in'),

    ("n3", "use the NAIVE unscoped placeholder grep, which refuses every "
           "correctly filled config because the template's prose names the token",
     INSTALLER,
     '    grep -qE "^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*[[:space:]]*=.*$NETWATCH_PLACEHOLDER" \\\n'
     '        "$NETWATCH_CONFIG"',
     '    grep -qE "$NETWATCH_PLACEHOLDER" \\\n'
     '        "$NETWATCH_CONFIG"'),

    ("n4", "treat an UNREADABLE config as an all-clear (fail open)",
     INSTALLER,
     '        *) echo "$NETWATCH_CONFIG cannot be read"\n'
     '           return 0 ;;',
     '        *) ;;'),

    ("n5", "let the gate report a problem but arm the unit anyway",
     INSTALLER,
     '            problem=$(netwatch_config_problem)\n'
     '            if [ -n "$problem" ]; then',
     '            problem=$(netwatch_config_problem)\n'
     '            if false; then'),

    ("n6", "drop the empty-file half of the gate",
     INSTALLER,
     '    if [ ! -f "$NETWATCH_CONFIG" ] || [ ! -s "$NETWATCH_CONFIG" ]; then',
     '    if false; then'),

    # --- option parsing (T-491) ---------------------------------------------
    ("o1", "accept an unknown option instead of refusing it",
     INSTALLER,
     '        *) usage >&2; fail "unknown option: $1" ;;',
     '        *) ;;'),
]

# Mutants that DELETE a file rather than edit one - the "a shipped unit quietly
# stops being shipped" regression, which no text replacement can express.
DELETE_MUTANTS = [
    ("d1", "a shipped unit disappears from the repo", NETWATCH_TIMER),
]

# Control B must be RED by construction and must not duplicate a real mutant.
CONTROL_B = (INSTALLER,
             'sudo install -m 0644 "$src" "$dest"',
             'sudo install -m 0600 "$src" "$dest"')


def purge_pycache():
    for root, dirs, _ in os.walk(REPO):
        if ".git" in root:
            continue
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)


def run_suite():
    """Return (passed, combined_output). stderr merged - unittest writes there."""
    purge_pycache()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", SUITE],
        cwd=REPO, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    return proc.returncode == 0, proc.stdout


def sha(path):
    if not os.path.exists(path):
        return "<missing>"
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def read(path):
    with open(path) as fh:
        return fh.read()


def write(path, text):
    with open(path, "w") as fh:
        fh.write(text)


def main():
    targets = [INSTALLER, SETUP, MQTT_UNIT, NETWATCH_TIMER]
    original = {p: read(p) for p in targets}
    original_sha = {p: sha(p) for p in targets}
    # Modes too. The delete mutant RECREATES its target, and a recreated file
    # gets whatever the umask gives - so a content-only comparison reports
    # "restored byte-identical: True" over a file whose permissions moved.
    original_mode = {p: stat.S_IMODE(os.stat(p).st_mode) for p in targets}
    stash = tempfile.mkdtemp(prefix="t477mut-")
    try:
        return _run(targets, original, original_sha, original_mode, stash)
    finally:
        _restore(targets, original, original_mode, stash)


def _run(targets, original, original_sha, original_mode, stash):

    print("=" * 70)
    print("CONTROL A - clean tree must be GREEN")
    ok, out = run_suite()
    print(f"  clean tree: {'GREEN' if ok else 'RED'}")
    if not ok:
        print(out[-3000:])
        print("\nABORT: clean tree is not green. No mutant verdict is readable.")
        return 2

    print("=" * 70)
    print("CONTROL B - deliberately broken code must be RED")
    path, old, new = CONTROL_B
    broken = original[path].replace(old, new)
    if broken == original[path]:
        print("ABORT: control-B anchor did not match. Harness is broken.")
        return 2
    write(path, broken)
    ok_b, _ = run_suite()
    write(path, original[path])
    print(f"  broken code: {'GREEN' if ok_b else 'RED'}")
    if ok_b:
        print("\nABORT: the suite passed a deliberately broken tree. The scorer")
        print("cannot tell pass from fail; every verdict below is meaningless.")
        return 2

    print("=" * 70)
    print("BOTH CONTROLS OK - mutant verdicts are readable\n")

    killed, survived = 0, []

    for tag, label, path, old, new in MUTANTS:
        src = original[path]
        count = src.count(old)
        if count != 1:
            print(f"  [{tag}] HARNESS ERROR ({count} anchor matches): {label}")
            survived.append((tag, label, f"anchor matched {count}x, not 1"))
            continue

        write(path, src.replace(old, new, 1))
        if read(path) == src:
            print(f"  [{tag}] HARNESS ERROR (file unchanged): {label}")
            survived.append((tag, label, "mutation did not apply"))
            write(path, src)
            continue

        ok_m, _ = run_suite()
        write(path, src)

        if ok_m:
            print(f"  [{tag}] SURVIVED  {label}")
            survived.append((tag, label, "suite stayed green"))
        else:
            killed += 1
            print(f"  [{tag}] killed    {label}")

    for tag, label, path in DELETE_MUTANTS:
        parked = os.path.join(stash, os.path.basename(path))
        shutil.move(path, parked)
        if os.path.exists(path):
            print(f"  [{tag}] HARNESS ERROR (file still present): {label}")
            survived.append((tag, label, "deletion did not apply"))
            continue
        ok_m, _ = run_suite()
        # copyfile, not move/copy2: do NOT restore the original mtime, or a
        # (mtime, size) bytecode cache can serve the previous run's bytecode.
        shutil.copyfile(parked, path)
        os.chmod(path, original_mode[path])
        os.remove(parked)
        if ok_m:
            print(f"  [{tag}] SURVIVED  {label}")
            survived.append((tag, label, "suite stayed green"))
        else:
            killed += 1
            print(f"  [{tag}] killed    {label}")

    total = len(MUTANTS) + len(DELETE_MUTANTS)
    print("\n" + "=" * 70)
    print(f"RESULT: {killed}/{total} killed")

    restored = all(sha(p) == original_sha[p]
                   and stat.S_IMODE(os.stat(p).st_mode) == original_mode[p]
                   for p in targets)
    print(f"tree restored byte-identical (content and mode): {restored}")
    shutil.rmtree(stash, ignore_errors=True)

    if survived:
        print("\nSURVIVORS - a survivor is a question about the CODE and the")
        print("HARNESS, not only about the suite. Three explanations: the test")
        print("is weak, the mutation never applied, or the mutated code is")
        print("redundant and genuinely changes nothing.")
        for tag, label, why in survived:
            print(f"  [{tag}] {label}: {why}")

    return 0 if (killed == total and restored) else 1


def _restore(targets, original, original_mode, stash):
    """Put every mutated file back, whatever happened.

    Without this a battery killed part-way through (^C, a timeout) leaves a
    mutant applied in the working tree - which is a silent, plausible-looking
    change to shipping code. Observed 2026-08-01: a 10-minute timeout left
    setup.sh with its installer call commented out.

    Each file is restored INDEPENDENTLY. A single loop abandons the remaining
    files the moment one raises - and it raises out of a `finally`, so the
    second file stays mutated with nothing reporting it. Errors are collected
    and re-raised after every file has had its turn.

    The stash is only a scratch area for the delete mutant, which copies its
    own file back; a file missing here is recreated from `original` like any
    other, because sha() on a missing path returns a value no digest matches.
    """
    errors = []
    for path, text in original.items():
        try:
            if sha(path) != hashlib.sha256(text.encode()).hexdigest():
                write(path, text)
            os.chmod(path, original_mode[path])
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    shutil.rmtree(stash, ignore_errors=True)
    if errors:
        raise OSError("could not restore: " + "; ".join(errors))


if __name__ == "__main__":
    sys.exit(main())
