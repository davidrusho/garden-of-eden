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
import stat
import subprocess
import sys
import tempfile
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
import atexit, hashlib, importlib.util, json, os, stat, sys, tempfile

repo, harness, targets, mode, shape = (sys.argv[1], sys.argv[2],
                                       json.loads(sys.argv[3]), sys.argv[4],
                                       sys.argv[5])
sys.path.insert(0, repo)

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


# ---------------------------------------------------------------- T-554.1 ---
# THE REPAIR. This probe exists to stop a harness with a mutant on disk, so
# whenever the restore under test FAILS - exactly when these tests do their job
# - the mutant was being left in a shipping file with nothing saying so.
#
# `_state` is filled in after the harness module is imported. It is declared
# here, and the atexit handler is registered here, because BOTH have to exist
# before `exec_module` runs. See the ordering note on atexit below.
_state = {"ready": False, "targets": [], "before": {}, "original": {},
          "touched": set(), "reached_mutant": False}


# ATOMIC, because the subject of this whole file is interruption. A plain
# open(p, "wb") + write() leaves a window in which a real ^C or SIGTERM strands
# a ZERO-BYTE shipping file - mqtt.py, bin/setup.sh, a systemd unit. Writing a
# sibling temp file and os.replace()ing it means the target is either the old
# bytes or the new ones and never nothing.
#
# NOTE FOR ANYONE EDITING THIS BLOCK: no function in here may carry a
# docstring. This whole program is a triple-quoted string in the enclosing
# file, so a nested triple quote terminates it and the outer file stops
# parsing. Comments only.
def _write_back(p, content, mode):
    d = os.path.dirname(p) or "."
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".t554-repair-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# Put back what the probe SAW THIS HARNESS CHANGE. Returns three lists:
# repaired, failed, skipped.
#
# SCOPED ON PURPOSE. Peer sessions on this machine share ONE worktree (the
# repo's CLAUDE.md says so), so a blanket "rewrite anything that differs from
# the snapshot" would silently revert a concurrent writer's save to mqtt.py
# with no error, no prompt and no git trace - a new harm this probe never had
# before it started writing at all. A path is rewritten only when the probe
# OBSERVED it change under this harness; anything else that differs is
# reported under `skipped` and left alone.
#
# BE PRECISE ABOUT WHAT THAT BUYS, because the first version of this comment
# was not and a review said so. Attribution is not achievable from filesystem
# observation alone, and this does NOT claim it. What it guarantees is narrow:
#
#   - A file the harness never touched is never written. That is the common
#     concurrent-writer case and it is fully closed.
#   - A file the harness DID touch is still rewritten to the pre-run snapshot.
#     If a peer saved that same file inside the window, their save is lost, and
#     nothing here can tell the two apart - the bytes look identical either way.
#
# The window is ~100ms per probe, a few seconds for the class. That residual is
# accepted deliberately: it is strictly smaller than the stranded-mutant class
# it replaces, and unlike that one it needs a collision on the same file.
# Do not read `skipped` as "these are a peer's" - read it as "the probe has no
# grounds to touch these", which is the only thing it knows.
def _repair():
    put_back, failed, skipped = [], [], []
    if not _state["ready"]:
        return put_back, failed, skipped
    for p in _state["targets"]:
        rel = os.path.relpath(p, repo)
        if fingerprint(p) == _state["before"][p]:
            continue
        # PER PATH, and the `and not reached_mutant` that used to be here was
        # DEAD CODE that undid the whole point. `touched` is populated three
        # lines after `reached_mutant` is set, in the same block, so
        # `touched` non-empty implied `reached_mutant` and the clause collapsed
        # to one global boolean: once ANY mutant was reached, EVERY divergent
        # target was rewritten, including files this harness never went near.
        # Found by review; a mutant deleting `p not in _state["touched"]` left
        # the whole module green, which is how it hid.
        if p not in _state["touched"]:
            skipped.append(rel)
            continue
        content, mode = _state["original"][p]
        try:
            if content is None:
                os.unlink(p)
            else:
                _write_back(p, content, mode)
        except OSError as exc:
            failed.append(f"{rel} (REPAIR FAILED: {exc})")
            continue
        # VERIFY, rather than treating "did not raise" as success. This is the
        # only claim in the verdict that anybody acts on by NOT looking at the
        # tree, so it has to be measured after the write like everything else
        # in this file.
        #
        # DECLARED UNCOVERED (T-554.1): no test reddens if this check is
        # deleted, and a mutant removing it survives the module. Reaching it
        # needs a filesystem where os.replace() reports success and the bytes
        # still differ, which the tests cannot simulate. It is kept as cheap
        # defence rather than as something the suite is asserting - do not read
        # the green suite as evidence this branch works.
        if fingerprint(p) == _state["before"][p]:
            put_back.append(rel)
        else:
            failed.append(f"{rel} (REPAIR FAILED: still differs after rewrite)")
    return put_back, failed, skipped


# Registered BEFORE the harness is imported so that it runs LAST.
#
# atexit is LIFO. Five of the seven IN_PLACE harnesses - camera_quality,
# ha_birth_message, connack_refusal, light_logging, retired_entities - register
# their own restore() with atexit while they are being imported. A repair that
# ran only in the `finally` below would therefore be OVERWRITTEN at shutdown by
# the very restore path under test, and a harness whose saved original is wrong
# would undo the repair silently while the verdict still said "repaired".
# Registering here, before exec_module, puts this handler at the bottom of the
# LIFO stack and gives it the last word.
#
# It normally finds nothing: the `finally` repair has already run and every
# fingerprint matches. It prints only when it had to act, and the driver merges
# that into the verdict so the reported state is the FINAL one.
def _final_repair():
    # UNCONDITIONAL, and it reports `still_dirty` as well as what it did.
    # Printing only when it acted left a stale `repair_failed` standing: a path
    # that failed in the `finally` and was then fixed by the harness's own
    # restore stayed on the failed list, and _tree_note led with THE WORKING
    # TREE IS STILL DIRTY over a clean tree. That is the mirror of the defect
    # this change exists to fix, so the final word has to be a measurement of
    # the final state, not a log of attempts.
    #
    # Wrapped, because an exception escaping an atexit callback is printed as
    # "Exception ignored" and leaves the exit code at 0 - a stale claim with no
    # signal anywhere.
    #
    # DECLARED UNCOVERED (T-554.1): a mutant narrowing this `except` survives
    # the module. Reaching it means making _repair raise something other than
    # the OSError it already catches, from inside an atexit callback, which the
    # fixtures cannot arrange. Cheap defence, not an asserted property - do not
    # read the green suite as evidence this branch works.
    try:
        put_back, failed, skipped = _repair()
        dirty = [os.path.relpath(p, repo) for p in _state["targets"]
                 if _state["ready"] and fingerprint(p) != _state["before"][p]]
        print("FINALREPAIR " + json.dumps(
            {"repaired": put_back, "repair_failed": failed,
             "repair_skipped": skipped, "still_dirty": dirty}))
    except BaseException as exc:
        print("FINALREPAIR " + json.dumps(
            {"repaired": [], "repair_skipped": [], "still_dirty": [],
             "repair_failed": [f"<final repair raised> (REPAIR FAILED: {exc})"]}))


atexit.register(_final_repair)

spec = importlib.util.spec_from_file_location(
    "_harness_under_test", os.path.join(repo, "tests", harness))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

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


# T-554.1. THE FINGERPRINT ABOVE CAN DETECT A STRANDED MUTANT BUT CANNOT UNDO
# ONE, and this probe's whole job is to stop a harness mid-mutation. Whenever
# the restore under test FAILS - which is precisely when these tests fire - the
# mutant is left sitting in the working tree, in a shipping file, with the
# failure message saying nothing about it. So keep the BYTES and the MODE, not
# just a digest of them, and repair from these in the `finally` below.
#
# Mode is captured deliberately: a repair that writes content and lets the
# umask pick the bits silently demotes 100755 to 100644, which is a known trap
# in this project and leaves a clean-looking `git status` behind a script that
# no longer runs.
def snapshot(p):
    try:
        with open(p, "rb") as fh:
            content = fh.read()
    except FileNotFoundError:
        # Absent BEFORE the harness ran. Repair means REMOVING whatever the
        # harness put here, not writing an empty file.
        #
        # FileNotFoundError specifically, never a bare OSError. Under OSError a
        # target that is merely UNREADABLE at this moment (EACCES, a transient
        # FS error) records as absent, and the repair for absent is os.unlink -
        # so a blanket except would let a hiccup here DELETE a shipping file
        # later. Anything other than "not there" propagates and kills the probe
        # now, before it has mutated anything, which is the safe direction.
        return None, None
    return content, stat.S_IMODE(os.stat(p).st_mode)


