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
import ast
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import mutation_scoring as ms  # noqa: E402


def _quoted(out):
    """Indent captured output before pasting it into an assertion message.

    A message is only ever printed when the run is RED, and it is printed into
    the very stream a mutation harness parses. Pasted flush-left, a captured
    summary line ("Ran 3 tests in 0.001s") is indistinguishable from this
    suite's own, so it is added to `ran_count` and the harness reads the moved
    count as an import death - turning every genuine kill into NO VERDICT.
    Indenting is enough, because both parsers anchor at column 0, and it keeps
    the evidence intact where a redaction would not.

    The same habit is what `test_an_EMBEDDED_error_line_is_not_a_named_case`
    was written for on the FAIL:/ERROR: side.
    """
    return "\n".join("    " + line for line in out.splitlines())


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


# Docstrings that LOOK like unittest's own summary. unittest prints a failing
# test's docstring first line on its own line at COLUMN 0, so these are the
# forgery the anchor exists to defeat - and the third one is the residual it
# cannot. Kept as a module constant so the shapes are visible beside the
# fixtures they defeat.
_FORGING_DOCSTRINGS_SRC = """
    import unittest
    class T(unittest.TestCase):
        def test_quotes_a_summary_mid_line(self):
            "unittest writes 'Ran 1 test', not 'Ran 1 tests' - mid-line."
            self.assertEqual(1, 0)
        def test_docstring_BEGINS_with_a_summary(self):
            "Ran 99 tests is how this docstring begins."
            self.assertEqual(1, 0)
        def test_ok(self):
            pass
"""


