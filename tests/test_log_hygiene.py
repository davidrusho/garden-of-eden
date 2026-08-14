"""The exc_info forgery route, closed and pinned (T-527.28).

WHY THIS FILE EXISTS SEPARATELY FROM THE OTHER LOGGING TESTS. Every existing
assertion in this repo reads a log MESSAGE or a logger call. None reads a
FORMATTED record - and the whole defect lives between those two, because
`logger.exception()` appends the traceback during formatting and the
traceback's last line is `str(e)`, not `repr(e)`. An instrument that stops at
the message cannot see it, which is why the ticket said the instrument had to
be built before the fix could be verified at all.

So these cases attach a real handler, log through it, and read the bytes it
produced.

THE PROPERTY, stated so nobody weakens it by accident: no line of a formatted
record after the first may begin at column 0, and no C0 control character
other than \\n and \\t may survive. The first half is what actually defeats a
forgery - a record that cannot start a line cannot impersonate one.

Run:  python3 -m unittest tests.test_log_hygiene
"""
import io
import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import log_hygiene  # noqa: E402

FMT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

# The forgery from the ticket, verbatim in shape: a newline, then something
# formatted exactly like a real record of this project's own shape.
FORGED = "2026-01-01 00:00:00,000 - mqtt - CRITICAL - reservoir empty, pump disabled"


class _Capture:
    """A real handler writing to a real buffer. Returns the formatted text."""

    def __init__(self, formatter):
        self.buf = io.StringIO()
        self.handler = logging.StreamHandler(self.buf)
        self.handler.setFormatter(formatter)
        self.logger = logging.getLogger(f"loghygiene.{id(self)}")
        self.logger.handlers = [self.handler]
        self.logger.propagate = False
        self.logger.setLevel(logging.DEBUG)

    def text(self):
        self.handler.flush()
        return self.buf.getvalue()


def _scrubbed():
    return _Capture(log_hygiene.ControlCharEscapingFormatter(FMT))


def _plain():
    return _Capture(logging.Formatter(FMT))


class TheForgeryIsReachableWithoutTheFormatter(unittest.TestCase):
    """POSITIVE CONTROL, and it runs first on purpose.

    If a plain Formatter does NOT put the forged line at column 0, then the
    defect this file is about does not exist on this Python and every
    assertion below is measuring nothing. That is the failure mode a scrub
    test cannot otherwise distinguish from success.
    """

    def test_a_plain_formatter_lets_the_exc_info_forgery_reach_column_zero(self):
        cap = _plain()
        try:
            raise ValueError(f"bad\n{FORGED}")
        except ValueError:
            cap.logger.exception("Error handling message on topic %r", "x/y")
        lines = cap.text().splitlines()
        self.assertIn(
            FORGED, lines,
            "CONTROL FAILED - the forged line is not at column 0 under a "
            "plain Formatter, so there is no defect here to close and every "
            "other case in this file is vacuous")


