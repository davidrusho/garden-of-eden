"""Tests for the deploy path: bin/deploy.sh and bin/verify-deployed-artifacts.sh.

Why this file exists. `git status` on the Gardyn Pi describes the checkout's own
pointer and nothing else. The systemd units live under /etc/systemd/system as
COPIES, so they can be edited in place, half-written, masked, or left behind by
a rollback while git reports a clean tree - and T-485 spent a session disproving
a drift claim that git could neither confirm nor deny. The verifier answers that
question with a hash against the commit; deploy.sh makes it run on every deploy
rather than when somebody remembers.

The property that matters most is that the verifier can go RED. A verifier that
cannot report a mismatch is worse than no verifier, because it reads as
assurance. Every failure mode below is therefore asserted on its exit code AND
on it naming the artifact, and the three-valued exit status (0 match / 1 differs
/ 2 could not check) is asserted separately everywhere the distinction matters -
a caller that collapses "could not check" into "clean" is the whole bug.

Nothing here needs a Pi, and nothing here touches the real repository. Each test
builds a THROWAWAY git repository in a temp directory with the same layout, a
temp directory standing in for /etc/systemd/system, and copies the two scripts
under test into it. That is deliberate rather than convenient: the scripts run
`git reset --hard` and `git merge`, and proving a bug in those by running them
against the checkout you are sitting in is how T-440 put 21 junk commits on a
live branch. Every git invocation also runs with the GIT_* namespace scrubbed,
because GIT_DIR and GIT_INDEX_FILE override `git -C` and would redirect a
fixture's writes into whatever repository the test runner was launched from.

The fakes reproduce what the real tools do ON FAILURE, observed first rather
than invented. Measured against bin/install-systemd-units.sh with an empty unit
source directory: rc 1, EMPTY stdout, one line on stderr. A double whose error
branch is a bare `exit 1` with no output hides exactly the bugs that live in
"the caller read stdout and found nothing".
"""
# Reviewed: 2026-08-02 against 9286e18 (T-493)
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VERIFIER_SRC = os.path.join(REPO, "bin", "verify-deployed-artifacts.sh")
DEPLOY_SRC = os.path.join(REPO, "bin", "deploy.sh")

# The three-valued contract, spelled out so a test reads as intent rather than
# as a magic number.
OK = 0
MISMATCH = 1
CANNOT_CHECK = 2

# Observed shape of bin/install-systemd-units.sh failing: rc 1, nothing on
# stdout, one line on stderr. Logs its argv so a test can assert what was
# forwarded, and what was NOT.
FAKE_TOOL = """#!/bin/bash
printf '%s\\n' "$*" >> "$TOOL_LOG"
rc="${TOOL_RC:-0}"
if [ "$rc" -ne 0 ]; then
    echo "fake tool failed" >&2
fi
exit "$rc"
"""

# A `sudo` that refuses rather than runs. The verifier is supposed to be
# unprivileged; if it ever reaches for sudo the test should fail loudly instead
# of silently executing something as the test runner.
FAKE_SUDO_TRIPWIRE = """#!/bin/bash
printf '%s\\n' "$*" >> "$SUDO_LOG"
echo "sudo: refused by the test tripwire" >&2
exit 99
"""


def clean_env(**extra):
    """An environment in which git cannot reach the real repository.

    GIT_DIR, GIT_INDEX_FILE and friends take precedence over `git -C`, so a
    stray one in the parent environment - the pre-commit hook exports several -
    would send every fixture commit into whatever repository launched the tests.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env["GIT_AUTHOR_NAME"] = "test"
    env["GIT_AUTHOR_EMAIL"] = "test@example.invalid"
    env["GIT_COMMITTER_NAME"] = "test"
    env["GIT_COMMITTER_EMAIL"] = "test@example.invalid"
    env.update(extra)
    return env


def git(repo, *args, env=None, check=True):
    proc = subprocess.run(
        ["git", "-C", repo, *args],
        capture_output=True, text=True, env=env or clean_env(),
    )
    if check and proc.returncode != 0:
        raise AssertionError(
            "git %s failed rc=%d\nstdout=%s\nstderr=%s"
            % (" ".join(args), proc.returncode, proc.stdout, proc.stderr)
        )
    return proc


UNIT_A = """[Unit]
Description=Fixture unit A

[Service]
ExecStart=/bin/true

[Install]
WantedBy=multi-user.target
"""

UNIT_TIMER = """[Unit]
Description=Fixture timer

[Timer]
OnCalendar=*:0/5

[Install]
WantedBy=timers.target
"""

NETWATCH_SERVICE = """[Unit]
Description=Fixture watchdog