original = {p: snapshot(p) for p in targets}
state = {"n": 0, "at_interrupt": None, "sandbox_before": None,
         "sandbox_at_interrupt": None}

_state.update({"targets": targets, "before": before, "original": original,
               "ready": True})

# THE SANDBOX SIDE OF THE MEASUREMENT (T-527.31 review).
#
# `applied: false` against the live tree is also what a probe that never
# reached a mutation would produce, so on its own it cannot tell a real
# sandbox from a dead probe. The fix is not to work out whether the
# interrupted call was a CONTROL or a MUTANT - that is not observable from
# out here, and it does not matter: "this harness writes its mutations into a
# copy rather than into the working tree" is demonstrated just as well by a
# control's mutation as by a mutant's.
#
# So find the copy and fingerprint it too. Five of the six sandboxed harnesses
# pass their sandbox root to the runner as the first positional argument;
# mutate_light_schedule.py takes none and rebinds a module-level ROOT instead.
_REPO_REAL = os.path.realpath(repo)


def _outside_repo(path):
    real = os.path.realpath(path)
    return real != _REPO_REAL and not real.startswith(_REPO_REAL + os.sep)


# Map each live target to its counterpart inside the harness's copy.
#
# EVERY candidate is tried, not just the first string-shaped one. An earlier
# version took args[0] if it was a str and only otherwise consulted mod.ROOT -
# which meant a runner called with the LIVE repo as its first argument shadowed
# a perfectly good sandboxed ROOT, and a runner whose first argument was a
# suite NAME produced a meaningless relative path that still reported
# sandbox_found: True. Both are the same defect: a candidate treated as
# authoritative because of its POSITION rather than because it resolved to
# anything.
#
# Module globals are searched too, because a harness may hold no sandbox ROOT
# at all and still be fully sandboxed through per-file paths - exactly the case
# for a harness whose ROOT points at the repo while its TARGET and TEST_FILE
# point into the copy. Judging that harness unsandboxed reads as a real defect
# and is not one.
def sandbox_paths(args):
    candidates = []
    if args and isinstance(args[0], (str, os.PathLike)):
        candidates.append(str(args[0]))
    for value in vars(mod).values():
        if isinstance(value, (str, os.PathLike)):
            candidates.append(str(value))

    found = {}
    for live in targets:
        rel = os.path.relpath(live, repo)
        for cand in candidates:
            if not cand or not _outside_repo(cand):
                continue
            if os.path.isdir(cand):
                joined = os.path.join(cand, rel)
                if os.path.exists(joined):
                    found[live] = joined
                    break
            elif cand.endswith(os.sep + rel) or cand.endswith(rel):
                if os.path.exists(cand):
                    found[live] = cand
                    break
    # All or nothing: a partial map would compare some targets and silently
    # ignore the rest, and "some of it is sandboxed" is not the property.
    return found if len(found) == len(targets) else None


def sandbox_fingerprints(args):
    paths = sandbox_paths(args)
    if paths is None:
        return None
    return {live: fingerprint(sand) for live, sand in paths.items()}

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

# THE DOUBLE'S FIRST ELEMENT HAS TWO SHAPES IN THIS REPO, and returning the
# wrong one is invisible at the call site (T-527.31). `run_suites()` returns
# (ok: bool, out: str); `run_suite(root)` in the netwatch/health_log/
# upgrade_policy family returns (rc: int, out: str). Hand a bool to an
# rc-shaped harness and its own control A reads `rc = True`, finds `True != 0`,
# and aborts in 0.3s — which the probe then reports as "main() returned without
# the injected interrupt", i.e. as a fault in the HARNESS rather than in the
# double. That misreading is what deferred this ticket once already: the
# measurement reproduced and the diagnosis did not.
_GREEN_OK, _RED_OK = (0, 1) if shape == "rc" else (True, False)

def fake(*a, **k):
    state["n"] += 1
    # Call 1 is control A (clean, must look GREEN), call 2 is control B (broken,
    # must look RED), and call 3 — where the harness has one — is control C (an
    # import-time death, must classify as NO VERDICT). Interrupting before those
    # pass would abort the harness through its own guard rather than mid-mutant.
    if state["n"] == 1:
        # Call 1 is control A, which every harness runs on a PRISTINE tree -
        # so this is the sandbox baseline, taken before anything is mutated.
        state["sandbox_before"] = sandbox_fingerprints(a)
    if state["n"] <= n_controls:
        if state["n"] == 1:
            return _GREEN_OK, _GREEN
        if state["n"] == 2:
            return _RED_OK, _RED
        return _RED_OK, _IMPORT_DEATH
    now = {p: fingerprint(p) for p in targets}
    # T-554.1. WHAT THE REPAIR IS ALLOWED TO TOUCH. Past the controls the
    # harness is mid-battery, so anything seen differing from `before` here is
    # attributable to it - which is what lets the repair put those back without
    # gambling that no peer session is writing the same shared worktree.
    # Accumulated across calls because delete mode polls until a file vanishes.
    _state["reached_mutant"] = True
    for p in targets:
        if now[p] != before[p]:
            _state["touched"].add(p)
    if mode == "delete":
        # Wait for the DELETE mutant, whose restore is a different code path
        # (move to a stash, copy back) from the text mutants' write().
        if not any(v == "<missing>" for v in now.values()):
            return False, "keep going until a file is gone"
    state["at_interrupt"] = now
    state["sandbox_at_interrupt"] = sandbox_fingerprints(a)
    raise KeyboardInterrupt("simulated interrupt with a mutant applied")

setattr(mod, runner, fake)


# ORDER IS LOAD-BEARING, and `_repair` is defined far above precisely so this
# stays visible: `restored` is computed from the PRE-REPAIR measurement, inside
# the `except` below, and only then does the repair run in the `finally`.
# Repair first and every verdict in this file becomes `restored: true` by
# construction - the probe would be marking its own homework, and the tests
# that exist to catch a broken restore could never fail again. Nothing enforces
# that ordering except this comment and one assertion,
# ProbeRepairTests.test_the_probe_repairs_content_and_mode_a_harness_did_not_
# restore's assertFalse(verdict["restored"]).
verdict = None
try:
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
                # True only when a mutation was demonstrably on disk INSIDE the
                # copy at the moment of the interrupt. That is the sandbox
                # property measured from the other side, and it is what stops
                # `applied: false` being satisfied by a probe that mutated nothing
                # anywhere.
                "sandbox_mutated": bool(
                    state["sandbox_before"] and state["sandbox_at_interrupt"]
                    and state["sandbox_before"] != state["sandbox_at_interrupt"]),
                "sandbox_found": state["sandbox_at_interrupt"] is not None,
            }
finally:
    # EVERY exit path, including one this probe does not model. An unexpected
    # exception out of mod.main() prints no verdict at all, which the driver
    # reports as "probe produced no verdict" - and that is exactly the run
    # most likely to have left a mutant on disk, because nothing got as far as
    # measuring. `repaired` rides on the verdict when there is one; when there
    # is not, the repair still happens and that is the part that matters.
    _put_back, _failed, _skipped = _repair()
    if verdict is not None:
        verdict["repaired"] = _put_back
        verdict["repair_failed"] = _failed
        verdict["repair_skipped"] = _skipped
if verdict is not None:
    print("VERDICT " + json.dumps(verdict))