class RanCountIsMeasuredAgainstRealUnittestOutput(unittest.TestCase):
    """The column-0 claim, checked against the real thing (T-527.36).

    `ran_count`'s rule rests on a claim about a TEXT FORMAT - "unittest
    prints its summary at column 0 in a fixed shape, and nothing else does".
    A fixture written by the same author as the rule shares its blind spot by
    construction, so these cases run real `unittest` over generated modules
    and read what it actually printed. That is the only arrangement in which
    the suite can contradict the rule.

    It has already done so once: an ANCHOR-ONLY rule (`^Ran (\\d+) tests?`)
    was measured returning 102 for a run that collected 3, because one
    generated docstring began "Ran 99 tests". The anchor was necessary and
    not sufficient, and nothing but real output would have said so.
    """

    def _real(self, src):
        """Run `src` for real and return (ok, output, tests-actually-collected).

        THE BASELINE IS DERIVED INDEPENDENTLY OF THE RULE. An earlier version
        of this helper re-ran `mutation_scoring`'s own regex character for
        character, which made two of the three assertions in the caller below
        the same computation twice - a control cannot contradict a rule it is
        a copy of. unittest prints `separator2` (70 dashes) immediately before
        its summary and prints a failing test's docstring somewhere else
        entirely, so the position is an independent handle on the same fact.
        Found by review, 2026-08-14.
        """
        ok, out = _UnittestOutput.run(test_x=src)
        lines = out.splitlines()
        collected = [ln for i, ln in enumerate(lines)
                     if i and set(lines[i - 1]) == {"-"}
                     and ln.startswith("Ran ")]
        self.assertEqual(
            1, len(collected),
            f"CONTROL FAILED - expected exactly one summary line preceded by "
            f"unittest's separator, found {collected}. There is nothing to "
            f"measure against:\n{_quoted(out)}")
        return ok, out, int(collected[0].split()[1])

    # A failing test whose assertion message pastes captured output. The
    # continuation lines of a multi-line message are printed at COLUMN 0
    # verbatim, so a captured summary inside one is indistinguishable from a
    # real one to any rule reading this stream.
    _PASTED_CAPTURE_SRC = """
        import unittest
        class T(unittest.TestCase):
            def test_pastes_captured_output(self):
                captured = "some preamble\\nRan 99 tests in 0.001s\\n\\nOK\\n"
                self.assertEqual(1, 2, "subprocess said:\\n" + captured)
            def test_ok(self):
                pass
    """

    def test_KNOWN_LIMITATION_a_pasted_capture_defeats_the_anchor(self):
        """The residual the anchor does NOT close, pinned so it stays measured.

        T-554. `ran_count`'s docstring used to claim that unittest's summary is
        the only thing printed at column 0, and offered "a subprocess's
        captured output pasted into an assertion message" as an example of what
        anchoring FIXES. It is the opposite: a multi-line assertion message
        puts its continuation lines at column 0 verbatim, so the anchored rule
        counts a pasted summary exactly as it counts a real one.

        This is not fixable inside `ran_count` - from in there the two lines
        are byte-identical. The repo's real defence is at the paste site, where
        tests/test_suite_isolation.py indents captured output before
        interpolating it. So this test pins the LIMITATION rather than the fix,
        and it should go RED if anyone ever closes the gap - at which point
        delete it and say so in the docstring.
        """
        ok, out, collected = self._real(self._PASTED_CAPTURE_SRC)
        self.assertFalse(ok)
        self.assertEqual(2, collected,
                         "CONTROL FAILED - the module did not collect the two "
                         f"tests it defines:\n{_quoted(out)}")

        # The pasted line really is flush left, which is the whole mechanism.
        self.assertIn("\nRan 99 tests in 0.001s\n", out,
                      "CONTROL FAILED - the captured summary was not printed "
                      f"at column 0, so this tests nothing:\n{_quoted(out)}")

        self.assertEqual(
            101, ms.ran_count(out),
            "the anchored rule no longer counts a pasted capture. If that is "
            "deliberate, this KNOWN_LIMITATION test has served its purpose - "
            "delete it and correct ran_count's docstring, which currently says "
            "the anchor does not touch this shape.")

        # And the anchor buys nothing HERE, which is the part the old docstring
        # got backwards. Unanchored gives the same wrong answer.
        unanchored = sum(int(m) for m in
                         re.findall(r"Ran (\d+) tests? in", out))
        self.assertEqual(
            ms.ran_count(out), unanchored,
            "anchored and unanchored disagree on a pasted capture, so the "
            "anchor DOES help with this shape after all - the docstring's "
            "correction is wrong")

    def test_the_genuine_summary_is_at_column_zero_in_a_fixed_shape(self):
        """POSITIVE CONTROL for every case below. If this shape ever moves,
        the rule is measuring something that no longer exists and every other
        verdict here is void."""
        _, out, collected = self._real(_GREEN_SRC)
        self.assertEqual(3, collected)
        self.assertEqual(3, ms.ran_count(out))
        # This one SPELLS OUT the shape on purpose, and it is not the
        # restatement the module docstring forbids. That rule is about
        # copying THIS PROJECT's rule into its own test, where the copy
        # scores itself. Here the subject is a THIRD PARTY's output format -
        # CPython's `Ran %d test%s in %.3fs` - and pinning it is the entire
        # job: `ran_count` requires that shape, so if CPython ever changes it
        # the rule starts missing real summaries, and a missed summary
        # collapses the count into NO VERDICT. This test is the tripwire for
        # that, and it can only be one by naming the shape.
        self.assertTrue(
            any(re.fullmatch(r"Ran \d+ tests? in [\d.]+s", line)
                for line in out.splitlines()),
            f"unittest's summary is no longer `Ran N test(s) in X.XXXs` at "
            f"column 0. `ran_count` will now MISS real summaries, which reads "
            f"as a collapsed count and suppresses every kill:\n{_quoted(out)}")

    def test_a_docstring_that_FORGES_a_summary_is_not_counted(self):
        """Both forging shapes at once, on real output.

        The mid-line quote is defeated by the anchor. The one that BEGINS
        with "Ran 99 tests" is printed at column 0 and is defeated only by
        requiring the full ` in <float>s` shape - which is why the rule
        carries both halves and not just the anchor.
        """
        _, out, collected = self._real(_FORGING_DOCSTRINGS_SRC)
        self.assertEqual(3, collected)
        # ASSERT THE LINE, NOT A SUBSTRING. The whole mechanism is that
        # unittest prints the docstring AT COLUMN 0; `assertIn` against the
        # whole output matches it anywhere, so prefixing the fixture's
        # docstring with one character moved the forgery off column 0 and
        # this control kept passing - including with the anchor-only mutant
        # applied, which is the one regression it exists to cover. Found by
        # review, 2026-08-14.
        self.assertIn(
            "Ran 99 tests is how this docstring begins.", out.splitlines(),
            "CONTROL FAILED - the forging docstring is not printed at column "
            "0, so this fixture cannot exhibit the defect and the assertion "
            "below measures nothing")
        self.assertEqual(
            3, ms.ran_count(out),
            f"a docstring was counted as a test run. Anchor-only scored 102 "
            f"here.\n{_quoted(out)}")

    def test_KNOWN_LIMITATION_a_docstring_reproducing_the_WHOLE_shape_wins(self):
        """Pinned, not papered over.

        Requiring ` in <float>s` narrows the forgery to a docstring that
        reproduces unittest's entire summary shape. It does not eliminate it,
        and a rule whose residual is undocumented gets rediscovered the
        expensive way. If this test ever fails, somebody has closed the gap -
        delete it and say how.

        Not reachable by accident: it needs a docstring whose first line is
        exactly a well-formed summary. It IS reachable on purpose, and this
        repo writes docstrings about unittest output more than most.
        """
        self.assertEqual(
            107, ms.ran_count("Ran 99 tests in 0.5s\nRan 8 tests in 0.001s"),
            "the whole-shape forgery no longer wins - the rule has been "
            "tightened further, so update this test rather than deleting it")