[Service]
ExecStart=/bin/true
"""


class DeployVerifyBase(unittest.TestCase):
    """A throwaway repository, a fake unit directory, and the real scripts."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gardyn-t493-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

        self.repo = os.path.join(self.tmp, "checkout")
        self.unit_dir = os.path.join(self.tmp, "systemd")
        self.logs = os.path.join(self.tmp, "logs")
        os.makedirs(self.unit_dir)
        os.makedirs(self.logs)

        self.src_dir = os.path.join(self.repo, "services", "etc", "systemd", "system")
        os.makedirs(os.path.join(self.repo, "bin"))
        os.makedirs(self.src_dir)

        self.write(os.path.join(self.repo, "app.py"), "print('hello')\n")
        self.write(os.path.join(self.src_dir, "fixture-a.service"), UNIT_A)
        self.write(os.path.join(self.src_dir, "fixture.timer"), UNIT_TIMER)
        self.write(os.path.join(self.src_dir, "gardyn-netwatch.service"), NETWATCH_SERVICE)

        for src in (VERIFIER_SRC, DEPLOY_SRC):
            dst = os.path.join(self.repo, "bin", os.path.basename(src))
            shutil.copy2(src, dst)
            os.chmod(dst, 0o755)

        git(self.repo, "init", "-q", "-b", "main")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "fixture base")
        self.base_rev = git(self.repo, "rev-parse", "HEAD").stdout.strip()

        self.verifier = os.path.join(self.repo, "bin", "verify-deployed-artifacts.sh")
        self.deploy = os.path.join(self.repo, "bin", "deploy.sh")

        self.tool_log = os.path.join(self.logs, "tool.log")
        self.sudo_log = os.path.join(self.logs, "sudo.log")

    # ---- helpers ----------------------------------------------------------
    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as fh:
            fh.write(text)

    def read(self, path):
        with open(path) as fh:
            return fh.read()

    def script(self, name, body, **env_defaults):
        path = os.path.join(self.logs, name)
        self.write(path, body)
        os.chmod(path, 0o755)
        return path

    def deploy_units(self, rev=None):
        """Install the unit files the way the real installer would: by copying."""
        for name in os.listdir(self.src_dir):
            shutil.copyfile(
                os.path.join(self.src_dir, name), os.path.join(self.unit_dir, name)
            )

    def run_verifier(self, *args, env=None, cwd=None):
        e = clean_env(SYSTEMD_UNIT_DIR=self.unit_dir)
        if env:
            e.update(env)
        return subprocess.run(
            [self.verifier, *args], capture_output=True, text=True,
            env=e, cwd=cwd or self.tmp,
        )

    def run_deploy(self, *args, env=None):
        e = clean_env(SYSTEMD_UNIT_DIR=self.unit_dir, TOOL_LOG=self.tool_log)
        if env:
            e.update(env)
        return subprocess.run(
            [self.deploy, *args], capture_output=True, text=True, env=e, cwd=self.tmp,
        )

    def all_output(self, proc):
        return proc.stdout + proc.stderr

    def make_origin(self):
        """A real upstream, so the fast-forward path is exercised for real."""
        origin = os.path.join(self.tmp, "origin.git")
        git(self.repo, "clone", "-q", "--bare", self.repo, origin)
        git(self.repo, "remote", "add", "origin", origin)
        git(self.repo, "fetch", "-q", "origin")
        git(self.repo, "branch", "--set-upstream-to=origin/main", "main")
        return origin

    def advance_origin(self, path, text, message="upstream change"):
        """Commit a change on a clone and push it, so origin/main moves ahead."""
        work = tempfile.mkdtemp(prefix="gardyn-t493-up-", dir=self.tmp)
        origin = os.path.join(self.tmp, "origin.git")
        git(work, "clone", "-q", origin, "wc")
        wc = os.path.join(work, "wc")
        self.write(os.path.join(wc, path), text)
        git(wc, "add", "-A")
        git(wc, "commit", "-q", "-m", message)
        git(wc, "push", "-q", "origin", "HEAD:main")
        return git(wc, "rev-parse", "HEAD").stdout.strip()


