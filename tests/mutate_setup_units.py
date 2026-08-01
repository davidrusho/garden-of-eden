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
import hashlib
import os
import shutil
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
     'sudo install -m 0644 "$src" "$dest" || fail "failed to install $u to $dest"',
     'true "$src" "$dest"'),

    ("i2", "swallow an install failure instead of aborting",
     INSTALLER,
     '|| fail "failed to install $u to $dest"',
     '|| true'),

    ("i3", "drop the mode, letting units land at whatever umask gives",
     INSTALLER,
     'sudo install -m 0644 "$src" "$dest"',
     'sudo install "$src" "$dest"'),

    ("i4", "restart on every run, bouncing the controller during a no-op setup",
     INSTALLER,
     '    if is_changed "$u"; then',
     '    if true; then'),

    ("i5", "never restart, so a changed unit file is installed but not applied",
     INSTALLER,
     'sudo systemctl restart "$u" || fail "systemctl restart $u failed"',
     'sudo systemctl start "$u" || fail "systemctl start $u failed"'),

    ("i6", "drop the daemon-reload",
     INSTALLER,
     'sudo systemctl daemon-reload || fail "systemctl daemon-reload failed"',
     'true'),

    ("i7", "enable everything, including the two units with no [Install]",
     INSTALLER,
     '    if ! grep -q \'^\\[Install\\]\' "$UNIT_SRC_DIR/$u"; then',
     '    if false; then'),

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
     'sudo systemctl enable "$u" || fail "systemctl enable $u failed"',
     'sudo systemctl enable "$u" || true'),

    ("i12", "install everything in the directory, unit file or not",
     INSTALLER,
     'for f in "$UNIT_SRC_DIR"/*.service "$UNIT_SRC_DIR"/*.timer; do',
     'for f in "$UNIT_SRC_DIR"/*; do'),

    ("i13", "drop the ExecStart-path preflight warning",
     INSTALLER,
     '            [ -e "$token" ] || log_warn "$unit: ExecStart path does not exist on this host: $token"',
     '            :'),

    ("s1", "REINTRODUCE the heredoc that overwrites the tracked unit file",
     SETUP,
     'function install_systemd_units {\n    if ! "$BIN_DIR/install-systemd-units.sh"; then\n        log_error "systemd unit installation failed."\n        return 1\n',
     OLD_GENERATOR),

    ("s2", "stop calling the installer from main",
     SETUP,
     '\ninstall_systemd_units\n',
     '\n#install_systemd_units\n'),

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
    stash = tempfile.mkdtemp(prefix="t477mut-")

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

    restored = all(sha(p) == original_sha[p] for p in targets)
    print(f"tree restored byte-identical: {restored}")
    shutil.rmtree(stash, ignore_errors=True)

    if survived:
        print("\nSURVIVORS - a survivor is a question about the CODE and the")
        print("HARNESS, not only about the suite. Three explanations: the test")
        print("is weak, the mutation never applied, or the mutated code is")
        print("redundant and genuinely changes nothing.")
        for tag, label, why in survived:
            print(f"  [{tag}] {label}: {why}")

    return 0 if (killed == total and restored) else 1


if __name__ == "__main__":
    sys.exit(main())