class RealUnittestShapes(unittest.TestCase):
    """The generator itself, controlled first.

    If these three do not produce three DIFFERENT shapes, every verdict below
    is void - they would all be scoring the same string.
    """

    def test_the_three_generated_shapes_are_actually_distinct(self):
        green_ok, green = _UnittestOutput.run(test_x=_GREEN_SRC)
        fail_ok, fail = _UnittestOutput.run(test_x=_ONE_FAILURE_SRC)
        dead_ok, dead = _UnittestOutput.run(test_x=_IMPORT_DEATH_SRC)

        self.assertTrue(green_ok,
                        f"CONTROL FAILED - green case is red:\n{_quoted(green)}")
        self.assertFalse(fail_ok, "CONTROL FAILED - failing case is green")
        self.assertFalse(dead_ok, "CONTROL FAILED - import-death case is green")

        self.assertEqual(3, ms.ran_count(green))
        self.assertEqual(3, ms.ran_count(fail),
                         "a failing test still gets COLLECTED; if this is not "
                         "3 the ran-count tell is measuring the wrong thing")
        self.assertEqual(
            1, ms.ran_count(dead),
            f"the import-death case must COLLAPSE the collected count - that "
            f"collapse is the only tell for this shape. Got:\n{_quoted(dead)}")

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

    def test_a_clean_ran_of_ZERO_still_arms_the_tell(self):
        """`is not None`, never truthiness - and only a baseline of 0 can
        tell the two spellings apart.

        A mutant swapping `if clean_ran is not None` for `if clean_ran`
        SURVIVED (found by review, 2026-08-14), because every case here
        passed either None or 3. Zero is a real baseline: a suite whose
        collection is already broken on the clean tree reports `Ran 0 tests`,
        and that is exactly when a harness most needs the tell armed rather
        than silently disabled. Under the mutant this returns KILLED, which
        credits the suite for a red run that collected nothing.
        """
        out = ("ERROR: test_x (unittest.loader._FailedTest.test_x)\n"
               "Ran 1 test in 0.000s\n\nFAILED (errors=1)\n")
        self.assertEqual(ms.NO_VERDICT, ms.score_run(False, out, clean_ran=0)[0])

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
        # _quoted() here too, and this is the site that matters most: it is a
        # CONTROL, so it only ever speaks when the generator itself has broken.
        # Pasted flush-left it forged three summary lines (+7 to the count),
        # which moved the battery's ran-count and turned a CONTROL FAILED into
        # a NO VERDICT - "no information" printed by the one assertion whose
        # whole job is to shout. Found by review; the other two sites were
        # fixed and this one was missed.
        self.assertTrue(ok_x and ok_y, _quoted(out_x + out_y))
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

    def test_an_EMBEDDED_summary_line_is_not_counted(self):
        """The ran-count sibling of the FAIL:/ERROR: rule below, and it was
        missing until T-527.36.

        Two forgeries, and they arrive by different routes. PROSE ABOUT a
        summary is the first: this class's own singular-form case has a
        docstring quoting both spellings, unittest prints a docstring in the
        failure header, and that added 2 to the count of every red run of this
        suite - which made a battery over `mutation_scoring.py` report NO
        VERDICT for all fourteen of its mutants. CAPTURED OUTPUT pasted into
        an assertion message is the second, and it is the one this repo
        produces habitually.

        Both are only ever emitted on a RED run, so the inflation lands
        exclusively on the runs being scored. The failure direction is
        suppression rather than a false kill - which is the quieter half of
        why it survived: `score_run` answers "no information", and no
        information is what a reader stops reading.

        The fixture must contain a summary that is REAL-looking but indented,
        or it cannot exhibit the defect - the unanchored and anchored forms
        agree on everything else.
        """
        out = ("FAIL: test_a (genpkg.test_x.T.test_a)\n"
               "unittest writes 'Ran 1 test', not 'Ran 1 tests'\n"
               "AssertionError: the case must collapse the count. Got:\n"
               "    Ran 3 tests in 0.001s\n"
               "\n"
               "    OK\n"
               "Ran 12 tests in 0.400s\n"
               "\n"
               "FAILED (failures=1)\n")
        self.assertEqual(
            12, ms.ran_count(out),
            "only the flush-left summary is this run's own; the quoted "
            "spellings and the indented capture are text ABOUT a run")


