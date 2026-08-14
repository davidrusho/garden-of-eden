"""The scoring rule every mutation battery is judged by, pinned directly.

tests/mutation_scoring.py exists because the same defect - a red run scored as
a KILL when the behaviour under test never executed - was found and fixed three
separate times in this repo's harnesses. A rule with one implementation needs
one test, and this is it.

THE TESTS CALL THE RULE; THEY DO NOT RESTATE IT. A regex or a threshold copied
into a test scores itself, and widening the module's copy then changes nothing
the test can see. Everything below imports `mutation_scoring` and drives it.

THE INPUTS ARE REAL unittest OUTPUT, not fixtures written by hand. A fixture
written by the parser's own author shares its blind spot by construction, and
this rule's whole subject is a shape - `unittest.loader._FailedTest` - that is
easy to get subtly wrong from memory. `_UnittestOutput` below generates throw-
away modules in a TemporaryDirectory OUTSIDE the repository and runs the real
`python -m unittest` over them, so the strings under test are produced by the
tool whose output the rule parses.

Why outside the repository: tests/test_suite_isolation.py exists to keep this
tree clean, and a suite that writes modules into it - even briefly - is a
concurrent writer on the thing every other harness measures.

Run:  python3 -m unittest tests.test_mutation_scoring
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import mutation_scoring as ms  # noqa: E402


class _UnittestOutput:
    """Runs real unittest over generated modules and returns (ok, output).

    A cache keyed on the module source, because each call is a subprocess and
    several cases below want the same three shapes.
    """

    _cache = {}

    @classmethod
    def run(cls, **modules):
        """modules: name -> source. Returns (all_passed, combined_output)."""
        key = tuple(sorted(modules.items()))
        if key in cls._cache:
            return cls._cache[key]
        with tempfile.TemporaryDirectory() as tmp:
            pkg = os.path.join(tmp, "genpkg")
            os.mkdir(pkg)
            open(os.path.join(pkg, "__init__.py"), "w").close()
            for name, source in modules.items():
                with open(os.path.join(pkg, name + ".py"), "w") as fh:
                    fh.write(textwrap.dedent(source))
            proc = subprocess.run(
                [sys.executable, "-m", "unittest"]
                + [f"genpkg.{name}" for name in sorted(modules)],
                cwd=tmp, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True,
                env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
            )
        result = (proc.returncode == 0, proc.stdout)
        cls._cache[key] = result
        return result


_GREEN_SRC = """
    import unittest
    class T(unittest.TestCase):
        def test_a(self): pass
        def test_b(self): pass
        def test_c(self): pass
"""

_ONE_FAILURE_SRC = """
    import unittest
    class T(unittest.TestCase):
        def test_a(self): self.assertEqual(1, 0)
        def test_b(self): pass
        def test_c(self): pass
"""

# The shape the whole no-verdict rule exists for: valid Python that dies at
# IMPORT. unittest reports this as a NAMED error through _FailedTest, which is
# why the zero-named-cases tell alone cannot see it (T-527.18).
_IMPORT_DEATH_SRC = """
    import a_module_that_certainly_does_not_exist
    import unittest
    class T(unittest.TestCase):
        def test_a(self): pass
        def test_b(self): pass
        def test_c(self): pass