class TheFormattedRecordCannotForgeALine(unittest.TestCase):

    def test_the_exc_info_route_is_closed(self):
        """The route no call-site escaping can reach.

        `{e!r}` in the message is escaped by the caller; the traceback prints
        `str(e)` underneath and is not. This is the case the ticket was filed
        for.
        """
        cap = _scrubbed()
        try:
            raise ValueError(f"bad\n{FORGED}")
        except ValueError:
            cap.logger.exception("Error handling message on topic %r", "x/y")
        text = cap.text()
        self.assertIn("bad", text, "the message itself was lost, not escaped")
        self.assertNotIn(
            f"\n{FORGED}", text,
            "the forged record still begins a line - the exc_info route is "
            "open")
        for i, line in enumerate(text.splitlines()):
            if i:
                self.assertTrue(
                    line.startswith(log_hygiene.CONTINUATION_INDENT) or not line,
                    f"line {i} begins at column 0 and can impersonate a "
                    f"record: {line!r}")

    def test_the_MESSAGE_route_is_closed_too(self):
        """Same property with no exception involved - a bare `logger.error`
        whose message carries a newline is the simpler half of the same
        defect, and it has five of the six call sites' shape."""
        cap = _scrubbed()
        cap.logger.error("reading failed\n%s", FORGED)
        lines = cap.text().splitlines()
        self.assertNotIn(FORGED, lines)
        self.assertIn(log_hygiene.CONTINUATION_INDENT + FORGED, lines)

    def test_carriage_returns_and_escapes_do_not_survive(self):
        """`\\r` overwrites a line in a terminal and `\\x1b` starts an ANSI
        sequence; neither has a legitimate place in this log. Unlike `\\n`
        they are escaped rather than indented, because nothing is lost."""
        cap = _scrubbed()
        cap.logger.error("a\rb\x1b[2Kc\x7fd")
        text = cap.text()
        for raw in ("\r", "\x1b", "\x7f"):
            self.assertNotIn(raw, text, f"{raw!r} reached the handler")
        self.assertIn("a\\x0db\\x1b[2Kc\\x7fd", text)

    def test_tabs_and_newlines_are_KEPT(self):
        """The escaping is deliberately narrow. A traceback is the only
        diagnostic surface this host has, and collapsing it onto one line
        would trade a forgery for an unreadable incident record."""
        cap = _scrubbed()
        cap.logger.error("col1\tcol2\nsecond line")
        text = cap.text()
        self.assertIn("\t", text, "tabs were escaped; indentation is lost")
        # NOT `assertIn("\n", text)`. StreamHandler appends its OWN terminator
        # after every record, so that assertion is true for free no matter
        # what the formatter did - it passed with newlines fully escaped, and
        # the battery caught it as a mutant killed by three OTHER tests while
        # the case named for it stayed green. Assert the RECORD spans two
        # lines instead, which is the property.
        self.assertEqual(
            2, len(text.rstrip("\n").splitlines()),
            f"newlines were escaped, so the record collapsed onto one line "
            f"and a traceback would be unreadable: {text!r}")
        self.assertIn(log_hygiene.CONTINUATION_INDENT + "second line", text)

    def test_unicode_line_separators_cannot_forge_a_line(self):
        """U+0085, U+2028 and U+2029 are line breaks to `str.splitlines()` and
        not to `str.split("\\n")`.

        `scrub` splits on "\\n", so before these were escaped a payload
        containing one produced a record that scrub did not indent but that
        every Python reader saw as a forgery at column 0 - including this
        suite's own idiom. Never a forgery for `less`, `grep` or journald,
        which split on "\\n" only; always one for a programmatic reader.
        Found by review, 2026-08-14.
        """
        # chr(), not literals. A literal U+2028 in this file is the very
        # thing under test, and it would sit invisibly in the source.
        for name, ch in (("NEL", chr(0x85)), ("LS", chr(0x2028)),
                         ("PS", chr(0x2029))):
            with self.subTest(separator=name):
                cap = _scrubbed()
                cap.logger.error("reading failed" + ch + FORGED)
                text = cap.text()
                self.assertNotIn(
                    ch, text, f"{name} survived into the record")
                self.assertNotIn(
                    FORGED, text.splitlines(),
                    f"{name} put the forged record at column 0 as far as "
                    f"splitlines() is concerned")

    def test_a_multi_line_traceback_stays_readable(self):
        cap = _scrubbed()
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            cap.logger.exception("tick failed")
        lines = cap.text().splitlines()
        self.assertTrue(any("Traceback (most recent call last):" in l
                            for l in lines), lines)
        self.assertTrue(any("RuntimeError: boom" in l for l in lines), lines)
        self.assertGreater(len(lines), 3, "the traceback was collapsed")


class ScrubContract(unittest.TestCase):

    def test_a_single_line_is_returned_unchanged_apart_from_escapes(self):
        self.assertEqual("plain", log_hygiene.scrub("plain"))

    def test_it_does_not_indent_a_string_with_no_newline(self):
        """Guards the `partition` branch: an early version that split
        unconditionally indented nothing but also rebuilt every single-line
        record through the join, which is where an off-by-one would hide."""
        self.assertEqual("only", log_hygiene.scrub("only"))

    def test_an_empty_trailing_line_is_still_indented(self):
        # Built from the constant, not the literal "  ". Review noted the
        # hardcoded form pins the VALUE rather than the property, so it
        # goes red on any indent change - including one that preserves
        # the security property completely - under a test whose name is
        # about trailing lines.
        self.assertEqual("a\n" + log_hygiene.CONTINUATION_INDENT,
                         log_hygiene.scrub("a\n"))


