"""Tests that the shipped systemd units are INSTALLED, never generated.

Why this file exists. `bin/setup.sh` generated `mqtt.service` with a heredoc
whose target was the git-tracked `services/etc/systemd/system/mqtt.service` -
the file it was supposed to be deploying. Three failures rode on that one line:

  1. A setup run left the working tree dirty, having overwritten its own source.
  2. The generated text dropped `StartLimitIntervalSec=0`, `RestartSec=10` and
     the `network-online.target` ordering that T-471 added at 9e00c2f. Without
     them, systemd's default 5-starts-in-10s limit plus the default
     RestartSec=100ms burns the whole restart budget in under a second when the
     broker is unreachable at boot, and parks the unit in `failed` permanently.
     That is the "router reboot leaves the garden with no controller" outage,
     and a setup re-run reintroduced it silently.
  3. It knew about one unit. `gardyn-health-log` and `gardyn-netwatch`
     (.service + .timer) reached the Pi by hand, so a rebuild lost the health
     sampler and the network watchdog - the two things that exist to make the
     next outage answerable. Their absence is invisible until an outage.

Nothing here needs a Pi. The installer is driven in a sandbox: a copy of the
repo layout, a temporary directory standing in for /etc/systemd/system, and
fake `sudo` and `systemctl` on PATH. The fakes reproduce what the real tools do
ON FAILURE, observed first rather than invented - systemd 252 on the Pi answers
a bad `systemctl` invocation with an EMPTY stdout, one line on stderr and rc 1,
and GNU `install` likewise (stdout empty, `install: cannot stat ...`, rc 1). A
double whose error branch is a bare `exit 1` with no output hides real bugs.

For the installer tests `sudo` is faked as `exec "$@"` and `install` is NOT
faked, so they exercise the real `install -m 0644` invocation the Pi will run.
The end-to-end setup.sh case at the bottom uses a LOGGING NO-OP `sudo` instead -
setup.sh writes to /boot/config.txt, /etc/modules and /usr/local/bin, and under
`exec` those would land on the machine running the tests.
"""
# Reviewed: 2026-08-01 against 3e8374c and 92dd3fd (T-477)
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALLER = os.path.join(REPO, "bin", "install-systemd-units.sh")
SETUP_SH = os.path.join(REPO, "bin", "setup.sh")
UNIT_SRC = os.path.join(REPO, "services", "etc", "systemd", "system")
NETWATCH_TEMPLATE = os.path.join(REPO, "services", "etc", "gardyn",
                                 "netwatch.env.example")

# Pinned deliberately. The installer derives its unit list from the directory,
# which is what stops a newly added unit being forgotten; this list is the
# opposite guard - it catches a unit silently DISAPPEARING from the repo.
EXPECTED_UNITS = {
    "mqtt.service",
    "gardyn-health-log.service",
    "gardyn-health-log.timer",
    "gardyn-netwatch.service",
    "gardyn-netwatch.timer",
}

# Units with an [Install] section. Only these can be `systemctl enable`d; the
# two Type=oneshot units are started by their timers.
ENABLEABLE = {"mqtt.service", "gardyn-health-log.timer", "gardyn-netwatch.timer"}

# Logs the command line, then RUNS it. The log is what lets a test assert on
# how a privileged write was issued - e.g. that the manifest goes through a
# temporary name and a rename rather than being truncated in place - which is
# not visible from the resulting file.
FAKE_SUDO = """#!/bin/bash
printf '%s\\n' "$*" >> "$SUDO_LOG"
exec "$@"
"""

# A LOGGING NO-OP, not `exec "$@"`. Used by the end-to-end setup.sh cases, which
# run a script that writes to /boot/config.txt, /etc/modules and /usr/local/bin -
# and by the one installer case that lets SYSTEMD_UNIT_DIR fall through to its
# production default, where on a Linux host the destination is the real
# /etc/systemd/system. Under `exec` either of those would take effect on the
# machine running the tests.
FAKE_NOOP_SUDO = """#!/bin/bash
printf '%s\\n' "$*" >> "$SUDO_LOG"
exit 0
"""

# Mirrors the real failure shape observed on the Pi (systemd 252):
#   $ systemctl is-enabled nosuch.service
#   rc=1  stdout=[]  stderr=[Failed to get unit file state for ...]
# Nothing on stdout, one line on stderr, non-zero exit.
FAKE_SYSTEMCTL = """#!/bin/bash
printf '%s\\n' "$*" >> "$SYSTEMCTL_LOG"
if [ -n "$SYSTEMCTL_FAIL_ON" ]; then
    case "$*" in
        *"$SYSTEMCTL_FAIL_ON"*)
            echo "Failed to $1 unit: Unit file does not exist." >&2
            exit 1
            ;;
    esac
fi
exit 0
"""


class Sandbox:
    """A disposable repo layout the installer can be run against for real.

    `runnable` rewrites the deployment prefix the shipped units hardcode
    (`/home/gardyn/garden-of-eden`) to a directory inside the sandbox, and
    creates the files their ExecStart lines name. Without it the installer's
    can-this-unit-run gate refuses to enable anything, which is a real
    behaviour with its own test - but it is not the state a healthy Pi is in,
    so the happy-path cases have to reproduce a host where the paths exist.
    """

    PI_PREFIX = "/home/gardyn/garden-of-eden"
    # The paths the shipped units name under that prefix.
    PI_FILES = ("venv/bin/python", "bin/gardyn-health-log.py",
                "bin/gardyn-netwatch.py", "mqtt.py")

    def __init__(self, units=None, runnable=True, netwatch_config=True):
        self.root = tempfile.mkdtemp(prefix="t477-")
        self.repo = os.path.join(self.root, "repo")
        self.src = os.path.join(self.repo, "services", "etc", "systemd", "system")
        self.dest = os.path.join(self.root, "etc")
        self.fakebin = os.path.join(self.root, "fakebin")
        # A SECOND fake bin whose `sudo` only logs. The installer's production
        # default for SYSTEMD_UNIT_DIR can only be exercised by letting the
        # script use it, and on a Linux host that directory is the real one -
        # so that one test runs with every privileged call inert.
        self.noopbin = os.path.join(self.root, "noopbin")
        self.log = os.path.join(self.root, "systemctl.log")
        self.sudo_log = os.path.join(self.root, "sudo.log")

        os.makedirs(os.path.join(self.repo, "bin"))
        os.makedirs(self.src)
        os.makedirs(self.dest)
        os.makedirs(self.fakebin)
        os.makedirs(self.noopbin)

        shutil.copy(INSTALLER, os.path.join(self.repo, "bin",
                                            "install-systemd-units.sh"))

        # gardyn-netwatch has no default network topology and refuses to run
        # without this file, so the installer refuses to ARM it without one.
        # A healthy Pi has it; `netwatch_config=False` reproduces a first
        # deploy that does not.
        self.netwatch_config = os.path.join(self.root, "netwatch.env")
        if netwatch_config:
            with open(self.netwatch_config, "w") as fh:
                fh.write("GARDYN_NETWATCH_PING_TARGETS=192.0.2.1,192.0.2.9\n"
                         "GARDYN_NETWATCH_TCP_HOST=192.0.2.9\n"
                         "GARDYN_NETWATCH_WLAN_UUID="
                         "11111111-2222-3333-4444-555555555555\n")

        self.fakeroot = os.path.join(self.root, "fakeroot")
        if runnable:
            for rel in self.PI_FILES:
                path = os.path.join(self.fakeroot, rel)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                open(path, "w").close()

        if units is None:
            for name in os.listdir(UNIT_SRC):
                text = read(os.path.join(UNIT_SRC, name)).decode()
                if runnable:
                    text = text.replace(self.PI_PREFIX, self.fakeroot)
                with open(os.path.join(self.src, name), "w") as fh:
                    fh.write(text)
        else:
            for name, content in units.items():
                with open(os.path.join(self.src, name), "w") as fh:
                    fh.write(content)

        self._write_exec(os.path.join(self.fakebin, "sudo"), FAKE_SUDO)
        self._write_exec(os.path.join(self.fakebin, "systemctl"), FAKE_SYSTEMCTL)
        self._write_exec(os.path.join(self.noopbin, "sudo"), FAKE_NOOP_SUDO)
        self._write_exec(os.path.join(self.noopbin, "systemctl"), FAKE_SYSTEMCTL)

    @staticmethod
    def _write_exec(path, content):
        with open(path, "w") as fh:
            fh.write(content)
        os.chmod(path, 0o755)

    def run(self, fail_on="", dest=None, args=(), unset_dest=False,
            noop_sudo=False):
        env = dict(os.environ)
        binpath = self.noopbin if noop_sudo else self.fakebin
        env["PATH"] = binpath + os.pathsep + env.get("PATH", "")
        if unset_dest:
            env.pop("SYSTEMD_UNIT_DIR", None)
        else:
            env["SYSTEMD_UNIT_DIR"] = dest if dest is not None else self.dest
        env["SYSTEMCTL_LOG"] = self.log
        env["SYSTEMCTL_FAIL_ON"] = fail_on
        env["SUDO_LOG"] = self.sudo_log
        env["GARDYN_NETWATCH_CONFIG"] = self.netwatch_config
        return subprocess.run(
            ["bash", os.path.join(self.repo, "bin", "install-systemd-units.sh")]
            + list(args),
            env=env, cwd=self.root, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )

    def systemctl_calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [line.strip() for line in fh if line.strip()]

    def sudo_calls(self):
        if not os.path.exists(self.sudo_log):
            return []
        with open(self.sudo_log) as fh:
            return [line.strip() for line in fh if line.strip()]

    def clear_log(self):
        if os.path.exists(self.log):
            os.remove(self.log)

    def git_init(self):
        """Make the sandbox repo a real checkout, so the installer's
        code-moved check has a revision to read."""
        for cmd in (["git", "init", "-q"],
                    ["git", "add", "-A"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                     "commit", "-qm", "baseline"]):
            subprocess.run(cmd, cwd=self.repo, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return self.head()

    def git_commit_python_only(self):
        """A commit that touches no unit file - the deploy shape that changes
        the running code while leaving every unit byte-identical."""
        path = os.path.join(self.repo, "mqtt.py")
        with open(path, "a") as fh:
            fh.write("# a python-only change\n")
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                        "commit", "-qm", "python only"], cwd=self.repo,
                       check=True, stdout=subprocess.DEVNULL)
        return self.head()

    def head(self):
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.repo,
                              stdout=subprocess.PIPE, text=True,
                              check=True).stdout.strip()

    def src_path(self, name):
        return os.path.join(self.src, name)

    def dest_path(self, name):
        return os.path.join(self.dest, name)

    def marker_path(self, name):
        return os.path.join(self.dest, f".{name}.needs-restart")

    def manifest_path(self):
        return os.path.join(self.dest, ".gardyn-installed-units")

    def manifest(self):
        if not os.path.exists(self.manifest_path()):
            return []
        with open(self.manifest_path()) as fh:
            return [line.strip() for line in fh if line.strip()]

    def revision_path(self):
        return os.path.join(self.dest, ".gardyn-source-revision")

    def recorded_revision(self):
        if not os.path.exists(self.revision_path()):
            return None
        with open(self.revision_path()) as fh:
            return fh.read().strip()

    def markers(self):
        return sorted(n for n in os.listdir(self.dest)
                      if n.endswith(".needs-restart"))

    def cleanup(self):
        # A test may have made the destination read-only to force a failure.
        os.chmod(self.dest, 0o755)
        shutil.rmtree(self.root, ignore_errors=True)


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