class NamedFailureParsing(unittest.TestCase):

    def test_it_takes_FAIL_and_ERROR_lines_and_nothing_else(self):
        """BOTH prefixes, because the name promises both.

        Until T-527.36 this ran only `_ONE_FAILURE_SRC`, which emits a FAIL:
        and no ERROR: at all - so the ERROR half of its own name was true for
        free, and a mutant dropping "ERROR:" from the prefix tuple survived
        the case named for it. The battery caught it the loud way: the mutant
        died, but under three OTHER tests, which is what a
        `killed, but NOT by the test named for it` line is for.
        """
        _, failed = _UnittestOutput.run(test_x=_ONE_FAILURE_SRC)
        fails = ms.named_failures(failed)
        self.assertEqual(1, len(fails), fails)
        self.assertTrue(fails[0].startswith("FAIL: test_a"), fails)

        # An import death is this repo's real source of an ERROR: line -
        # unittest reports the unimportable module through _FailedTest.
        _, dead = _UnittestOutput.run(test_x=_IMPORT_DEATH_SRC)
        errors = ms.named_failures(dead)
        self.assertEqual(1, len(errors), errors)
        self.assertTrue(
            errors[0].startswith("ERROR: "),
            f"the ERROR: prefix is not being collected, so half of this "
            f"test's name is unpinned. Got: {errors}")

    def test_it_preserves_the_ORDER_unittest_printed(self):
        """The docstring promises "in order" and nothing pinned it: the only
        ordering assertion ran on a ONE-element list, where every ordering is
        the same ordering. Mutants wrapping the return in `sorted()` and in
        `reversed()` both SURVIVED (found by review, 2026-08-14).

        Order is not cosmetic here. A harness prints the first few named
        cases as the evidence for a kill, and attribution - "was this mutant
        killed by the test named for it?" - is read off that list. Re-sorted
        alphabetically, the case a reader sees first is the one whose name
        sorts first, not the one that failed first.

        The fixture must therefore be in an order that is NOT its sorted
        order, or the two implementations agree and this is vacuous again.
        """
        out = ("FAIL: test_zulu (genpkg.test_x.T.test_zulu)\n"
               "AssertionError: 1 != 0\n"
               "ERROR: test_alpha (genpkg.test_x.T.test_alpha)\n"
               "ValueError: boom\n"
               "FAIL: test_mike (genpkg.test_x.T.test_mike)\n")
        got = ms.named_failures(out)
        self.assertNotEqual(sorted(got), got,
                            "CONTROL FAILED - the fixture is already in "
                            "sorted order, so a sorting mutant is invisible")
        self.assertEqual(
            ["FAIL: test_zulu (genpkg.test_x.T.test_zulu)",
             "ERROR: test_alpha (genpkg.test_x.T.test_alpha)",
             "FAIL: test_mike (genpkg.test_x.T.test_mike)"], got)

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