class VerifierMatchTests(DeployVerifyBase):
    """The GREEN case, and the counts that prove both halves were exercised."""

    def test_all_artifacts_matching_exits_zero(self):
        self.deploy_units()
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, OK, self.all_output(proc))

    def test_reports_both_class_counts_separately(self):
        # A single total would let one class silently go unchecked behind the
        # other class's count - the failure this script exists to remove.
        self.deploy_units()
        proc = self.run_verifier()
        out = self.all_output(proc)
        self.assertIn("tracked files", out)
        self.assertIn("3 installed units", out, out)

    def test_verifier_never_invokes_sudo(self):
        # `--check` is documented as safe to run on the live host at any time.
        # That claim is only worth anything if it is pinned.
        self.deploy_units()
        sudo = self.script("sudo", FAKE_SUDO_TRIPWIRE)
        bindir = os.path.dirname(sudo)
        proc = self.run_verifier(env={
            "PATH": bindir + os.pathsep + os.environ["PATH"],
            "SUDO_LOG": self.sudo_log,
        })
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertFalse(os.path.exists(self.sudo_log),
                         "the verifier invoked sudo: " + self.all_output(proc))

    def test_verifier_writes_nothing_to_the_unit_directory(self):
        self.deploy_units()
        before = {
            n: (os.stat(os.path.join(self.unit_dir, n)).st_mtime,
                self.read(os.path.join(self.unit_dir, n)))
            for n in os.listdir(self.unit_dir)
        }
        self.run_verifier()
        after = {
            n: (os.stat(os.path.join(self.unit_dir, n)).st_mtime,
                self.read(os.path.join(self.unit_dir, n)))
            for n in os.listdir(self.unit_dir)
        }
        self.assertEqual(before, after)


class VerifierMismatchTests(DeployVerifyBase):
    """RED. Each of these must exit 1 AND name the artifact."""

    def test_altered_deployed_unit_exits_one_and_names_it(self):
        self.deploy_units()
        with open(os.path.join(self.unit_dir, "fixture-a.service"), "a") as fh:
            fh.write("\n# edited in place on the host\n")
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, MISMATCH, self.all_output(proc))
        self.assertIn("fixture-a.service", self.all_output(proc))
        self.assertIn("DEPLOYED COPY DIFFERS", self.all_output(proc))

    def test_missing_deployed_unit_exits_one(self):
        self.deploy_units()
        os.remove(os.path.join(self.unit_dir, "fixture.timer"))
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, MISMATCH, self.all_output(proc))
        self.assertIn("not deployed", self.all_output(proc))

    def test_masked_unit_is_refused_rather_than_followed(self):
        # `systemctl mask` links a unit to /dev/null. Measured: `shasum -a 256`
        # on that link returns the empty-string digest with rc 0, so a masked
        # grow-light controller would otherwise read as a quiet PASS - and the
        # mask is exactly the state nobody would notice.
        self.deploy_units()
        dest = os.path.join(self.unit_dir, "fixture-a.service")
        os.remove(dest)
        os.symlink("/dev/null", dest)
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, MISMATCH, self.all_output(proc))
        self.assertIn("symlink", self.all_output(proc))
        self.assertNotIn(
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            self.all_output(proc),
            "the masked unit was hashed rather than refused",
        )

    def test_deployed_path_that_is_a_directory_is_refused(self):
        self.deploy_units()
        dest = os.path.join(self.unit_dir, "fixture-a.service")
        os.remove(dest)
        os.makedirs(dest)
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, MISMATCH, self.all_output(proc))
        self.assertIn("not a plain file", self.all_output(proc))

    def test_modified_tracked_file_in_the_checkout_exits_one(self):
        self.deploy_units()
        self.write(os.path.join(self.repo, "app.py"), "print('tampered')\n")
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, MISMATCH, self.all_output(proc))
        self.assertIn("app.py", self.all_output(proc))

    def test_deleted_tracked_file_in_the_checkout_exits_one(self):
        self.deploy_units()
        os.remove(os.path.join(self.repo, "app.py"))
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, MISMATCH, self.all_output(proc))
        self.assertIn("app.py", self.all_output(proc))

    def test_rev_flag_selects_the_revision_compared_against(self):
        # Advance the unit source by one commit and deploy the NEW copy, so the
        # host is genuinely current. The two revisions must then give opposite
        # answers, or the flag is decorative - and the older one has to name the
        # UNIT, which is what proves --rev reached the installed-copy comparison
        # rather than only the checkout diff.
        self.write(os.path.join(self.src_dir, "fixture-a.service"), UNIT_A + "# v2\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "unit v2")
        self.deploy_units()

        against_head = self.run_verifier()
        self.assertEqual(against_head.returncode, OK, self.all_output(against_head))

        against_base = self.run_verifier("--rev", self.base_rev)
        self.assertEqual(against_base.returncode, MISMATCH, self.all_output(against_base))
        self.assertIn("fixture-a.service: DEPLOYED COPY DIFFERS",
                      self.all_output(against_base))


class VerifierCannotCheckTests(DeployVerifyBase):
    """Exit 2. "Could not look" must never be reported as "found nothing wrong"."""

    def test_empty_unit_source_directory_refuses(self):
        self.deploy_units()
        for name in os.listdir(self.src_dir):
            os.remove(os.path.join(self.src_dir, name))
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "drop the units")
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("no unit files found", self.all_output(proc))

    def test_unit_source_dir_outside_the_checkout_refuses(self):
        # No revision holds those files, so there is nothing to compare against.
        # Fail closed rather than hand `git show` an absolute path.
        self.deploy_units()
        outside = os.path.join(self.tmp, "elsewhere")
        shutil.copytree(self.src_dir, outside)
        proc = self.run_verifier(env={"GARDYN_UNIT_SRC_DIR": outside})
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("outside the checkout", self.all_output(proc))

    def test_unit_absent_from_the_revision_refuses_rather_than_passing(self):
        # A unit file added but never committed has no blob at HEAD.
        # `git show HEAD:<path>` then prints nothing and exits 128, and piping
        # that into a hasher yields the SHA-256 of the empty string - which
        # compares equal to any other failed read. Exit 2, never 0.
        self.deploy_units()
        self.write(os.path.join(self.src_dir, "uncommitted.service"), UNIT_A)
        shutil.copyfile(
            os.path.join(self.src_dir, "uncommitted.service"),
            os.path.join(self.unit_dir, "uncommitted.service"),
        )
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("uncommitted.service", self.all_output(proc))

    def test_not_a_git_checkout_refuses(self):
        self.deploy_units()
        shutil.rmtree(os.path.join(self.repo, ".git"))
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("not the root of a git checkout", self.all_output(proc))

    def test_unresolvable_revision_refuses(self):
        self.deploy_units()
        proc = self.run_verifier("--rev", "no-such-ref")
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("cannot resolve revision", self.all_output(proc))

    def test_missing_systemd_unit_dir_refuses(self):
        proc = self.run_verifier(env={"SYSTEMD_UNIT_DIR": os.path.join(self.tmp, "nope")})
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("systemd unit directory not found", self.all_output(proc))

    def test_unknown_option_refuses(self):
        # A typo must not read as the flag being absent - `--quite` silently
        # doing nothing is harmless, but the same slip on `--rev` would verify
        # against the wrong commit and pass.
        self.deploy_units()
        proc = self.run_verifier("--quite")
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("unknown option", self.all_output(proc))

    def test_rev_flag_without_a_value_refuses(self):
        self.deploy_units()
        proc = self.run_verifier("--rev")
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))

    def test_no_hasher_on_path_refuses(self):
        # Neither sha256sum nor shasum is "cannot check", never "clean".
        self.deploy_units()
        stub = os.path.join(self.tmp, "nohash-bin")
        os.makedirs(stub)
        needed = ["git", "mktemp", "cat", "rm", "wc", "tr", "basename",
                  "dirname", "readlink", "bash", "sh", "env", "uname"]
        for name in needed:
            found = shutil.which(name)
            if found:
                os.symlink(found, os.path.join(stub, name))
        proc = self.run_verifier(env={"PATH": stub})
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("cannot verify anything", self.all_output(proc))


