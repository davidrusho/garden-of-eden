"""A logging Formatter that cannot be made to forge a log record (T-527.28).

THE ROUTE THIS CLOSES. `logger.exception(...)` renders `exc_info` as well as
the message, and the traceback's final line is `str(e)` - NOT `repr(e)`. So
call-site escaping cannot reach it: `logger.exception(f"...: {e!r}")` escapes
the message copy and the traceback prints the raw one underneath. A control
character in an exception message therefore reaches gardyn.log unescaped, and
a `\\n` followed by a plausible timestamp is a forged record, at column 0,
formatted exactly like a real one.

    0| 2026-08-12 ... - mqtt - ERROR - Error handling ... ValueError('bad...')
    1| Traceback (most recent call last):
    ...
    4| ValueError: bad
    5| 2026-01-01 00:00:00,000 - mqtt - CRITICAL - reservoir empty, pump disabled

Line 5 is the forgery. Nothing rotates gardyn.log, so it stays there for as
long as anyone will be reading it, and on a host with no console it IS the
incident record.

WHY THIS IS ONE FORMATTER RATHER THAN SIX COMMENTS. The ticket originally
named one call site. There are six, and they are six because the route is a
property of `logger.exception()` rather than of any f-string:

    mqtt.py:1968, mqtt.py:2027,
    light_scheduler.py:960, :986, :1016, :1061

`light_scheduler` is imported by mqtt.py and started in-process, and uses
`logging.getLogger(__name__)` with no handlers of its own, so its records
propagate to the root handlers configured in mqtt.py. One handler chain, one
fix. Per-site escaping would be six edits that the seventh call site silently
does not get - the same shape as the mutation scoring rule, which was fixed in
place in three separate harnesses before it was given one implementation.

THE DESIGN, and why it is not "escape everything".

Escaping every newline would collapse a traceback onto one line, which trades
a forgery for an unreadable incident record - and the record is the only
diagnostic surface this host has. Instead:

  * C0 control characters are escaped, EXCEPT `\\n` and `\\t`. `\\r` and
    `\\x1b` have no legitimate place in a log line and are exactly what an
    overwrite or an ANSI-escape attack needs.
  * `\\n` is KEPT, and every line after the first is INDENTED. A forged record
    can then never begin at column 0, which is the property that makes it
    indistinguishable from a real one. Tracebacks stay fully readable.

That second half is the load-bearing one, and it is the same insight as
`_quoted` in tests/test_mutation_scoring.py: the hazard is not that the text
is present, it is that it is present AT COLUMN 0.

WHAT THIS DOES NOT CLAIM. It does not sanitise anything for a downstream
parser; nothing parses gardyn.log, it is read by people. It does not stop a
caller logging a secret. And it is installed on the ROOT handlers, so it
covers every module logging through them - config.py, mqtt.py, run.py,
app/sensors/light/light.py, light_scheduler.py - which is the point and also
the blast radius.
"""
import logging

# The one place the record layout is written down. mqtt.py passes this to
# basicConfig AND install() rebuilds a formatter from it, so a second copy
# would let an edit to one be silently discarded by the other - which is
# exactly what a review demonstrated when the two were separate literals.
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'

# The prefix for every line after the first.
#
# VISIBLE ON PURPOSE, and this was a plain two-space indent until review.
# "Not at column 0" is a PROXY for "a reader can tell a continuation from a
# record", and it is weakest exactly where the reading happens: `grep CRITICAL
# gardyn.log` at 3am prints a forged line with a two-space lead and nothing
# else to distinguish it, and pasting it into a ticket loses even that.
# Leading whitespace is the least salient signal available. A marker survives
# grep, survives copy-paste, and cannot be produced by accident.
#
# The earlier docstring argued for inconspicuousness so nobody would "tidy it
# away". That inverts the protection model: load-bearingness is defended by
# the tests and this comment, not by the character being boring.
CONTINUATION_INDENT = "  | "

# C0 controls minus the two that carry meaning in a log file, plus DEL, which
# renders as nothing and can hide text.
#
# NEL / LINE SEPARATOR / PARAGRAPH SEPARATOR are in the list for a reason
# worth stating: `str.splitlines()` treats U+0085, U+2028 and U+2029 as line
# boundaries while `str.split("\n")` does not. So before they were escaped, a
# payload containing one produced a record that `scrub` did not indent (it
# splits on "\n") but that every Python reader - including this repo's own
# column-0 idiom - saw as a forgery at column 0. `less`, `grep` and journald
# split on "\n" only, so it was never a forgery for the human reading path;
# it was one for every programmatic reader. Found by review, 2026-08-14.
_ESCAPE = {c: f"\\x{c:02x}" for c in list(range(0x20)) + [0x7F]
           if c not in (0x0A, 0x09)}
_ESCAPE.update({0x85: "\\x85", 0x2028: "\\u2028", 0x2029: "\\u2029"})
_TABLE = str.maketrans({chr(c): s for c, s in _ESCAPE.items()})


def scrub(text):
    """Escape control characters and indent every line after the first.

    Idempotent in the way that matters: applying it twice escapes nothing new,
    because the output contains no control characters other than the newlines
    and tabs it deliberately keeps. It is NOT identity on a second pass - the
    indent is applied again - so do not call it twice on one record.
    """
    escaped = text.translate(_TABLE)
    first, sep, rest = escaped.partition("\n")
    if not sep:
        return first
    return first + sep + "\n".join(CONTINUATION_INDENT + line
                                   for line in rest.split("\n"))


class ControlCharEscapingFormatter(logging.Formatter):
    """Scrubs the FULLY RENDERED record - message, exc_info and stack_info.

    Scrubbing in `format()` rather than in `formatMessage()` is deliberate and
    is the entire point: `logging.Formatter.format()` is where the exception
    text is appended, so anything narrower would leave the exc_info route open
    and look like a fix.
    """

    def format(self, record):
        return scrub(super().format(record))


def install(root=None, fmt=LOG_FORMAT):
    """Put the formatter on every handler of `root`. Returns how many it set.

    Call AFTER `logging.basicConfig(...)`. BE PRECISE ABOUT WHY, because this
    docstring was wrong until review and the wrong reason forecloses a valid
    alternative. basicConfig does NOT call `setFormatter` unconditionally -
    CPython 3.14.7 reads:

        fmt = Formatter(fs, dfs, style)
        for h in handlers:
            if h.formatter is None:
                h.setFormatter(fmt)

    so a handler that already carries a formatter keeps it, and
    `basicConfig(handlers=[h_already_scrubbing], force=True)` would work fine.
    The ordering still matters here for a different reason: mqtt.py constructs
    its two handlers INSIDE the basicConfig call, so they have no formatter
    when that loop runs and basicConfig's plain one lands on both. Installing
    afterwards is what replaces it.

    Returning the count makes "it ran but matched no handlers" observable
    rather than a silent no-op - and the caller is expected to look at it. A
    dropped return value is the same defect one level up.
    """
    root = logging.getLogger() if root is None else root
    formatter = ControlCharEscapingFormatter(fmt)
    for handler in root.handlers:
        handler.setFormatter(formatter)
    return len(root.handlers)