class TheInstallerIsWiredWhereItClaims(unittest.TestCase):

    def test_install_sets_the_formatter_on_every_handler_and_says_how_many(self):
        root = logging.getLogger(f"loghygiene.install.{id(self)}")
        root.handlers = [logging.StreamHandler(io.StringIO()),
                         logging.StreamHandler(io.StringIO())]
        self.assertEqual(2, log_hygiene.install(root))
        for h in root.handlers:
            self.assertIsInstance(
                h.formatter, log_hygiene.ControlCharEscapingFormatter)

    def test_it_reports_ZERO_rather_than_silently_doing_nothing(self):
        """A no-op installer is the failure this fix cannot afford: the log
        would look normal and the route would be open. The count is the only
        observable, so it has to be honest about the empty case."""
        root = logging.getLogger(f"loghygiene.empty.{id(self)}")
        root.handlers = []
        self.assertEqual(0, log_hygiene.install(root))


class TheSHIPPEDProcessEndsUpScrubbing(unittest.TestCase):
    """Import mqtt.py the way the service does, and read the real handlers.

    THIS IS THE ONE THAT MATTERS, and it did not exist until review. Every
    other case here constructs its own handler, and the source-order check
    below pins TEXT POSITION rather than execution - three realistic edits
    walk straight through it (moving the install into a function nobody
    calls, guarding it behind a false env flag, wrapping it in a try/except
    that swallows). Worse, `install()`'s default `root=None` branch is the
    SHIPPED path and no other test enters it: a mutant pointing that default
    at a non-root logger SURVIVED the whole suite and the whole battery.

    Reading the handlers after a real import covers all of that at once, and
    it is the only assertion here that would notice `gardyn.log` silently
    losing its escaping.

    The stub apparatus is borrowed from tests/test_water_interlock.py rather
    than rebuilt, and WITHDRAWN in a finally. Importing mqtt.py runs
    `basicConfig(force=True)`, which replaces the root handlers for the whole
    process, so the root logger is saved and restored too - otherwise this
    file would contaminate every module discovered after it, which is exactly
    what tests/test_suite_isolation.py exists to forbid.
    """

    def _import_mqtt_under_stubs(self):
        import importlib
        from tests import test_water_interlock as wi

        root = logging.getLogger()
        saved_handlers, saved_level = root.handlers[:], root.level
        wi._install_stubs()
        try:
            import mqtt
            importlib.reload(mqtt)
            return list(root.handlers)
        finally:
            wi._withdraw_stubs()
            root.handlers[:] = saved_handlers
            root.setLevel(saved_level)

    def test_importing_mqtt_leaves_every_root_handler_scrubbing(self):
        handlers = self._import_mqtt_under_stubs()
        self.assertTrue(
            handlers,
            "CONTROL FAILED - importing mqtt.py left the root logger with no "
            "handlers at all, so this measures nothing")
        for handler in handlers:
            self.assertIsInstance(
                handler.formatter,
                log_hygiene.ControlCharEscapingFormatter,
                f"a root handler ({type(handler).__name__}) carries "
                f"{type(handler.formatter).__name__}, not the scrubbing "
                f"formatter. Records reaching it can forge a log line.")

    def test_the_shipped_format_is_the_single_sourced_one(self):
        """`install()` rebuilds a formatter and overwrites what basicConfig
        built, so a second copy of the format string would let an edit to
        basicConfig's `format=` be silently discarded. Review demonstrated
        exactly that with two literals in place."""
        for handler in self._import_mqtt_under_stubs():
            self.assertEqual(log_hygiene.LOG_FORMAT, handler.formatter._fmt)


class MqttInstallsItAfterBasicConfig(unittest.TestCase):
    """Ordering is load-bearing and invisible at runtime.

    `basicConfig` builds a plain Formatter from its own `format=` and calls
    `setFormatter` on every handler unconditionally, so an install placed
    BEFORE it is silently discarded - the log keeps its normal shape and the
    escaping is simply absent. Asserted in the source because the two live in
    module scope and cannot be reordered by a test.
    """

    def test_the_install_call_comes_after_basicConfig(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "mqtt.py")
        with open(path) as fh:
            src = fh.read()
        basic = src.index("logging.basicConfig(")
        install = src.index("log_hygiene.install(")
        self.assertLess(
            basic, install,
            "log_hygiene.install() runs BEFORE logging.basicConfig(), which "
            "silently replaces the formatter - the escaping is absent and "
            "nothing reports it")


if __name__ == "__main__":
    unittest.main()