class VerifierAdvisoryTests(DeployVerifyBase):
    """The .gardyn-source-revision read, and the untracked-file signal."""

    def test_untracked_file_warns_but_does_not_fail(self):
        # The drift that started T-485 looked exactly like this - a script
        # hand-copied onto the Pi, showing as untracked. Worth saying; not worth
        # refusing a deploy over, since a stray log file produces the same shape.
        self.deploy_units()
        self.write(os.path.join(self.repo, "stray.txt"), "hand-copied\n")
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertIn("stray.txt", self.all_output(proc))
        self.assertIn("untracked", self.all_output(proc))

    def test_absent_source_revision_file_warns(self):
        self.deploy_units()
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertIn("does not exist", self.all_output(proc))

    def test_source_revision_matching_head_is_reported_ok(self):
        self.deploy_units()
        head = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.write(os.path.join(self.unit_dir, ".gardyn-source-revision"), head + "\n")
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertIn("which is this checkout's HEAD", self.all_output(proc))

    def test_source_revision_behind_head_warns_that_running_code_is_stale(self):
        # The installer records this only when it actually restarts
        # mqtt.service, so a mismatch means the running process predates the
        # code on disk. Reported, never written - that file has one writer.
        self.deploy_units()
        self.write(os.path.join(self.unit_dir, ".gardyn-source-revision"),
                   "0" * 40 + "\n")
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertIn("the RUNNING code is not the code on disk",
                      self.all_output(proc))

    def test_verifier_does_not_write_the_source_revision_file(self):
        self.deploy_units()
        path = os.path.join(self.unit_dir, ".gardyn-source-revision")
        self.run_verifier()
        self.assertFalse(os.path.exists(path),
                         "the verifier wrote a file whose single writer is the installer")

    def test_empty_source_revision_file_warns(self):
        self.deploy_units()
        self.write(os.path.join(self.unit_dir, ".gardyn-source-revision"), "")
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertIn("is empty", self.all_output(proc))


