"""Invariants of the test tooling itself: it must not contaminate what it
measures.

Two of them live here, because both fail silently and neither is visible from
inside the file that breaks it - a module leaking stubs into sys.modules for
everything discovered after it, and a mutation harness leaving a mutant applied
in the working tree when it is interrupted.

--- one: no test module may leave a stub behind in sys.modules ---

The defect this pins is invisible to every other file here, because it is a
property of the SUITE rather than of any one test: two modules stub the
hardware and broker packages so that `import mqtt` and `import light` can run
without a Pi, and both used to leave those stubs installed forever.

`python -m unittest` - the invocation the README documents - discovers modules
in alphabetical order and imports all of them before running any test, so the
stubs reached every module that sorts later. tests/test_camera_quality.py
("c") pulls in tests/test_water_interlock.py at module scope, ahead of
test_distance, test_light and test_pump. Measured on the tree before this file
existed: running the whole suite produced 7 failures and 16 errors, against 4
honest ModuleNotFoundErrors when each module was run on its own. Nothing was
wrong with the code under test - `test_distance`'s one missing-dependency
import error had become six `InvalidSpecError: Cannot autospec attr
'DistanceSensor' ... as it has already been mocked out`, which reads as a
broken driver rather than as another file's doing.

Two independent checks, because each catches something the other cannot:

  * the leak itself, by importing a module in a clean interpreter and diffing
    sys.modules. This one names the offending module.
  * the SYMPTOM, by running a victim module with and without the stubbing
    module ahead of it and requiring identical output. This one keeps holding
    if a future file leaks by some route the roots list below does not know
    about.

--- two: a mutation harness must restore the tree even when interrupted ---

Each tests/mutate_*.py writes a mutant into a shipping file, runs the suites,
and writes the original back. Interrupt it between those two steps - ^C, a
timeout, an exception in the harness itself - and without a try/finally the
mutant stays. It is a silent, plausible-looking change to code that runs the
garden; one such interruption on 2026-08-01 left bin/setup.sh with its
installer call commented out. The check below drives each harness with its
suite runner replaced by one that raises at the exact moment a mutant is on
disk, and requires the file to come back byte-identical.

Run:  python3 -m unittest tests.test_suite_isolation
"""
import json
import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The packages a stub could plausibly displace: the hardware and broker
# libraries, this project's own top-level modules, and dotenv, which two files
# neutralise while loading config.py from disk.
ROOTS = ("gpiozero", "paho", "pigpio", "app", "config", "mqtt", "dotenv")

# Every test module that installs stubs at IMPORT time, directly or by
# importing a module that does. The list is checked for completeness by
# test_every_sys_modules_writer_is_listed below - an aspirational comment is
# not a guard, and a new stubbing module that nobody lists is exactly the
# regression this file exists to catch.
#
# What this cannot see: a module that writes sys.modules only while a TEST
# runs. tests/test_camera_quality.py and tests/test_water_interlock.py both do
# that when they load config.py from disk, and both restore in a `finally`;
# the probe below imports and does not run, so it scores their import scope
# only. They stay listed because their import pulls in the stub apparatus.
STUBBING_MODULES = (
    "tests.test_water_interlock",
    "tests.test_camera_quality",
    "tests.test_light_logging",
    "tests.test_retired_entities",
    "tests.test_pump_api_interlock",
    # Imports the stub apparatus transitively, via test_water_interlock (for
    # mqtt_mod) and test_retired_entities (for RecordingClient), rather than
    # installing any stub of its own (T-527.1).
    "tests.test_ha_birth_message",
    # Same transitive route, same two sources, no stub of its own (T-527.11).
    "tests.test_connack_refusal",
)