"""


def _merge_final_repair(verdict, stdout):
    """Fold the probe's atexit-time repair into the verdict it already printed.

    The verdict is printed before interpreter shutdown, and five of the seven
    IN_PLACE harnesses restore from an `atexit` handler that runs after it. The
    probe's own final repair is registered before those (so it runs last, atexit
    being LIFO) and prints a FINALREPAIR line when it had to act. Without this
    merge, `repaired` would describe a state that no longer existed by the time
    the process exited - which is exactly the kind of stale claim the failure
    messages below are being careful not to make.
    """
    for line in stdout.splitlines():
        if not line.startswith("FINALREPAIR "):
            continue
        final = json.loads(line[len("FINALREPAIR "):])
        for key in ("repaired", "repair_failed", "repair_skipped"):
            merged = list(verdict.get(key) or [])
            # DEDUPE ON THE PATH, not the whole entry. A failed repair is
            # retried at exit and fails again, and the two messages differ
            # (each names its own temp file), so a plain `not in` check would
            # report one path twice and read as two damaged files.
            seen = {item.split(" (", 1)[0] for item in merged}
            for item in final.get(key) or []:
                if item.split(" (", 1)[0] not in seen:
                    merged.append(item)
                    seen.add(item.split(" (", 1)[0])
            verdict[key] = merged
        # AND DROP A FAILURE THE TREE NO LONGER SHOWS. The final handler
        # measures what is STILL dirty at exit, which is the only authority on
        # the final state: a path that failed in the `finally` and was then put
        # back by the harness's own restore is clean, and leaving it on the
        # failed list makes _tree_note announce a dirty tree over a clean one.
        if "still_dirty" in final:
            dirty = set(final["still_dirty"])
            verdict["repair_failed"] = [
                item for item in verdict["repair_failed"]
                if item.split(" (", 1)[0] in dirty
                or item.startswith("<final repair raised>")]
    return verdict


def _tree_note(verdict):
    """One sentence about what the probe did to the working tree.

    Shared by every failure message that follows, because getting this wrong
    RE-CREATES THE DEFECT THE TICKET WAS FILED TO REMOVE. The first version of
    this change hardcoded "the probe has since put it back, so `git status` is
    clean" into each message - true in the common case, and flatly false when
    the repair itself failed, which is precisely when a maintainer most needs
    to be told the tree is still dirty. A review caught it. So the sentence is
    DERIVED from the verdict rather than asserted, and the bad news sorts
    first.
    """
    failed = verdict.get("repair_failed") or []
    skipped = verdict.get("repair_skipped") or []
    repaired = verdict.get("repaired") or []
    if failed:
        return (f"*** THE WORKING TREE IS STILL DIRTY *** the probe could NOT "
                f"put these back: {failed}. Deal with the tree before anything "
                f"else - a shipping file is holding a mutant.")
    parts = []
    if repaired:
        parts.append(f"the probe has since put back {repaired}, so a clean "
                     f"`git status` is NOT evidence the harness restored "
                     f"anything - this assertion is")
    if skipped:
        parts.append(f"the probe deliberately left {skipped} alone: they "
                     f"differ from the pre-run snapshot but it never saw this "
                     f"harness touch them, so a concurrent session writing the "
                     f"shared worktree is the likelier explanation. Check "
                     f"before reverting anything")
    if not parts:
        parts.append("the probe found nothing to put back")
    return "; ".join(parts) + "."


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
                 "mutate_payload_scanner.py",
                 # T-527.36. Sandboxed from the start, and not as a default:
                 # its target is tests/mutation_scoring.py, which TEN other
                 # harnesses import at their own module scope (eleven
                 # importers, one of which is this harness itself). Mutating it
                 # in the working tree would hand a concurrent session verdicts
                 # from a deliberately broken scorer, with nothing to say so.
                 #
                 # The count was "three" until T-554.1 and was already stale at
                 # four when it was written; T-550 moved six more harnesses
                 # onto the shared rule. Re-derive rather than trusting it:
                 #
                 #   grep -rl "^from tests.mutation_scoring import" \
                 #       tests/mutate_*.py | wc -l        # -> 11
                 #
                 # SCOPED TO tests/mutate_*.py AND ANCHORED ON PURPOSE. A
                 # recipe reading `grep -rl ... tests/` returns 12, because
                 # THIS FILE matches on the strength of this very comment - so
                 # a maintainer following it would get 12, conclude the figure
                 # was stale again, and "fix" a correct number. (Also note
                 # `-lc` is not two flags doing two jobs: BSD grep lets -l win
                 # and prints no counts at all.)
                 #
                 # The CONCLUSION has never depended on the number - it only
                 # gets stronger - but a wrong figure in a comment arguing for
                 # a sandbox is the kind of thing a reader checks once.
                 "mutate_mutation_scoring.py",
                 # T-527.28. log_hygiene.py is imported by mqtt.py at module
                 # scope, so mutating it in the working tree would hand a
                 # concurrent session a broken log formatter.
                 "mutate_log_hygiene.py"}

    # Harnesses that also DELETE a file rather than only editing one. The
    # restore path for a deletion is different code (move to a stash, copy
    # back), so interrupting at the first text mutant never reaches it.
    HAS_DELETE_MUTANTS = {"mutate_setup_units.py"}

    # The SANDBOXED harnesses' targets: the files each would rewrite IN THE
    # WORKING TREE if its copytree were removed or bypassed. These are what
    # test_a_sandboxed_harness_leaves_the_working_tree_untouched fingerprints,
    # so a harness listed with the wrong paths would report "untouched" about
    # files it never had any intention of writing.
    #
    # mutate_deploy_verify.py is deliberately absent: it spells its runner
    # `run_one`, which the probe refuses by design, and
    # test_the_probe_refuses_a_harness_whose_runner_it_cannot_find is the case
    # that covers it.
    SANDBOX_TARGETS = {
        "mutate_health_log.py": ["bin/gardyn-health-log.py",
                                 "tests/test_health_log.py"],
        # NOT tests/fixtures/gardyn-upgrade-policy.json, though the harness
        # names it: CONFIG_MUTANTS mutate a deepcopy of the PARSED fixture in
        # memory, and the file itself is only ever read.
        #
        # T-554.1 CORRECTED THE REASON, which was backwards and is worth
        # stating because two comments in this table argued from it. It said
        # listing a never-written path "makes `applied: false` true for a
        # reason that has nothing to do with sandboxing". It cannot: `applied`
        # is an any() over the declared paths, so an unchanged extra
        # contributes False and cannot move the verdict in either direction.
        # Verified by re-adding the exact path 5d37b51 removed and re-running -
        # OK, no verdict changed.
        #
        # So the pruning is HARMLESS rather than necessary, and the real blind
        # spot runs the OTHER WAY: a harness that regresses to writing a file
        # NOT on this list is invisible to `applied`, `restored` and
        # `sandbox_mutated` alike. Shortening the list is what creates that
        # gap. Keep an entry off only when the harness demonstrably never
        # writes it, as here - never merely to tidy the table.
        #
        # ONE THING AN EXTRA PATH *CAN* MOVE, so this is not a licence to pad
        # the table either: sandbox_paths() is all-or-nothing (it returns None
        # unless EVERY target maps to a counterpart inside the copy), so a path
        # with no counterpart there flips `sandbox_found` to False. That does
        # not fire for a harness that copies the whole repo, which is all of
        # them today.
        "mutate_upgrade_policy.py": ["bin/gardyn-check-upgrade-policy.py",
                                     "tests/test_upgrade_policy.py"],
        "mutate_netwatch.py": ["bin/gardyn-netwatch.py",
                               "bin/install-systemd-units.sh",
                               "services/etc/systemd/system/mqtt.service",
                               "services/etc/gardyn/netwatch.env.example",
                               "tests/test_netwatch.py"],
        "mutate_light_scheduler.py": ["light_scheduler.py", "mqtt.py",
                                      "tests/test_light_scheduler.py",
                                      "services/etc/systemd/system/mqtt.service"],
        "mutate_light_schedule.py": ["light_schedule.py",
                                     "tests/test_light_schedule.py"],
        "mutate_payload_scanner.py": ["tests/test_connack_refusal.py"],
        # Only the module, not tests/test_mutation_scoring.py: the battery
        # mutates the RULE and leaves the suite that drives it alone. Listing
        # the suite too would be harmless rather than wrong - see the corrected
        # note on mutate_upgrade_policy.py above for why an unchanged extra
        # path cannot move any verdict. It is left off because the harness
        # demonstrably does not write it, not to keep the list short.
        "mutate_mutation_scoring.py": ["tests/mutation_scoring.py"],
        # Only the module. The suite that drives it is not mutated.
        "mutate_log_hygiene.py": ["log_hygiene.py"],
    }

    # What each harness's suite runner returns as its FIRST element, which the
    # probe's double has to match (T-527.31).
    #
    # `run_suites()` returns (ok: bool, out: str). `run_suite(root)` in the
    # netwatch / health_log / upgrade_policy family returns (rc: int, out: str).
    # These cannot be told apart without running the thing, so this is a hand
    # table - but a WRONG entry is loud rather than silent: the harness aborts
    # at its own control A and the probe reports "main() returned without the
    # injected interrupt", with the shape named in the failure message below.
    #
    # Before this table existed the double returned bools unconditionally, so
    # every rc-shaped harness read `rc = True`, found `True != 0` and gave up in
    # 0.3s. That was measured and then MISDIAGNOSED as "the probe's interception
    # does not reach these harnesses", which is what deferred driving them.
    RUNNER_SHAPE = {
        "mutate_retired_entities.py": "bool",
        "mutate_camera_quality.py": "bool",
        "mutate_light_logging.py": "bool",
        "mutate_setup_units.py": "bool",
        "mutate_pump_api_interlock.py": "bool",
        "mutate_ha_birth_message.py": "bool",
        "mutate_connack_refusal.py": "bool",
        "mutate_light_scheduler.py": "bool",
        "mutate_light_schedule.py": "bool",
        "mutate_payload_scanner.py": "bool",
        "mutate_mutation_scoring.py": "bool",
        "mutate_log_hygiene.py": "bool",
        "mutate_health_log.py": "rc",
        "mutate_upgrade_policy.py": "rc",
        "mutate_netwatch.py": "rc",
        # Refused by the probe before its shape ever matters; listed so the
        # completeness check below has one entry per harness.
        "mutate_deploy_verify.py": "rc",
    }

    def _drive(self, harness, targets, mode="text"):
        proc = subprocess.run(
            [sys.executable, "-c", _RESTORE_PROBE, REPO, harness,
             json.dumps([os.path.join(REPO, t) for t in targets]), mode,
             self.RUNNER_SHAPE[harness]],
            cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
        lines = [ln for ln in proc.stdout.splitlines()
                 if ln.startswith("VERDICT ")]
        # INDENTED, and that is not cosmetic (T-527.36). This pastes a
        # subprocess's captured output into an assertion message, and the
        # subprocess is a mutation harness whose own stdout carries unittest
        # summary lines at column 0. Flush-left, those become indistinguishable
        # from THIS run's summary, so any battery scoring this file would read
        # an inflated ran-count, and `mutation_scoring.score_run` turns a moved
        # count into NO VERDICT - suppressing every genuine kill, silently,
        # because "no information" is what a reader skims past.
        #
        # Nothing scores this file today, so this was a latent coupling rather
        # than a live fault. It is fixed here rather than guarded against: an
        # earlier version of this change added a test asserting no harness
        # SCORES a forging suite, and a review showed that guard was blind for
        # three of fifteen harnesses (two spell their suite inline in argv, one
        # names it MQTT_SUITE) while asserting in its own docstring that it
        # could not be. It also pinned a PROXY - "does the file contain a
        # summary-shaped literal" - when the hazard is a MECHANISM, the
        # flush-left paste. Removing the hazard beats guarding it.
        indented = "\n".join("    " + ln
                             for ln in proc.stdout[-3000:].splitlines())
        self.assertEqual(1, len(lines),
                         f"probe produced no verdict:\n{indented}")
        return _merge_final_repair(json.loads(lines[0][len("VERDICT "):]),
                                   proc.stdout)

    def _assert_a_mutation_was_actually_on_disk(self, harness, verdict):
        """The positive control for a SANDBOXED harness's verdict.

        `applied: false` against the working tree is the result we want - and
        it is also exactly what a probe that mutated nothing anywhere would
        produce. A dead probe and a real sandbox are otherwise byte-identical,
        which the T-527.31 review demonstrated by injecting a fourth
        pre-mutant suite call into one harness: the interrupt landed on that
        control, nothing was ever mutated, and this class stayed GREEN.

        The fix is NOT to work out whether the interrupted call was a control
        or a mutant. That is not observable from outside the harness, and it
        does not matter: "this harness writes its mutations into a copy rather
        than into the working tree" is demonstrated just as well by a
        control's mutation as by a mutant's. What matters is that a mutation
        was on disk SOMEWHERE, and that somewhere was not here.

        Deliberately NOT built on the harness's stdout. Counting the
        `CONTROL <letter>` lines it prints was the first attempt and it is
        wrong: mutate_health_log.py and mutate_upgrade_policy.py announce
        their controls only when one FAILS, so a healthy run reports zero
        letters and the check fires falsely on two of the six.
        """
        self.assertTrue(
            verdict.get("sandbox_found"),
            f"{harness}: the probe could not locate this harness's sandbox, so "
            f"it cannot tell a real copy from a probe that mutated nothing. It "
            f"looks for the runner's first positional argument and then for a "
            f"module-level ROOT; this harness exposes neither.")
        self.assertTrue(
            verdict.get("sandbox_mutated"),
            f"{harness}: no mutation was on disk ANYWHERE when the battery was "
            f"interrupted - not in the working tree and not in the copy. So "
            f"`applied: {verdict.get('applied')}` says nothing about "
            f"sandboxing; the probe never reached a mutation at all. Suspect "
            f"the probe's n_controls inference "
            f"(hasattr(mod, 'CONTROL_C')) against this harness's real control "
            f"count.")

    def test_an_interrupted_battery_leaves_no_mutant_behind(self):
        for harness, targets in sorted(self.IN_PLACE.items()):
            with self.subTest(harness=harness):
                verdict = self._drive(harness, targets)
                self.assertNotIn(
                    "error", verdict,
                    f"{verdict.get('error')} {_tree_note(verdict)}")
                # The positive control: if no mutant was ever on disk, a
                # "restored" verdict means nothing.
                self.assertTrue(verdict["applied"],
                                "the probe never caught a mutant on disk, so "
                                "the restore was not exercised")
                self.assertTrue(
                    verdict["restored"],
                    f"{harness} left a mutant in the working tree. "
                    f"{_tree_note(verdict)} Fix the harness's own restore "
                    f"path.")

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
                self.assertNotIn(
                    "error", verdict,
                    f"{verdict.get('error')} {_tree_note(verdict)}")
                self.assertTrue(verdict["deleted"],
                                "the probe never caught a deleted file, so the "
                                "deletion restore was not exercised")
                self.assertTrue(
                    verdict["restored"],
                    f"{harness} did not restore a deleted file's content and "
                    f"mode. {_tree_note(verdict)} That matters here more than "
                    f"anywhere else in this file: this harness's delete mutant "
                    f"moves gardyn-netwatch.timer out of the tree entirely, "
                    f"and DEPLOY.md calls netwatch the one component whose bad "
                    f"version can take the host away permanently.")

    def test_a_sandboxed_harness_leaves_the_working_tree_untouched(self):
        """The sandbox property MEASURED, not parsed (T-527.31).

        test_a_sandboxed_harness_still_works_on_a_copy below asks the AST
        whether a `copytree(REPO, ...)` CALL NODE exists. That is a necessary
        condition and its own docstring lists five constructs it accepts while
        the harness still writes the originals - the call under `if False:`,
        the call in a function nobody invokes, the call after a `return`,
        `copytree(REPO, backup)` followed by `root = REPO`, and a locally
        defined `def copytree(a, b): pass`. SANDBOXED membership is what
        EXEMPTS a harness from test_an_interrupted_battery_leaves_no_mutant_
        behind, the only check that would catch a stranded mutant in a shipping
        file, so a false pass there removes real coverage.

        This drives the harness for real and fingerprints the LIVE files it
        would rewrite. `applied: false` with the interrupt reached is the
        sandbox property itself: the harness got as far as running a suite for
        a mutant, and at that moment nothing in the working tree had changed.

        THE CONTROLS ARE THE HALF THAT MATTERS, and there are two of them
        because one is not enough. `applied: false` is also what a probe that
        never reached a mutant would produce.

        The first control is global: an IN_PLACE harness driven the same way
        must report `applied: true`, proving the probe can put a mutant on
        disk at all.

        The second is PER HARNESS, and the first does not imply it. Whether
        the probe reaches a mutant depends on `n_controls`, which the probe
        INFERS from `hasattr(mod, "CONTROL_C")` - a per-harness property. One
        harness passing establishes nothing about the other six. The
        T-527.31 review proved this by injecting a fourth pre-mutant suite
        call into one sandboxed harness: the interrupt landed on that control,
        no mutant was ever applied anywhere, and this class stayed GREEN. So
        each harness's verdict must also show that a mutation was on disk
        INSIDE ITS COPY at the moment of the interrupt - see
        _assert_a_mutation_was_actually_on_disk, which is where that is
        checked and why it is not done by reading the harness's stdout.
        """
        control = self._drive("mutate_ha_birth_message.py", ["mqtt.py"])
        self.assertNotIn("error", control,
                         f"{control.get('error')} {_tree_note(control)}")
        self.assertTrue(
            control["applied"],
            "CONTROL FAILED - an IN_PLACE harness driven through this probe "
            "did not put a mutant on disk, so `applied: false` below would be "
            "consistent with the probe never reaching a mutant at all. Every "
            "verdict in this test is void.")

        for harness in sorted(self.SANDBOX_TARGETS):
            with self.subTest(harness=harness):
                verdict = self._drive(harness, self.SANDBOX_TARGETS[harness])
                # ORDER IS LOAD-BEARING, and getting it wrong was a HIGH
                # finding on the first version of this test. The three
                # assertions diagnose three different faults, and the first one
                # to fire is the message a maintainer acts on. `sandbox_found`
                # fires when the PROBE could not locate the copy; `error` and
                # `applied` fire when the HARNESS is broken. Put the probe's
                # own complaint first and a real loss of sandboxing - a mutant
                # sitting in the working tree, which is what this test exists
                # to catch - gets reported as "the probe could not find your
                # sandbox", sending the reader to the wrong file entirely.
                self.assertNotIn(
                    "error", verdict,
                    f"{harness}: {verdict.get('error')}. If this says main() "
                    f"returned without the injected interrupt, suspect "
                    f"RUNNER_SHAPE[{harness!r}] "
                    f"(declared {self.RUNNER_SHAPE[harness]!r}) before "
                    f"suspecting the harness - a bool handed to an rc-shaped "
                    f"runner aborts it at its own control A.")
                # THE TREE NOTE BELONGS ON THIS ASSERTION, not only on the
                # `restored` one below it (T-554.1). The order above is
                # load-bearing and deliberately not changed - but it means that
                # in the scenario this message exists for, a SANDBOXED harness
                # regressing to writing the live tree, THIS is the message the
                # maintainer reads and the `restored` one is unreachable. Left
                # bare, it says "a mutant was on disk IN THE WORKING TREE", the
                # maintainer runs `git status`, finds it clean because the
                # probe repaired it, and concludes the probe is broken.
                self.assertFalse(
                    verdict["applied"],
                    f"{harness} is listed SANDBOXED, but a mutant was on disk "
                    f"IN THE WORKING TREE when the battery was interrupted. It "
                    f"is writing the originals, so its exemption from the "
                    f"interrupted-battery restore check is unearned. "
                    f"{_tree_note(verdict)}")
                self.assertTrue(
                    verdict["restored"],
                    f"{harness} left the working tree changed after an "
                    f"interrupt. {_tree_note(verdict)}")
                # Last, because it is the weakest claim of the three: it says
                # the measurement was taken on something real, not that the
                # harness behaved.
                self._assert_a_mutation_was_actually_on_disk(harness, verdict)

    def test_every_harness_declares_a_runner_shape(self):
        """RUNNER_SHAPE is hand-maintained, so it decays - and a missing entry
        is a KeyError in _drive rather than a skipped harness, which is the
        loud direction. This catches it at the table instead."""
        self.assertEqual(
            set(self.RUNNER_SHAPE), set(self.IN_PLACE) | self.SANDBOXED,
            "a mutation harness has no RUNNER_SHAPE entry, so the probe cannot "
            "be pointed at it")
        self.assertEqual({"bool", "rc"}, set(self.RUNNER_SHAPE.values()),
                         "an unrecognised runner shape - the probe only knows "
                         "these two, and anything else silently falls back to "
                         "the bool branch")

    def test_every_sandboxed_harness_declares_its_targets(self):
        """The other hand table. A SANDBOXED harness absent from
        SANDBOX_TARGETS is simply not driven, and its absence is invisible in
        the result of the test that matters."""
        self.assertEqual(
            set(self.SANDBOX_TARGETS), self.SANDBOXED - {"mutate_deploy_verify.py"},
            "a SANDBOXED harness is not driven through the restore probe (or "
            "one is listed that is not SANDBOXED)")
        for harness, targets in sorted(self.SANDBOX_TARGETS.items()):
            for target in targets:
                self.assertTrue(
                    os.path.exists(os.path.join(REPO, target)),
                    f"{harness} declares {target}, which does not exist - so "
                    f"it fingerprints '<missing>' on both sides and can never "
                    f"report a mutant on disk")

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


class ProbeRepairTests(unittest.TestCase):
    """The probe must put the tree back when the restore under test FAILS.

    T-554.1. Every test in MutationHarnessRestoreTests drives a real harness to
    the moment a mutant is on disk and then raises. When the harness restores,
    the tree is clean and nobody notices this question. When it does NOT - the
    case those tests exist to catch - the mutant was left sitting in a shipping
    file, and the failure message never said so. Three things made that worse
    than a dirty file: `assertFalse(applied)` fires before `assertTrue
    (restored)` in the sandboxed test, so the one message that would mention
    the tree is unreachable exactly when it is true; mutate_setup_units.py's
    delete mutant moves gardyn-netwatch.timer out of the tree entirely, and
    DEPLOY.md calls netwatch the one component whose bad version can take the
    host away permanently; and it cascades, because every later test then
    measures against a mutated baseline.

    THESE FIXTURES ARE HERMETIC ON PURPOSE. A test for "the probe repairs a
    working tree" that used the real working tree would have to break the real
    working tree first, in a repo concurrent sessions share. So each builds a
    throwaway directory that is shaped like a repo - `tests/<harness>.py` plus
    the files it mutates - and points the probe at that. REPO is never passed.
    """

    # A harness that applies a mutant and never puts it back. `run_suites` is
    # what the probe replaces; `main` is what it calls. The first two calls are
    # the probe's controls A and B, so the mutation has to land after them and
    # be followed by a third call for the interrupt to catch it mid-mutant.
    _NO_RESTORE = '''\
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT_TARGET = os.path.join(ROOT, "victim.py")
EXEC_TARGET = os.path.join(ROOT, "victim.sh")


def run_suites(*a, **k):
    return True, "Ran 40 tests in 0.100s\\n\\nOK\\n"


def main():
    run_suites()
    run_suites()
    with open(TEXT_TARGET, "w") as fh:
        fh.write("MUTATED\\n")
    # Mode-only damage on the second target. A content-only repair calls this
    # restored and silently ships a script that no longer runs.
    os.chmod(EXEC_TARGET, 0o644)
    run_suites()
    raise AssertionError("unreachable - the probe should have interrupted")
'''

    # Mutates, lets the probe SEE the mutation, and then dies in a way the
    # probe does not model at all. No KeyboardInterrupt, so no verdict is ever
    # printed - which is the exact run the `finally` exists for, and the one
    # most likely to strand a mutant, because nothing got as far as measuring.
    #
    # Driven in "delete" mode on purpose. That is the one mode where the probe
    # keeps handing control back (it polls until a file VANISHES) instead of
    # raising at the first mutant, which is what lets this harness both be
    # observed mutating and then reach its own RuntimeError.
    _NO_RESTORE_RAISES = '''\
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT_TARGET = os.path.join(ROOT, "victim.py")


def run_suites(*a, **k):
    return True, "Ran 40 tests in 0.100s\\n\\nOK\\n"


def main():
    run_suites()
    run_suites()
    with open(TEXT_TARGET, "w") as fh:
        fh.write("MUTANT LEFT BEHIND\\n")
    run_suites()
    raise RuntimeError("a failure mode the probe does not model")
'''

    # Registers its restore with atexit - the shape five of the seven IN_PLACE
    # harnesses really use - and restores the WRONG bytes, which is the defect
    # class these tests exist to catch. atexit runs after the probe's `finally`,
    # so without the probe's own atexit handler being registered FIRST (and
    # therefore running LAST) this harness gets the last word and undoes the
    # repair silently.
    _ATEXIT_WRONG_RESTORE = '''\
import atexit, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT_TARGET = os.path.join(ROOT, "victim.py")


def restore():
    with open(TEXT_TARGET, "w") as fh:
        fh.write("HARNESS RESTORED THE WRONG BYTES\\n")


atexit.register(restore)


def run_suites(*a, **k):
    return True, "Ran 40 tests in 0.100s\\n\\nOK\\n"


def main():
    run_suites()
    run_suites()
    with open(TEXT_TARGET, "w") as fh:
        fh.write("MUTATED\\n")
    run_suites()
    raise AssertionError("unreachable - the probe should have interrupted")
'''

    # Restores CORRECTLY on the way out, then damages the file again from an
    # atexit handler. The probe's `finally` repair therefore finds nothing to
    # do, and the only record of the later damage is the FINALREPAIR line the
    # driver merges back into the verdict.
    _CORRECT_RESTORE_THEN_ATEXIT_DAMAGE = '''\
import atexit, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT_TARGET = os.path.join(ROOT, "victim.py")
ORIGINAL = open(TEXT_TARGET, "rb").read()


def wreck():
    with open(TEXT_TARGET, "wb") as fh:
        fh.write(b"atexit wrote this AFTER the verdict\\n")


atexit.register(wreck)


def run_suites(*a, **k):
    return True, "Ran 40 tests in 0.100s\\n\\nOK\\n"


def main():
    run_suites()
    run_suites()
    try:
        with open(TEXT_TARGET, "wb") as fh:
            fh.write(b"MUTATED\\n")
        run_suites()
    finally:
        with open(TEXT_TARGET, "wb") as fh:
            fh.write(ORIGINAL)
'''

    # Makes the probe's `finally` repair FAIL (by locking the directory it
    # needs to write a temp file into), then fixes the file itself from an
    # atexit handler that also unlocks the directory. So the repair genuinely
    # failed, and by the time the process exits the tree is genuinely clean -
    # which is the state that used to leave a stale `repair_failed` standing.
    _FAILS_THEN_RESTORES_FROM_ATEXIT = '''\
import atexit, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT_TARGET = os.path.join(ROOT, "victim.py")
ORIGINAL = open(TEXT_TARGET, "rb").read()


def restore():
    os.chmod(ROOT, 0o755)
    with open(TEXT_TARGET, "wb") as fh:
        fh.write(ORIGINAL)


atexit.register(restore)


def run_suites(*a, **k):
    return True, "Ran 40 tests in 0.100s\\n\\nOK\\n"


def main():
    run_suites()
    run_suites()
    with open(TEXT_TARGET, "w") as fh:
        fh.write("MUTATED\\n")
    os.chmod(ROOT, 0o555)
    run_suites()
    raise AssertionError("unreachable - the probe should have interrupted")
'''

    # Its target is unreadable when the probe snapshots it. The harness would
    # make it readable and mutate it - but must never get the chance, because
    # a probe that cannot read a target has no original to repair from.
    _MAKES_UNREADABLE_TARGET_READABLE = '''\
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT_TARGET = os.path.join(ROOT, "victim.py")


def run_suites(*a, **k):
    return True, "Ran 40 tests in 0.100s\\n\\nOK\\n"


def main():
    run_suites()
    run_suites()
    os.chmod(TEXT_TARGET, 0o644)
    with open(TEXT_TARGET, "wb") as fh:
        fh.write(b"MUTATED\\n")
    run_suites()
'''

    _NO_RESTORE_DELETE = '''\
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXEC_TARGET = os.path.join(ROOT, "victim.sh")


def run_suites(*a, **k):
    return True, "Ran 40 tests in 0.100s\\n\\nOK\\n"


def main():
    run_suites()
    run_suites()
    os.unlink(EXEC_TARGET)
    run_suites()
    raise AssertionError("unreachable - the probe should have interrupted")
'''

    TEXT_BODY = b"original text target\n"
    EXEC_BODY = b"#!/bin/sh\necho original\n"

    def _fixture(self, harness_source):
        """A throwaway directory shaped like a repo, with two victim files."""
        tmp = tempfile.mkdtemp(prefix="t554-probe-repair-")
        self.addCleanup(_rmtree, tmp)
        os.mkdir(os.path.join(tmp, "tests"))
        with open(os.path.join(tmp, "tests", "mutate_fixture.py"), "w") as fh:
            fh.write(harness_source)
        text = os.path.join(tmp, "victim.py")
        with open(text, "wb") as fh:
            fh.write(self.TEXT_BODY)
        os.chmod(text, 0o644)
        ex = os.path.join(tmp, "victim.sh")
        with open(ex, "wb") as fh:
            fh.write(self.EXEC_BODY)
        os.chmod(ex, 0o755)
        return tmp, text, ex

    def _drive_fixture(self, tmp, targets, mode="text", before_probe=None):
        if before_probe is not None:
            before_probe()
        proc = subprocess.run(
            [sys.executable, "-c", _RESTORE_PROBE, tmp, "mutate_fixture.py",
             json.dumps(targets), mode, "bool"],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
        lines = [ln for ln in proc.stdout.splitlines()
                 if ln.startswith("VERDICT ")]
        indented = "\n".join("    " + ln
                             for ln in proc.stdout[-3000:].splitlines())
        self.assertEqual(1, len(lines),
                         f"fixture probe produced no verdict:\n{indented}")
        return _merge_final_repair(json.loads(lines[0][len("VERDICT "):]),
                                   proc.stdout)

    @staticmethod
    def _mode(path):
        return stat.S_IMODE(os.stat(path).st_mode)

    def test_the_probe_repairs_content_and_mode_a_harness_did_not_restore(self):
        tmp, text, ex = self._fixture(self._NO_RESTORE)
        verdict = self._drive_fixture(tmp, [text, ex])

        self.assertNotIn("error", verdict, verdict.get("error"))
        # The positive control. Without this, every assertion below is
        # satisfied by a probe that never reached the mutation at all - the
        # files would be untouched because nothing touched them.
        self.assertTrue(
            verdict["applied"],
            "CONTROL FAILED - the fixture harness's mutation was not on disk "
            "when the probe interrupted, so this test proves nothing about "
            "repair. Suspect the control count (the probe treats the first two "
            "runner calls as controls A and B).")

        # HONESTY OF THE VERDICT, and the reason repair runs in a `finally`
        # AFTER this is computed. The fixture harness restores nothing, so the
        # only truthful answer is False. If a future edit repairs before
        # measuring, this flips to True and every restore test in this file
        # becomes incapable of failing - which is why it is asserted here
        # rather than left implicit.
        self.assertFalse(
            verdict["restored"],
            "the probe reported the tree restored by a harness that restores "
            "nothing - the repair is running BEFORE the measurement, so "
            "`restored` is now true by construction for every harness.")

        self.assertEqual(
            sorted(["victim.py", "victim.sh"]), sorted(verdict["repaired"]),
            "the probe did not report repairing both damaged targets")

        # THE POINT OF THE TICKET: the tree is actually back.
        with open(text, "rb") as fh:
            self.assertEqual(self.TEXT_BODY, fh.read(),
                             "the probe left a text mutant on disk")
        with open(ex, "rb") as fh:
            self.assertEqual(self.EXEC_BODY, fh.read(),
                             "the probe corrupted a target it only needed to "
                             "chmod back")
        self.assertEqual(
            0o755, self._mode(ex),
            "the probe restored content but not MODE - a reconstruction that "
            "demotes 100755 to 100644 leaves a clean `git status` behind a "
            "script that no longer runs.")
        self.assertEqual(0o644, self._mode(text))

    def test_the_probe_recreates_a_file_the_harness_deleted(self):
        """The deletion path is different code and has to come back with both
        halves. A repair that writes bytes and lets the umask choose the mode
        is the exact trap this project has hit before."""
        tmp, _text, ex = self._fixture(self._NO_RESTORE_DELETE)
        verdict = self._drive_fixture(tmp, [ex], mode="delete")

        self.assertNotIn("error", verdict, verdict.get("error"))
        self.assertTrue(
            verdict["deleted"],
            "CONTROL FAILED - the probe never caught the file missing, so the "
            "deletion repair was not exercised.")
        self.assertFalse(verdict["restored"])
        self.assertEqual(["victim.sh"], verdict["repaired"])

        self.assertTrue(os.path.exists(ex),
                        "the probe did not recreate a deleted target")
        with open(ex, "rb") as fh:
            self.assertEqual(self.EXEC_BODY, fh.read())
        self.assertEqual(
            0o755, self._mode(ex),
            "the deleted file came back with the wrong permissions")

    def test_the_probe_removes_a_file_that_did_not_exist_before(self):
        """The mirror case, and the one a content-only repair gets wrong in the
        other direction: a target absent at snapshot time must be REMOVED, not
        rewritten as an empty file. `<missing>` is a fingerprint value, so
        without this the repair would write b"" and call it restored."""
        tmp, _text, ex = self._fixture(self._NO_RESTORE)
        absent = os.path.join(tmp, "victim.py")
        os.unlink(absent)
        # The fixture harness CREATES victim.py, which is the mutation here.
        verdict = self._drive_fixture(tmp, [absent, ex])

        self.assertNotIn("error", verdict, verdict.get("error"))
        self.assertTrue(verdict["applied"])
        self.assertIn("victim.py", verdict["repaired"])
        self.assertFalse(
            os.path.exists(absent),
            "the probe left behind a file that did not exist before the "
            "harness ran, or recreated it empty instead of removing it")


    def test_the_probe_repairs_even_when_it_prints_no_verdict(self):
        """The `finally` path, which is the change's centrepiece and the run
        most likely to strand a mutant - nothing got as far as measuring.

        Every other test here reaches the probe through
        `except KeyboardInterrupt`, where a verdict exists. A review found that
        a mutant collapsing the `finally` into the verdict branch survived the
        whole module, so this is the test that was missing.
        """
        tmp, text, _ex = self._fixture(self._NO_RESTORE_RAISES)
        proc = subprocess.run(
            [sys.executable, "-c", _RESTORE_PROBE, tmp, "mutate_fixture.py",
             json.dumps([text]), "delete", "bool"],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
        # The CONTROL for this test: the probe must really have died the way
        # the fixture intended. Without this, a probe that failed to import the
        # harness at all would leave the file untouched and pass below.
        self.assertIn("RuntimeError", proc.stdout,
                      "CONTROL FAILED - the fixture harness did not reach its "
                      "un-modelled failure, so the `finally` path was never "
                      "exercised")
        self.assertNotIn("VERDICT ", proc.stdout,
                         "the probe printed a verdict for a run it cannot "
                         "measure, so this no longer tests the no-verdict path")

        with open(text, "rb") as fh:
            self.assertEqual(
                self.TEXT_BODY, fh.read(),
                "the probe stranded a mutant on a path it prints no verdict "
                "for - the repair is not running in a `finally`, so it is "
                "skipped by exactly the failure mode it matters most for.")

    def test_the_probe_gets_the_last_word_over_an_atexit_restore(self):
        """Five of the seven IN_PLACE harnesses restore from `atexit`, which
        runs AFTER the probe's `finally`. The probe's own repair is registered
        before the harness is imported so that LIFO puts it last; if that
        ordering is lost, the harness's restore overwrites the repair and a
        harness that restores the WRONG bytes does so silently while the
        verdict still reports the paths as repaired."""
        tmp, text, _ex = self._fixture(self._ATEXIT_WRONG_RESTORE)
        verdict = self._drive_fixture(tmp, [text])

        self.assertNotIn("error", verdict, verdict.get("error"))
        self.assertTrue(verdict["applied"],
                        "CONTROL FAILED - no mutation was on disk, so nothing "
                        "was there for either restore to fight over")
        self.assertFalse(verdict["restored"])

        with open(text, "rb") as fh:
            self.assertEqual(
                self.TEXT_BODY, fh.read(),
                "the harness's atexit restore got the last word and wrote its "
                "own wrong bytes over the probe's repair. The probe's repair "
                "must be registered with atexit BEFORE the harness module is "
                "imported (atexit is LIFO, so first registered runs last).")
        self.assertIn(
            "victim.py", verdict.get("repaired") or [],
            "the verdict does not report the final on-disk state - a "
            "FINALREPAIR line was printed after the verdict and not merged "
            "into it, so `repaired` describes a state that no longer exists.")

    def test_a_concurrent_writers_edit_is_reported_and_not_reverted(self):
        """The repair must not silently revert somebody else's work.

        Peer sessions on this machine share ONE worktree, so a blanket "rewrite
        anything that differs from the snapshot" would revert a concurrent
        save to a shipping file with no error and no git trace. The repair is
        therefore scoped to paths the probe SAW this harness change; anything
        else that differs is reported under `repair_skipped` and left alone.
        """
        tmp, text, ex = self._fixture(self._NO_RESTORE)
        # `ex` is declared as a target and this harness does change it (a
        # chmod), so it is repairable. Stand in for the peer with a THIRD file
        # the harness never touches.
        peer = os.path.join(tmp, "peer.py")
        with open(peer, "wb") as fh:
            fh.write(b"peer original\n")
        os.chmod(peer, 0o644)
        peer_edit = b"peer was here, mid-run\n"

        verdict = self._drive_fixture(tmp, [text, ex, peer])
        self.assertNotIn("error", verdict, verdict.get("error"))
        self.assertTrue(verdict["applied"])

        # It repaired what it saw the harness do...
        self.assertEqual(sorted(["victim.py", "victim.sh"]),
                         sorted(verdict["repaired"]))
        # ...and left the untouched third file alone, with nothing to skip
        # because it never diverged.
        self.assertEqual([], verdict["repair_skipped"])
        with open(peer, "rb") as fh:
            self.assertEqual(b"peer original\n", fh.read())

        # THE LIVE CASE, and the one the first version of this test could not
        # reach. A declared target diverges WHILE the harness is mid-battery -
        # so the probe has reached a mutant, and the only thing separating this
        # file from the mutant is that the probe never saw the harness touch
        # THIS one. A review found the gate here was dead code (it also
        # required `not reached_mutant`, which is false by then), so every
        # divergent target was being rewritten and a peer's save really was
        # being eaten. This arm is what would have caught it.
        tmp3, text3, _ex3 = self._fixture(self._MUTATES_ONE_THEN_A_PEER_WRITES)
        peer3 = os.path.join(tmp3, "peer.py")
        with open(peer3, "wb") as fh:
            fh.write(b"peer original\n")
        verdict3 = self._drive_fixture(tmp3, [text3, peer3], mode="delete")

        # CONTROL: the probe really did get deep into the battery. Without this
        # the assertions below are satisfied by a run that never reached a
        # mutant, which is the WEAK case and is covered separately.
        self.assertEqual(
            "main() returned without the injected interrupt",
            verdict3.get("error"),
            "CONTROL FAILED - this fixture must run to completion in delete "
            "mode so the peer write lands after the probe's last look")
        self.assertIn(
            "victim.py", verdict3["repaired"],
            "the probe did not put back the mutant it watched the harness "
            "apply, so the per-path gate has gone too far the other way")
        self.assertEqual(
            ["peer.py"], verdict3["repair_skipped"],
            "the probe rewrote a file it never saw this harness touch. On the "
            "real repo that is a peer session's save to mqtt.py disappearing "
            "with no error and no git trace.")
        with open(peer3, "rb") as fh:
            self.assertEqual(b"peer edited me mid-battery\n", fh.read(),
                             "the peer's edit was reverted")

        # And the run that never reaches a mutant at all, which is the weaker
        # case the gate also has to cover.
        tmp2, text2, _ex2 = self._fixture(self._NO_RESTORE_CONTROLS_ONLY)
        peer2 = os.path.join(tmp2, "peer.py")
        with open(peer2, "wb") as fh:
            fh.write(b"peer original\n")
        verdict2 = self._drive_fixture(tmp2, [text2, peer2])

        self.assertEqual("main() returned without the injected interrupt",
                         verdict2.get("error"),
                         "CONTROL FAILED - this fixture is supposed to return "
                         "without ever reaching a mutant; if it did reach one, "
                         "the attribution question below never arises")
        self.assertEqual(
            [], verdict2["repaired"],
            "the probe WROTE to a file in a run where it never observed the "
            "harness mutate anything. On a shared worktree that is how a peer "
            "session's save gets reverted with no error and no git trace.")
        self.assertEqual(
            ["peer.py"], verdict2["repair_skipped"],
            "the probe left the divergent file alone but did not SAY so - an "
            "unreported skip is indistinguishable from having not noticed")
        with open(peer2, "rb") as fh:
            self.assertEqual(peer_edit, fh.read(),
                             "the probe reverted an edit it could not "
                             "attribute to the harness")

    # Reaches a mutant on victim.py - so `reached_mutant` is true and the probe
    # is deep in the battery - and only THEN does peer.py diverge, after the
    # probe's last observation. Driven in delete mode so the probe keeps
    # handing control back instead of raising at the first mutant.
    #
    # THE TIMING IS THE WHOLE POINT AND IT IS NOT ARBITRARY. The probe learns
    # what the harness touched by fingerprinting at each runner call, so a peer
    # write that lands BETWEEN two observations is attributable and one that
    # lands in the same interval as a mutant is not - both files simply read as
    # divergent at the same instant. This fixture models the separable case,
    # which is the one the gate can actually close; the inseparable case is
    # documented as an accepted residual in _repair's comment rather than
    # pretended away here.
    _MUTATES_ONE_THEN_A_PEER_WRITES = '''\
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEXT_TARGET = os.path.join(ROOT, "victim.py")
PEER = os.path.join(ROOT, "peer.py")


def run_suites(*a, **k):
    return True, "Ran 40 tests in 0.100s\\n\\nOK\\n"


def main():
    run_suites()
    run_suites()
    with open(TEXT_TARGET, "w") as fh:
        fh.write("MUTATED\\n")
    run_suites()
    # The concurrent session, landing after the probe's last look.
    with open(PEER, "wb") as fh:
        fh.write(b"peer edited me mid-battery\\n")
'''

    # Returns after its two controls without ever reaching a mutant, having
    # changed a declared target on the way out. Stands in for any writer the
    # probe cannot attribute - a peer session, an editor, a stranded mutant
    # from an earlier run.
    _NO_RESTORE_CONTROLS_ONLY = '''\
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PEER = os.path.join(ROOT, "peer.py")


def run_suites(*a, **k):
    return True, "Ran 40 tests in 0.100s\\n\\nOK\\n"


def main():
    run_suites()
    run_suites()
    with open(PEER, "wb") as fh:
        fh.write(b"peer was here, mid-run\\n")
'''


    def test_a_repair_that_fails_says_so_instead_of_claiming_success(self):
        """The F3 case, and the one that re-created the ticket's own defect.

        The first version of this change hardcoded "the probe has since put it
        back, so `git status` is clean" into every failure message. When the
        repair itself fails, that sentence is false in the one situation where
        a maintainer most needs the truth - and it appeared in the same message
        as the `REPAIR FAILED` entry proving it false.
        """
        tmp, text, _ex = self._fixture(self._NO_RESTORE)
        # Make the write fail: the repair creates a sibling temp file, so
        # removing write permission on the DIRECTORY defeats it while leaving
        # the mutated file perfectly readable for fingerprinting.
        self.addCleanup(os.chmod, tmp, 0o755)
        verdict = self._drive_fixture(tmp, [text], mode="text",
                                      before_probe=lambda: os.chmod(tmp, 0o555))

        self.assertNotIn("error", verdict, verdict.get("error"))
        self.assertEqual([], verdict["repaired"],
                         "a repair that could not write reported success")
        # ONE entry, not two. The repair is attempted again at exit and fails
        # again with a different message, so an un-deduped merge would name the
        # same file twice and read as two damaged files.
        self.assertEqual(1, len(verdict["repair_failed"]),
                         f"the failure was not reported once: {verdict}")
        self.assertTrue(verdict["repair_failed"][0].startswith("victim.py "),
                        verdict["repair_failed"][0])
        self.assertIn("REPAIR FAILED", verdict["repair_failed"][0])

        note = _tree_note(verdict)
        self.assertIn("STILL DIRTY", note,
                      "the failure message tells the maintainer the tree was "
                      "put back while its own data says the repair failed - "
                      "which is exactly the misreading this ticket exists to "
                      "remove, re-created one layer down.")
        self.assertNotIn("put back", note)


    def test_damage_done_after_the_verdict_still_reaches_the_verdict(self):
        """The FINALREPAIR merge, which nothing else covers.

        In the other atexit test the `finally` repair already had work to do,
        so `repaired` was populated whether or not the merge happened. Here the
        harness restores correctly and only damages the file from atexit -
        AFTER the verdict has been printed. The probe's own atexit handler
        fixes it and prints FINALREPAIR, and the driver folding that back in is
        the only way the verdict can describe the real final state.
        """
        tmp, text, _ex = self._fixture(
            self._CORRECT_RESTORE_THEN_ATEXIT_DAMAGE)
        verdict = self._drive_fixture(tmp, [text])

        self.assertNotIn("error", verdict, verdict.get("error"))
        self.assertTrue(verdict["applied"])
        # The harness's in-band restore really is correct, which is what makes
        # the `finally` repair a no-op and isolates the merge.
        self.assertTrue(
            verdict["restored"],
            "CONTROL FAILED - this fixture is supposed to restore correctly "
            "in-band; if it does not, the `finally` repair has work to do and "
            "this test no longer isolates the atexit path")

        with open(text, "rb") as fh:
            self.assertEqual(self.TEXT_BODY, fh.read(),
                             "atexit damage after the verdict was not undone")
        self.assertEqual(
            ["victim.py"], verdict["repaired"],
            "the verdict does not mention a repair that happened after it was "
            "printed - the driver is not merging the FINALREPAIR line, so "
            "`repaired` describes a state that was already out of date.")

    def test_an_unreadable_target_stops_the_probe_rather_than_being_deleted(self):
        """snapshot() must treat 'unreadable' and 'absent' as different things.

        The repair for 'absent before' is os.unlink. So a snapshot that
        recorded any OSError as absent would let a momentary permission problem
        turn into the DELETION of a shipping file later in the same run. Dying
        at snapshot time is the safe direction: nothing has been mutated yet.
        """
        tmp, text, _ex = self._fixture(self._MAKES_UNREADABLE_TARGET_READABLE)
        self.addCleanup(os.chmod, text, 0o644)
        os.chmod(text, 0o000)

        proc = subprocess.run(
            [sys.executable, "-c", _RESTORE_PROBE, tmp, "mutate_fixture.py",
             json.dumps([text]), "text", "bool"],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
        self.assertIn("PermissionError", proc.stdout,
                      f"CONTROL FAILED - the probe did not hit the unreadable "
                      f"target at all, so nothing here is about snapshot(): "
                      f"{proc.stdout[-800:]}")
        self.assertTrue(
            os.path.exists(text),
            "the probe DELETED a target it merely could not read - snapshot() "
            "is catching OSError where it must catch FileNotFoundError, so "
            "'unreadable' was recorded as 'absent' and the repair for absent "
            "is os.unlink.")


    def test_a_failure_the_tree_no_longer_shows_is_dropped_from_the_verdict(self):
        """A repair can fail and the tree still end up clean, and then the
        verdict must not keep saying it is dirty.

        Found by review, as the mirror of the defect this whole change fixes.
        The `finally` repair records a failure; the harness's own atexit
        restore then puts the file back; the probe's final handler measures
        what is STILL dirty at exit and the driver drops any recorded failure
        the tree no longer shows. Without that, `_tree_note` leads with
        *** THE WORKING TREE IS STILL DIRTY *** over a clean tree - which is
        exactly the kind of false statement about the tree the ticket was
        filed to remove, pointing the other way.
        """
        tmp, text, _ex = self._fixture(self._FAILS_THEN_RESTORES_FROM_ATEXIT)
        self.addCleanup(os.chmod, tmp, 0o755)
        proc = subprocess.run(
            [sys.executable, "-c", _RESTORE_PROBE, tmp, "mutate_fixture.py",
             json.dumps([text]), "text", "bool"],
            cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
        )
        raw = [json.loads(ln[len("VERDICT "):]) for ln in proc.stdout.splitlines()
               if ln.startswith("VERDICT ")]
        self.assertEqual(1, len(raw), proc.stdout[-2000:])

        # CONTROL, on the RAW verdict rather than the merged one: the `finally`
        # repair really did fail. If the directory lock did not take, the
        # assertions below are satisfied by there being nothing to drop, which
        # is true for entirely the wrong reason.
        self.assertEqual(
            1, len(raw[0]["repair_failed"]),
            f"CONTROL FAILED - the in-band repair did not fail, so there is no "
            f"stale failure for the merge to drop:\n{proc.stdout[-2000:]}")
        with open(text, "rb") as fh:
            self.assertEqual(
                self.TEXT_BODY, fh.read(),
                "CONTROL FAILED - the harness's atexit restore did not put the "
                "file back, so the tree is genuinely dirty and the failure "
                "SHOULD stand")

        verdict = _merge_final_repair(raw[0], proc.stdout)
        self.assertNotIn("error", verdict, verdict.get("error"))
        self.assertEqual(
            [], verdict["repair_failed"],
            f"a repair failure was reported for a file the tree no longer "
            f"shows as dirty: {verdict['repair_failed']}")
        self.assertNotIn(
            "STILL DIRTY", _tree_note(verdict),
            "the failure message announces a dirty working tree over a clean "
            "one, which is the same class of false claim about the tree that "
            "this change exists to remove")


class TreeNoteTests(unittest.TestCase):
    """_tree_note renders the sentence every failure message above depends on.

    Tested directly because it is pure, because getting it wrong is the F3
    blocker, and because reaching all three branches through a real probe run
    needs three separate fixtures.
    """

    def test_a_failed_repair_leads_and_never_claims_the_tree_is_clean(self):
        note = _tree_note({"repaired": ["a.py"],
                           "repair_failed": ["b.py (REPAIR FAILED: nope)"],
                           "repair_skipped": ["c.py"]})
        self.assertTrue(note.startswith("*** THE WORKING TREE IS STILL DIRTY"),
                        f"the bad news does not sort first: {note}")
        self.assertIn("b.py", note)
        self.assertNotIn("put back", note)

    def test_a_successful_repair_says_git_status_is_not_the_evidence(self):
        note = _tree_note({"repaired": ["a.py"], "repair_failed": [],
                           "repair_skipped": []})
        self.assertIn("a.py", note)
        self.assertIn("`git status`", note)
        self.assertNotIn("STILL DIRTY", note)

    def test_a_skipped_path_is_named_and_attributed_to_a_concurrent_writer(self):
        note = _tree_note({"repaired": [], "repair_failed": [],
                           "repair_skipped": ["mqtt.py"]})
        self.assertIn("mqtt.py", note)
        self.assertIn("concurrent", note)

    def test_nothing_to_report_is_still_a_sentence(self):
        self.assertIn("nothing to put back", _tree_note({}))


class ProbeSourceTests(unittest.TestCase):
    """_RESTORE_PROBE is a program embedded as a triple-quoted string.

    That makes one editing mistake both easy and quiet: a nested triple quote -
    a docstring on one of the probe's own functions is the natural way to write
    it - terminates the enclosing literal. It happened once while writing
    T-554.1. The outer file then fails to parse, which is loud; but a nested
    quote that happens to BALANCE would silently truncate the probe into
    something that still parses and no longer does what it says.
    """

    def test_the_embedded_probe_is_valid_python(self):
        import ast
        ast.parse(_RESTORE_PROBE)

    def test_the_probe_carries_no_triple_quote(self):
        self.assertNotIn(
            '"""', _RESTORE_PROBE,
            "a triple quote inside _RESTORE_PROBE terminates the string that "
            "contains it - use # comments for every note in the probe, "
            "including function docstrings.")
        # POSITIVE CONTROL: the scan can find a triple quote when one is there.
        self.assertIn('"""', _RESTORE_PROBE + '"""')


def _rmtree(path):
    import shutil
    shutil.rmtree(path, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