class DeploySequencingTests(DeployVerifyBase):
    """What deploy.sh runs, in what order, and what it runs anyway."""

    def setUp(self):
        super().setUp()
        self.deploy_units()
        self.fake_installer = self.script("fake-installer.sh", FAKE_TOOL)
        self.fake_verifier = self.script("fake-verifier.sh", FAKE_TOOL)

    def fakes(self, **extra):
        env = {
            "GARDYN_INSTALLER": self.fake_installer,
            "GARDYN_VERIFIER": self.fake_verifier,
            "TOOL_LOG": self.tool_log,
        }
        env.update(extra)
        return env

    def tool_calls(self):
        if not os.path.exists(self.tool_log):
            return []
        return [l for l in self.read(self.tool_log).splitlines()]

    def test_check_runs_the_verifier_and_not_the_installer(self):
        # Distinct log lines, so "the installer did not run" is provable rather
        # than inferred from a count.
        inst = self.script("inst-tagged.sh", """#!/bin/bash
printf '%s\\n' "installer $*" >> "$TOOL_LOG"
exit 0
""")
        ver = self.script("ver-tagged.sh", """#!/bin/bash
printf '%s\\n' "verifier $*" >> "$TOOL_LOG"
exit 0
""")
        proc = self.run_deploy("--check", env=self.fakes(
            GARDYN_INSTALLER=inst, GARDYN_VERIFIER=ver))
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertEqual([c.split()[0] for c in self.tool_calls()], ["verifier"],
                         self.tool_calls())

    def test_check_exit_status_is_the_verifiers(self):
        proc = self.run_deploy("--check", env=self.fakes(TOOL_RC="1"))
        self.assertEqual(proc.returncode, MISMATCH, self.all_output(proc))

    def test_check_does_not_pull(self):
        self.make_origin()
        self.advance_origin("upstream.txt", "new\n")
        head_before = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.run_deploy("--check", env=self.fakes())
        head_after = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        self.assertEqual(head_before, head_after)

    def test_normal_deploy_runs_installer_then_verifier(self):
        proc = self.run_deploy("--no-pull", env=self.fakes())
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        # pre-flight verify, install, post verify
        self.assertEqual(len(self.tool_calls()), 3, self.tool_calls())
        self.assertIn("--quiet", self.tool_calls()[0])

    def test_installer_failure_still_verifies_and_exits_one(self):
        # The single most important sequencing property. A failed install is
        # exactly the run whose aftermath nobody can describe - some units
        # copied, some not, the service maybe restarted - so withholding
        # verification there withholds the answer at the one moment it is
        # needed. Asserted on the ORDER, not just on the verifier having run.
        failing = self.script("always-fail.sh", """#!/bin/bash
printf '%s\\n' "installer $*" >> "$TOOL_LOG"
echo "fake installer failed" >&2
exit 1
""")
        passing = self.script("always-pass.sh", """#!/bin/bash
printf '%s\\n' "verifier $*" >> "$TOOL_LOG"
exit 0
""")
        proc = self.run_deploy("--no-pull", env=self.fakes(
            GARDYN_INSTALLER=failing, GARDYN_VERIFIER=passing))
        calls = self.tool_calls()
        self.assertEqual(proc.returncode, MISMATCH, self.all_output(proc))
        self.assertEqual(
            [c.split()[0] for c in calls], ["verifier", "installer", "verifier"], calls)

    def test_verifier_failure_after_a_clean_install_still_exits_one(self):
        passing = self.script("inst-ok.sh", """#!/bin/bash
printf '%s\\n' "installer $*" >> "$TOOL_LOG"
exit 0
""")
        # Passes pre-flight, fails the post-deploy check: the shape of a deploy
        # that installed something other than what the commit says.
        flipflop = self.script("verify-second-fails.sh", """#!/bin/bash
printf '%s\\n' "verifier $*" >> "$TOOL_LOG"
n=$(grep -c '^verifier' "$TOOL_LOG")
[ "$n" -ge 2 ] && exit 1
exit 0
""")
        proc = self.run_deploy("--no-pull", env=self.fakes(
            GARDYN_INSTALLER=passing, GARDYN_VERIFIER=flipflop))
        self.assertEqual(proc.returncode, MISMATCH, self.all_output(proc))
        self.assertIn("DEPLOY NOT VERIFIED", self.all_output(proc))
        self.assertIn("--rollback-to", self.all_output(proc))

    def test_installer_arguments_are_forwarded(self):
        proc = self.run_deploy("--no-pull", "--restart-on-code-change",
                               "--remove-retired", env=self.fakes())
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        joined = "\n".join(self.tool_calls())
        self.assertIn("--restart-on-code-change", joined)
        self.assertIn("--remove-retired", joined)

    def test_deploy_flags_are_not_forwarded_to_the_installer(self):
        # `--force` and `--check` mean nothing to the installer, which refuses
        # unknown options by design; forwarding them would break every deploy.
        proc = self.run_deploy("--no-pull", "--force", env=self.fakes())
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertNotIn("--force", "\n".join(self.tool_calls()))

    def test_unknown_option_refuses(self):
        proc = self.run_deploy("--no-pul", env=self.fakes())
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("unknown option", self.all_output(proc))
        self.assertEqual(self.tool_calls(), [])

    def test_check_and_rollback_together_are_refused(self):
        proc = self.run_deploy("--check", "--rollback-to", self.base_rev,
                               env=self.fakes())
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertEqual(self.tool_calls(), [])

    def test_missing_installer_refuses_before_doing_anything(self):
        proc = self.run_deploy("--no-pull", env=self.fakes(
            GARDYN_INSTALLER=os.path.join(self.tmp, "no-such-installer")))
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("installer not executable", self.all_output(proc))