# Names that may legitimately appear in sys.modules after an import, outside
# the roots above: the stdlib. Anything else is something the import pulled in
# and left behind.
_LEAK_PROBE = r"""
import contextlib, io, json, sys, importlib
roots = tuple(json.loads(sys.argv[2]))
def owned(name):
    return name.split(".")[0] in roots
before_owned = {n: m for n, m in sys.modules.items() if owned(n)}
before_all = set(sys.modules)
# The import's OWN stdout is swallowed, because this probe's result is parsed
# as JSON from stdout and several drivers print at import time -- pump.py says
# "Setting pump frequency to 50", the temperature and humidity drivers announce
# that they could not initialise. Any one of them turns a correct probe run
# into a JSONDecodeError that reads like a broken probe rather than a chatty
# module. Not reproducible without the real Flask installed, which is why it
# survived the suite that introduced this file.
with contextlib.redirect_stdout(io.StringIO()):
    importlib.import_module(sys.argv[1])
after_owned = {n: m for n, m in sys.modules.items() if owned(n)}


def _stub_shaped(m):
    # A hand-built types.ModuleType has neither; a real module or a namespace
    # package has at least one.
    return getattr(m, "__file__", None) is None and not hasattr(m, "__path__")


# Same reasoning as `foreign` below: a real module left behind is what an
# ordinary import does. `config` is a root and it imports python-dotenv, so a
# suite that loads the real config necessarily registers real dotenv modules -
# flagging those says nothing about isolation.
added = sorted(n for n in after_owned
               if n not in before_owned and _stub_shaped(after_owned[n]))
# NOT filtered by shape: something standing where a real module used to be is
# the actual contamination, and it is just as bad when the replacement is
# itself real.
replaced = sorted(n for n in before_owned if after_owned.get(n) is not before_owned[n])
# Everything new, whatever its root - this is what catches a module the stub
# window created under a name the roots list has never heard of. The stdlib is
# expected; so are the test module itself and the package it lives in, which
# importing it necessarily registers.
def expected(name):
    if name.split(".")[0] in sys.stdlib_module_names:
        return True
    if name == sys.argv[1] or sys.argv[1].startswith(name + "."):
        return True
    return name == "tests" or name.startswith("tests.")

# A REAL library left in sys.modules is what every ordinary import does and
# harms nobody -- tests/test_pump_api_interlock.py exercises the real Flask
# app, so importing it necessarily registers flask, werkzeug, jinja2, click
# and the rest. What this file is actually guarding against is a STUB standing
# where a real module should be, and the two are cheaply distinguishable: a
# types.ModuleType built by hand has neither __file__ nor __path__, while a
# real module or namespace package has at least one. Requiring `foreign == []`
# instead would have forced a suite to either stub Flask (defeating its point)
# or be exempted from the check entirely.
def stub_shaped(name):
    m = sys.modules.get(name)
    return getattr(m, "__file__", None) is None and not hasattr(m, "__path__")

foreign = sorted(n for n in set(sys.modules) - before_all
                 if not expected(n) and stub_shaped(n))
print(json.dumps({"added": added, "replaced": replaced, "foreign": foreign}))
"""


def read_text(path):
    with open(path) as fh:
        return fh.read()


def _probe(module, extra_path=None):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    if extra_path:
        env["PYTHONPATH"] = extra_path + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        [sys.executable, "-c", _LEAK_PROBE, module, json.dumps(list(ROOTS))],
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env=env,
    )


# A module that leaks on purpose, for the positive control below. It reproduces
# both shapes: a name that was not there before, and a name that was.
_LEAKY = """
import sys, types
sys.modules["gpiozero"] = types.ModuleType("gpiozero")
sys.modules["config"] = types.ModuleType("config")
"""