"""


class RealUnittestShapes(unittest.TestCase):
    """The generator itself, controlled first.

    If these three do not produce three DIFFERENT shapes, every verdict below
    is void - they would all be scoring the same string.
    """

    def test_the_three_generated_shapes_are_actually_distinct(self):
        green_ok, green = _UnittestOutput.run(test_x=_GREEN_SRC)
        fail_ok, fail = _UnittestOutput.run(test_x=_ONE_FAILURE_SRC)
        dead_ok, dead = _UnittestOutput.run(test_x=_IMPORT_DEATH_SRC)

        self.assertTrue(green_ok, f"CONTROL FAILED - green case is red:\n{green}")
        self.assertFalse(fail_ok, "CONTROL FAILED - failing case is green")
        self.assertFalse(dead_ok, "CONTROL FAILED - import-death case is green")

        self.assertEqual(3, ms.ran_count(green))
        self.assertEqual(3, ms.ran_count(fail),
                         "a failing test still gets COLLECTED; if this is not "
                         "3 the ran-count tell is measuring the wrong thing")
        self.assertEqual(
            1, ms.ran_count(dead),
            f"the import-death case must COLLAPSE the collected count - that "
            f"collapse is the only tell for this shape. Got:\n{dead}")

    def test_an_import_death_is_reported_as_a_NAMED_error(self):
        """The premise the ran-count tell exists for, asserted rather than
        assumed. If unittest ever stopped naming _FailedTest, the zero-named-
        cases tell would cover this shape on its own and the ran-count
        comparison would be dead weight - so pin which world we are in."""
        _, dead = _UnittestOutput.run(test_x=_IMPORT_DEATH_SRC)
        fails = ms.named_failures(dead)
        self.assertTrue(
            fails,
            "an unimportable module produced NO named FAIL:/ERROR: line. The "
            "zero-named-cases tell would now cover this shape by itself; "
            "re-derive the rule before simplifying it.")
        self.assertIn("_FailedTest", fails[0])


class ScoreRunVerdicts(unittest.TestCase):

    def test_a_green_run_is_a_SURVIVOR(self):
        ok, out = _UnittestOutput.run(test_x=_GREEN_SRC)
        verdict, fails = ms.score_run(ok, out, clean_ran=3)
        self.assertEqual(ms.SURVIVED, verdict)
        self.assertEqual([], fails)

    def test_a_red_run_with_named_cases_and_a_STABLE_count_is_a_KILL(self):
        ok, out = _UnittestOutput.run(test_x=_ONE_FAILURE_SRC)
        verdict, fails = ms.score_run(ok, out, clean_ran=3)
        self.assertEqual(ms.KILLED, verdict)
        self.assertEqual(1, len(fails), fails)

    def test_an_IMPORT_DEATH_is_NO_VERDICT_despite_naming_a_case(self):
        """The T-527.18 correction, and the reason this module exists.

        This mutant shape reddens the run AND names an ERROR, so a scorer
        reading colour - or reading colour plus 'did anything get named' -
        calls it a kill. The behaviour under test never executed.
        """
        ok, out = _UnittestOutput.run(test_x=_IMPORT_DEATH_SRC)
        verdict, fails = ms.score_run(ok, out, clean_ran=3)
        self.assertEqual(
            ms.NO_VERDICT, verdict,
            f"an import-time death scored '{verdict}' with {len(fails)} named "
            f"case(s). This is the exact defect the rule exists to prevent.")
        self.assertTrue(fails, "the named case is present - that is the point")

    def test_a_red_run_naming_NOTHING_is_NO_VERDICT(self):
        """The other tell, on the input it is actually for.

        Driven through score_run with clean_ran deliberately MATCHING, so the
        ran-count branch cannot be what produces the verdict. Without that the
        two tells are indistinguishable in this test and either one alone
        would satisfy it.
        """
        out = "Ran 3 tests in 0.001s\n\nFAILED (errors=1)\n"
        verdict, fails = ms.score_run(False, out, clean_ran=3)
        self.assertEqual(ms.NO_VERDICT, verdict)
        self.assertEqual([], fails)

    def test_omitting_clean_ran_DISABLES_the_ran_count_tell(self):
        """Documented behaviour, pinned so nobody 'simplifies' the parameter
        away believing it is optional decoration.

        Same input as the import-death case above, scored without a baseline:
        it comes back KILLED. That is the blindness a harness accepts by not
        passing one, and it is the pre-T-527.18 behaviour verbatim.
        """
        ok, out = _UnittestOutput.run(test_x=_IMPORT_DEATH_SRC)
        self.assertEqual(ms.KILLED, ms.score_run(ok, out)[0])
        self.assertEqual(ms.NO_VERDICT, ms.score_run(ok, out, clean_ran=3)[0])

    def test_a_count_that_moves_UPWARD_is_also_NO_VERDICT(self):
        """Not symmetry for its own sake - a mutant that REINTRODUCES deleted
        code can add collected tests, and tests/mutate_retired_entities.py has
        six such mutants. An honest mutant changes how many tests PASS, never
        how many are collected, in either direction."""
        out = "FAIL: test_a (genpkg.test_x.T.test_a)\nRan 5 tests in 0.001s\n\nFAILED (failures=1)\n"
        self.assertEqual(ms.NO_VERDICT, ms.score_run(False, out, clean_ran=3)[0])


class RanCountParsing(unittest.TestCase):

    def test_it_sums_across_several_suites(self):
        """A harness runs several suites per verdict and concatenates them, so
        a parser reading only the first match undercounts and every mutant
        scores no-verdict.

        THE CONCATENATION IS THE POINT, and getting it wrong makes this test
        vacuous. Running `unittest a b` in ONE invocation emits ONE summary
        line, so it cannot distinguish a sum from a first-match read - that was
        this test's original shape and a mutant replacing the sum with
        `int(matches[0])` survived it. Every harness in this repo runs each
        suite as its OWN subprocess and joins the outputs, which is reproduced
        here.
        """
        ok_x, out_x = _UnittestOutput.run(test_x=_GREEN_SRC)
        ok_y, out_y = _UnittestOutput.run(test_y=_GREEN_SRC)
        self.assertTrue(ok_x and ok_y, out_x + out_y)
        combined = f"--- x ---\n{out_x}\n--- y ---\n{out_y}"
        self.assertEqual(
            2, combined.count("Ran 3 tests"),
            "CONTROL FAILED - the combined output does not carry two separate "
            "summary lines, so this cannot tell a sum from a first-match read")
        self.assertEqual(6, ms.ran_count(combined))

    def test_it_reads_the_SINGULAR_form(self):
        """unittest writes 'Ran 1 test', not 'Ran 1 tests' - and 1 is exactly
        the count an import-death collapses to, so a parser that misses the
        singular reads 0 for the shape the rule is built around."""
        _, dead = _UnittestOutput.run(test_x=_IMPORT_DEATH_SRC)
        self.assertIn("Ran 1 test in", dead)
        self.assertEqual(1, ms.ran_count(dead))

    def test_no_summary_line_at_all_reads_zero(self):
        self.assertEqual(0, ms.ran_count("segmentation fault\n"))


class NamedFailureParsing(unittest.TestCase):

    def test_it_takes_FAIL_and_ERROR_lines_and_nothing_else(self):
        _, out = _UnittestOutput.run(test_x=_ONE_FAILURE_SRC)
        fails = ms.named_failures(out)
        self.assertEqual(1, len(fails), fails)
        self.assertTrue(fails[0].startswith("FAIL: test_a"), fails)

    def test_an_EMBEDDED_error_line_is_not_a_named_case(self):
        """The lines must be matched at the START of the line, not anywhere in
        it, because counting an embedded one inflates the failing-case count
        `format_verdict` reports - the one number a reader uses to tell an
        honest kill from a broad one.

        THE FIXTURE HAS TO CONTAIN AN UPPERCASE `ERROR:` THAT IS NOT A CASE, or
        it cannot exhibit the defect. This test's first version used
        'AssertionError: 1 != 0' and 'ImportError: something', and a mutant
        swapping `startswith` for `in` SURVIVED it: `in` is case-sensitive, so
        neither of those contains `ERROR:` at all and the two implementations
        agreed. The real source of an embedded uppercase one is this repo's own
        habit of pasting a subprocess's captured unittest output into an
        assertion message - tests/test_suite_isolation.py does exactly that -
        so the fixture below is that shape, indented as a real one is.
        """
        out = ("FAIL: test_a (genpkg.test_x.T.test_a)\n"
               "AssertionError: probe produced no verdict:\n"
               "    ERROR: test_x (unittest.loader._FailedTest.test_x)\n"
               "    FAIL: test_inner (genpkg.test_y.T.test_inner)\n"
               "ImportError: something\n")
        self.assertEqual(["FAIL: test_a (genpkg.test_x.T.test_a)"],
                         ms.named_failures(out))


class CompileGate(unittest.TestCase):

    def test_it_accepts_valid_python(self):
        self.assertIsNone(ms.compile_gate("x = 1\n", "<probe>"))

    def test_it_refuses_a_syntax_error_and_says_where(self):
        message = ms.compile_gate("def f(:\n    pass\n", "<probe>")
        self.assertIsNotNone(
            message, "CONTROL FAILED - the gate passed invalid Python, so "
                     "every 'accepts valid python' result above is void")
        self.assertIn("line", message)

    def test_it_catches_the_INDENT_shape_specifically(self):
        """The dominant real cause: a replacement inserted at the wrong indent.
        A gate that only handles a mangled `def` would miss it, and this is the
        one that actually happens when an anchor's surrounding whitespace
        shifts."""
        self.assertIsNotNone(ms.compile_gate("x = 1\n  y = 2\n", "<probe>"))

    def test_it_does_NOT_promise_the_mutant_will_import(self):
        """The narrowness of the gate, stated as a test so nobody widens the
        claim in a comment. `compile()` is syntax only - this source compiles
        and dies the moment it is imported, which is exactly why score_run's
        third verdict has to exist."""
        self.assertIsNone(
            ms.compile_gate("import a_module_that_certainly_does_not_exist\n",
                            "<probe>"))


class VerdictFormatting(unittest.TestCase):

    def test_a_kill_reports_its_named_case_COUNT(self):
        """A real kill names one or two cases; `killed (0 failing case(s))` is
        what a module that stopped importing looks like. The count is the only
        tell, so it must be in the line."""
        line = ms.format_verdict(ms.KILLED, ["FAIL: a", "FAIL: b"])
        self.assertIn("2 failing case(s)", line)

    def test_each_verdict_formats_distinguishably(self):
        lines = {ms.format_verdict(v, [])
                 for v in (ms.SURVIVED, ms.KILLED, ms.NO_VERDICT)}
        self.assertEqual(3, len(lines), lines)


if __name__ == "__main__":
    unittest.main()