class ShaContract(unittest.TestCase):
    """`sha()` returning None instead of raising is what makes the harnesses'
    per-file restore independent, and nothing pinned it until the T-527.32
    review ran a mutant that made it raise and watched the mutant SURVIVE.

    The claim in `mutate_camera_quality.py` and `mutate_light_logging.py` is
    explicit: *"Each file independently: one loop abandons the rest the moment
    one raises, and it raises out of a `finally`, so the second file stays
    mutated with nothing reporting it."* The `try` in those loops wraps only
    the WRITE - the `sha(path)` read above it is unguarded, so the contract has
    to hold here or that comment is false.
    """

    def test_it_hashes_a_real_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("hello\n")
            path = fh.name
        try:
            # The real sha256 of b"hello\n", not a self-consistent re-derivation
            # through the same function - which would pass with any hash.
            self.assertEqual(
                "5891b5b522d5df086d0ff0b110fbd9d21bb4fc7163af34d08286a2e846f6be03",
                ms.sha(path))
        finally:
            os.unlink(path)

    def test_a_MISSING_file_returns_None_rather_than_raising(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(ms.sha(os.path.join(tmp, "not-there")))

    def test_an_UNREADABLE_file_returns_None_rather_than_raising(self):
        """The OSError branch that is not a missing file. A restore loop reads
        this before its own try/except, so raising here would abandon every
        remaining file in the loop."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "locked.txt")
            with open(path, "w") as fh:
                fh.write("x")
            os.chmod(path, 0o000)
            try:
                if os.access(path, os.R_OK):
                    self.skipTest("running as a user that ignores file modes "
                                  "(root), so this OSError cannot be produced")
                self.assertIsNone(ms.sha(path))
            finally:
                os.chmod(path, 0o600)

    def test_an_unreadable_file_compares_DIFFERENT_from_a_real_snapshot(self):
        """The direction the restore paths depend on. They write whenever
        `sha(path) != snapshot`, so a file that has become unreadable must
        compare as DIFFERENT and be rewritten - never be skipped as unchanged.

        The first version of this test asserted `sha(real_file) != None`, which
        is implied by any test that pins a real digest and exercised none of
        the stated direction. It is the comparison against a REAL snapshot that
        matters, because that is the one the restore loops actually make.
        """
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "target.py")
            with open(path, "w") as fh:
                fh.write("hello\n")
            snapshot = ms.sha(path)
            self.assertIsNotNone(snapshot, "CONTROL FAILED - no baseline")
            os.unlink(path)
            self.assertNotEqual(
                snapshot, ms.sha(path),
                "a vanished file compared EQUAL to its snapshot, so a restore "
                "loop would skip it and leave a mutant in the tree")


class PurgePycache(unittest.TestCase):
    """`purge_pycache` was exported and untested; a no-op version survived the
    T-527.32 review's battery. Its own docstring argues it is not redundant
    with PYTHONDONTWRITEBYTECODE (*"neither stops a stale one being READ"*), so
    a silently inert purge would make every battery's verdicts suspect with no
    signal at all.
    """

    def _tree(self, tmp):
        for rel in ("__pycache__", "pkg/__pycache__",
                    ".git/objects/__pycache__", ".github/scripts/__pycache__"):
            os.makedirs(os.path.join(tmp, rel), exist_ok=True)
        marks = {}
        # `.github/scripts/__pycache__` is the one that discriminates, and it
        # is here deliberately. A stale .pyc under `.github` is as poisonous as
        # any other, and the OLD substring test (`if ".git" in root`) skipped
        # that whole subtree - so it left this one behind. Without it every
        # assertion in this class passes under both implementations, because
        # the substring form only ever skips MORE and skipping never deletes
        # anything. A fixture that cannot exhibit the defect is not coverage of
        # it, however carefully the assertion is written.
        for rel in ("__pycache__/a.pyc", "pkg/__pycache__/b.pyc",
                    ".github/scripts/__pycache__/c.pyc"):
            marks[rel] = os.path.join(tmp, rel)
            open(marks[rel], "w").close()
        return marks

    def test_it_removes_every_pycache_it_should(self):
        with tempfile.TemporaryDirectory() as tmp:
            marks = self._tree(tmp)
            # Control first: if these are not there, "removed" proves nothing.
            for rel, path in marks.items():
                self.assertTrue(os.path.exists(path),
                                f"CONTROL FAILED - fixture never created {rel}")
            ms.purge_pycache(tmp)
            for rel, path in marks.items():
                self.assertFalse(os.path.exists(path),
                                 f"{rel} survived the purge")

    def test_it_leaves_the_git_directory_alone(self):
        """`.git` is skipped by DIRECTORY NAME, not by substring.

        The two forms differ only on a path that CONTAINS `.git` without being
        it - `.github/` being the one that actually occurs - and they differ
        only in the purge direction, never in the delete-something-it-should-
        not direction. So the discriminating assertion is in
        test_it_removes_every_pycache_it_should, which requires the
        `.github/scripts/__pycache__` mark to go. This test covers the other
        half: real `.git` content is left alone.
        """
        with tempfile.TemporaryDirectory() as tmp:
            self._tree(tmp)
            git_file = os.path.join(tmp, ".git", "objects", "keep")
            open(git_file, "w").close()
            # THE DISCRIMINATING MARK. Asserting only that `keep` survives is
            # true for free: purge_pycache never deletes anything but a
            # directory named __pycache__, so that assertion cannot tell the
            # name-prune from the old substring form, from no guard at all, or
            # from a stub. A __pycache__ INSIDE .git is the case where the
            # guard is the only thing doing any work.
            git_pycache = os.path.join(tmp, ".git", "objects", "__pycache__",
                                       "d.pyc")
            open(git_pycache, "w").close()
            ms.purge_pycache(tmp)
            self.assertTrue(os.path.exists(git_file), ".git content was touched")
            self.assertTrue(
                os.path.exists(git_pycache),
                "a __pycache__ INSIDE .git was purged - the guard is not "
                "pruning the walk. Note `topdown=False` produces exactly this, "
                "because the prune then happens after os.walk has already "
                "yielded the subtree.")

    def test_a_tree_with_no_pycache_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "pkg"))
            ms.purge_pycache(tmp)  # must not raise


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

    def test_the_DEFAULT_indent_is_the_two_spaces_the_grep_recipe_anchors_on(self):
        """This module's own docstring prescribes
        `grep -E "^  killed \\( *0 failing"` for finding a kill that named
        nothing. That recipe anchors on the two-space default, and nothing
        pinned it - a mutant changing `indent="  "` to `indent=""` SURVIVED
        the suite (found by review, 2026-08-14).

        The failure is silent in the worst direction: the recipe stops
        matching, so a search for the one signature that exposes a mutant
        which tested nothing returns zero hits and reads as "there are none".
        """
        for verdict in (ms.SURVIVED, ms.KILLED, ms.NO_VERDICT):
            with self.subTest(verdict=verdict):
                line = ms.format_verdict(verdict, [])
                self.assertTrue(
                    line.startswith("  ") and not line.startswith("   "),
                    f"the default indent is no longer exactly two spaces, so "
                    f"the documented grep recipe cannot match: {line!r}")
        self.assertRegex(ms.format_verdict(ms.KILLED, []),
                         r"^  killed \( *0 failing")

    def test_a_NO_VERDICT_also_reports_its_named_case_count(self):
        """The count is what separates the two tells that produce NO VERDICT.

        Zero named cases means the module never collected anything; a
        non-zero count with a moved ran-count means unittest wrapped an
        unimportable module in `_FailedTest` and named it. Same verdict, two
        different reasons to go looking, and the count is the only thing in
        the line that distinguishes them. A mutant hardcoding it to 0
        SURVIVED (found by review, 2026-08-14).
        """
        self.assertIn("2 named case(s)",
                      ms.format_verdict(ms.NO_VERDICT, ["ERROR: a", "ERROR: b"]))
        self.assertIn("0 named case(s)",
                      ms.format_verdict(ms.NO_VERDICT, []))


class QuotedHelper(unittest.TestCase):
    """`_quoted` is this file's own guard against forging a summary line.

    It had no test at all until review found it (2026-08-14), and it has no
    mutation coverage by construction - the battery's SANDBOX_TARGETS reach
    `tests/mutation_scoring.py` only, so a regression to `return out` would
    re-create the exact defect this helper exists to remove and nothing in
    the repo would report it.
    """

    def test_it_defeats_ran_count_on_a_real_capture(self):
        _, dead = _UnittestOutput.run(test_x=_IMPORT_DEATH_SRC)
        self.assertEqual(
            1, ms.ran_count(dead),
            "CONTROL FAILED - the raw capture carries no summary line, so "
            "this cannot show that quoting removes one")
        self.assertEqual(
            0, ms.ran_count(_quoted(dead)),
            "a quoted capture still forges a summary line; anything pasting "
            "it into an assertion message will move a battery's ran-count")

    def test_every_line_is_indented_and_nothing_is_dropped(self):
        raw = "alpha\nbeta\n\ngamma"
        quoted = _quoted(raw)
        self.assertEqual(["    alpha", "    beta", "    ", "    gamma"],
                         quoted.splitlines())


class HarnessesUseTheVerdictCONSTANTS(unittest.TestCase):
    """A harness that borrows this module's verdicts must borrow its NAMES.

    T-554. mutation_scoring.py:143 says why the constants exist: "Verdicts,
    named so a harness cannot typo one into a silent miss." A harness comparing
    against the bare spelling instead has taken the vocabulary without the
    guarantee, and the failure direction is the worst one available - rename
    SURVIVED here and every `verdict == "survived"` out there goes quietly
    false, so every mutant falls through to the else branch and the battery
    reports ALL KILLED with nothing raising.

    A review of 703a497 found one such harness. Grepping the corpus for it
    found a second site in the same file that the review had not named, which
    is the argument for a standing check rather than three fixed lines.

    SCOPED BY THE IMPORT, deliberately, and this is the part that is easy to
    get wrong. mutate_payload_scanner.py MENTIONS `score_run` in a comment
    while importing only `compile_gate` and `ran_count`; it computes its own
    verdicts with its own local `score()`, so its `verdict == "killed"` is a
    comparison against its OWN vocabulary and is none of this check's business.
    A substring scan for "score_run" flags it and is simply wrong. The import
    list is the only honest test of whose vocabulary is in use.
    """

    VERDICT_SPELLINGS = ("survived", "killed", "no-verdict")
    COMPARISON = re.compile(
        r"""(?:==|!=)\s*(['"])(survived|killed|no-verdict)\1""")

    @staticmethod
    def _repo():
        return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def _importers_of(self, name):
        """Harnesses that import `name` from tests.mutation_scoring, by AST."""
        import glob
        found = []
        for path in sorted(glob.glob(os.path.join(self._repo(),
                                                  "tests", "mutate_*.py"))):
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                if (isinstance(node, ast.ImportFrom)
                        and node.module == "tests.mutation_scoring"
                        and any(a.name == name for a in node.names)):
                    found.append(path)
                    break
        return found

    def test_the_comparison_pattern_can_actually_match(self):
        """POSITIVE CONTROL, first, because the check below reports an ABSENCE
        and a dead regex is indistinguishable from a clean corpus."""
        for spelling in self.VERDICT_SPELLINGS:
            with self.subTest(spelling=spelling):
                self.assertRegex(f'if verdict == "{spelling}":',
                                 self.COMPARISON)
                self.assertRegex(f"if verdict != '{spelling}':",
                                 self.COMPARISON)
        self.assertNotRegex('SURVIVED = "survived"', self.COMPARISON,
                            "the pattern matches the definition as well as a "
                            "comparison, so mutation_scoring.py itself would "
                            "be reported")

    def test_the_corpus_scan_finds_the_harnesses_it_is_scanning(self):
        """The other half of the control: prove the AST scope is not empty.

        An import-scoped check that resolves to zero files reports a clean
        corpus for exactly the same reason a real one does."""
        importers = self._importers_of("score_run")
        self.assertGreaterEqual(
            len(importers), 5,
            f"the scan found only {len(importers)} score_run importers, which "
            f"is too few to be right - suspect the AST walk, not the corpus")
        self.assertNotIn(
            "mutate_payload_scanner.py", [os.path.basename(p) for p in importers],
            "payload_scanner only MENTIONS score_run in a comment; if it is "
            "in this list the scope has fallen back to a substring scan")

    @staticmethod
    def _baseline_use(src):
        """(arms_the_tell, has_zero_guard) for one harness, by AST.

        BY AST AND NOT BY REGEX, and that is not fastidiousness. A regex for
        `clean_ran ==` matched mutate_health_log.py on the strength of its own
        COMMENT - a line explaining that the harness deliberately has no
        `clean_ran == 0` abort - and put the one correctly-exempt harness on
        the missing list. A scanner that reads prose as code reports a defect
        in the file that documents why there isn't one.
        """
        tree = ast.parse(src)
        armed = guarded = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Compare):
                if not any(isinstance(x, ast.Name) and x.id == "clean_ran"
                           for x in [node.left] + list(node.comparators)):
                    continue
                zero = (len(node.ops) == 1
                        and isinstance(node.ops[0], ast.Eq)
                        and isinstance(node.comparators[0], ast.Constant)
                        and node.comparators[0].value == 0)
                if zero:
                    guarded = True
                else:
                    armed = True
            elif isinstance(node, ast.Call):
                fn = node.func
                name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                if name in ("score", "score_run") and any(
                        isinstance(a, ast.Name) and a.id == "clean_ran"
                        for a in node.args):
                    armed = True
        return armed, guarded

    def test_a_harness_that_ARMS_the_ran_count_tell_guards_a_zero_baseline(self):
        """The ran-count tell is INERT at clean_ran == 0, silently.

        T-554. The tell is `ran < clean_ran`, so a zero baseline can never fire
        it - the harness degrades to the older zero-named-cases rule while
        still printing a measurement-shaped "0 tests ran". A review found one
        harness in that state; this scan found two more.

        SCOPED BY WHETHER THE BASELINE IS EVER COMPARED, not by which scorer is
        used, because the tell is implemented three different ways here: inside
        `score_run`, inside a harness's own local `score()`
        (mutate_payload_scanner.py, which imports no `score_run` at all), and
        inline as `mutant_ran != clean_ran` (both light harnesses). Scoping on
        `score_run` importers missed the first two of those and let a mutant
        survive; that miss is the reason this predicate is what it is.

        mutate_health_log.py and mutate_netwatch.py score on the suite's RETURN
        CODE and never compare the baseline at all - it is a display label on a
        run already proved green - so a zero there is cosmetic. They are out of
        scope rather than exempt from it, which is why nothing here has to
        name them.
        """
        checked, missing = [], []
        for path in self._importers_of("ran_count"):
            with open(path, encoding="utf-8") as fh:
                armed, guarded = self._baseline_use(fh.read())
            if not armed:
                continue
            checked.append(os.path.basename(path))
            if not guarded:
                missing.append(os.path.basename(path))

        # CONTROL: an empty scope reports a clean corpus for the same reason a
        # healthy one does.
        self.assertGreaterEqual(
            len(checked), 8,
            f"only {len(checked)} harness(es) matched as arming the tell, "
            f"which is too few to be right - suspect _baseline_use, not the "
            f"corpus")
        self.assertEqual(
            [], missing,
            "these harnesses arm the ran-count tell with a baseline they never "
            "check for zero, so on a suite that collects nothing the tell is "
            "inert and the run still prints a number:\n  "
            + "\n  ".join(missing))

    def test_the_baseline_predicate_separates_the_three_shapes(self):
        """POSITIVE AND NEGATIVE CONTROLS for the predicate above, which
        otherwise reports an absence and cannot be told from a dead scan.

        Each arm is a real shape from the corpus, including the comment that
        defeated the regex version."""
        armed, guarded = self._baseline_use(
            "def f():\n    verdict = score_run(ok, out, clean_ran)\n")
        self.assertTrue(armed, "a scorer call passing the baseline")
        self.assertFalse(guarded)

        armed, _ = self._baseline_use(
            "def f():\n    if mutant_ran != clean_ran:\n        pass\n")
        self.assertTrue(armed, "the inline comparison both light harnesses use")

        armed, _ = self._baseline_use(
            "def f():\n    verdict = score(ok, out, clean_ran)\n")
        self.assertTrue(armed, "a harness's own local score()")

        armed, guarded = self._baseline_use(
            "def f():\n    if clean_ran == 0:\n        return 1\n")
        self.assertTrue(guarded, "the zero guard")
        self.assertFalse(armed, "the zero guard must not count as arming")

        armed, guarded = self._baseline_use(
            "def f():\n"
            "    # this harness has no `clean_ran == 0` abort of the usual kind\n"
            "    print(f'{clean_ran} tests' if clean_ran else 'none')\n")
        self.assertFalse(armed, "prose is not code")
        self.assertFalse(
            guarded,
            "a COMMENT mentioning the guard was read as the guard - this is "
            "the exact regex defect the AST version replaced")

    def test_no_score_run_importer_compares_a_verdict_to_a_literal(self):
        offenders = []
        for path in self._importers_of("score_run"):
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
            for i, line in enumerate(src.splitlines(), 1):
                if self.COMPARISON.search(line):
                    offenders.append(
                        f"{os.path.basename(path)}:{i}: {line.strip()}")
        self.assertEqual(
            [], offenders,
            "these harnesses take their verdicts from mutation_scoring but "
            "compare against the bare spelling, so renaming a constant here "
            "makes every comparison silently false and the battery reports "
            "ALL KILLED:\n  " + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main()