class ModuleScopeLeakTests(unittest.TestCase):

    def test_aaa_the_probe_can_see_a_leak(self):
        """Positive control. Named to sort FIRST - unittest orders methods
        alphabetically, and `test_no_...` sorts before `test_the_...`, so the
        obvious name ran the control second and an interrupted run could report
        the absence-check as passed with the control never having executed.

        The check below reports an ABSENCE, so a probe that returns an empty
        delta because it never ran is indistinguishable from a clean result.
        Import a module written to leak and require the probe to say so; if
        this fails, no verdict in this class means anything.
        """
        import tempfile
        tmp = tempfile.mkdtemp(prefix="t491-leak-")
        self.addCleanup(__import__("shutil").rmtree, tmp, True)
        with open(os.path.join(tmp, "_leaky_for_control.py"), "w") as fh:
            fh.write(_LEAKY)
        proc = _probe("_leaky_for_control", extra_path=tmp)
        self.assertEqual(0, proc.returncode, proc.stderr)
        delta = json.loads(proc.stdout)
        self.assertIn("gpiozero", delta["added"])
        self.assertIn("config", delta["added"])
        # …and the root-agnostic half sees it too.
        self.assertIn("gpiozero", delta["foreign"])

    def test_no_stubbing_module_leaves_a_stub_installed(self):
        for module in STUBBING_MODULES:
            with self.subTest(module=module):
                proc = _probe(module)
                self.assertEqual(0, proc.returncode,
                                 f"{module} would not import:\n{proc.stderr}")
                delta = json.loads(proc.stdout)
                self.assertEqual(
                    {"added": [], "replaced": [], "foreign": []}, delta,
                    f"{module} left sys.modules changed. Every test module "
                    f"discovered after it inherits this.")

    def test_every_sys_modules_writer_is_listed(self):
        """STUBBING_MODULES is a hand-maintained list, so it decays. Without
        this, a new stubbing module that sorts early leaks to every module
        after it and the whole class stays green - measured: a planted
        tests/test_aaa_new_stubber.py left all of this passing."""
        writers = set()
        tests_dir = os.path.join(REPO, "tests")
        for name in sorted(os.listdir(tests_dir)):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            if name == os.path.basename(__file__):
                continue
            with open(os.path.join(tests_dir, name)) as fh:
                text = fh.read()
            # The transitive clause used to name ONE module:
            # "from tests.test_water_interlock import". That was the only
            # inheritance route in the tree when it was written, and it went
            # stale silently the moment a second stubbing module was imported
            # from - which T-527.1 is the first change to do
            # (tests/test_ha_birth_message.py imports RecordingClient from
            # tests.test_retired_entities). Demonstrated during review: a module
            # importing only from test_retired_entities passed this check while
            # unlisted, so nothing ever probed it for leaks.
            #
            # Matched against the LIST rather than one hardcoded name, so a new
            # stubbing module is covered by adding it to STUBBING_MODULES - the
            # one place that already has to be edited - instead of needing this
            # regex widened too. A guard whose completeness depends on somebody
            # remembering to update the guard is not a completeness guard.
            inherits = any(f"from {mod} import" in text
                           for mod in STUBBING_MODULES)
            if ("sys.modules[" in text or "sys.modules.pop" in text
                    or "sys.modules.setdefault" in text
                    or inherits):
                writers.add("tests." + name[:-3])
        self.assertEqual(
            writers, set(STUBBING_MODULES),
            "a test module writes sys.modules (or imports one that does) "
            "without being listed in STUBBING_MODULES, so nothing here checks "
            "it")