class DeployPreflightTests(DeployVerifyBase):
    """Deploying on top of unknown drift makes the result unattributable."""

    def setUp(self):
        super().setUp()
        self.deploy_units()

    def test_preflight_failure_refuses_before_installing(self):
        failing_verifier = self.script("verify-fail.sh", """#!/bin/bash
printf '%s\\n' "verifier $*" >> "$TOOL_LOG"
exit 1
""")
        inst = self.script("inst.sh", """#!/bin/bash
printf '%s\\n' "installer $*" >> "$TOOL_LOG"
exit 0
""")
        proc = self.run_deploy("--no-pull", env={
            "GARDYN_VERIFIER": failing_verifier, "GARDYN_INSTALLER": inst,
            "TOOL_LOG": self.tool_log})
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        calls = [c.split()[0] for c in self.read(self.tool_log).splitlines()]
        self.assertEqual(calls, ["verifier"], calls)
        self.assertIn("--rollback-to", self.all_output(proc))

    def test_preflight_cannot_check_also_refuses(self):
        # Exit 2 from the verifier is not a milder version of exit 1.
        broken = self.script("verify-broken.sh", """#!/bin/bash
printf '%s\\n' "verifier $*" >> "$TOOL_LOG"
exit 2
""")
        inst = self.script("inst2.sh", """#!/bin/bash
printf '%s\\n' "installer $*" >> "$TOOL_LOG"
exit 0
""")
        proc = self.run_deploy("--no-pull", env={
            "GARDYN_VERIFIER": broken, "GARDYN_INSTALLER": inst,
            "TOOL_LOG": self.tool_log})
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertNotIn("installer", self.read(self.tool_log))

    def test_force_proceeds_past_a_failed_preflight(self):
        # Refusing forever would be its own trap: the fix for a drifted host
        # sometimes IS a deploy, and there is no console to fall back on.
        first_fails = self.script("verify-first-fails.sh", """#!/bin/bash
printf '%s\\n' "verifier $*" >> "$TOOL_LOG"
n=$(grep -c '^verifier' "$TOOL_LOG")
[ "$n" -eq 1 ] && exit 1
exit 0
""")
        inst = self.script("inst3.sh", """#!/bin/bash
printf '%s\\n' "installer $*" >> "$TOOL_LOG"
exit 0
""")
        proc = self.run_deploy("--no-pull", "--force", env={
            "GARDYN_VERIFIER": first_fails, "GARDYN_INSTALLER": inst,
            "TOOL_LOG": self.tool_log})
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertIn("installer", self.read(self.tool_log))
        self.assertIn("--force given, continuing anyway", self.all_output(proc))


