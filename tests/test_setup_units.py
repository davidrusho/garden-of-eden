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

    def __init__(self, units=None, runnable=True):
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
        # `|| exit 1`, not a bare call. The call is currently the last line, so
        # a bare call would make setup.sh's exit status correct by position
        # alone and silently wrong the moment anything is appended.
        self.assertRegex(self.text, r"(?m)^install_systemd_units \|\| exit 1\s*$")

    def test_the_installer_uses_escape_sequences_bash_3_2_understands(self):
        """macOS ships bash 3.2 as /bin/bash, and the installer runs under its
        own shebang - where `echo -e "\\e["` emits the literal characters."""
        text = read(INSTALLER).decode()
        self.assertNotIn(r"\e[", text)
        self.assertIn(r"\033[", text)

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


FAKE_NOOP_SUDO = """#!/bin/bash
# A LOGGING NO-OP, not `exec "$@"`. setup.sh runs `sudo sed -i /boot/config.txt`,
# `sudo tee -a /etc/modules` and `sudo ln -fs ... /usr/local/bin/light`; under
# `exec` those would take effect on the machine running the tests.
printf '%s\\n' "$*" >> "$SUDO_LOG"
exit 0
"""

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