class DiscoveryOrderTests(unittest.TestCase):
    """The symptom, measured rather than reasoned about.

    A victim module's result must not depend on whether a stubbing module was
    imported first.

    KNOW WHAT THIS CAN AND CANNOT CATCH. The signature it compares is built
    from the failure lines and the final verdict, so it only sees contamination
    that changes a pass/fail OUTCOME. Off the Pi that is powerful - the victims
    flip from one loader ModuleNotFoundError to six InvalidSpecErrors, which is
    what the pre-fix tree did. On the Pi, where the victims pass either way, it
    compares OK to OK and is a weak backstop; there ModuleScopeLeakTests above
    is the load-bearing half. Kept because it is the check that keeps holding
    if a future module leaks by a route the roots list has never heard of.
    """

    VICTIMS = ("tests.test_distance", "tests.test_pump", "tests.test_light")

    def _run(self, *modules):
        proc = subprocess.run(
            [sys.executable, "-m", "unittest"] + list(modules),
            cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
        # The last line is the verdict; the body carries the failure shape.
        return proc.returncode, proc.stdout

    @staticmethod
    def _signature(output):
        """The exception types and the final verdict, with the run's own
        counts and timings dropped - those differ because the combined run
        executes more tests, and that is not what is under test."""
        keep = []
        for line in output.splitlines():
            line = line.strip()
            if line.startswith(("ERROR:", "FAIL:")) or "Error:" in line:
                keep.append(line)
            elif line.startswith(("OK", "FAILED")):
                keep.append(line)
        return keep

    def test_a_victims_result_is_the_same_with_the_stubbing_module_ahead_of_it(self):
        for victim in self.VICTIMS:
            with self.subTest(victim=victim):
                rc_alone, out_alone = self._run(victim)
                rc_after, out_after = self._run("tests.test_camera_quality",
                                                victim)
                sig_alone = self._signature(out_alone)
                sig_after = [line for line in self._signature(out_after)
                             if "test_camera_quality" not in line]
                self.assertEqual(
                    sig_alone, sig_after,
                    f"{victim} behaves differently once "
                    f"tests.test_camera_quality has been imported.")
                self.assertEqual(rc_alone, rc_after)


_RESTORE_PROBE = r"""
import hashlib, importlib.util, json, os, stat, sys

repo, harness, targets, mode = (sys.argv[1], sys.argv[2],
                                json.loads(sys.argv[3]), sys.argv[4])
sys.path.insert(0, repo)

spec = importlib.util.spec_from_file_location(
    "_harness_under_test", os.path.join(repo, "tests", harness))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

# Content AND mode. A harness that recreates a deleted file with
# write()/copyfile() gives it whatever the umask allows, and a content-only
# comparison calls that restored.
def fingerprint(p):
    try:
        with open(p, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return "<missing>"
    return f"{digest}:{oct(stat.S_IMODE(os.stat(p).st_mode))}"

runner = "run_suites" if hasattr(mod, "run_suites") else "run_suite"
# ASSERT THE NAME EXISTS. `setattr` below happily creates a new attribute on a
# harness that spells its runner differently, and the harness then runs its
# REAL battery against the live tree while this probe waits for an interrupt
# that can never arrive. mutate_deploy_verify.py defines `run_one` and is one
# such harness today; it is SANDBOXED so nothing drives it yet, which is
# exactly the kind of latent trap that fires the first time somebody widens
# this probe's coverage. Found by the review of 2a8c951.
if not hasattr(mod, runner):
    print("VERDICT " + json.dumps({
        "error": f"{harness} has no {runner}() - this probe would have "
                 f"created one and let the harness run its real battery"}))
    raise SystemExit(0)
before = {p: fingerprint(p) for p in targets}
state = {"n": 0, "at_interrupt": None}

# The controls' canned output MUST LOOK LIKE unittest, not like a sentinel.
# This double used to return the bare string "control", which was fine until
# the harnesses learned to read the ran-count (T-527.18): a "green" run
# reporting zero collected tests is itself a NO DATA condition, so the harness
# correctly aborted at control A and main() returned before the interrupt could
# be injected. The double was written from the happy path and modelled the
# COLOUR of a run without its CONTENT.
_GREEN = "Ran 40 tests in 0.100s\n\nOK\n"
_RED = ("FAIL: test_x (tests.test_x.X.test_x)\nAssertionError: 1 != 0\n"
        "Ran 40 tests in 0.100s\n\nFAILED (failures=1)\n")
# Control C's shape: compiles, dies at IMPORT. unittest reports it as a named
# ERROR through _FailedTest, and the collapsed ran-count is the only tell.
_IMPORT_DEATH = ("ERROR: test_x (unittest.loader._FailedTest.test_x)\n"
                 "ModuleNotFoundError: No module named 'nope'\n"
                 "Ran 1 test in 0.000s\n\nFAILED (errors=1)\n")
n_controls = 3 if hasattr(mod, "CONTROL_C") else 2

def fake(*a, **k):
    state["n"] += 1
    # Call 1 is control A (clean, must look GREEN), call 2 is control B (broken,
    # must look RED), and call 3 — where the harness has one — is control C (an
    # import-time death, must classify as NO VERDICT). Interrupting before those
    # pass would abort the harness through its own guard rather than mid-mutant.
    if state["n"] <= n_controls:
        if state["n"] == 1:
            return True, _GREEN
        if state["n"] == 2:
            return False, _RED
        return False, _IMPORT_DEATH
    now = {p: fingerprint(p) for p in targets}
    if mode == "delete":
        # Wait for the DELETE mutant, whose restore is a different code path
        # (move to a stash, copy back) from the text mutants' write().
        if not any(v == "<missing>" for v in now.values()):
            return False, "keep going until a file is gone"
    state["at_interrupt"] = now
    raise KeyboardInterrupt("simulated interrupt with a mutant applied")

setattr(mod, runner, fake)

try:
    mod.main()
    verdict = {"error": "main() returned without the injected interrupt"}
except KeyboardInterrupt:
    if state["at_interrupt"] is None:
        verdict = {"error": "never reached a mutant"}
    else:
        verdict = {
            "applied": any(state["at_interrupt"][p] != before[p]
                           for p in targets),
            "deleted": any(v == "<missing>"
                           for v in state["at_interrupt"].values()),
            "restored": all(fingerprint(p) == before[p] for p in targets),
        }
print("VERDICT " + json.dumps(verdict))
"""


class MutationHarnessRestoreTests(unittest.TestCase):
    """Every tests/mutate_*.py must put the tree back if it is interrupted."""

    # harness -> the shipping files it rewrites IN THE WORKING TREE.
    IN_PLACE = {
        "mutate_retired_entities.py": ["mqtt.py"],
        "mutate_camera_quality.py": ["mqtt.py", "config.py"],
        "mutate_light_logging.py": ["app/sensors/light/light.py", "mqtt.py"],
        "mutate_setup_units.py": ["bin/install-systemd-units.sh", "bin/setup.sh",
                                  "services/etc/systemd/system/mqtt.service",
                                  "services/etc/systemd/system/gardyn-netwatch.timer"],
        "mutate_pump_api_interlock.py": ["app/sensors/pump/routes.py"],
        "mutate_ha_birth_message.py": ["mqtt.py"],
        "mutate_connack_refusal.py": ["mqtt.py"],
    }

    # Harnesses that copy the repository and mutate the COPY. They cannot leave
    # anything behind and need no restore - which is the better design, and the
    # reason they are listed rather than exempted silently.
    SANDBOXED = {"mutate_health_log.py", "mutate_upgrade_policy.py",
                 "mutate_netwatch.py", "mutate_deploy_verify.py",
                 # T-527.5. Sandboxed from the start rather than converted
                 # later: this battery mutates mqtt.py and mqtt.service, which
                 # a concurrent session is as likely to be editing as not.
                 "mutate_light_scheduler.py",
                 # T-527.13. Moved here from IN_PLACE. It mutated TWO files in
                 # the live tree, and its docstring's argument for doing so —
                 # "a sandbox buys nothing here" — was disproved by a reviewer
                 # simply running the battery from a copy.
                 "mutate_light_schedule.py",
                 # T-527.17. The only harness whose TARGET is a test file
                 # rather than a shipping one: it mutates the payload-sink
                 # scanner in tests/test_connack_refusal.py. A battery over
                 # mqtt.py structurally cannot narrow the rule that reads
                 # mqtt.py, which is how that scanner shipped with two
                 # confirmed forgery escapes under a green control.
                 "mutate_payload_scanner.py"}

    # Harnesses that also DELETE a file rather than only editing one. The
    # restore path for a deletion is different code (move to a stash, copy
    # back), so interrupting at the first text mutant never reaches it.
    HAS_DELETE_MUTANTS = {"mutate_setup_units.py"}

    def _drive(self, harness, targets, mode="text"):
        proc = subprocess.run(
            [sys.executable, "-c", _RESTORE_PROBE, REPO, harness,
             json.dumps([os.path.join(REPO, t) for t in targets]), mode],
            cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
        lines = [ln for ln in proc.stdout.splitlines()
                 if ln.startswith("VERDICT ")]
        self.assertEqual(1, len(lines),
                         f"probe produced no verdict:\n{proc.stdout[-3000:]}")
        return json.loads(lines[0][len("VERDICT "):])

    def test_an_interrupted_battery_leaves_no_mutant_behind(self):
        for harness, targets in sorted(self.IN_PLACE.items()):
            with self.subTest(harness=harness):
                verdict = self._drive(harness, targets)
                self.assertNotIn("error", verdict, verdict.get("error"))
                # The positive control: if no mutant was ever on disk, a
                # "restored" verdict means nothing.
                self.assertTrue(verdict["applied"],
                                "the probe never caught a mutant on disk, so "
                                "the restore was not exercised")
                self.assertTrue(verdict["restored"],
                                f"{harness} left a mutant in the working tree")

    def test_an_interrupted_DELETE_mutant_restores_content_and_mode(self):
        """The deletion restore is a separate path, and it recreates the file -
        so it can come back with the right bytes and the wrong permissions.
        The harness's own `restored` check compares content only, which means
        it prints "tree restored byte-identical: True" and returns 0 over a
        file whose mode has changed. Today's delete target is 0644 so the
        defect is masked; a `bin/*.sh` target would make it live."""
        for harness in sorted(self.HAS_DELETE_MUTANTS):
            with self.subTest(harness=harness):
                verdict = self._drive(harness, self.IN_PLACE[harness],
                                      mode="delete")
                self.assertNotIn("error", verdict, verdict.get("error"))
                self.assertTrue(verdict["deleted"],
                                "the probe never caught a deleted file, so the "
                                "deletion restore was not exercised")
                self.assertTrue(verdict["restored"],
                                f"{harness} did not restore a deleted file's "
                                f"content and mode")

    def test_the_probe_refuses_a_harness_whose_runner_it_cannot_find(self):
        """The guard added to _RESTORE_PROBE in eaf159f, which had no test.

        `setattr(mod, runner, fake)` happily CREATES an attribute, so a harness
        spelling its entry point differently would have had an unused one
        planted while its REAL battery ran against the live tree. The guard
        refuses instead - but `_drive` only ever runs over IN_PLACE, and all
        seven of those define `run_suites` or `run_suite`, so nothing exercised
        it. mutate_deploy_verify.py is the only harness spelling it `run_one`,
        and it is SANDBOXED. Named by the review of eaf159f; this is the case
        that reaches it.

        Cheap because the guard fires before anything is imported for real -
        no battery runs, no tree is touched.
        """
        verdict = self._drive("mutate_deploy_verify.py", ["mqtt.py"])
        self.assertIn(
            "error", verdict,
            "the probe accepted a harness with no run_suite()/run_suites(), "
            "which means it planted an unused attribute and let that harness "
            "run its real battery")
        self.assertIn("run_suite", verdict["error"])
        # Control: a harness that DOES have the runner must not hit this
        # branch, or the guard is refusing everything and proves nothing.
        other = self._drive("mutate_ha_birth_message.py", ["mqtt.py"])
        self.assertNotIn(
            "error", other,
            f"CONTROL FAILED: an IN_PLACE harness with a real runner was also "
            f"refused, so the assertion above is not about the runner name: "
            f"{other}")

    @staticmethod
    def _sandbox_copy_calls(harness):
        """Every real `copytree(REPO, ...)` CALL in a harness, as AST nodes.

        PARSE, do not grep. This assertion is what entitles a harness to sit
        in SANDBOXED, and SANDBOXED membership is what exempts it from
        test_an_interrupted_battery_leaves_no_mutant_behind - so a false pass
        here silently removes the only check that would catch a stranded
        mutant in a shipping file.

        It has now been fooled twice by text that is not code, each time by a
        different construct, which is the argument for stopping at the level
        of text entirely:

          1. A COMMENT quoting the literal (c9a6a8d, found 2026-08-11). Fixed
             in c536b3b by stripping `#` comments before matching.
          2. A module DOCSTRING quoting the literal (found 2026-08-12 by the
             T-527.27 review of that very fix). A docstring is not a comment,
             so stripping comments does not touch it - and BOTH
             mutate_deploy_verify.py and mutate_light_scheduler.py described
             their own sandboxing in their opening docstring. Each therefore
             held two occurrences, which is exactly the state (1) produced.
             Confirmed by converting both harnesses to mutate the live tree
             (`root = REPO`) on copies, docstrings untouched: this test stayed
             GREEN for both, with mutate_netwatch.py as the red control.

        A third spelling would have defeated a docstring-stripping fix too, so
        the fix is not to enumerate the places text can hide. An `ast` walk
        cannot see comments, docstrings, string literals or `#` inside
        strings, and is indifferent to wrapping and whitespace - which also
        retires the nine-line comment mutate_payload_scanner.py carried
        begging future editors not to reformat one line.

        Accepts both `shutil.copytree(REPO, ...)` and a bare
        `copytree(REPO, ...)` after `from shutil import copytree`; the grep
        this replaces silently rejected the second, so its only realistic
        failure was spurious.

        WHAT THIS IS AND IS NOT, stated because the review of 094eac0 measured
        it against 21 synthetic harnesses and the answer is narrower than the
        AST framing suggests. This finds a CALL NODE. It says nothing about
        whether that call EXECUTES, and nothing about where the mutants land.
        Measured accepting, all five confirmed: the call under `if False:`;
        the call inside a function nobody invokes; the call after a `return`
        or `raise`; `copytree(REPO, backup)` followed by `root = REPO`, which
        copies and then mutates the originals; and a locally-defined
        `def copytree(a, b): pass`. The fourth is the mechanism the failure
        message below warns about, standing on its own.

        THE DESIGN CALL: this stays call-presence FOR NOW, and the reason
        recorded here on 2026-08-12 was WRONG. Both versions are kept, because
        the wrong one is the plausible mistake.

        ~~"The probe's runner-name interception does not reach these harnesses
        as written - driving mutate_netwatch.py through it returns 'main()
        returned without the injected interrupt' in 0.3s - so it needs the
        harnesses' own entry points changed."~~ The MEASUREMENT reproduces;
        the diagnosis does not. `_RESTORE_PROBE`'s `fake` returns
        `(True, _GREEN)` / `(False, _RED)`, while mutate_netwatch's
        `run_suite` returns `(rc: int, out: str)` - so `main()` reads
        `rc = True`, finds `True != 0`, and aborts at its OWN control A in
        0.3s. The interception reaches it perfectly; the DOUBLE is wrong about
        the shape, which is the same happy-path-double defect this probe's
        comment above records fixing once already for the ran-count.

        And the honest instrument ALREADY WORKS, unchanged, on at least one
        SANDBOXED harness. Measured by the review of 2a8c951:

          mutate_light_scheduler.py  {"applied": false, "restored": true}
          mutate_ha_birth_message.py {"applied": true,  "restored": true}  (IN_PLACE control)

        `applied: false, restored: true` against the live tree IS the sandbox
        property, measured rather than parsed. So the work is a per-harness
        runner-shape fix in the double - NOT a rewrite of the harnesses - and
        a single global flip to `(0, ...)` is not it either, because
        mutate_light_scheduler.py is already driven correctly by the current
        shape. That is real work with its own failure modes, it is filed
        rather than rushed into the commit that discovered it, and until it
        lands this gate is a NECESSARY condition on SANDBOXED membership and
        is not claimed as a sufficient one - which is why the reverse test's
        failure message refuses to prescribe a move.

        Also rejected, and this direction is the safe one because it fails
        LOUDLY: `copytree(src=REPO, dst=...)` keyword form, a bound alias,
        `str(REPO)`, a helper module, `cp -a`, `git worktree add`. A harness
        that genuinely sandboxes by one of those spellings fails this test and
        gets read by a human, which is the correct outcome for a construct
        nothing here can verify.
        """
        return MutationHarnessRestoreTests._copy_calls_in_source(
            read_text(os.path.join(REPO, "tests", harness)))

    @staticmethod
    def _copy_calls_in_source(source):
        """The text-analysis half, split out 2026-08-12 so it can be pinned.

        Nothing tested the AST walk itself - see
        test_the_sandbox_gate_does_not_count_PROSE below, and F3 of the
        094eac0 review, which measured that swapping the walk back for
        c536b3b's grep changed no verdict in this file. Taking SOURCE rather
        than a harness NAME is what lets that control run on files written for
        the purpose, without writing anything into the live tree - which this
        class exists to keep clean.
        """
        import ast

        tree = ast.parse(source)
        found = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            if isinstance(func, ast.Attribute):
                name = func.attr
            elif isinstance(func, ast.Name):
                name = func.id
            else:
                continue
            first = node.args[0]
            if name == "copytree" and isinstance(first, ast.Name) \
                    and first.id == "REPO":
                found.append(node)
        return found

    def test_a_sandboxed_harness_still_works_on_a_copy(self):
        """Its exemption from the check above rests on this one fact, so state
        it.

        NOT "a harness converted to in-place editing has to move lists" -
        that sentence stood here until 2026-08-12 and was false. Two of these
        harnesses were converted to in-place on copies, moved no lists, and
        this test stayed green, because their docstrings carried the literal
        it was matching. What is true is narrower and is what the AST walk
        now checks: a harness that does not CALL copytree(REPO, ...) fails
        here, whatever its prose says about itself.
        """
        for harness in sorted(self.SANDBOXED):
            with self.subTest(harness=harness):
                calls = self._sandbox_copy_calls(harness)
                self.assertTrue(
                    calls,
                    f"{harness} is listed SANDBOXED - which exempts it from "
                    f"the interrupted-battery restore check - but contains no "
                    f"copytree(REPO, ...) call. Prose about sandboxing is not "
                    f"evidence of it.")

    def test_an_in_place_harness_does_not_claim_a_sandbox(self):
        """The reverse direction, which nothing asserted before 2026-08-12.

        The test above can only catch a sandboxed harness that stopped
        sandboxing. This catches the other way round: a harness that gained a
        `copytree(REPO, ...)` and was never moved out of IN_PLACE is being
        held to a restore check it no longer needs, and - more to the point -
        the two lists have silently stopped describing the tree. Neither
        direction is deducible from the other, and a membership table nobody
        checks in both directions drifts.
        """
        for harness in sorted(self.IN_PLACE):
            with self.subTest(harness=harness):
                calls = self._sandbox_copy_calls(harness)
                self.assertFalse(
                    calls,
                    f"{harness} calls copytree(REPO, ...) but is listed "
                    f"IN_PLACE, so the lists have stopped describing the "
                    f"tree. DO NOT resolve this by moving it to SANDBOXED on "
                    f"the strength of the call alone: SANDBOXED exempts a "
                    f"harness from the interrupted-battery restore check, and "
                    f"a harness that copies the tree AND STILL WRITES the "
                    f"originals satisfies this gate while needing that check "
                    f"more than ever. Move it only after confirming every "
                    f"mutant it writes lands under the copy.")

    def test_the_sandbox_gate_does_not_count_PROSE(self):
        """NEGATIVE AND POSITIVE CONTROL for the AST walk, which nothing
        pinned until 2026-08-12.

        F3 of the 094eac0 review: swapping the walk back for c536b3b's
        comment-stripping grep changes no verdict anywhere in this file,
        because every real harness carries BOTH the prose and the call - which
        is precisely why the old gate was fooled twice. A "simplification"
        back to text would have been invisible. So state the property on
        inputs written for it rather than on the corpus, where the two
        implementations agree by accident.

        Controls first: if the walk cannot see a real call, every other
        verdict in this class is void.
        """
        cases = {
            "a real shutil.copytree(REPO, ...) call":
                ("import shutil\nREPO = '/x'\nshutil.copytree(REPO, '/y')\n",
                 True),
            "a bare copytree(REPO, ...) after from-import":
                ("from shutil import copytree\nREPO = '/x'\n"
                 "copytree(REPO, '/y')\n", True),
            "a module DOCSTRING quoting the call (the 2026-08-12 defect)":
                ('"""RUNS IN A shutil.copytree(REPO) SANDBOX."""\n'
                 'REPO = "/x"\n', False),
            "a COMMENT quoting the call (the 2026-08-11 defect)":
                ("REPO = '/x'\n"
                 "# shutil.copytree(REPO, dst) stays on ONE line\n", False),
            "a string literal quoting the call":
                ("REPO = '/x'\nNOTE = 'shutil.copytree(REPO, dst)'\n", False),
        }
        for label, (source, expected) in cases.items():
            with self.subTest(case=label):
                found = self._copy_calls_in_source(source)
                if expected:
                    self.assertTrue(
                        found,
                        f"CONTROL FAILED - {label}: the gate cannot see a "
                        f"real copytree(REPO, ...) call, so every other "
                        f"verdict in this class is void")
                else:
                    self.assertFalse(
                        found,
                        f"{label}: the gate counted text that is not code - "
                        f"the exact defect c536b3b and 094eac0 were each "
                        f"written for")

    def test_every_mutation_harness_is_covered(self):
        """A new harness must be listed above, or it is untested by default -
        and its absence is invisible in the result of the test that matters."""
        found = {n for n in os.listdir(os.path.join(REPO, "tests"))
                 if n.startswith("mutate_") and n.endswith(".py")}
        self.assertEqual(found, set(self.IN_PLACE) | self.SANDBOXED,
                         "a mutation harness is in neither IN_PLACE nor "
                         "SANDBOXED, so nothing here checks it")
        self.assertEqual(set(), set(self.IN_PLACE) & self.SANDBOXED)


if __name__ == "__main__":
    unittest.main()
