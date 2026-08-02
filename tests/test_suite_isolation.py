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
            if ("sys.modules[" in text or "sys.modules.pop" in text
                    or "sys.modules.setdefault" in text
                    or "from tests.test_water_interlock import" in text):
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
before = {p: fingerprint(p) for p in targets}
state = {"n": 0, "at_interrupt": None}

def fake(*a, **k):
    state["n"] += 1
    # Call 1 is control A (the clean tree, must look GREEN) and call 2 is
    # control B (broken, must look RED). Interrupting before those pass would
    # abort the harness through its own guard rather than mid-mutant.
    if state["n"] <= 2:
        return (state["n"] == 1), "control"
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
    }

    # Harnesses that copy the repository and mutate the COPY. They cannot leave
    # anything behind and need no restore - which is the better design, and the
    # reason they are listed rather than exempted silently.
    SANDBOXED = {"mutate_health_log.py", "mutate_upgrade_policy.py"}

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

    def test_a_sandboxed_harness_still_works_on_a_copy(self):
        """Its exemption from the check above rests on this one fact, so state
        it. A harness converted to in-place editing has to move lists."""
        for harness in sorted(self.SANDBOXED):
            with self.subTest(harness=harness):
                text = read_text(os.path.join(REPO, "tests", harness))
                self.assertIn("shutil.copytree(REPO", text)

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
