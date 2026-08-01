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

`sudo` is faked as `exec "$@"` and `install` is NOT faked, so the tests exercise
the real `install -m 0644` invocation the Pi will run.
"""
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

FAKE_SUDO = """#!/bin/bash
exec "$@"
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
    """A disposable repo layout the installer can be run against for real."""

    def __init__(self, units=None):
        self.root = tempfile.mkdtemp(prefix="t477-")
        self.repo = os.path.join(self.root, "repo")
        self.src = os.path.join(self.repo, "services", "etc", "systemd", "system")
        self.dest = os.path.join(self.root, "etc")
        self.fakebin = os.path.join(self.root, "fakebin")
        self.log = os.path.join(self.root, "systemctl.log")

        os.makedirs(os.path.join(self.repo, "bin"))
        os.makedirs(self.src)
        os.makedirs(self.dest)
        os.makedirs(self.fakebin)

        shutil.copy(INSTALLER, os.path.join(self.repo, "bin",
                                            "install-systemd-units.sh"))

        if units is None:
            for name in os.listdir(UNIT_SRC):
                shutil.copy(os.path.join(UNIT_SRC, name),
                            os.path.join(self.src, name))
        else:
            for name, content in units.items():
                with open(os.path.join(self.src, name), "w") as fh:
                    fh.write(content)

        self._write_exec(os.path.join(self.fakebin, "sudo"), FAKE_SUDO)
        self._write_exec(os.path.join(self.fakebin, "systemctl"), FAKE_SYSTEMCTL)

    @staticmethod
    def _write_exec(path, content):
        with open(path, "w") as fh:
            fh.write(content)
        os.chmod(path, 0o755)

    def run(self, fail_on="", dest=None):
        env = dict(os.environ)
        env["PATH"] = self.fakebin + os.pathsep + env.get("PATH", "")
        env["SYSTEMD_UNIT_DIR"] = dest if dest is not None else self.dest
        env["SYSTEMCTL_LOG"] = self.log
        env["SYSTEMCTL_FAIL_ON"] = fail_on
        return subprocess.run(
            ["bash", os.path.join(self.repo, "bin", "install-systemd-units.sh")],
            env=env, cwd=self.root, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True,
        )

    def systemctl_calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as fh:
            return [line.strip() for line in fh if line.strip()]

    def clear_log(self):
        if os.path.exists(self.log):
            os.remove(self.log)

    def src_path(self, name):
        return os.path.join(self.src, name)

    def dest_path(self, name):
        return os.path.join(self.dest, name)

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
        self.assertRegex(self.text, r"(?m)^install_systemd_units\s*$")

    def test_installer_script_is_executable(self):
        self.assertTrue(os.access(INSTALLER, os.X_OK))


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
        self.assertIn("no *.service or *.timer files", proc.stderr)
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

    def test_daemon_reload_failure_is_fatal(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        proc = box.run(fail_on="daemon-reload")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("daemon-reload failed", proc.stderr)
        # Nothing was enabled on the back of a reload that did not happen.
        self.assertFalse([c for c in box.systemctl_calls()
                          if c.startswith("enable ")])

    def test_enable_failure_is_fatal(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        proc = box.run(fail_on="enable mqtt.service")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("enable mqtt.service failed", proc.stderr)

    def test_restart_failure_is_fatal(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        proc = box.run(fail_on="restart mqtt.service")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("restart mqtt.service failed", proc.stderr)

    def test_start_failure_is_fatal(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        box.run()
        box.clear_log()
        proc = box.run(fail_on="start mqtt.service")
        self.assertNotEqual(0, proc.returncode)
        self.assertIn("start mqtt.service failed", proc.stderr)


class InstallerPreflightTests(unittest.TestCase):

    UNIT = ("[Unit]\nDescription=probe\n\n[Service]\nType=oneshot\n"
            "ExecStart=%s\n\n[Install]\nWantedBy=multi-user.target\n")

    def test_warns_when_an_execstart_path_does_not_exist(self):
        box = Sandbox(units={"probe.service":
                             self.UNIT % "/nowhere/at/all/probe.py"})
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("ExecStart path does not exist", proc.stderr)
        self.assertIn("/nowhere/at/all/probe.py", proc.stderr)

    def test_no_warning_when_the_execstart_path_exists(self):
        box = Sandbox(units={"probe.service": self.UNIT % "/bin/sh"})
        self.addCleanup(box.cleanup)
        proc = box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertNotIn("ExecStart path does not exist", proc.stderr)

    def test_non_unit_files_are_reported_and_not_installed(self):
        box = Sandbox()
        self.addCleanup(box.cleanup)
        with open(os.path.join(box.src, "README.md"), "w") as fh:
            fh.write("not a unit\n")
        proc = box.run()
        self.assertEqual(0, proc.returncode, proc.stderr)
        self.assertIn("README.md", proc.stderr)
        self.assertFalse(os.path.exists(box.dest_path("README.md")))


if __name__ == "__main__":
    unittest.main()