class TrackedUnitContentTests(unittest.TestCase):
    """The regression guard proper: what the units must still say."""

    def test_repo_ships_exactly_the_expected_units(self):
        found = {n for n in os.listdir(UNIT_SRC)
                 if n.endswith(".service") or n.endswith(".timer")}
        self.assertEqual(EXPECTED_UNITS, found)

    def test_mqtt_unit_keeps_the_boot_resilience_directives(self):
        text = read(os.path.join(UNIT_SRC, "mqtt.service")).decode()
        # Every assertion here is LINE-ANCHORED, and that is load-bearing rather
        # than tidy. A bare `assertIn("RestartSec=10", text)` also matches the
        # unit's own comment about the default `RestartSec=100ms`, so deleting
        # the directive left the test green - caught by mutant [m2].
        #
        # Without this, five restarts inside ten seconds park the unit in
        # `failed` for good when the broker is not up yet at boot.
        self.assertRegex(text, r"(?m)^StartLimitIntervalSec=0$")
        # The other half: the default RestartSec=100ms is what burns the budget.
        self.assertRegex(text, r"(?m)^RestartSec=10$")
        # `network.target` means "networking has been configured", not
        # "the network is usable" - the broker probe needs the latter.
        self.assertRegex(text, r"(?m)^Wants=network-online\.target$")
        self.assertRegex(text, r"(?m)^After=.*network-online\.target")

    def test_mqtt_unit_declares_Type_exec_so_a_broken_start_is_VISIBLE(self):
        """The default Type=simple made every guard in front of this unit
        decorative. systemd.service(5): under `simple` "systemctl start command
        lines ... will report success even if the service's binary cannot be
        invoked successfully"; under `exec` they "report failure". Measured on
        systemd 255 with an ExecStart naming a path that does not exist:
        `systemctl start` exits 0 with no Type= and with Type=simple, and 1
        with Type=exec.

        Line-anchored for the same reason as the directives above - the unit's
        own comment block names both `simple` and `exec`, so a substring test
        would stay green after the directive was deleted.

        Type=exec has been available since systemd 240; the Pi runs 252.
        """
        text = read(os.path.join(UNIT_SRC, "mqtt.service")).decode()
        self.assertRegex(text, r"(?m)^Type=exec$")
        self.assertNotRegex(text, r"(?m)^Type=simple$")

    def test_no_shipped_service_relies_on_the_default_Type(self):
        """A .service with no Type= at all is the shape the defect had: there
        is nothing to grep for, so it reads as deliberate. Requiring the
        declaration makes the choice explicit for every unit, including the
        next one somebody adds."""
        for name in sorted(EXPECTED_UNITS):
            if not name.endswith(".service"):
                continue
            with self.subTest(unit=name):
                text = read(os.path.join(UNIT_SRC, name)).decode()
                self.assertRegex(text, r"(?m)^Type=\S+$",
                                 f"{name} declares no Type=")

    def test_enableable_units_are_exactly_those_with_an_install_section(self):
        have_install = set()
        for name in EXPECTED_UNITS:
            if "[Install]" in read(os.path.join(UNIT_SRC, name)).decode():
                have_install.add(name)
        self.assertEqual(ENABLEABLE, have_install)


class SetupScriptTests(unittest.TestCase):
    """setup.sh must not generate, or otherwise write to, a tracked unit."""

    def setUp(self):
        self.text = read(SETUP_SH).decode()
        # Comments may name the tracked unit path - the point of the change is
        # explained there. Code may not: if no executable line can name it,
        # no executable line can redirect into it.
        self.code = "\n".join(line for line in self.text.splitlines()
                              if not line.lstrip().startswith("#"))

    def test_setup_code_does_not_reference_the_unit_source_directory(self):
        self.assertNotIn("services/etc/systemd/system", self.code)
        self.assertNotIn("service_file", self.code)

    def test_setup_contains_no_generated_unit_body(self):
        for marker in ("[Unit]", "[Service]", "WantedBy=multi-user.target",
                       "Description=MQTT Service"):
            self.assertNotIn(marker, self.text)

    def test_setup_delegates_to_the_installer(self):
        self.assertIn("install-systemd-units.sh", self.text)
        # `|| exit 1`, not a bare call. The call is currently the last line, so
        # a bare call would make setup.sh's exit status correct by position
        # alone and silently wrong the moment anything is appended.
        self.assertRegex(self.text,
                         r'(?m)^install_systemd_units "\$@" \|\| exit 1\s*$')

    def test_setup_forwards_its_arguments_to_the_installer(self):
        """Without the passthrough the installer's documented escape hatch is
        unreachable from the documented entrypoint: a Python-only pull makes
        the installer exit non-zero by design, setup.sh propagates that, and
        `./bin/setup.sh --restart-on-code-change` still runs it bare."""
        self.assertRegex(
            self.code,
            r'"\$BIN_DIR/install-systemd-units\.sh" "\$@"')

    def test_the_installer_uses_escape_sequences_bash_3_2_understands(self):
        """macOS ships bash 3.2 as /bin/bash, and the installer runs under its
        own shebang - where `echo -e "\\e["` emits the literal characters."""
        text = read(INSTALLER).decode()
        self.assertNotIn(r"\e[", text)
        self.assertIn(r"\033[", text)

    def test_installer_script_is_executable(self):
        self.assertTrue(os.access(INSTALLER, os.X_OK))

    def test_the_production_unit_directory_default_is_pinned(self):
        """Every behavioural test below sets SYSTEMD_UNIT_DIR, which means the
        value a real deploy uses was asserted by nothing: changing the default
        to /tmp/WRONG-UNIT-DIR left the whole suite green.

        The regex is anchored to the whole assignment, so a default that merely
        STARTS with the right path (/etc/systemd/system-old) fails too."""
        text = read(INSTALLER).decode()
        self.assertRegex(
            text,
            r'(?m)^UNIT_DEST_DIR="\$\{SYSTEMD_UNIT_DIR:-/etc/systemd/system\}"$')

    def test_the_source_directory_default_is_pinned(self):
        """The other half of the same gap. This one IS exercised behaviourally
        (the sandbox lets it fall through), but only as <repo>/services/... -
        nothing pins which subdirectory of the repo that is."""
        text = read(INSTALLER).decode()
        self.assertRegex(
            text,
            r'(?m)^UNIT_SRC_DIR="\$\{GARDYN_UNIT_SRC_DIR:'
            r'-\$INSTALL_DIR/services/etc/systemd/system\}"$')