class DeployPullTests(DeployVerifyBase):
    """Fast-forward only, and the gate in front of the reboot-capable unit."""

    def setUp(self):
        super().setUp()
        self.deploy_units()
        self.ok_tool = self.script("ok.sh", """#!/bin/bash
printf '%s\\n' "$*" >> "$TOOL_LOG"
exit 0
""")

    def fakes(self, **extra):
        env = {"GARDYN_INSTALLER": self.ok_tool, "GARDYN_VERIFIER": self.ok_tool,
               "TOOL_LOG": self.tool_log}
        env.update(extra)
        return env

    def test_no_upstream_refuses_and_names_the_deploy_branch(self):
        proc = self.run_deploy(env=self.fakes())
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("tracks no upstream branch", self.all_output(proc))
        self.assertIn("origin/deploy", self.all_output(proc))

    def test_fast_forward_moves_head_to_the_upstream(self):
        self.make_origin()
        target = self.advance_origin("upstream.txt", "new\n")
        proc = self.run_deploy(env=self.fakes())
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), target)

    def test_already_current_is_not_an_error(self):
        self.make_origin()
        proc = self.run_deploy(env=self.fakes())
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertIn("nothing to fast-forward", self.all_output(proc))

    def test_a_diverged_checkout_refuses_rather_than_merging(self):
        # A rollback deliberately diverges the checkout. Merging that would
        # invent a commit nobody promoted, and ship it to the plants.
        self.make_origin()
        self.advance_origin("upstream.txt", "new\n")
        self.write(os.path.join(self.repo, "local.txt"), "local\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "local divergence")
        proc = self.run_deploy(env=self.fakes())
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("cannot fast-forward", self.all_output(proc))

    def test_a_netwatch_change_is_refused_without_acknowledgement(self):
        # The one artifact whose bad version can take the host away for good.
        self.make_origin()
        self.advance_origin(
            "services/etc/systemd/system/gardyn-netwatch.service",
            NETWATCH_SERVICE + "# changed\n")
        head_before = git(self.repo, "rev-parse", "HEAD").stdout.strip()
        proc = self.run_deploy(env=self.fakes())
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("can REBOOT this host", self.all_output(proc))
        self.assertIn("systemctl disable --now gardyn-netwatch.timer",
                      self.all_output(proc))
        # Refused BEFORE the merge: the tree must be untouched.
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(),
                         head_before)

    def test_a_netwatch_change_proceeds_once_acknowledged(self):
        self.make_origin()
        target = self.advance_origin(
            "services/etc/systemd/system/gardyn-netwatch.service",
            NETWATCH_SERVICE + "# changed\n")
        proc = self.run_deploy("--netwatch-change-ok", env=self.fakes())
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), target)

    def test_a_netwatch_python_change_is_gated_too(self):
        # The unit file is only half of it: bin/gardyn-netwatch.py is what
        # actually decides to reboot.
        self.make_origin()
        self.advance_origin("bin/gardyn-netwatch.py", "print('watchdog')\n")
        proc = self.run_deploy(env=self.fakes())
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("gardyn-netwatch.py", self.all_output(proc))

    def test_an_unrelated_change_needs_no_acknowledgement(self):
        # The gate has to be narrow, or it becomes a flag people always pass.
        self.make_origin()
        target = self.advance_origin("app.py", "print('v2')\n")
        proc = self.run_deploy(env=self.fakes())
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), target)