class InstallerTests(unittest.TestCase):

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)

    def test_installs_every_tracked_unit_byte_for_byte(self):
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        for name in EXPECTED_UNITS:
            self.assertTrue(os.path.exists(self.box.dest_path(name)), name)
            self.assertEqual(read(self.box.src_path(name)),
                             read(self.box.dest_path(name)), name)

    def test_installed_units_are_mode_0644(self):
        self.box.run()
        for name in EXPECTED_UNITS:
            mode = stat.S_IMODE(os.stat(self.box.dest_path(name)).st_mode)
            self.assertEqual(0o644, mode, f"{name} is {oct(mode)}")

    def test_source_units_are_never_written_to(self):
        """Acceptance criterion 1, at the file level: no dirty tree."""
        before = {n: read(self.box.src_path(n)) for n in EXPECTED_UNITS}
        names_before = sorted(os.listdir(self.box.src))
        self.box.run()
        for name, content in before.items():
            self.assertEqual(content, read(self.box.src_path(name)), name)
        self.assertEqual(names_before, sorted(os.listdir(self.box.src)))

    def test_daemon_reload_is_called(self):
        self.box.run()
        self.assertIn("daemon-reload", self.box.systemctl_calls())

    def test_enables_only_units_with_an_install_section(self):
        self.box.run()
        enabled = {c.split()[-1] for c in self.box.systemctl_calls()
                   if c.startswith("enable ")}
        self.assertEqual(ENABLEABLE, enabled)

    def test_oneshot_services_are_installed_but_not_enabled(self):
        self.box.run()
        calls = self.box.systemctl_calls()
        for name in ("gardyn-health-log.service", "gardyn-netwatch.service"):
            self.assertTrue(os.path.exists(self.box.dest_path(name)))
            self.assertNotIn(f"enable {name}", calls)
            self.assertNotIn(f"start {name}", calls)
            self.assertNotIn(f"restart {name}", calls)

    def test_first_install_restarts_because_the_unit_is_new(self):
        self.box.run()
        self.assertIn("restart mqtt.service", self.box.systemctl_calls())

    def test_rerun_with_no_change_does_not_restart_anything(self):
        """A routine setup run must not bounce the grow-light controller."""
        self.box.run()
        self.box.clear_log()
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        calls = self.box.systemctl_calls()
        self.assertFalse([c for c in calls if c.startswith("restart ")],
                         f"unexpected restart: {calls}")
        self.assertIn("start mqtt.service", calls)

    def test_changed_unit_is_restarted_so_the_new_definition_takes_effect(self):
        self.box.run()
        with open(self.box.dest_path("mqtt.service"), "a") as fh:
            fh.write("\n# drifted out of band\n")
        self.box.clear_log()
        self.box.run()
        calls = self.box.systemctl_calls()
        self.assertIn("restart mqtt.service", calls)
        # …and the drift is gone.
        self.assertEqual(read(self.box.src_path("mqtt.service")),
                         read(self.box.dest_path("mqtt.service")))

    def test_a_first_install_is_not_reported_as_uncomparable(self):
        """`cmp` returns 2 both for "could not read" and for "destination does
        not exist", and the second is the ordinary case."""
        proc = self.box.run()
        self.assertNotIn("cannot compare", proc.stderr)

    def test_deleted_unit_is_restored(self):
        """Acceptance criterion 4 - prove the installer can actually fail."""
        self.box.run()
        os.remove(self.box.dest_path("gardyn-netwatch.timer"))
        self.assertFalse(os.path.exists(self.box.dest_path("gardyn-netwatch.timer")))
        self.box.clear_log()
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(read(self.box.src_path("gardyn-netwatch.timer")),
                         read(self.box.dest_path("gardyn-netwatch.timer")))
        # It changed, so it is re-armed rather than merely left in place.
        self.assertIn("restart gardyn-netwatch.timer", self.box.systemctl_calls())


class InstallerFailureTests(unittest.TestCase):
    """The paths a healthy Pi never takes."""

    def test_empty_unit_directory_is_a_loud_failure(self):
        """A glob matching nothing exits 0 - the clean-zero failure shape."""
        box = Sandbox(units={})
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("no unit files found", proc.stderr)
        self.assertEqual([], box.systemctl_calls())

    def test_missing_unit_source_directory_is_fatal(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        shutil.rmtree(box.src)
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("unit source directory not found", proc.stderr)
        self.assertEqual([], box.systemctl_calls())

    def test_missing_destination_directory_is_fatal(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        proc = box.run(dest=os.path.join(box.root, "no-such-etc"))
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("systemd unit directory not found", proc.stderr)
        self.assertEqual([], box.systemctl_calls())

    @unittest.skipIf(os.geteuid() == 0, "root can write to a read-only directory")
    def test_install_failure_aborts_before_touching_systemd(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        os.chmod(box.dest, 0o500)
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("failed to install", proc.stderr)
        self.assertEqual([], box.systemctl_calls())

    def test_daemon_reload_failure_is_reported_without_abandoning_the_rest(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        proc = box.run(fail_on="daemon-reload")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("daemon-reload failed", proc.stderr)
        # Aborting here is what leaves a unit installed on disk with the old
        # definition still running and no signal. Carry on and report instead.
        enabled = {c.split()[-1] for c in box.systemctl_calls()
                   if c.startswith("enable ")}
        self.assertEqual(ENABLEABLE, enabled)

    def test_one_unit_failing_to_enable_does_not_strand_the_others(self):
        """The regression that mattered: mqtt is processed first."""
        box = Sandbox()
        self.addCleanup(box.cleanup)
        proc = box.run(fail_on="enable mqtt.service")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("enable mqtt.service failed", proc.stderr)
        enabled = {c.split()[-1] for c in box.systemctl_calls()
                   if c.startswith("enable ")}
        # The health sampler and the network watchdog are the two units this
        # script exists to stop losing; they must still be armed.
        self.assertIn("gardyn-health-log.timer", enabled)
        self.assertIn("gardyn-netwatch.timer", enabled)

    def test_a_failed_restart_does_not_strand_the_later_units(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        proc = box.run(fail_on="restart mqtt.service")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("restart mqtt.service failed", proc.stderr)
        calls = box.systemctl_calls()
        self.assertIn("restart gardyn-health-log.timer", calls)
        self.assertIn("restart gardyn-netwatch.timer", calls)

    def test_start_failure_is_reported(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        box.run()
        box.clear_log()
        proc = box.run(fail_on="start mqtt.service")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("start mqtt.service failed", proc.stderr)

    def test_the_installer_and_the_watchdog_agree_on_the_config_PATH(self):
        """Two files name /etc/gardyn/netwatch.env independently, and nothing
        in the sandbox exercises either default - the harness always overrides
        it. A silent drift between them is the worst shape available: the
        installer would green-light a file the watchdog never reads, which is
        exactly the false all-clear this check exists to remove."""
        installer = read(INSTALLER).decode()
        script = read(os.path.join(REPO, "bin", "gardyn-netwatch.py")).decode()
        self.assertIn(
            'NETWATCH_CONFIG="${GARDYN_NETWATCH_CONFIG:-/etc/gardyn/netwatch.env}"',
            installer)
        self.assertIn(
            'os.environ.get("GARDYN_NETWATCH_CONFIG", "/etc/gardyn/netwatch.env")',
            script)

    def test_netwatch_is_NOT_armed_without_its_config(self):
        """The same defect Type=exec closes for mqtt.service, one layer out.

        gardyn-netwatch refuses to run without /etc/gardyn/netwatch.env, but
        only once the TIMER has fired - which no part of install/enable/start
        can observe. Without this check a first deploy onto a host with no
        config is a fully green run over a watchdog that then fails every two
        minutes forever.
        """
        box = Sandbox(netwatch_config=False)
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("netwatch.env", proc.stderr)
        calls = box.systemctl_calls()
        self.assertNotIn("enable gardyn-netwatch.timer", calls)
        self.assertNotIn("start gardyn-netwatch.timer", calls)
        self.assertNotIn("restart gardyn-netwatch.timer", calls)

    def test_a_missing_netwatch_config_does_not_strand_the_other_units(self):
        """It is recorded, not fatal. The grow-light controller is the thing
        that must come up, and refusing to arm a watchdog is no reason to
        leave the controller down."""
        box = Sandbox(netwatch_config=False)
        self.addCleanup(box.cleanup)
        box.run()
        calls = box.systemctl_calls()
        self.assertIn("enable mqtt.service", calls)
        self.assertIn("enable gardyn-health-log.timer", calls)

    def test_an_EMPTY_netwatch_config_is_refused_like_a_missing_one(self):
        """A truncated edit or a half-finished `install` leaves a zero-byte
        file. gardyn-netwatch rejects it; so must the installer, or the two
        disagree about whether the host is configured."""
        box = Sandbox(netwatch_config=False)
        self.addCleanup(box.cleanup)
        open(box.netwatch_config, "w").close()
        proc = box.run()
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("enable gardyn-netwatch.timer", box.systemctl_calls())

    def test_netwatch_IS_armed_when_the_config_is_present(self):
        """The positive control. Without it every assertion above is satisfied
        by an installer that never arms the watchdog at all."""
        box = Sandbox()
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("enable gardyn-netwatch.timer", box.systemctl_calls())

    def _box_with_config(self, text=None, template=False):
        box = Sandbox(netwatch_config=False)
        self.addCleanup(box.cleanup)
        if template:
            shutil.copy(NETWATCH_TEMPLATE, box.netwatch_config)
        elif text is not None:
            with open(box.netwatch_config, "w") as fh:
                fh.write(text)
        return box

    @staticmethod
    def _filled_template(leave_placeholder_in=()):
        """The shipped template with its VALUES replaced, comments untouched.

        This is what copy-then-edit actually produces, and it is the case that
        separates a value-scoped placeholder check from a bare `grep CHANGEME`
        - the template EXPLAINS the placeholder in its own prose, and that
        prose survives every correct edit.
        """
        real = {
            "GARDYN_NETWATCH_PING_TARGETS": "192.0.2.1,192.0.2.9",
            "GARDYN_NETWATCH_TCP_HOST": "192.0.2.9",
            "GARDYN_NETWATCH_WLAN_UUID": "11111111-2222-3333-4444-555555555555",
        }
        out = []
        for line in read(NETWATCH_TEMPLATE).decode().splitlines(True):
            key = line.split("=", 1)[0]
            if key in real and key not in leave_placeholder_in:
                line = f"{key}={real[key]}\n"
            out.append(line)
        return "".join(out)

    def test_an_UNEDITED_template_is_refused_like_a_missing_config(self):
        """`-s` tests PRESENCE, not usability.

        The README says copy-then-edit, so copy-and-forget is the single most
        likely first-deploy mistake - and it produced a completely green run
        over a watchdog that then refuses on every tick, because gardyn-netwatch
        rejects a CHANGEME value and nothing in install/enable/start can see
        that until the TIMER has fired.
        """
        box = self._box_with_config(template=True)
        proc = box.run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("CHANGEME", proc.stderr)
        self.assertNotIn("enable gardyn-netwatch.timer", box.systemctl_calls())

    def test_a_correctly_edited_config_KEEPING_its_comments_is_accepted(self):
        """The control that makes the check above worth having.

        The obvious implementation - `grep -q CHANGEME` - is WRONG, and wrong
        in the direction that never gets noticed in a test that only feeds it
        bad input: the template's own comments say "replace every CHANGEME"
        and "a CHANGEME left in place", so a bare grep refuses every properly
        filled config forever. Measured before the check was written.
        """
        box = self._box_with_config(self._filled_template())
        self.assertIn("CHANGEME", read(box.netwatch_config).decode(),
                      "fixture no longer exercises the trap: the template's "
                      "prose no longer mentions the placeholder")
        proc = box.run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("enable gardyn-netwatch.timer", box.systemctl_calls())

    def test_a_PARTIALLY_edited_config_is_refused(self):
        """Two keys filled in and one forgotten is a config the watchdog
        refuses, so it is one the installer must not arm."""
        box = self._box_with_config(
            self._filled_template(leave_placeholder_in=("GARDYN_NETWATCH_WLAN_UUID",)))
        proc = box.run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("enable gardyn-netwatch.timer", box.systemctl_calls())

    def test_a_DIRECTORY_at_the_config_path_is_refused_AND_named_as_one(self):
        """`[ -s ]` is true for a directory, so `mkdir` where a file belonged
        armed the watchdog against something it can never read.

        The message is asserted, not just the refusal. `[ ! -f ]` already
        refuses a directory on its own, so the dedicated branch earns its place
        only by SAYING which of the two happened - and a mutation battery found
        exactly that: dropping the branch left every refusal assertion green.
        On a host nobody can walk up to, "someone ran mkdir here" and "the file
        was never created" are different problems with different fixes, and the
        journal line is all the operator gets.
        """
        box = Sandbox(netwatch_config=False)
        self.addCleanup(box.cleanup)
        os.makedirs(box.netwatch_config)
        proc = box.run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("enable gardyn-netwatch.timer", box.systemctl_calls())
        self.assertIn("is a directory, not a file", proc.stderr)
        self.assertNotIn("is missing or empty", proc.stderr)

    def test_an_UNREADABLE_config_is_refused_rather_than_armed(self):
        """"Could not look" and "found no placeholder" are the same empty
        result from grep, and only one of them is an all-clear. The whole file
        fails closed on that distinction elsewhere; so must this."""
        box = self._box_with_config(self._filled_template())
        os.chmod(box.netwatch_config, 0o000)
        self.addCleanup(os.chmod, box.netwatch_config, 0o644)
        if os.access(box.netwatch_config, os.R_OK):
            self.skipTest("running as root - an unreadable file is still readable")
        proc = box.run()
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("enable gardyn-netwatch.timer", box.systemctl_calls())

    def test_the_installer_and_the_watchdog_agree_on_the_PLACEHOLDER(self):
        """A third independent naming of the same token, alongside the config
        PATH pinned above. If the script's PLACEHOLDER ever moves and the
        installer's does not, the installer green-lights a config the watchdog
        refuses - which is precisely the false all-clear this check exists to
        close, restored by drift."""
        installer = read(INSTALLER).decode()
        script = read(os.path.join(REPO, "bin", "gardyn-netwatch.py")).decode()
        self.assertIn('PLACEHOLDER = "CHANGEME"', script)
        self.assertIn('NETWATCH_PLACEHOLDER="CHANGEME"', installer)

    def test_a_masked_destination_is_refused_and_left_intact(self):
        """`systemctl mask` links the unit to /dev/null; GNU install would
        unlink it and silently unmask the unit."""
        box = Sandbox()
        self.addCleanup(box.cleanup)
        dest = box.dest_path("mqtt.service")
        os.symlink("/dev/null", dest)
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("destination is a symlink", proc.stderr)
        self.assertTrue(os.path.islink(dest))
        self.assertEqual("/dev/null", os.readlink(dest))
        # BSD `install` refuses a symlink destination on its own, so the
        # assertions above pass with or without the guard. This one does not:
        # it proves the install was never attempted, which is what matters on
        # the Pi, where GNU install unlinks the destination before writing.
        self.assertNotIn("failed to install mqtt.service", proc.stderr)
        calls = box.systemctl_calls()
        self.assertNotIn("enable mqtt.service", calls)
        # The other four are still installed and armed.
        self.assertIn("enable gardyn-netwatch.timer", calls)

    def test_a_pending_restart_survives_a_failed_run(self):
        """An interrupted run leaves the new file on disk with the old
        definition loaded. Without a marker the next run sees no difference,
        issues `start`, and reports success over a stale service."""
        box = Sandbox()
        self.addCleanup(box.cleanup)
        box.run()                                   # everything current
        with open(box.src_path("mqtt.service"), "a") as fh:
            fh.write("\n# a new definition\n")
        box.clear_log()
        proc = box.run(fail_on="restart mqtt.service")
        self.assertNotEqual(0, proc.returncode)
        # The file is now deployed but the running unit is stale.
        self.assertEqual(read(box.src_path("mqtt.service")),
                         read(box.dest_path("mqtt.service")))
        box.clear_log()
        proc = box.run()                            # a healthy re-run
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("restart mqtt.service", box.systemctl_calls())
        # …and once restarted, the pending state is cleared.
        box.clear_log()
        box.run()
        self.assertNotIn("restart mqtt.service", box.systemctl_calls())

    def test_a_service_with_no_execstart_is_not_enabled(self):
        """"found no problem" and "could not look" are the same empty result;
        only one of them is an all-clear."""
        box = Sandbox(units={"probe.service":
                             "[Unit]\nDescription=probe\n\n[Service]\n"
                             "Type=oneshot\nExecStartPre=/nowhere/x.py\n\n"
                             "[Install]\nWantedBy=multi-user.target\n"})
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("no ExecStart line found", proc.stderr)
        self.assertEqual(["daemon-reload"], box.systemctl_calls())

    def test_whitespace_around_the_execstart_equals_is_still_checked(self):
        """systemd.syntax(7): whitespace either side of `=` is ignored."""
        box = Sandbox(units={"probe.service":
                             "[Unit]\nDescription=probe\n\n[Service]\n"
                             "Type=oneshot\nExecStart = /nowhere/x.py\n\n"
                             "[Install]\nWantedBy=multi-user.target\n"})
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("/nowhere/x.py", proc.stderr)

    def test_a_quoted_execstart_path_is_still_checked(self):
        box = Sandbox(units={"probe.service":
                             "[Unit]\nDescription=probe\n\n[Service]\n"
                             'Type=oneshot\nExecStart="/nowhere/x.py"\n\n'
                             "[Install]\nWantedBy=multi-user.target\n"})
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("/nowhere/x.py", proc.stderr)

    def test_a_timer_is_not_armed_when_its_service_was_refused(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        os.symlink("/dev/null", box.dest_path("gardyn-netwatch.service"))
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("gardyn-netwatch.service was not installed", proc.stderr)
        self.assertNotIn("enable gardyn-netwatch.timer", box.systemctl_calls())
        # The unrelated sibling timer is unaffected.
        self.assertIn("enable gardyn-health-log.timer", box.systemctl_calls())

    def test_an_unreadable_unit_source_is_not_read_as_an_all_clear(self):
        """A grep that cannot open the file and a grep that finds no problem
        return the same empty result."""
        box = Sandbox()
        self.addCleanup(box.cleanup)
        os.chmod(box.src_path("gardyn-netwatch.service"), 0o000)
        self.addCleanup(os.chmod, box.src_path("gardyn-netwatch.service"), 0o644)
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("cannot read", proc.stderr)
        self.assertNotIn("enable gardyn-netwatch.timer", box.systemctl_calls())

    def test_a_failing_run_does_not_print_a_pass_summary(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        proc = box.run(fail_on="enable mqtt.service")
        self.assertNotEqual(0, proc.returncode)
        self.assertNotIn("units installed;", proc.stdout)

    def test_a_directory_destination_is_refused(self):
        """`install SOURCE DIRECTORY` is a valid second form that exits 0 and
        copies INTO the directory, so `|| fail` cannot see it."""
        box = Sandbox()
        self.addCleanup(box.cleanup)
        dest = box.dest_path("mqtt.service")
        os.makedirs(dest)
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("destination is a directory", proc.stderr)
        self.assertEqual([], os.listdir(dest))

    def test_a_unit_that_cannot_run_here_is_installed_but_not_enabled(self):
        """A public repo: gardyn-netwatch can reboot the host and is wired to
        one LAN, so a checkout elsewhere must not end up with it armed."""
        box = Sandbox(runnable=False)
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("NOT enabled", proc.stderr)
        for name in EXPECTED_UNITS:
            self.assertTrue(os.path.exists(box.dest_path(name)), name)
        self.assertFalse([c for c in box.systemctl_calls()
                          if c.startswith(("enable ", "start ", "restart "))])

    def test_a_drop_in_directory_is_refused_rather_than_silently_skipped(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        os.makedirs(os.path.join(box.src, "mqtt.service.d"))
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("drop-in directory not supported", proc.stderr)
        self.assertEqual([], box.systemctl_calls())

    def test_other_unit_types_are_installed_too(self):
        """The list is derived from the directory - that promise has to hold
        for unit types this project does not ship yet."""
        box = Sandbox(units={
            "api.socket": "[Socket]\nListenStream=8080\n\n[Install]\n"
                          "WantedBy=sockets.target\n",
            "watch.path": "[Path]\nPathExists=/tmp\n\n[Install]\n"
                          "WantedBy=multi-user.target\n",
        })
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertTrue(os.path.exists(box.dest_path("api.socket")))
        self.assertTrue(os.path.exists(box.dest_path("watch.path")))


class InstallerPreflightTests(unittest.TestCase):

    UNIT = ("[Unit]\nDescription=probe\n\n[Service]\nType=oneshot\n"
            "ExecStart=%s\n\n[Install]\nWantedBy=multi-user.target\n")

    def test_a_missing_execstart_path_names_the_path_and_blocks_the_enable(self):
        box = Sandbox(units={"probe.service":
                             self.UNIT % "/nowhere/at/all/probe.py"})
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("ExecStart path does not exist", proc.stderr)
        self.assertIn("/nowhere/at/all/probe.py", proc.stderr)
        self.assertEqual(["daemon-reload"], box.systemctl_calls())

    def test_a_timer_is_judged_on_the_service_it_arms(self):
        """A .timer has no ExecStart, so judging it alone passes every timer -
        and the timer is what arms gardyn-netwatch's reboot ladder."""
        box = Sandbox(units={
            "probe.service": ("[Unit]\nDescription=probe\n\n[Service]\n"
                              "Type=oneshot\nExecStart=/nowhere/probe.py\n"),
            "probe.timer": ("[Unit]\nDescription=probe timer\n\n[Timer]\n"
                            "OnCalendar=*:0/5\n\n[Install]\n"
                            "WantedBy=timers.target\n"),
        })
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertTrue(os.path.exists(box.dest_path("probe.timer")))
        self.assertNotIn("enable probe.timer", box.systemctl_calls())

    def test_no_warning_when_the_execstart_path_exists(self):
        box = Sandbox(units={"probe.service": self.UNIT % "/bin/sh"})
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("ExecStart path does not exist", proc.stderr)

    def test_an_execstart_path_is_not_globbed_against_the_cwd(self):
        """`/etc/host*` must be checked literally. Let the shell expand it and
        it resolves to a file that exists, so the warning silently vanishes."""
        box = Sandbox(units={"probe.service": self.UNIT % "/etc/host*"})
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("/etc/host*", proc.stderr)

    def test_non_unit_files_are_reported_and_not_installed(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        with open(os.path.join(box.src, "README.md"), "w") as fh:
            fh.write("not a unit\n")
        proc = box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("README.md", proc.stderr)
        self.assertFalse(os.path.exists(box.dest_path("README.md")))


class ProductionDefaultTests(unittest.TestCase):
    """The value a real deploy uses, exercised rather than grepped.

    Every other case here overrides SYSTEMD_UNIT_DIR, so the default was
    covered by nothing at all - the suite stayed at 48/48 green with the
    default changed to /tmp/WRONG-UNIT-DIR.

    Letting the script use its own default means the destination on a Linux
    host is the REAL /etc/systemd/system, so this case runs with a `sudo` that
    only logs. Nothing privileged executes: no install, no daemon-reload, no
    enable. The two assertions come from the two shapes that gives -
    a machine without that directory refuses by name, and a machine with one
    logs the install destination it was about to use.
    """

    def test_the_default_directory_is_the_one_the_script_actually_uses(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        proc = box.run(unset_dest=True, noop_sudo=True)

        # Whatever happened, the sandbox destination must not have been used -
        # that is what proves the default was consulted at all.
        trace = proc.stdout + proc.stderr + "\n".join(box.sudo_calls())
        self.assertNotIn(box.dest, trace)

        if os.path.isdir("/etc/systemd/system"):
            # Anchored to the unit name so a default of /etc/systemd/system-old
            # cannot satisfy it.
            self.assertIn(f"install -m 0644 {box.src_path('mqtt.service')} "
                          "/etc/systemd/system/mqtt.service", box.sudo_calls())
            # `sudo` is inert, so nothing was written and the run must fail.
            self.assertNotEqual(0, proc.returncode)
        else:
            self.assertEqual([], box.sudo_calls())
            self.assertNotEqual(0, proc.returncode)
            self.assertRegex(
                proc.stderr,
                r"(?m)systemd unit directory not found: /etc/systemd/system$")

    def test_no_unit_reached_a_real_systemd_directory(self):
        """The safety assertion for the case above, stated separately so it
        cannot be lost in a later edit: with an inert `sudo`, the run must
        touch neither systemd nor any file outside the sandbox."""
        box = Sandbox()
        self.addCleanup(box.cleanup)
        box.run(unset_dest=True, noop_sudo=True)
        self.assertEqual([], box.systemctl_calls())
        for call in box.sudo_calls():
            self.assertTrue(call.startswith("install -m 0644 "), call)


class OptionParsingTests(unittest.TestCase):

    def test_an_unknown_option_is_refused(self):
        """A typo in --remove-retired doing nothing quietly is the safe
        direction; the same typo in --restart-on-code-change is not. Refuse."""
        box = Sandbox()
        self.addCleanup(box.cleanup)
        proc = box.run(args=["--remove-retried"])
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("unknown option: --remove-retried", proc.stderr)
        self.assertEqual([], box.systemctl_calls())
        self.assertFalse(os.path.exists(box.dest_path("mqtt.service")))

    def test_help_exits_zero_without_installing_anything(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        proc = box.run(args=["--help"])
        self.assertEqual(0, proc.returncode)
        self.assertIn("--remove-retired", proc.stdout)
        self.assertIn("--restart-on-code-change", proc.stdout)
        self.assertEqual([], box.systemctl_calls())


class PendingMarkerLifecycleTests(unittest.TestCase):
    """A marker that is written and never removed is worse than no marker.

    The two Type=oneshot services have no [Install] section, so they are
    skipped by the enable loop and never reach the `rm -f` in the restart
    loop. Their markers were therefore PERMANENT: every later run called them
    changed, and the "none changed" summary was unreachable.
    """

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)

    def test_no_marker_survives_a_healthy_run(self):
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual([], self.box.markers())

    def test_a_second_run_reports_that_nothing_changed(self):
        """The branch a leaked marker made unreachable."""
        self.box.run()
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("none changed", proc.stdout)
        self.assertNotIn("changed:", proc.stdout)

    def test_a_oneshot_service_is_not_restarted_when_its_marker_is_cleared(self):
        """Clearing the marker must not turn into starting the unit. These are
        oneshots: their timer runs them, and starting them here would fire the
        job on every deploy."""
        self.box.run()
        for name in ("gardyn-health-log.service", "gardyn-netwatch.service"):
            for verb in ("start", "restart", "enable"):
                self.assertNotIn(f"{verb} {name}", self.box.systemctl_calls())

    def test_a_oneshot_marker_survives_a_failed_daemon_reload(self):
        """A failed reload means systemd is still holding the old definition,
        so the pending state has to cross runs exactly as it does for a unit
        whose restart failed."""
        proc = self.box.run(fail_on="daemon-reload")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn(".gardyn-health-log.service.needs-restart",
                      self.box.markers())
        self.assertIn("still pending", proc.stderr)
        # …and a healthy run afterwards clears it.
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual([], self.box.markers())

    def test_a_changed_oneshot_is_reported_as_changed_once_and_not_again(self):
        self.box.run()
        with open(self.box.src_path("gardyn-health-log.service"), "a") as fh:
            fh.write("\n# a new definition\n")
        proc = self.box.run()
        self.assertIn("changed: gardyn-health-log.service", proc.stdout)
        proc = self.box.run()
        self.assertIn("none changed", proc.stdout)


class RetiredUnitTests(unittest.TestCase):
    """A unit deleted from the repository stays deployed and enabled forever.

    Nothing else in this script can see it: every loop is driven by what the
    source directory holds now, so a removed unit simply stops being mentioned.
    That is the deployment twin of a retained MQTT message outliving the code
    that published it - and the unit most likely to be retired is the watchdog
    that can reboot the host.

    Removal is opt-in and fail-closed: only names in the manifest THIS script
    writes are eligible, so a host that has never run it can lose nothing, and
    a unit belonging to another package can never be selected.
    """

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.box.run()                       # establishes the manifest
        os.remove(self.box.src_path("gardyn-netwatch.timer"))
        os.remove(self.box.src_path("gardyn-netwatch.service"))
        self.box.clear_log()

    def test_the_manifest_records_what_was_installed(self):
        self.assertEqual(sorted(EXPECTED_UNITS), sorted(self.box.manifest()))

    def test_the_manifest_is_written_through_a_temporary_file(self):
        """A `tee` truncated by ENOSPC or a kill leaves a SHORT manifest, and a
        short manifest silently un-claims whatever fell off the end - in the
        reassuring direction, because nothing is deleted and the warning simply
        stops. Staging and renaming means a reader sees the old file or the new
        one. Asserted on the commands issued, not on the resulting file, which
        looks identical either way."""
        calls = self.box.sudo_calls()
        manifest = self.box.manifest_path()
        self.assertIn(f"tee {manifest}.new", calls)
        self.assertIn(f"mv -f {manifest}.new {manifest}", calls)
        self.assertFalse(os.path.exists(manifest + ".new"))

    def test_a_manifest_with_no_trailing_newline_keeps_its_last_entry(self):
        """`while read` drops a final line with no newline, which would un-claim
        that unit permanently - it leaves the manifest and can never be retired
        or warned about again."""
        entries = self.box.manifest()
        self.assertIn("gardyn-netwatch.timer", entries)
        with open(self.box.manifest_path(), "w") as fh:
            fh.write("\n".join(entries))          # no trailing newline
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("gardyn-netwatch.timer is deployed but the repository no "
                      "longer ships it", proc.stderr)

    def test_removal_is_deferred_when_the_run_has_already_failed(self):
        """`disable --now` acts on systemd's current picture. A failed
        daemon-reload means that picture is known to be out of date, and the
        deletion is the one step here that cannot be undone by re-running."""
        proc = self.box.run(args=["--remove-retired"], fail_on="daemon-reload")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("removal is deferred", proc.stderr)
        self.assertTrue(os.path.exists(self.box.dest_path("gardyn-netwatch.timer")))
        self.assertNotIn("disable --now gardyn-netwatch.timer",
                         self.box.systemctl_calls())

    def test_removal_is_deferred_when_an_earlier_unit_failed(self):
        proc = self.box.run(args=["--remove-retired"],
                            fail_on="enable mqtt.service")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("removal is deferred", proc.stderr)
        self.assertTrue(os.path.exists(self.box.dest_path("gardyn-netwatch.timer")))

    def test_a_removal_is_followed_by_a_daemon_reload(self):
        """systemd is left holding a definition whose file has just been
        deleted until something reloads it."""
        self.box.clear_log()
        self.box.run(args=["--remove-retired"])
        calls = self.box.systemctl_calls()
        self.assertIn("disable --now gardyn-netwatch.timer", calls)
        self.assertGreater(calls.index("daemon-reload", 1),
                           calls.index("disable --now gardyn-netwatch.timer"),
                           f"no daemon-reload after the removals: {calls}")

    def test_a_directory_at_a_retired_destination_is_refused(self):
        """The other half of the not-a-plain-file guard. Only the symlink half
        was covered, so narrowing the guard to `-L` alone stayed green."""
        dest = self.box.dest_path("gardyn-netwatch.timer")
        os.remove(dest)
        os.makedirs(dest)
        proc = self.box.run(args=["--remove-retired"])
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("refusing to remove", proc.stderr)
        self.assertTrue(os.path.isdir(dest))
        self.assertNotIn("disable --now gardyn-netwatch.timer",
                         self.box.systemctl_calls())

    def test_a_retired_unit_is_reported_and_left_alone_by_default(self):
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("gardyn-netwatch.timer is deployed but the repository no "
                      "longer ships it", proc.stderr)
        self.assertTrue(os.path.exists(self.box.dest_path("gardyn-netwatch.timer")))
        self.assertNotIn("disable --now gardyn-netwatch.timer",
                         self.box.systemctl_calls())

    def test_the_warning_repeats_until_the_unit_is_dealt_with(self):
        """A one-shot warning is no warning. The manifest keeps claiming a
        retired unit for as long as it is still on disk."""
        self.box.run()
        proc = self.box.run()
        self.assertIn("no longer ships it", proc.stderr)
        self.assertIn("gardyn-netwatch.timer", self.box.manifest())

    def test_the_flag_disables_and_deletes_the_retired_units(self):
        proc = self.box.run(args=["--remove-retired"])
        self.assertEqual(0, proc.returncode, proc.stderr)
        calls = self.box.systemctl_calls()
        for name in ("gardyn-netwatch.timer", "gardyn-netwatch.service"):
            self.assertIn(f"disable --now {name}", calls)
            self.assertFalse(os.path.exists(self.box.dest_path(name)), name)
        # The units still shipped are untouched.
        self.assertTrue(os.path.exists(self.box.dest_path("mqtt.service")))
        self.assertNotIn("disable --now mqtt.service", calls)

    def test_a_removed_unit_leaves_the_manifest(self):
        self.box.run(args=["--remove-retired"])
        self.assertEqual(sorted(EXPECTED_UNITS - {"gardyn-netwatch.timer",
                                                  "gardyn-netwatch.service"}),
                         sorted(self.box.manifest()))

    def test_the_pending_marker_of_a_removed_unit_goes_with_it(self):
        marker = self.box.marker_path("gardyn-netwatch.timer")
        open(marker, "w").close()
        self.box.run(args=["--remove-retired"])
        self.assertFalse(os.path.exists(marker))

    def test_a_masked_retired_unit_is_refused_rather_than_deleted(self):
        """A symlink here is `systemctl mask`. Deleting it would silently
        unmask a unit the operator deliberately turned off."""
        dest = self.box.dest_path("gardyn-netwatch.timer")
        os.remove(dest)
        os.symlink("/dev/null", dest)
        proc = self.box.run(args=["--remove-retired"])
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("refusing to remove", proc.stderr)
        self.assertTrue(os.path.islink(dest))
        self.assertNotIn("disable --now gardyn-netwatch.timer",
                         self.box.systemctl_calls())

    def test_a_failed_disable_leaves_the_unit_file_in_place(self):
        proc = self.box.run(args=["--remove-retired"],
                            fail_on="disable --now gardyn-netwatch.timer")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("left in place", proc.stderr)
        self.assertTrue(os.path.exists(self.box.dest_path("gardyn-netwatch.timer")))

    def test_a_unit_already_gone_from_disk_is_not_warned_about(self):
        os.remove(self.box.dest_path("gardyn-netwatch.timer"))
        os.remove(self.box.dest_path("gardyn-netwatch.service"))
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("no longer ships it", proc.stderr)
        self.assertNotIn("gardyn-netwatch.timer", self.box.manifest())


class RetiredUnitFailClosedTests(unittest.TestCase):
    """Removal must be incapable of selecting anything this script did not
    install. Both cases here are the ones that would be unrecoverable."""

    def test_a_host_with_no_manifest_removes_nothing(self):
        """The upgrade case: a Pi running the previous installer has no
        manifest, so the first run of this one has no ownership record and
        must delete nothing at all."""
        box = Sandbox()
        self.addCleanup(box.cleanup)
        box.run()
        os.remove(box.manifest_path())
        foreign = box.dest_path("someone-elses.service")
        open(foreign, "w").close()
        os.remove(box.src_path("gardyn-netwatch.timer"))
        proc = box.run(args=["--remove-retired"])
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertTrue(os.path.exists(box.dest_path("gardyn-netwatch.timer")))
        self.assertTrue(os.path.exists(foreign))

    def test_a_unit_whose_install_failed_is_not_claimed(self):
        """The manifest is the ONLY thing standing between --remove-retired and
        a file this script never wrote, so it must record what was installed -
        not what the source directory happens to hold.

        The scenario is not exotic: a foreign unit of the same name is already
        deployed, the `install` over it fails (ENOSPC, a read-only remount, an
        SD-card read error, `chattr +i`), and the repo later drops the name.
        Claiming it on the strength of shipping it would make that foreign file
        removable.
        """
        box = Sandbox()
        self.addCleanup(box.cleanup)
        foreign = box.dest_path("gardyn-netwatch.service")
        with open(foreign, "w") as fh:
            fh.write("[Unit]\nDescription=not ours\n")
        # Make the source unreadable so `install` fails for this unit only.
        # No addCleanup for the chmod - the file is deleted later in this test,
        # and cleanup runs after that. Sandbox.cleanup() removes the tree.
        os.chmod(box.src_path("gardyn-netwatch.service"), 0o000)

        proc = box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertNotIn("gardyn-netwatch.service", box.manifest())
        # …and it stays unclaimed, so a later run cannot remove it.
        os.chmod(box.src_path("gardyn-netwatch.service"), 0o644)
        os.remove(box.src_path("gardyn-netwatch.service"))
        os.remove(box.src_path("gardyn-netwatch.timer"))
        box.clear_log()
        proc = box.run(args=["--remove-retired"])
        self.assertTrue(os.path.exists(foreign))
        self.assertEqual("[Unit]\nDescription=not ours\n",
                         read(foreign).decode())
        self.assertNotIn("disable --now gardyn-netwatch.service",
                         box.systemctl_calls())

    def test_a_still_shipped_unit_keeps_its_claim_across_a_failed_install(self):
        """The inverse of the case above: a transient failure on a unit we DO
        still ship must not drop the ownership we already had, or the next run
        would treat a unit we installed as somebody else's."""
        box = Sandbox()
        self.addCleanup(box.cleanup)
        box.run()
        self.assertIn("gardyn-netwatch.service", box.manifest())
        os.chmod(box.src_path("gardyn-netwatch.service"), 0o000)
        self.addCleanup(os.chmod, box.src_path("gardyn-netwatch.service"), 0o644)
        box.run()
        self.assertIn("gardyn-netwatch.service", box.manifest())

    def test_a_unit_this_script_never_installed_is_never_removed(self):
        """A neighbouring unit in the same directory is not ours. It is absent
        from the source directory by definition, which is exactly the shape of
        a retired unit - the manifest is the only thing telling them apart."""
        box = Sandbox()
        self.addCleanup(box.cleanup)
        box.run()
        foreign = box.dest_path("someone-elses.service")
        open(foreign, "w").close()
        proc = box.run(args=["--remove-retired"])
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertTrue(os.path.exists(foreign))
        self.assertNotIn("someone-elses.service", box.manifest())
        self.assertNotIn("disable --now someone-elses.service",
                         box.systemctl_calls())


class CodeRevisionTests(unittest.TestCase):
    """`git pull && ./bin/install-systemd-units.sh` is the deploy, and a pull
    that touches only Python changes no unit file - so the restart decision
    correctly finds nothing to do and the run prints a column of PASS lines
    over a service still executing the previous revision.

    The recorded revision is written only when the service is actually
    restarted, which is what makes the warning survive a run that only warned.
    """

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.base = self.box.git_init()

    def test_the_revision_is_recorded_when_the_service_is_restarted(self):
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("restart mqtt.service", self.box.systemctl_calls())
        self.assertEqual(self.base, self.box.recorded_revision())

    def test_a_python_only_change_is_not_reported_as_success(self):
        self.box.run()
        moved = self.box.git_commit_python_only()
        self.box.clear_log()
        proc = self.box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("has NOT taken effect", proc.stderr)
        self.assertIn(moved, proc.stderr)
        self.assertNotIn("units installed;", proc.stdout)
        # Nothing was restarted - that IS the defect being reported.
        self.assertNotIn("restart mqtt.service", self.box.systemctl_calls())

    def test_a_run_that_only_warned_does_not_advance_the_recorded_revision(self):
        """Recording it would silence the warning on the next run while the
        service is still on the old code - the leaked-marker bug again."""
        self.box.run()
        self.box.git_commit_python_only()
        self.box.run()
        self.assertEqual(self.base, self.box.recorded_revision())
        proc = self.box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("has NOT taken effect", proc.stderr)

    def test_the_flag_restarts_the_service_and_records_the_new_revision(self):
        self.box.run()
        moved = self.box.git_commit_python_only()
        self.box.clear_log()
        proc = self.box.run(args=["--restart-on-code-change"])
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("restart mqtt.service", self.box.systemctl_calls())
        self.assertEqual(moved, self.box.recorded_revision())
        # …and the run after that has nothing left to say.
        self.box.clear_log()
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("restart mqtt.service", self.box.systemctl_calls())

    def test_a_failed_restart_under_the_flag_is_a_failure(self):
        self.box.run()
        self.box.git_commit_python_only()
        proc = self.box.run(args=["--restart-on-code-change"],
                            fail_on="restart mqtt.service")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("restart mqtt.service failed", proc.stderr)
        self.assertEqual(self.base, self.box.recorded_revision())

    def test_the_error_does_not_prescribe_a_remedy_that_cannot_clear_it(self):
        """A restart issued out of band is invisible here - the revision is
        recorded only by the run that performs the restart - so telling the
        operator to restart by hand would leave the deploy permanently red
        while the service was in fact current."""
        self.box.run()
        self.box.git_commit_python_only()
        proc = self.box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("--restart-on-code-change", proc.stderr)
        self.assertNotIn("Run 'sudo systemctl restart", proc.stderr)

    def test_the_revision_is_written_through_a_temporary_file(self):
        self.box.run()
        rev = self.box.revision_path()
        self.assertIn(f"tee {rev}.new", self.box.sudo_calls())
        self.assertIn(f"mv -f {rev}.new {rev}", self.box.sudo_calls())
        self.assertFalse(os.path.exists(rev + ".new"))

    def test_a_unit_file_change_restarts_and_re_records_in_one_run(self):
        """The ordinary case: both the code and a unit moved. The restart the
        unit change causes is the same restart the code needs."""
        self.box.run()
        with open(self.box.src_path("mqtt.service"), "a") as fh:
            fh.write("\n# a new definition\n")
        moved = self.box.git_commit_python_only()
        self.box.clear_log()
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("restart mqtt.service", self.box.systemctl_calls())
        self.assertEqual(moved, self.box.recorded_revision())


class BothFailureConditionsTests(unittest.TestCase):
    """The two fail-closed checks in this script were written independently and
    can hold in the SAME run - which is the case neither side's suite covers.

    A missing /etc/gardyn/netwatch.env records a failure; a pull that moved only
    Python sets the code-stale flag. Both are true on the shape of host that
    provokes them: a first deploy that never created the config, followed by
    ordinary Python-only pulls. Before these tests the failure list was reported
    with an `exit 1` of its own and the stale-deploy block below it was
    unreachable - so the operator fixed the config, re-ran, and only THEN
    discovered the deploy had never taken effect. Two round trips for one run's
    worth of information, and the failure list's closing advice ("re-run this
    script") is actively wrong for the half it was hiding: a plain re-run
    restarts nothing and cannot clear it.
    """

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        self.base = self.box.git_init()

    def _make_both_true(self):
        """A healthy first run, then remove the config and move the code."""
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        os.remove(self.box.netwatch_config)
        moved = self.box.git_commit_python_only()
        self.box.clear_log()
        return moved

    def test_ONE_run_reports_both_the_missing_config_and_the_stale_deploy(self):
        moved = self._make_both_true()
        proc = self.box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("netwatch.env", proc.stderr)
        self.assertIn("has NOT taken effect", proc.stderr)
        self.assertIn(moved, proc.stderr)
        # Neither half turns the run into a reported success.
        self.assertNotIn("units installed;", proc.stdout)

    def test_the_remedy_for_the_stale_half_survives_the_other_half(self):
        """The failure list ends with "re-run this script", which does not
        restart anything. If that is the last word the operator gets, the run
        has told them to do something that cannot work."""
        self._make_both_true()
        proc = self.box.run()
        self.assertIn("--restart-on-code-change", proc.stderr)

    def test_control_a_missing_config_ALONE_says_nothing_about_a_stale_deploy(self):
        """Positive control for the pair above: without it, an installer that
        printed the stale-deploy text unconditionally would satisfy them."""
        self.box.run()
        os.remove(self.box.netwatch_config)
        proc = self.box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("netwatch.env", proc.stderr)
        self.assertNotIn("has NOT taken effect", proc.stderr)

    def test_control_a_stale_deploy_ALONE_says_nothing_about_the_config(self):
        """The other direction of the same control."""
        self.box.run()
        self.box.git_commit_python_only()
        proc = self.box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("has NOT taken effect", proc.stderr)
        self.assertNotIn("netwatch.env", proc.stderr)

    def test_fixing_only_the_config_still_leaves_the_run_red(self):
        """The sequence an operator actually walks. Having been told both
        things, fixing one must not produce a green run over the other - that
        would be the masking defect with the roles swapped."""
        self._make_both_true()
        with open(self.box.netwatch_config, "w") as fh:
            fh.write("GARDYN_NETWATCH_PING_TARGETS=192.0.2.1,192.0.2.9\n")
        proc = self.box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("has NOT taken effect", proc.stderr)
        self.assertNotIn("netwatch.env is missing", proc.stderr)

    def test_the_flag_clears_both_once_the_config_is_back(self):
        """The exit from the state, and the positive control for every
        assertion above: they are all satisfied by a script that can never
        report success at all."""
        moved = self._make_both_true()
        with open(self.box.netwatch_config, "w") as fh:
            fh.write("GARDYN_NETWATCH_PING_TARGETS=192.0.2.1,192.0.2.9\n")
        self.box.clear_log()
        proc = self.box.run(args=["--restart-on-code-change"])
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertEqual(moved, self.box.recorded_revision())
        self.assertIn("enable gardyn-netwatch.timer",
                      self.box.systemctl_calls())


class ExistingDeploymentTests(unittest.TestCase):
    """The host that matters: a Pi where the units are already deployed and
    byte-identical, running an installer that has never recorded a revision.

    Nothing here restarts a unit, so the revision was never written - and the
    check that exists to catch a Python-only deploy stayed dormant forever
    while every run printed `none changed` and exited 0. The feature was
    silently OFF on the only machine it was written for.
    """

    def setUp(self):
        self.box = Sandbox()
        self.addCleanup(self.box.cleanup)
        # Deploy WITHOUT git, so no revision can be recorded, then make it a
        # checkout - exactly the shape of upgrading the installer in place.
        self.box.run()
        self.assertIsNone(self.box.recorded_revision())
        self.base = self.box.git_init()
        self.box.clear_log()

    def test_the_first_run_on_a_settled_host_records_a_baseline(self):
        proc = self.box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        # Nothing changed, so nothing was restarted…
        self.assertFalse([c for c in self.box.systemctl_calls()
                          if c.startswith("restart ")])
        # …and yet the baseline is now recorded, which is what arms the check.
        self.assertEqual(self.base, self.box.recorded_revision())

    def test_the_baseline_is_taken_out_loud(self):
        """Recording a revision nobody confirmed is an assumption, and a silent
        assumption is indistinguishable from a verified one."""
        proc = self.box.run()
        self.assertIn("no revision was recorded", proc.stderr)
        self.assertIn("--restart-on-code-change", proc.stderr)

    def test_the_check_is_armed_from_that_point_on(self):
        self.box.run()
        self.box.git_commit_python_only()
        proc = self.box.run()
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("has NOT taken effect", proc.stderr)

    def test_the_flag_can_seed_a_host_that_has_no_revision_recorded(self):
        """Ordered the other way, the empty-revision branch swallows the run
        and the flag's restart is unreachable - so the documented escape hatch
        does nothing on the host that needs it."""
        moved = self.box.git_commit_python_only()
        proc = self.box.run(args=["--restart-on-code-change"])
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("restart mqtt.service", self.box.systemctl_calls())
        self.assertEqual(moved, self.box.recorded_revision())


class NonGitCheckoutTests(unittest.TestCase):

    def test_a_checkout_with_no_git_metadata_skips_the_check(self):
        """Fail-open, and deliberately: an unversioned copy is not a reason to
        refuse a deploy. It has to SAY it skipped, though - a silent skip is
        indistinguishable from a clean result."""
        box = Sandbox()
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("cannot tell whether the code moved", proc.stderr)
        self.assertIsNone(box.recorded_revision())

    def test_a_repo_nested_inside_another_checkout_is_not_read(self):
        """`git rev-parse` walks UP. Reading an enclosing repository's HEAD
        would make the advisory fire forever on a revision that has nothing to
        do with the deployed code."""
        box = Sandbox()
        self.addCleanup(box.cleanup)
        for cmd in (["git", "init", "-q"],
                    ["git", "add", "-A"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                     "commit", "-qm", "outer"]):
            subprocess.run(cmd, cwd=box.root, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        proc = box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("cannot tell whether the code moved", proc.stderr)
        self.assertIsNone(box.recorded_revision())


FAKE_TRUE = """#!/bin/bash
exit 0
"""


class SetupScriptEndToEndTests(unittest.TestCase):
    """Acceptance criterion 1, executed rather than grepped.

    The text assertions in SetupScriptTests are necessary but not sufficient:
    they pin the one shape the old bug had. A setup.sh that wrote to the
    tracked unit by any other route - a variable, a glob, a sed -i - would
    satisfy every one of them. So run the real script against a real git
    checkout and read `git status`.

    Every privileged command is neutered: `sudo` is a logging no-op, so nothing
    setup.sh does with root escapes the sandbox. That also means the installer
    cannot actually install, which is why the run is EXPECTED to exit non-zero.
    That is not incidental - it is the propagation check, and it is the mutant
    (`return 1` -> `return 0`) the text tests cannot kill.
    """

    FAKES = ("apt", "apt-get", "i2cdetect", "raspi-config", "lsmod",
             "modprobe", "getent", "usermod", "python3", "pip3", "systemctl")

    def _checkout(self):
        root = tempfile.mkdtemp(prefix="t477-e2e-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        repo = os.path.join(root, "repo")
        os.makedirs(repo)

        listing = subprocess.run(["git", "ls-files", "-z"], cwd=REPO,
                                 stdout=subprocess.PIPE, check=True)
        for rel in listing.stdout.decode().split("\0"):
            if not rel:
                continue
            dst = os.path.join(repo, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(os.path.join(REPO, rel), dst)

        for cmd in (["git", "init", "-q"],
                    ["git", "add", "-A"],
                    ["git", "-c", "user.email=t@t", "-c", "user.name=t",
                     "commit", "-qm", "baseline"]):
            subprocess.run(cmd, cwd=repo, check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                               stdout=subprocess.PIPE, text=True, check=True)
        self.assertEqual("", dirty.stdout, "baseline checkout is not clean")

        fakebin = os.path.join(root, "fakebin")
        os.makedirs(fakebin)
        Sandbox._write_exec(os.path.join(fakebin, "sudo"), FAKE_NOOP_SUDO)
        for name in self.FAKES:
            Sandbox._write_exec(os.path.join(fakebin, name), FAKE_TRUE)
        return root, repo, fakebin

    def _run_setup(self, root, repo, fakebin):
        env = dict(os.environ)
        env["PATH"] = fakebin + os.pathsep + env.get("PATH", "")
        # A bad INSTALL_DIR would send setup.sh's `cd` to $HOME; keep $HOME
        # inside the sandbox so even that failure cannot escape.
        env["HOME"] = root
        env["SUDO_LOG"] = os.path.join(root, "sudo.log")
        env["SYSTEMD_UNIT_DIR"] = root
        return subprocess.run(["bash", os.path.join(repo, "bin", "setup.sh")],
                              cwd=repo, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=120)

    @staticmethod
    def _status(repo):
        return subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                              stdout=subprocess.PIPE, text=True,
                              check=True).stdout

    def setUp(self):
        for tool in ("readlink -f /", "realpath /"):
            if subprocess.run(["bash", "-c", f"{tool} >/dev/null 2>&1"]).returncode:
                self.skipTest(f"`{tool}` unavailable; setup.sh needs it")

    def test_a_setup_run_leaves_the_working_tree_clean(self):
        root, repo, fakebin = self._checkout()
        self._run_setup(root, repo, fakebin)
        self.assertEqual("", self._status(repo),
                         "setup.sh dirtied the working tree")

    def test_setup_exits_non_zero_when_the_unit_install_fails(self):
        root, repo, fakebin = self._checkout()
        proc = self._run_setup(root, repo, fakebin)
        # `sudo` is inert, so nothing reaches the unit directory and the real
        # units name /home/gardyn paths this machine does not have. Either way
        # the installer fails, and what is under test is that setup.sh says so
        # in its own voice rather than exiting 0.
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("systemd unit installation failed", proc.stderr)

    def test_setup_exits_zero_when_the_unit_install_succeeds(self):
        """The inverse, so the check above cannot pass by always failing."""
        root, repo, fakebin = self._checkout()
        Sandbox._write_exec(os.path.join(repo, "bin",
                                         "install-systemd-units.sh"), FAKE_TRUE)
        proc = self._run_setup(root, repo, fakebin)
        self.assertEqual(0, proc.returncode, proc.stderr[-2000:])

    def test_setup_propagates_an_installer_failure(self):
        root, repo, fakebin = self._checkout()
        Sandbox._write_exec(os.path.join(repo, "bin",
                                         "install-systemd-units.sh"),
                            "#!/bin/bash\nexit 7\n")
        proc = self._run_setup(root, repo, fakebin)
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("systemd unit installation failed", proc.stderr)


if __name__ == "__main__":
    unittest.main()