class DeployRollbackTests(DeployVerifyBase):
    """The remote recovery path. There is no physical one."""

    def setUp(self):
        super().setUp()
        self.deploy_units()
        self.ok_tool = self.script("ok.sh", """#!/bin/bash
printf '%s\\n' "$*" >> "$TOOL_LOG"
exit 0
""")
        self.write(os.path.join(self.repo, "app.py"), "print('v2')\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "a change to roll back")
        self.newer = git(self.repo, "rev-parse", "HEAD").stdout.strip()

    def fakes(self, **extra):
        env = {"GARDYN_INSTALLER": self.ok_tool, "GARDYN_VERIFIER": self.ok_tool,
               "TOOL_LOG": self.tool_log}
        env.update(extra)
        return env

    def test_rollback_resets_head_to_the_named_commit(self):
        proc = self.run_deploy("--rollback-to", self.base_rev, env=self.fakes())
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(),
                         self.base_rev)
        self.assertEqual(self.read(os.path.join(self.repo, "app.py")),
                         "print('hello')\n")

    def test_rollback_forces_a_restart_even_though_no_unit_file_moved(self):
        # A rollback that leaves the old code running is not a rollback, and
        # rolling back Python alone changes no unit file - so nothing else in
        # the deploy would restart the service.
        self.run_deploy("--rollback-to", self.base_rev, env=self.fakes())
        self.assertIn("--restart-on-code-change", self.read(self.tool_log))

    def test_rollback_does_not_double_the_restart_flag(self):
        self.run_deploy("--rollback-to", self.base_rev, "--restart-on-code-change",
                        env=self.fakes())
        installer_line = [l for l in self.read(self.tool_log).splitlines()
                          if "--restart-on-code-change" in l][0]
        self.assertEqual(installer_line.count("--restart-on-code-change"), 1,
                         installer_line)

    def test_rollback_skips_the_preflight_verification(self):
        # Pre-flight refuses on drift, and drift is the reason to roll back.
        # Gating recovery on the check that recovery exists to clear would make
        # the host unrecoverable remotely - which is the only way left.
        failing = self.script("verify-fail.sh", """#!/bin/bash
printf '%s\\n' "verifier $*" >> "$TOOL_LOG"
n=$(grep -c '^verifier' "$TOOL_LOG")
[ "$n" -eq 1 ] && exit 1
exit 0
""")
        inst = self.script("inst.sh", """#!/bin/bash
printf '%s\\n' "installer $*" >> "$TOOL_LOG"
exit 0
""")
        proc = self.run_deploy("--rollback-to", self.base_rev, env={
            "GARDYN_VERIFIER": failing, "GARDYN_INSTALLER": inst,
            "TOOL_LOG": self.tool_log})
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(),
                         self.base_rev)
        self.assertIn("installer", self.read(self.tool_log))

    def test_rollback_refuses_to_discard_local_modifications(self):
        self.write(os.path.join(self.repo, "app.py"), "print('unsaved work')\n")
        proc = self.run_deploy("--rollback-to", self.base_rev, env=self.fakes())
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("app.py", self.all_output(proc))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(),
                         self.newer)
        self.assertEqual(self.read(os.path.join(self.repo, "app.py")),
                         "print('unsaved work')\n")

    def test_rollback_with_force_discards_them(self):
        self.write(os.path.join(self.repo, "app.py"), "print('unsaved work')\n")
        proc = self.run_deploy("--rollback-to", self.base_rev, "--force",
                               env=self.fakes())
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(),
                         self.base_rev)

    def test_rollback_to_an_unknown_revision_refuses(self):
        proc = self.run_deploy("--rollback-to", "deadbeef", env=self.fakes())
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("cannot resolve", self.all_output(proc))
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(),
                         self.newer)

    def test_rollback_does_not_pull(self):
        # The objects are already local, so recovery does not depend on the
        # network being healthy - which after a bad deploy it may not be.
        self.make_origin()
        self.advance_origin("upstream.txt", "new\n")
        self.run_deploy("--rollback-to", self.base_rev, env=self.fakes())
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(),
                         self.base_rev)


class RealToolsEndToEndTests(DeployVerifyBase):
    """deploy.sh driving the REAL verifier, not a double.

    The fakes above pin sequencing and exit-code folding. This pins that the two
    real scripts actually agree with each other - a contract no amount of
    double-driven testing can establish.
    """

    def setUp(self):
        super().setUp()
        self.deploy_units()
        self.ok_installer = self.script("inst.sh", """#!/bin/bash
printf '%s\\n' "installer $*" >> "$TOOL_LOG"
exit 0
""")

    def test_real_verifier_green_end_to_end(self):
        proc = self.run_deploy("--no-pull", env={
            "GARDYN_INSTALLER": self.ok_installer, "TOOL_LOG": self.tool_log})
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertIn("deploy complete and verified", self.all_output(proc))

    def test_real_verifier_red_end_to_end_stops_the_deploy(self):
        with open(os.path.join(self.unit_dir, "fixture-a.service"), "a") as fh:
            fh.write("\n# drifted\n")
        proc = self.run_deploy("--no-pull", env={
            "GARDYN_INSTALLER": self.ok_installer, "TOOL_LOG": self.tool_log})
        self.assertEqual(proc.returncode, CANNOT_CHECK, self.all_output(proc))
        self.assertIn("fixture-a.service", self.all_output(proc))
        # Refused at pre-flight, so the installer never ran.
        self.assertFalse(os.path.exists(self.tool_log))

    def test_real_check_is_read_only_end_to_end(self):
        before = sorted(os.listdir(self.unit_dir))
        proc = self.run_deploy("--check", env={
            "GARDYN_INSTALLER": self.ok_installer, "TOOL_LOG": self.tool_log})
        self.assertEqual(proc.returncode, OK, self.all_output(proc))
        self.assertEqual(sorted(os.listdir(self.unit_dir)), before)
        self.assertFalse(os.path.exists(self.tool_log))


if __name__ == "__main__":
    unittest.main()
