"""Tests for the refused-CONNACK gate and the escaped decode line (T-527.11).

TWO DEFECTS, ONE FILE, BECAUSE BOTH LIVE ON THE EDGE WHERE UNTRUSTED INPUT
REACHES THIS DEVICE.

1. on_connect did not gate on `rc`.

   paho calls on_connect from _handle_connack whatever the CONNACK said. On a
   refusal - "Not authorized", "Bad user name or password", "Server
   unavailable" - the old code logged `Connected with result code Not
   authorized`, subscribed, announced discovery, and then called
   start_publisher_threads() against a socket the broker was closing.

   The subscribe and the announce are wasted work and heal on the next connect.
   start_publisher_threads() does NOT heal, and the shorthand for why is wrong
   in a way worth stating: it is not that the publishers never start. It sets
   its once-only flag AND spawns both loops, so a refused CONNACK starts them
   against a client that is not connected - each loop's first publish is lost,
   by whichever of two routes wins the race (MQTT_ERR_NO_CONN once the network
   loop has closed the socket, or discarded by reconnect() clearing the out
   queue), and each then sleeps to its own period. The connect
   that succeeds seconds later finds the flag set and returns early, so nothing
   re-sends. Nothing leaks and nothing looks broken; the device is simply up
   with no fresh PCB temperature for up to half an hour and no fresh camera
   frame for up to an hour - the two loops have different periods (30 minutes,
   and IMAGE_INTERVAL_SECONDS which ships as 3600).

   WHAT HOME ASSISTANT SHOWS in that window is a separate question from how
   long the window is, and an earlier version of this docstring stated one case
   as if it were the only one. Neither publisher retains - publish_pcb_
   temperature() omits retain= (paho defaults it False) and _capture_and_
   publish() passes retain=False - so nothing replays the gap on connect. A
   device-only reconnect leaves HA holding the value it last received, STALE
   rather than `unknown`; `unknown` is what an HA that restarted inside the
   window gets, having no earlier value of its own. The window, and the
   severity, are the same either way.

   Not to be shortened to "the guard exists to prevent this". It does not:
   start_publisher_threads()'s docstring credits the PLACEMENT with avoiding
   that race and the guard only with not spawning a second set of threads. A
   refused CONNACK satisfies "on_connect fired" without a connection behind it,
   which is what neither of them covers.

   The credential case is the one that matters on this device: a broker
   password rotated while the Pi is running produces exactly this, repeatedly,
   on a host with no console and no physical recovery path.

2. Log lines interpolated the payload raw. TWO of them.

   `'{payload}'` wrote a newline or an ANSI escape into gardyn.log byte for
   byte, which is enough to forge a whole log line - timestamp, logger name,
   level - in the file these incidents get reconstructed from. Nothing in this
   repo rotates that file, so a forged line stays there.

   The generic decode line was the one the ticket named. Review found a second,
   in the water/low/cm/set handler, still raw after the first was fixed. Both
   are covered here, plus a source-level test for the RULE, because
   case-by-case tests cannot notice a further sink being added.

   NEITHER SINK IS "THE MORE EXPOSED ONE" and two earlier versions of this file
   said the ERROR one was, on the grounds that "ERROR is above the root
   logger's WARNING, so it survives the INFO line being filtered out". That is
   wrong twice. The INFO line is not filtered - mqtt.py raises its own logger to
   INFO deliberately and tests/test_water_interlock.py asserts it - and an
   ancestor logger's LEVEL never filters a propagated record in the first
   place. The emitting logger's effective level decides whether a record exists;
   from there callHandlers consults each HANDLER's level, which is the mechanism
   test_handlers_do_not_filter_above_the_logger_levels documents in that same
   file. basicConfig leaves both handlers at NOTSET, so INFO and ERROR reach
   gardyn.log alike and a forged line is worth the same on either.

   The framing that produced the miss is worth keeping: a topic under
   BASE_TOPIC is ADDRESSED to this device, which is not the same as the broker
   vouching for who published it. Every subscribed topic carries remote input,
   not just homeassistant/status.

WHAT `rc` ACTUALLY IS, AND HOW THAT WAS ESTABLISHED

Not from recall. paho-mqtt 2.0.0 (the version requirements.txt pins) was
installed into a throwaway venv and a synthetic CONNACK driven through
Client._handle_connack with CallbackAPIVersion.VERSION2 registered and a
non-empty client_id, which is how mqtt.py constructs its client:

    v3 rc=0  -> on_connect CALLED, ReasonCode(Connack, 'Success')                   value=0   is_failure=False
    v3 rc=1  -> on_connect NOT called; paho downgrades to v3.1 and reconnects
                (only the FIRST time - the guard is `self._protocol == MQTTv311`,
                 so a repeat rejection after the downgrade DOES arrive, as 132)
    v3 rc=2  -> on_connect CALLED, ReasonCode(Connack, 'Client identifier not valid') value=133 is_failure=True
    v3 rc=3  -> on_connect CALLED, ReasonCode(Connack, 'Server unavailable')          value=136 is_failure=True
    v3 rc=4  -> on_connect CALLED, ReasonCode(Connack, 'Bad user name or password')   value=134 is_failure=True
    v3 rc=5  -> on_connect CALLED, ReasonCode(Connack, 'Not authorized')              value=135 is_failure=True

    v3 rc=6+ -> on_connect CALLED, ReasonCode(Connack, 'Unspecified error')          value=128 is_failure=True

Note rc=2: paho's early return for a rejected identifier is guarded by
`self._client_id == b''`, and mqtt.py passes client_id=IDENTIFIER, so that
rejection DOES reach on_connect on this client. It is unreachable for a SECOND,
independent reason as well: paho refuses to construct a clean_session=False
client with an empty or None client_id at all, raising
`ValueError('A client id must be provided if clean session is False.')` from
Client.__init__ - so the branch that early return guards cannot exist on this
client's configuration.

Note rc=6 and above: convert_connack_rc_to_reason_code() maps every out-of-range
v3 code to 128 'Unspecified error', is_failure=True. The fallback closes the
space - there is no v3 CONNACK return code that reaches this callback as a
non-failure other than 0.

Note also that the value is the v5 reason code, not the v3 one - "server
unavailable" is 3 on the wire and 136 here - which is why nothing in this suite
compares rc against the v3 numbers.

HOW PAHO CALLS THE CALLBACKS, measured in the same run. This is the INBOUND
counterpart of RecordingClient's declared publish() signature in
tests/test_retired_entities.py, and it exists for the same reason: so a double
cannot disagree with the library about what was meant.

    on_connect     5 positional args  (client, userdata, ConnectFlags,
                                       ReasonCode, Properties)
    on_disconnect  5 positional args  (client, userdata, DisconnectFlags,
                                       ReasonCode, Properties)
    on_message     3 positional args  (client, userdata, MQTTMessage)

on_connect and on_disconnect are called with FIVE in every case: paho
synthesises Properties(PacketTypes.CONNACK) / (DISCONNECT) when the packet
carried none, so the fifth argument is never omitted and never None. Every
pre-existing call site in this repo's suite passes FOUR, which is why deleting
`properties=None` from either signature used to leave the whole suite green
while production raised TypeError inside the callback on every connect -
suppress_exceptions is False, so paho re-raises, the exception leaves
loop_forever(), the process exits, and Restart=always makes that permanent.
TestPahoCallsTheCallbacksLikeThis below is what closes that.

paho is stubbed out in these tests, so the numbers and shapes above are
reproduced in the doubles below rather than imported. They are written as
literals for that reason: a test that derived them would agree with itself.

Stubs and RecordingClient come from the existing test modules rather than being
re-installed - tests.test_water_interlock owns the sys.modules hardware stubs
and the real `import mqtt`, and a second stubbing module fights the first. Only
non-TestCase names are imported, so nothing here re-runs another module's cases.

Run:  python3 -m unittest tests.test_connack_refusal
"""

import ast
import inspect
import re
import string
import textwrap
import unittest
from unittest.mock import MagicMock, patch

from tests.test_water_interlock import mqtt_mod
from tests.test_retired_entities import RecordingClient

# As the stubbed config sets it. Written out, not imported - see the module
# docstring of tests/test_ha_birth_message.py for why every topic here is a
# literal.
ID = "gardyn-xx"

HA_STATUS = "homeassistant/status"
LIGHT_COMMAND = "gardyn/light/command"

# paho's ReasonCode.is_failure is `self.value >= 0x80` (paho 2.0.0,
# src/paho/mqtt/reasoncodes.py). 0x80 is 128.
#
# A COPY, and nothing here can notice it going stale - paho is stubbed out of
# this suite, so there is no library to compare against and any test that used
# this constant on both sides of a comparison would be measuring itself. See
# test_the_gate_does_not_reimplement_the_boundary, which used to be named as
# though it pinned this against paho and did not.
FAILURE_THRESHOLD = 128


class ReasonCodeDouble:
    """Stands in for paho.mqtt.reasoncodes.ReasonCode, which is stubbed away.

    Deliberately not a MagicMock. Three of this class's behaviours decide
    whether the code under test is right, and a MagicMock would satisfy all
    three by accident:

      is_failure  a property, `value >= 0x80`. A MagicMock's attribute is a
                  truthy Mock, so EVERY rc would read as a refusal and the
                  accepted path would never be exercised.
      __eq__      compares against a bare int by value, which is what makes
                  `rc != 0` a legal thing to write about a ReasonCode at all.
      __str__     the reason name. This is what f-strings put in the log, and
                  it is why the pre-fix log line read `Connected with result
                  code Not authorized`.

    Written from a measured run of the real library rather than from the happy
    path - see the table in this module's docstring.
    """

    def __init__(self, value, name):
        self.value = value
        self.name = name

    @property
    def is_failure(self):
        return self.value >= FAILURE_THRESHOLD

    def __eq__(self, other):
        if isinstance(other, int):
            return self.value == other
        if isinstance(other, ReasonCodeDouble):
            return self.value == other.value
        return NotImplemented

    def __hash__(self):
        return hash(self.value)

    def __str__(self):
        return self.name


ACCEPTED = ReasonCodeDouble(0, "Success")
NOT_AUTHORIZED = ReasonCodeDouble(135, "Not authorized")
BAD_CREDENTIALS = ReasonCodeDouble(134, "Bad user name or password")
SERVER_UNAVAILABLE = ReasonCodeDouble(136, "Server unavailable")
IDENTIFIER_REJECTED = ReasonCodeDouble(133, "Client identifier not valid")
# Where every out-of-range v3 code lands (rc=6 and up), so the refusal set is
# not just the four named wire codes - see the table in the module docstring.
UNSPECIFIED_ERROR = ReasonCodeDouble(128, "Unspecified error")

EVERY_REFUSAL = [NOT_AUTHORIZED, BAD_CREDENTIALS, SERVER_UNAVAILABLE,
                 IDENTIFIER_REJECTED, UNSPECIFIED_ERROR]

NORMAL_DISCONNECTION = ReasonCodeDouble(0, "Normal disconnection")
CONNECTION_LOST = ReasonCodeDouble(128, "Unspecified error")


class ConnectFlagsDouble:
    """paho's ConnectFlags - the THIRD positional argument to a VERSION2
    on_connect. A namespace object, not the v1 dict; nothing in mqtt.py reads
    it, which is exactly why only its POSITION matters here."""

    def __init__(self, session_present=False):
        self.session_present = session_present


class DisconnectFlagsDouble:
    """paho's DisconnectFlags - the third positional argument to a VERSION2
    on_disconnect."""

    def __init__(self, is_disconnect_packet_from_server=False):
        self.is_disconnect_packet_from_server = is_disconnect_packet_from_server


class PropertiesDouble:
    """paho's Properties - the FIFTH positional argument, and the one that
    matters. It is never omitted and never None: when the packet carried no
    properties paho builds an empty Properties(PacketTypes.CONNACK) rather than
    dropping the argument."""

    def __init__(self, packet_type="CONNACK"):
        self.packet_type = packet_type


def call_on_connect_as_paho_does(client, rc, userdata=None,
                                 session_present=False):
    """Deliver a CONNACK the way paho 2.0.0 delivers one: FIVE positional args.

    Every other call site in this repo's suite passes four, which is the gap
    this exists to close. Positional on purpose - paho passes them positionally,
    so a keyword call here would keep passing against a signature that had been
    reordered.
    """
    return mqtt_mod.on_connect(
        client,
        userdata,
        ConnectFlagsDouble(session_present),
        rc,
        PropertiesDouble("CONNACK"),
    )


def call_on_disconnect_as_paho_does(client, rc, userdata=None,
                                    from_server=False):
    """The same, for on_disconnect - which had NO call site in this suite at
    all before T-527.11, so its arity was pinned by nothing whatsoever."""
    return mqtt_mod.on_disconnect(
        client,
        userdata,
        DisconnectFlagsDouble(from_server),
        rc,
        PropertiesDouble("DISCONNECT"),
    )


# --- the source-level payload-sink scanner --------------------------------
#
# Escaping forms this scanner recognises as NOT reaching the log verbatim.
# `!a` is ascii(), which escapes \n, \r and \x1b exactly as repr() does; it
# differs from repr() only in how it renders non-ASCII, which is not what this
# rule is about. It is therefore safe but non-canonical, and the two tests below
# separate those two verdicts instead of conflating them.
_SAFE_CONVERSIONS = {ord("r"): "!r", ord("a"): "!a"}
_SAFE_WRAPPERS = {"repr": "repr()", "ascii": "ascii()"}
# The same escaping reached through the other two formatting syntaxes. Until
# T-527.17 the scanner understood neither, so `"%r" % payload`,
# `logger.error("%r", payload)` and `"{!r}".format(payload)` were all reported
# RAW - four false alarms telling the next reader that correct, escaped code
# was a remote log-forgery path. A check that cries wolf on shipping code is
# the kind that gets deleted rather than fixed.
_SAFE_PERCENT = {"r": "%r", "a": "%a"}
_SAFE_FORMAT = {"r": "{!r}", "a": "{!a}"}
_CANONICAL_FORMS = {"!r", "repr()"}

# WHAT COUNTS AS A SINK. Matched on the METHOD NAME against any receiver, not
# on the receiver being spelled `logger`. Keying on the receiver is what made
# T-527.17's H2 escape work: renaming a local and reaching a sink through
# `self.logger`, `logging` or `log` disarmed the rule with 32 tests still
# green. `print()` and a raised exception's arguments are here for the same
# reason - both end up in gardyn.log through the service's stdout capture and
# through on_message's `logger.exception(...)` handler respectively.
# `fatal` is here because a review found it missing: it is a real method on
# logging.Logger (an alias for critical, like `warn` is for warning), so
# `logger.fatal(f"{payload}")` was a live forgery path reachable by one word.
# The set already carried `warn`, which is what made the omission read as a
# decision rather than an oversight. Both aliases are deprecated and both
# still emit.
_LOG_METHODS = frozenset({
    "debug", "info", "warning", "warn", "error", "exception", "critical",
    "fatal", "log",
})
# print() is a sink, but NOT because it reaches gardyn.log - it does not.
# mqtt.service sets no StandardOutput=, so systemd's default sends stdout to
# the journal, while gardyn.log is written only by the logging.FileHandler in
# mqtt.py. An earlier version of this comment claimed otherwise. The journal
# is still a durable record a human reads during an incident, which is reason
# enough; it is simply a different artifact from the one the module docstring's
# rotation argument is about.
_BARE_CALL_SINKS = frozenset({"print"})

# WHERE TAINT STOPS. The result of these cannot carry a control character, so
# a value derived through one of them cannot forge a line. This is not a
# nicety: mqtt.py binds `requested = int(payload)`, `brightness = int(payload)`
# and `candidate = float(payload)`, and `WATER_LOW_CM` descends from the last
# of those and is logged at mqtt.py:1405. Without this set the propagation
# below reddens correct shipping code, which is the direction that gets a
# check deleted rather than fixed.
_SANITISERS = frozenset({"int", "float", "bool", "len", "round", "abs", "ord"})

# `%`-format specifiers, in the order they appear. Deliberately rejects the
# mapping form `%(name)s` by capturing it and bailing - positional matching is
# meaningless there, and guessing is how a false negative gets in.
_PERCENT_SPEC = re.compile(
    r"%(?:\((?P<key>[^)]*)\))?"
    r"[-#0 +]*(?P<width>\*|\d+)?(?:\.(?P<prec>\*|\d+))?[hlL]?"
    r"(?P<conv>[diouxXeEfFgGcrsa%])"
)


def _log_it(value):
    """Stand-in for a helper that logs, used by one fixture below.

    Never called - the fixture is read with inspect.getsource() and parsed,
    never executed - but defined so the module has no undefined name in it.
    """
    raise AssertionError("fixture helper is parsed, never run")


# THE ATTRIBUTES OF `msg` A REMOTE PARTY CHOOSES. Per name rather than as one
# predicate, so dropping either is a one-word mutation a maintainer would
# plausibly make - which is what the battery perturbs.
#
# `topic` joined `payload` in T-527.12. An MQTT topic name is UTF-8 excluding
# only `+`, `#` and NUL, so it carries \n, \r and \x1b as freely as a payload
# does and forges a log line by the identical mechanism. Escaping one and not
# the other was never a decision; it was the T-527.11 ask being written for
# payloads and the sibling going unasked.
#
# NOT a claim that a topic is as REACHABLE as a payload, and the reachability
# argument took three attempts. The current one, and both wrong versions, live
# at mqtt.py's decode site so there is one copy to correct; the short form is
# that a subscription list is the CLIENT'S INTENT rather than the broker's
# state, that the durable session's real floor is ten literal topics rather
# than the eight the lists name, and that all ten are ASCII and locally
# derived - so a remotely-chosen topic needs a broker that does not conform.
#
# The seed is here anyway. The scanner's job is to make a class of sink
# impossible to write, not to rank today's inputs by likelihood.
_TAINT_SEEDS = frozenset({"payload", "topic"})


def _is_taint_seed(node):
    """The places untrusted bytes ENTER, independent of what they get called.

    `msg.payload` and `msg.topic` are the real ones - paho hands the callback
    an object and both are attributes of it.

    Seeding on the SOURCE rather than on the spelling is the whole T-527.17
    H2 fix: `body = msg.payload.decode()` is tainted because of where it came
    from, so a rename cannot disarm the rule. The same holds for
    `topic_suffix = topic.replace(...)`, which nothing logs today - the
    propagation is what keeps that true if anything ever does.

    THE BARE-NAME HALF BUYS NOTHING FOR SHIPPING CODE, and this docstring said
    the opposite until review measured it. It claimed the names were kept
    because on_message binds both as locals. It does - and narrowing this
    predicate to the Attribute branch alone leaves EVERY on_message assertion
    green, because _tainted_names()'s fixpoint already taints a local bound
    from `msg.topic` or `msg.payload`. That is what the propagation is for.
    The one test that goes red takes `payload` as a PARAMETER, where there is
    no assignment for the fixpoint to follow, and it fails on a form count
    rather than on safety.

    So: kept for the fixture parameters and for symmetry between the two
    seeds, NOT because shipping code needs it - and the distinction matters
    because the cost is real while the benefit is not. Any local called
    `topic` or `payload` is treated as remote input wherever this scanner is
    pointed, and mqtt.py already has two outbound functions that would
    qualify: `publish_config(topic, payload)` and `_capture_and_publish(...,
    topic, ...)`. Neither is accused today only because the scanner is pointed
    at `on_message` and nothing else, so widening its scope to the module
    means reading those two first.
    """
    return ((isinstance(node, ast.Name) and node.id in _TAINT_SEEDS)
            or (isinstance(node, ast.Attribute) and node.attr in _TAINT_SEEDS))


def _is_payload_reference(node, names=frozenset()):
    return _is_taint_seed(node) or (isinstance(node, ast.Name)
                                    and node.id in names)


def _carries_taint(node, names):
    """Does evaluating `node` produce something a remote client controls?

    Short-circuits at a sanitiser, which is why it recurses through
    iter_child_nodes rather than using ast.walk - `int(payload)` must claim
    its whole subtree, not merely fail to match at the top.
    """
    if node is None:
        return False
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _SANITISERS):
        return False
    if _is_payload_reference(node, names):
        return True
    return any(_carries_taint(child, names)
               for child in ast.iter_child_nodes(node))


def _bindings(node):
    """(target names, value expression) for every statement that binds a name.

    Covers assignment in its four spellings plus `for` and `with`, which bind
    without looking like it. NOT covered, and stated here rather than left to
    be discovered: `except ... as e`, subscript and attribute targets, and
    anything binding inside a comprehension. See the boundary list on
    _payload_sinks().
    """
    def names_in(target):
        if isinstance(target, ast.Name):
            return [target.id]
        if isinstance(target, (ast.Tuple, ast.List)):
            return [n for elt in target.elts for n in names_in(elt)]
        return []

    if isinstance(node, ast.Assign):
        return [n for t in node.targets for n in names_in(t)], node.value
    if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
        return names_in(node.target), node.value
    if isinstance(node, (ast.For, ast.AsyncFor)):
        return names_in(node.target), node.iter
    if isinstance(node, ast.withitem):
        return names_in(node.optional_vars), node.context_expr
    return [], None


def _tainted_names(tree):
    """Every local that carries remote bytes, by transitive assignment.

    Fixpoint rather than a single pass, so a chain (`raw = msg.payload`,
    `body = raw.decode()`, `trimmed = body.strip()`) is followed however long
    it is and whatever order the statements appear in. Flow-insensitive on
    purpose: a name tainted anywhere in the function is treated as tainted
    everywhere, which over-reports rather than under-reports.
    """
    names = set()
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            targets, value = _bindings(node)
            if value is None or not _carries_taint(value, names):
                continue
            for name in targets:
                if name not in names:
                    names.add(name)
                    changed = True
    return frozenset(names)


def _sink_calls(tree):
    """Every sink, as (method name, positional args, keyword value nodes).

    Yielded per CALL rather than per argument, because %-style lazy logging -
    `logger.error("bad: %r", payload)`, the idiom the logging docs recommend -
    spreads the format string and the value it escapes across two arguments of
    the same call. An argument-at-a-time scanner cannot see that the payload
    landed in an `%r` slot, so it reported the most common escaped spelling in
    the language as RAW.

    Keyword arguments are included: unscanned before T-527.17, so
    `logger.error("...", extra={"raw": payload})` slipped past entirely.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS:
                yield func.attr, node.args, [kw.value for kw in node.keywords]
            elif (isinstance(func, ast.Name)
                    and func.id in _BARE_CALL_SINKS):
                yield func.id, node.args, [kw.value for kw in node.keywords]
        elif isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call):
            yield "raise", node.exc.args, [kw.value for kw in node.exc.keywords]


def _percent_conversions(fmt):
    """The conversion character of each `%` placeholder, in order.

    None means "do not analyse this one" - a mapping-keyed format string, or a
    `*` width that consumes an argument and breaks positional matching. The
    caller then falls through to reporting RAW, which is the safe direction.
    """
    convs = []
    for match in _PERCENT_SPEC.finditer(fmt):
        if match.group("conv") == "%":
            continue
        if match.group("key") is not None:
            return None
        # A `*` WIDTH or PRECISION, not any asterisk in the string. This was
        # `if "*" in fmt` and a review caught it: "*** rejected %r" bailed to
        # RAW on correctly-escaped code. Only a star inside a specifier
        # consumes an argument and breaks positional matching.
        if "*" in (match.group("width") or "", match.group("prec") or ""):
            return None
        convs.append(match.group("conv"))
    return convs


def _mentions_payload(node, names=frozenset()):
    return any(_is_payload_reference(sub, names) for sub in ast.walk(node))


def _payload_sinks(func):
    """Every place `func` puts remote-controlled bytes into a durable record.

    Returns [(lineno, form, source_line)], where `form` is how that occurrence
    is escaped - '!r', '!a', 'repr()', 'ascii()', '%r', '%a', '{!r}', '{!a}' -
    or 'RAW' for an occurrence that reaches the record byte for byte.

    AST, NOT A LINE FILTER, and the difference is the whole point. The filter
    this replaces kept lines containing both 'logger.' and '{payload}', which
    requires the call and the f-string to be on the SAME PHYSICAL LINE.
    mqtt.py's 'Rejecting water low threshold' sink is not written that way - the
    call is on one line and the f-string on the next - so a raw payload could be
    planted there with the whole suite staying green. Measured before this was
    rewritten, not assumed: 23 tests, OK.

    TAINT, NOT A NAME (T-527.17). The version before this one matched the
    literal identifier `payload` at a call whose receiver was spelled
    `logger`, and both halves were escapable by writing ordinary code:

        requested = payload                            # H1, a bound local
        logger.error(f"Unsupported effect: {requested}")

        body = msg.payload.decode()                    # H2, a rename
        self.logger.error(f"bad: {body}")

    Both were CONFIRMED by running - the first left all four scored suites at
    `Ran 175 tests ... OK` with a live forgery in the tree, the second needed
    only a no-op refactor of a local. 13 of 18 measured shapes escaped. So the
    seed is now the SOURCE (`msg.payload`, or a parameter named `payload`),
    taint follows assignments to a fixpoint, and a sink is any of the logging
    method names on ANY receiver, plus `print`, plus a raised exception's
    arguments - keyword arguments included.

    WHAT IT STILL CANNOT SEE, stated specifically because the acceptance for
    T-527.17 asks for it and because a guard that overstates its reach is what
    stops the next reader looking:

      * A sink in a HELPER the function calls. This is intraprocedural; a
        tainted value passed to `_log_rejection(payload)` is invisible here.
      * Taint through a CONTAINER or an attribute - `d["raw"] = payload`,
        `self.last = payload`, `items.append(payload)`. Only plain name
        binding propagates.
      * Taint carried by an EXCEPTION. `int(payload)` raises a ValueError
        whose str() embeds the operand, so `except Exception as e:` followed
        by `logger.exception(f"...: {e}")` is a real forgery path that no AST
        rule can confirm, because whether a given exception's message quotes
        its input is a runtime property of that exception class. mqtt.py:1420
        is exactly that shape.

        It appears unreachable TODAY, but NOT for the reason this paragraph
        first gave. It said the int() calls are gated on `.isdigit()`, which
        is not a guarantee at all: `"\N{SUPERSCRIPT TWO}".isdigit()` is True
        and int() of it raises, so the exception really does reach :1420. What
        actually holds is narrower - a string passing `.isdigit()` cannot
        contain a newline or a carriage return, so the operand CPython quotes
        into that message cannot break a log line. Right verdict, wrong
        argument, caught by review. Superseded reasoning, kept visible so the
        next reader does not restore it: every int()/
        float() over the payload is either gated on `.isdigit()` or caught
        locally and logged with `!r` - but that is a property of today's
        branches, not of the guard.

        THE BLANKET FIX `{e!r}` IS NOW APPLIED at that line (T-527.12), so the
        shape is still invisible here and the instance is no longer live. It is
        pinned by test_the_catch_all_handler_reprs_its_exception below, which
        reads the source, because that is the only instrument that can. Note
        what `{e!r}` does NOT close and what the source assertion therefore
        does not promise: logger.exception() renders exc_info as well, and the
        traceback's last line is str(e). That route is formatted by the logging
        module and no call-site escaping reaches it.
      * A format string that is not a literal, `%`-formatting with a mapping
        key or a `*` width, and `.format()` with keyword fields or a splat.
        Each of those bails to RAW rather than guessing, so they are loud.
      * `%s`-vs-`%r` position matching assumes the argument tuple lines up
        with the specifiers. A mismatched count bails to RAW.

    The tests below this one are the positive and negative controls that it
    really reports the shapes it claims, and really does not report the ones
    it claims are safe.
    """
    source = textwrap.dedent(inspect.getsource(func))
    lines = source.splitlines()
    tree = ast.parse(source)
    names = _tainted_names(tree)
    found = []
    for method, args, kwargs in _sink_calls(tree):
        found.extend(_classify_payload_uses(method, args, kwargs, names))
    return [(lineno, form, lines[lineno - 1].strip())
            for lineno, form in sorted(found)]


def _classify_payload_uses(method, args, kwargs, names=frozenset()):
    """One sink call -> [(lineno, form)] for each tainted occurrence in it.

    Six passes over the call's arguments, and the ORDER matters. Lazy logging
    is claimed first because it is the only pass that needs the call's shape
    rather than one argument's. Sanitisers and wrappers come next, because
    `repr(payload)` sits inside a FormattedValue carrying no conversion of its
    own - scanning the f-string first reports that as RAW and then reports the
    repr() separately, which is one false alarm and one missed accounting from
    a single expression. The two in-argument formatting syntaxes follow, then
    f-strings, and whatever no pass has claimed by the end is reported RAW:
    reporting the REMAINDER rather than enumerating known-bad shapes means a
    shape nobody thought of comes out as RAW by default rather than as
    silence, which is the failure the line filter had.
    """
    accounted, found = set(), []
    every_arg = list(args) + list(kwargs)

    def claim(node, lineno=None, form=None):
        """Account for a subtree, optionally reporting how it was escaped.

        The no-form call is pass 1's: a sanitiser's subtree is claimed so no
        later pass reports the payload inside it, and there is nothing to
        report about it, so `lineno` is not required either.
        """
        if form is not None:
            found.append((lineno, form))
        accounted.update(id(sub) for sub in ast.walk(node))

    def unclaimed(node):
        return any(id(sub) not in accounted for sub in ast.walk(node)
                   if _is_payload_reference(sub, names))

    def walk_all():
        for argument in every_arg:
            yield from ast.walk(argument)

    # 0. %-style LAZY LOGGING, the idiom the logging docs recommend and the
    #    one an argument-at-a-time scanner structurally cannot read:
    #
    #        logger.error("rejected %r on %s", payload, topic)
    #
    #    logging does the interpolation itself inside getMessage(), so the
    #    format string is args[0] (args[1] for logger.log(), whose first
    #    argument is the level) and the values fill its specifiers in order.
    #    Matched by POSITION, never by whether an `%r` appears somewhere in
    #    the string - "at %s on %r" escapes the second argument and not the
    #    first, and a presence test would call both of them safe.
    offset = 1 if method == "log" else 0
    if len(args) > offset:
        template = args[offset]
        values = args[offset + 1:]
        if isinstance(template, ast.Constant) and isinstance(template.value, str):
            convs = _percent_conversions(template.value)
            if convs and len(convs) == len(values):
                for conv, value in zip(convs, values):
                    if _carries_taint(value, names):
                        claim(value, value.lineno,
                              _SAFE_PERCENT.get(conv, "RAW"))

    # 1. Sanitisers. int(payload) cannot carry a control character, so the
    #    whole subtree is claimed and nothing is reported for it.
    for node in walk_all():
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _SANITISERS):
            claim(node)

    # 2. repr() / ascii() wrappers.
    for node in walk_all():
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _SAFE_WRAPPERS):
            for wrapped in node.args:
                if _mentions_payload(wrapped, names) and unclaimed(wrapped):
                    claim(wrapped, node.lineno, _SAFE_WRAPPERS[node.func.id])

    # 3. "..." % (...) - positional, so the specifier is matched to the
    #    argument that fills it rather than assumed.
    for node in walk_all():
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod)
                and isinstance(node.left, ast.Constant)
                and isinstance(node.left.value, str)):
            continue
        convs = _percent_conversions(node.left.value)
        if convs is None:
            continue
        values = (node.right.elts if isinstance(node.right, ast.Tuple)
                  else [node.right])
        if len(values) != len(convs):
            continue
        for conv, value in zip(convs, values):
            if _carries_taint(value, names) and unclaimed(value):
                claim(value, value.lineno, _SAFE_PERCENT.get(conv, "RAW"))

    # 4. "...".format(...) - same, via the stdlib's own field parser so the
    #    grammar cannot drift from what str.format actually accepts.
    for node in walk_all():
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "format"
                and isinstance(node.func.value, ast.Constant)
                and isinstance(node.func.value.value, str)):
            continue
        if node.keywords or any(isinstance(a, ast.Starred) for a in node.args):
            continue
        auto, pairs, usable = 0, [], True
        for _, field, _, conv in string.Formatter().parse(node.func.value.value):
            if field is None:
                continue
            head = field.split(".")[0].split("[")[0]
            if head == "":
                index, auto = auto, auto + 1
            elif head.isdigit():
                index = int(head)
            else:
                usable = False
                break
            if index >= len(node.args):
                usable = False
                break
            pairs.append((conv, node.args[index]))
        if not usable:
            continue
        # Every FIELD gets its verdict before anything is claimed. One
        # argument can fill two fields - `"{0!r} also at {0}".format(body)` -
        # and that is ONE AST node, so claiming on the first field made
        # unclaimed() false for the second and the raw half vanished from the
        # report while the call was scored as escaped. Found by review; the
        # forged newline really did reach the record.
        pending = [(conv, value) for conv, value in pairs
                   if _carries_taint(value, names) and unclaimed(value)]
        for conv, value in pending:
            found.append((value.lineno, _SAFE_FORMAT.get(conv, "RAW")))
        for _, value in pending:
            claim(value)

    # 5. f-strings.
    for node in walk_all():
        if (isinstance(node, ast.FormattedValue)
                and _mentions_payload(node.value, names)
                and unclaimed(node.value)):
            claim(node.value, node.lineno,
                  _SAFE_CONVERSIONS.get(node.conversion, "RAW"))

    # 6. The remainder. Anything tainted that no pass above explained.
    for node in walk_all():
        if id(node) not in accounted and _is_payload_reference(node, names):
            found.append((node.lineno, "RAW"))
    return found


class ConnackTestBase(unittest.TestCase):
    def setUp(self):
        self.client = RecordingClient()
        mqtt_mod.client = self.client
        mqtt_mod.pump = MagicMock()
        mqtt_mod.pump.get_speed.return_value = 0
        mqtt_mod.light = MagicMock()
        mqtt_mod.light.get_brightness.return_value = 42
        mqtt_mod.distance_sensor = MagicMock()
        patch.object(mqtt_mod, "flash_lights").start()

        # start_publisher_threads() is NOT patched here, unlike in the sibling
        # suites. Its once-only flag IS the thing the acceptance criterion names
        # ("leaves the publisher-threads guard unburned"), so a double in front
        # of it would move the assertion off the state and onto a call count.
        #
        # What is patched instead is the two THREAD TARGETS it hands to
        # threading.Thread. Both are `while True:` loops in the real module, so
        # letting them start would leave the suite with two spinning daemon
        # threads publishing camera frames. A MagicMock target runs once and the
        # thread exits, which keeps the real guard, the real lock and the real
        # thread creation in the path.
        patch.object(mqtt_mod, "publish_pcb_temperature").start()
        patch.object(mqtt_mod, "publish_images").start()

        # MODULE-SCOPED STATE. The flag is a process global and survives between
        # test methods, so whether a case saw an unburned guard would otherwise
        # depend on unittest's method ordering.
        mqtt_mod._publisher_threads_started = False
        self.addCleanup(setattr, mqtt_mod, "_publisher_threads_started", False)
        mqtt_mod._last_birth_announce = None
        self.addCleanup(setattr, mqtt_mod, "_last_birth_announce", None)
        self.addCleanup(patch.stopall)

    def assertGuardUnburned(self):
        self.assertFalse(
            mqtt_mod._publisher_threads_started,
            "the publisher-threads once-only guard was burned on a connection "
            "that never came up, so the loops are running against a dead "
            "socket and the connect that succeeds will correctly decline to "
            "re-start them - PCB temperature `unknown` for up to 30 minutes "
            "and the camera images for up to an hour",
        )


class TestARefusedConnackDoesNothing(ConnackTestBase):
    """The acceptance criterion, asserted directly on each of its three halves.

    Every case here runs the REAL on_connect against a refused reason code. The
    accepted-path class below is what makes these assertions mean anything: on
    its own, "the guard is unburned" is also what a permanently broken
    on_connect would report.
    """

    def test_a_refusal_leaves_the_publisher_thread_guard_unburned(self):
        for rc in EVERY_REFUSAL:
            with self.subTest(rc=str(rc)):
                mqtt_mod._publisher_threads_started = False
                mqtt_mod.on_connect(self.client, None, None, rc)
                self.assertGuardUnburned()

    def test_a_refusal_announces_nothing(self):
        for rc in EVERY_REFUSAL:
            with self.subTest(rc=str(rc)):
                self.client.calls.clear()
                mqtt_mod.on_connect(self.client, None, None, rc)
                self.assertEqual(
                    [], self.client.calls,
                    "discovery, availability or light state was published into "
                    "a connection the broker is closing",
                )

    def test_a_refusal_subscribes_to_nothing(self):
        for rc in EVERY_REFUSAL:
            with self.subTest(rc=str(rc)):
                self.client.subscriptions.clear()
                mqtt_mod.on_connect(self.client, None, None, rc)
                self.assertEqual([], self.client.subscriptions)

    def test_a_refusal_is_logged_as_an_error_naming_the_reason(self):
        """The device's failure mode is silence, so the log is the only place a
        refusal is ever visible. Asserted on "REFUSED", which appears in exactly
        one string in mqtt.py - matching on the reason name instead would also
        match the generic decode line if a payload happened to contain it."""
        with self.assertLogs(mqtt_mod.logger, level="ERROR") as captured:
            mqtt_mod.on_connect(self.client, None, None, NOT_AUTHORIZED)
        messages = [r.getMessage() for r in captured.records]
        refusals = [m for m in messages if "REFUSED" in m]
        self.assertEqual(1, len(refusals), messages)
        self.assertIn("Not authorized", refusals[0],
                      "the log names the refusal but not which one, so a "
                      "credential problem is indistinguishable from a broker "
                      "that is down")

    def test_a_refusal_does_not_claim_the_client_connected(self):
        """The original symptom, and the reason this went unnoticed for so long:
        the log line said `Connected with result code Not authorized`."""
        with self.assertLogs(mqtt_mod.logger, level="INFO") as captured:
            mqtt_mod.on_connect(self.client, None, None, NOT_AUTHORIZED)
        for message in (r.getMessage() for r in captured.records):
            self.assertNotIn(
                "Connected with result code", message,
                "the log claims a connection that was refused",
            )


class TestAnAcceptedConnackStillDoesEverything(ConnackTestBase):
    """The other direction, and the control on the class above.

    An over-correction - gating too much, or inverting the comparison - is not
    a smaller bug than the one being fixed. It would leave the device connected
    and permanently silent, which is the 2026-08-05 outage again.
    """

    def test_an_accepted_connack_burns_the_publisher_thread_guard(self):
        mqtt_mod.on_connect(self.client, None, None, ACCEPTED)
        self.assertTrue(
            mqtt_mod._publisher_threads_started,
            "the publishers never started on a good connection, so the PCB "
            "temperature and camera entities never update",
        )

    def test_an_accepted_connack_announces(self):
        mqtt_mod.on_connect(self.client, None, None, ACCEPTED)
        topics = [c.topic for c in self.client.calls]
        self.assertIn("gardyn/status", topics)
        self.assertIn(f"homeassistant/light/gardyn/{ID}_light/config", topics)

    def test_an_accepted_connack_subscribes(self):
        mqtt_mod.on_connect(self.client, None, None, ACCEPTED)
        self.assertIn(HA_STATUS, [t for t, _ in self.client.subscriptions])

    def test_a_bare_int_zero_is_still_an_acceptance(self):
        """paho's VERSION1 + MQTTv3 callback passes the bare v3 return code, and
        every existing case in this repo's suite calls on_connect(..., 0). A
        gate that only understood ReasonCode would refuse both."""
        mqtt_mod.on_connect(self.client, None, None, 0)
        self.assertTrue(mqtt_mod._publisher_threads_started)
        self.assertNotEqual([], self.client.calls)


class TestTheGuardTheFixProtects(ConnackTestBase):
    """The defect end to end, counted in threads rather than in flags.

    Everything above asserts the flag, which is a PROXY for the thing that
    actually breaks: the periodic publishers never starting. This class counts
    the publisher threads themselves, so it holds even if the guard is ever
    reimplemented with different state.

    threading.Thread is patched here and nowhere else in this file, because
    every other case wants the real thread creation exercised. Nothing else in
    the process spawns a thread inside these windows.
    """

    def _connect(self, rc):
        mqtt_mod.on_connect(self.client, None, None, rc)

    def test_one_good_connect_starts_both_publishers(self):
        with patch.object(mqtt_mod.threading, "Thread") as thread:
            self._connect(ACCEPTED)
        self.assertEqual(2, thread.call_count,
                         "the PCB temperature and camera loops are the two "
                         "publishers on_connect is responsible for starting")

    def test_a_second_good_connect_does_not_start_them_again(self):
        """The invariant the fix's whole rationale rests on: the flag is
        per-PROCESS, and on_connect fires on every reconnect (roughly 25 times a
        day). Without this, "burning the guard" would not be a durable loss."""
        with patch.object(mqtt_mod.threading, "Thread") as thread:
            self._connect(ACCEPTED)
            self._connect(ACCEPTED)
        self.assertEqual(2, thread.call_count,
                         "a reconnect spawned a second set of publisher "
                         "threads; they accumulate on a 512 MB host")

    def test_the_publishers_start_on_the_connect_that_actually_succeeded(self):
        """THE DEFECT, AS IT ACTUALLY HAPPENS - a broker password rotated under
        a running Pi, so the first CONNACK is refused and the next succeeds.

        WHICH ASSERTION HERE IS THE DISCRIMINATING ONE. The intermediate 0 is:
        pre-fix, the refused pass spawned both loops against a client that was
        not connected, so it read 2 and this line fails. The final 2 is NOT
        discriminating - it holds pre-fix too, because those same two threads
        already existed. It is kept as a guard against over-correcting into a
        gate that swallows the healthy connect as well, which is the failure
        mode that would silence the device completely.
        """
        with patch.object(mqtt_mod.threading, "Thread") as thread:
            self._connect(NOT_AUTHORIZED)
            self.assertEqual(
                0, thread.call_count,
                "publisher loops were spawned against a refused connection; "
                "their first publish is lost and the PCB temperature entity is "
                "`unknown` for the next 30 minutes",
            )
            self._connect(ACCEPTED)
        self.assertEqual(
            2, thread.call_count,
            "the gate swallowed a HEALTHY connect - no publishers at all",
        )


class TestPahoCallsTheCallbacksLikeThis(ConnackTestBase):
    """THE ARITY CONTRACT, driven the way paho 2.0.0 actually drives it.

    Everything else in this repo calls on_connect with FOUR positional
    arguments, because that is what the callbacks were first written against.
    paho's VERSION2 dispatch passes FIVE - and never omits the fifth, since it
    synthesises an empty Properties when the packet carried none. The gap that
    leaves is not theoretical and not gradual: delete `properties=None` from the
    signature and this whole suite stays green while the device raises TypeError
    inside the callback on every single connect. paho's suppress_exceptions is
    False, so it re-raises; the exception leaves loop_forever(); the process
    exits; mqtt.service is Restart=always with StartLimitIntervalSec=0, so the
    Pi enters a permanent 10-second crash loop with the grow light off and no
    console to reach it from.

    THIS IS THE INBOUND HALF of what RecordingClient does outbound. That double
    declares paho's real publish() signature so it cannot disagree with the
    library about whether an omitted `retain` is the same as retain=False; these
    cases declare paho's real CALL so the suite cannot disagree with the library
    about how many arguments arrive.

    Positional throughout, deliberately. A keyword call would keep passing
    against a signature whose parameters had been reordered, which is the other
    half of what "arity" is protecting.
    """

    def test_on_connect_takes_the_five_positional_arguments_paho_passes(self):
        """The accepted path, delivered the way the library delivers it."""
        call_on_connect_as_paho_does(self.client, ACCEPTED)
        self.assertTrue(
            mqtt_mod._publisher_threads_started,
            "on_connect could not be called the way paho calls it, or did not "
            "reach start_publisher_threads once it was",
        )

    def test_the_refusal_gate_still_fires_under_the_five_argument_call(self):
        """The gate and the arity are independent, so both directions are
        driven through the real calling convention rather than only the
        accepted one."""
        for rc in EVERY_REFUSAL:
            with self.subTest(rc=str(rc)):
                mqtt_mod._publisher_threads_started = False
                self.client.calls.clear()
                call_on_connect_as_paho_does(self.client, rc)
                self.assertGuardUnburned()
                self.assertEqual([], self.client.calls)

    def test_on_connect_does_not_require_a_fifth_argument_either(self):
        """The other side of the same contract, and the reason `properties=None`
        is a default rather than a required parameter: every pre-existing call
        site in this repo passes four, and those cases are the regression suite
        for the accepted path. A signature that DEMANDED five would redden them
        all - loudly, which is the safe direction, but it would still be a
        change nobody asked for."""
        mqtt_mod.on_connect(self.client, None, None, ACCEPTED)
        self.assertTrue(mqtt_mod._publisher_threads_started)

    def test_on_disconnect_takes_the_five_positional_arguments_paho_passes(self):
        """on_disconnect had NO call site in this suite before T-527.11, so its
        arity was pinned by nothing at all - a strictly wider gap than
        on_connect's, and one that fails on the DISCONNECT path rather than the
        connect path, i.e. exactly when the broker has already gone away."""
        with self.assertLogs(mqtt_mod.logger, level="WARNING") as captured:
            call_on_disconnect_as_paho_does(self.client, NORMAL_DISCONNECTION)
        self.assertTrue(
            any("cleanly" in r.getMessage() for r in captured.records),
            [r.getMessage() for r in captured.records])

    def test_on_disconnect_reports_an_unclean_drop_as_an_error(self):
        """The branch that matters operationally, and the one that pins that
        `rc == 0` is being asked of a ReasonCode rather than of an int. paho
        hands a ReasonCode here too; a clean local drop arrives as value 0
        ('Normal disconnection') and a connection loss as 128 ('Unspecified
        error'), so the comparison only works because ReasonCode.__eq__ accepts
        a bare int."""
        with self.assertLogs(mqtt_mod.logger, level="ERROR") as captured:
            call_on_disconnect_as_paho_does(self.client, CONNECTION_LOST,
                                            from_server=True)
        messages = [r.getMessage() for r in captured.records]
        self.assertTrue(any("Unexpectedly disconnected" in m for m in messages),
                        messages)

    def test_on_message_takes_the_three_positional_arguments_paho_passes(self):
        """Checked rather than assumed, and it is the one that is NOT at risk:
        paho passes (client, userdata, message) to on_message in every callback
        API version, so there is no VERSION1/VERSION2 divergence to be caught
        out by. It is pinned anyway because "we checked and it was three" is
        worth exactly as much as the assertion that keeps it three."""
        mqtt_mod.light = MagicMock()
        mqtt_mod.light.get_brightness.return_value = 42
        msg = MagicMock()
        msg.topic = LIGHT_COMMAND
        msg.payload = b"ON"
        with self.assertLogs(mqtt_mod.logger, level="INFO") as captured:
            mqtt_mod.on_message(self.client, None, msg)
        self.assertTrue(
            any(r.getMessage().startswith("Decoded payload on ")
                for r in captured.records),
            [r.getMessage() for r in captured.records])


class TestWhichQuestionTheGateAsks(unittest.TestCase):
    """_connack_refused() in isolation, including the case that separates the
    two plausible spellings of it."""

    def test_the_measured_connack_refusals_are_all_refusals(self):
        # The numbers are the ones a real paho 2.0.0 produced - see the table in
        # this module's docstring. Written as literals rather than derived from
        # FAILURE_THRESHOLD, so a mutant that moves the threshold cannot move
        # the expectation with it.
        for value, name in [(133, "Client identifier not valid"),
                            (134, "Bad user name or password"),
                            (135, "Not authorized"),
                            (136, "Server unavailable"),
                            # v3 rc=6 and every code above it, via
                            # convert_connack_rc_to_reason_code's fallback. It
                            # is what closes the space: no v3 CONNACK code
                            # reaches this callback as a non-failure except 0.
                            (128, "Unspecified error")]:
            with self.subTest(name=name):
                self.assertTrue(
                    mqtt_mod._connack_refused(ReasonCodeDouble(value, name)))

    def test_success_is_not_a_refusal(self):
        self.assertFalse(mqtt_mod._connack_refused(ReasonCodeDouble(0, "Success")))

    def test_it_reads_is_failure_rather_than_comparing_against_zero(self):
        """The one input on which the two spellings disagree.

        BE PRECISE ABOUT WHAT THIS PINS. No CONNACK reason code is both non-zero
        and not a failure - for CONNACK the two spellings agree everywhere - so
        this input is not reachable through a real connection. It is here
        because `is_failure` and `rc != 0` are interchangeable by inspection,
        which is exactly the condition under which somebody simplifies one into
        the other. Other packet types do use the low values (SUBACK's granted-
        QoS codes are 0, 1 and 2), so the distinction is real in the library
        even where it is unreachable in this callback.
        """
        low_but_successful = ReasonCodeDouble(1, "Granted QoS 1")
        self.assertNotEqual(0, low_but_successful.value)
        self.assertFalse(low_but_successful.is_failure)
        self.assertFalse(
            mqtt_mod._connack_refused(low_but_successful),
            "the gate is comparing rc against 0 rather than asking whether the "
            "reason code is a failure",
        )

    def test_the_gate_does_not_reimplement_the_boundary(self):
        """WHAT THIS CANNOT DO, said first, because its previous name
        (`test_the_failure_boundary_is_where_paho_puts_it`) claimed the
        opposite. ReasonCodeDouble.is_failure is `value >= FAILURE_THRESHOLD`
        and FAILURE_THRESHOLD is defined at the top of THIS file, so both sides
        of the comparison below come from the same place. If paho moved 0x80
        this test would go on passing, cheerfully, against a stale copy of the
        number. It measures its own constant.

        Nothing in this suite can do better, and that is a property of the
        suite's design rather than an oversight to fix here: paho is stubbed out
        of sys.modules by tests.test_water_interlock and
        tests/test_suite_isolation.py asserts that no real one leaks in, so
        there is no library present to compare against. The pin against the real
        library is the measured run recorded in this module's docstring, which
        is evidence with a date on it rather than an assertion.

        WHAT IT DOES PIN, which is worth keeping: that _connack_refused()
        delegates the boundary to the reason code instead of carrying a
        threshold of its own. A gate rewritten as `rc.value >= 200`, or as
        `rc.value > 128`, disagrees with the double at these two inputs and
        fails here.
        """
        self.assertFalse(mqtt_mod._connack_refused(ReasonCodeDouble(127, "below")))
        self.assertTrue(mqtt_mod._connack_refused(ReasonCodeDouble(128, "Unspecified error")))

    def test_bare_ints_fall_back_to_a_comparison_against_zero(self):
        """An int has no is_failure, so the fallback is the only thing that can
        classify it. 1..5 are the v3.1.1 refusal codes."""
        self.assertFalse(mqtt_mod._connack_refused(0))
        for v3_code in (1, 2, 3, 4, 5):
            with self.subTest(v3_code=v3_code):
                self.assertTrue(mqtt_mod._connack_refused(v3_code))


class TestTheDecodeLineCannotBeForged(unittest.TestCase):
    """The generic decode line, fed a payload from outside this namespace."""

    def setUp(self):
        self.client = RecordingClient()
        mqtt_mod.light = MagicMock()
        mqtt_mod.light.get_brightness.return_value = 42
        mqtt_mod.pump = MagicMock()
        mqtt_mod.pump.get_speed.return_value = 0
        patch.object(mqtt_mod, "flash_lights").start()
        patch.object(mqtt_mod, "announce_to_home_assistant").start()
        mqtt_mod._last_birth_announce = None
        self.addCleanup(setattr, mqtt_mod, "_last_birth_announce", None)
        self.addCleanup(patch.stopall)

    def _decode_record(self, topic, raw_payload):
        """Deliver one message and return ONLY the generic decode line.

        Selected by prefix rather than by index or by searching the whole
        capture. The Home Assistant status branch logs the payload too, with
        {payload!r} of its own, so an assertion over every captured message
        would pass on that line while the line under test stayed broken.
        """
        msg = MagicMock()
        msg.topic = topic
        msg.payload = raw_payload
        with self.assertLogs(mqtt_mod.logger, level="INFO") as captured:
            mqtt_mod.on_message(self.client, None, msg)
        decoded = [r.getMessage() for r in captured.records
                   if r.getMessage().startswith("Decoded payload on ")]
        self.assertEqual(1, len(decoded),
                         f"expected exactly one decode line, got {decoded!r}")
        return decoded[0]

    def test_a_newline_in_the_payload_cannot_forge_a_log_line(self):
        forged = b"online\n2026-08-08 12:00:00,000 - mqtt - ERROR - pump seized"
        # The fixture control: the payload really does carry a raw newline, so
        # a green result below is the escaping working rather than the input
        # being harmless.
        self.assertIn(b"\n", forged)

        message = self._decode_record(HA_STATUS, forged)
        self.assertNotIn("\n", message,
                         "a remote payload put a line break into gardyn.log, so "
                         "it can write log lines of its own")
        self.assertIn("\\n", message,
                      "the line break should survive as an escape, not vanish")
        self.assertIn("pump seized", message,
                      "the payload must still be readable; escaping it is not "
                      "the same as dropping it")

    def test_a_carriage_return_in_the_payload_is_escaped(self):
        """Worse than a newline in a terminal: CR rewrites the line just
        printed, so the forgery replaces real output rather than adding to it."""
        message = self._decode_record(HA_STATUS, b"online\rall clear")
        self.assertNotIn("\r", message)
        self.assertIn("\\r", message)

    def test_an_ansi_escape_in_the_payload_is_escaped(self):
        message = self._decode_record(HA_STATUS, b"online\x1b[2Jcleared")
        self.assertNotIn("\x1b", message)
        self.assertIn("\\x1b", message)

    def test_a_rejected_water_threshold_cannot_forge_a_log_line_either(self):
        """The sibling the first version of this suite missed.

        gardyn/water/low/cm/set is in COMMAND_SUBSCRIPTIONS at QoS 1, so its
        payload is remote input like any other, and everything that fails
        float() reaches a second log line further down on_message. Review found
        it still raw after the decode line had been fixed.

        NOT because it is "the worse of the two" - two earlier versions of this
        file said so and both were wrong; see the module docstring. Both sinks
        land in gardyn.log identically. It matters because it is a SECOND one,
        which is a statement about coverage rather than about severity.

        Asserted on the record that names this handler, not on the whole
        capture: the decode line above logs the same payload (escaped, now) and
        would satisfy a looser search on its own.
        """
        forged = b"x\n2026-08-08 12:00:00,000 - mqtt - ERROR - pump seized"
        self.assertIn(b"\n", forged)  # the fixture can exhibit the defect

        msg = MagicMock()
        msg.topic = "gardyn/water/low/cm/set"
        msg.payload = forged
        with self.assertLogs(mqtt_mod.logger, level="INFO") as captured:
            mqtt_mod.on_message(self.client, None, msg)

        rejections = [r.getMessage() for r in captured.records
                      if r.getMessage().startswith("Invalid water low cm value")]
        self.assertEqual(1, len(rejections),
                         [r.getMessage() for r in captured.records])
        self.assertNotIn("\n", rejections[0],
                         "a rejected threshold put a line break into "
                         "gardyn.log at ERROR level")
        self.assertIn("\\n", rejections[0])

    def test_no_log_line_in_on_message_interpolates_a_payload_raw(self):
        """The rule rather than the instances, so a further sink cannot be
        added raw without this failing.

        Source-level on purpose: the case-by-case tests above pin the lines that
        exist, and none of them can notice a new one. What changed in T-527.11
        is HOW the source is read - see _payload_sinks(). There are three payload
        sinks in on_message today; the third, in the threshold-rejection branch,
        is written across two lines and the line-based version of this test could
        not see it at all.
        """
        raw = [(lineno, line) for lineno, form, line
               in _payload_sinks(mqtt_mod.on_message) if form == "RAW"]
        self.assertEqual(
            [], raw,
            "a log line puts the payload into a record without escaping it, so "
            "a remote client can write arbitrary lines into gardyn.log. "
            f"Escaping forms this rule recognises: "
            f"{sorted(set(_SAFE_CONVERSIONS.values()) | set(_SAFE_WRAPPERS.values()))}",
        )

    def test_the_payload_sink_scanner_reports_the_shapes_that_defeated_the_old_one(self):
        """POSITIVE CONTROL on the test above, which is a check whose passing
        result is an ABSENCE - the one shape that cannot tell a clean scan from
        a dead one.

        Its predecessor failed exactly here. It reported no offenders whether
        the code was clean or the offender was simply written in a shape it
        could not parse, and the multi-line shape was one of those. So each
        shape gets fed to the scanner directly, including the four the line
        filter missed: the multi-line f-string, %-style lazy logging,
        str.format(), and concatenation.

        NOT ONE OF THESE FIXTURES IS SPELLED THE WAY THE SCANNER USED TO
        REQUIRE, and that is the T-527.17 M1 fix rather than a style choice.
        Every fixture here previously took parameters named exactly `payload`
        and `logger` - the two names the old scanner matched on - so the
        control supplied the scanner with the only two things it could read
        and could therefore only vary the dimension already handled. That is
        the "assertion satisfied by the test's own input" shape, and it is why
        two confirmed escapes lived under a green control. These take `msg`
        and a receiver named `sink`, so the taint has to be found through
        `msg.payload` and the sink through a name the scanner does not know.
        """
        def multi_line_fstring(msg, sink):
            body = msg.payload.decode()
            sink.error(
                f"Rejecting water low threshold {body} - "
                f"must be a number"
            )

        def percent_style_lazy_logging(msg, sink):
            sink.error("Rejecting water low threshold %s", msg.payload)

        def str_format(msg, sink):
            sink.error("Rejecting water low threshold {}".format(msg.payload))

        def concatenation(msg, sink):
            sink.error("Rejecting water low threshold " + msg.payload)

        def str_conversion(msg, sink):
            sink.error(f"Rejecting water low threshold {msg.payload!s}")

        def bound_intermediate(msg, sink):
            # H1, confirmed by running: the normal way a new branch gets
            # written. Left all four scored suites at `Ran 175 tests ... OK`
            # with a live forgery in the tree.
            requested = msg.payload
            sink.error(f"Unsupported light effect: {requested}")

        def renamed_local(msg, sink):
            # H2: a no-op refactor of the local disarmed the whole rule.
            body = msg.payload.decode("utf-8").strip()
            sink.error(f"bad: {body}")

        def taint_through_a_chain(msg, sink):
            raw = msg.payload
            decoded = raw.decode()
            trimmed = decoded.strip()
            sink.warning(f"three hops from the wire: {trimmed}")

        def taint_through_nested_blocks(msg, sink):
            # The fixpoint's reason for existing, and it took a surviving
            # mutant to find a case that needs it. ast.walk is BREADTH-first,
            # not source order, so the depth-2 assignment below is visited
            # AFTER the depth-1 one that consumes it. A single pass sees `raw`
            # as untainted at the moment it processes `body` and stops there.
            if msg.topic:
                raw = msg.payload
            body = raw.decode()
            sink.error(f"across two nesting levels: {body}")

        def attribute_receiver(msg, self_like):
            self_like.logger.error(f"through self.logger: {msg.payload}")

        def module_level_logging(msg, logging_mod):
            logging_mod.error("through the logging module: %s", msg.payload)

        def keyword_argument(msg, sink):
            sink.error("nothing raw here", extra={"raw": msg.payload})

        def fatal_alias(msg, sink):
            # logging.Logger.fatal is a real alias for critical, and it was
            # missing from the sink set - so this exact line was a live
            # forgery path reachable by renaming one word. Found by review.
            sink.fatal("water threshold rejected: %s" % msg.payload.decode())

        def reused_format_argument(msg, sink):
            # One argument, two fields. The escaped field used to claim the
            # node and hide the raw one, so this whole call reported as
            # `{!r}` while half of it reached the record verbatim.
            body = msg.payload.decode()
            sink.error("{0!r} also at {0}".format(body))

        def printed(msg, sink):
            print(f"stdout is captured into the journal too: {msg.payload}")

        def raised(msg, sink):
            # on_message wraps everything in `except Exception as e:` and logs
            # it, so a raised message is a sink one indirection away.
            raise ValueError(f"cannot parse {msg.payload}")

        for shape in (multi_line_fstring, percent_style_lazy_logging,
                      str_format, concatenation, str_conversion,
                      bound_intermediate, renamed_local, taint_through_a_chain,
                      taint_through_nested_blocks, fatal_alias,
                      reused_format_argument,
                      attribute_receiver, module_level_logging,
                      keyword_argument, printed, raised):
            with self.subTest(shape=shape.__name__):
                forms = [form for _, form, _ in _payload_sinks(shape)]
                self.assertIn(
                    "RAW", forms,
                    f"the scanner cannot see a raw payload written as "
                    f"{shape.__name__}, so a clean report from it means nothing",
                )

    def test_the_control_fixtures_do_not_hand_the_scanner_the_names_it_reads(self):
        """Pins the T-527.17 M1 fix, which nothing else can see.

        The control above is only a control while its fixtures are spelled in
        a way the scanner does not already special-case. Every one of them
        used to take parameters named `payload` and `logger` - the two names
        the old scanner matched on - so the control supplied the scanner with
        its own answer and could only ever vary the dimension already handled.
        Two confirmed forgery escapes lived underneath it, green.

        Nothing about that failure is visible from reading either the control
        or the scanner: both are correct in isolation, and the defect is only
        in their relationship. A mutant renaming a fixture parameter back to
        `payload` would restore the blind spot and no other assertion in this
        file would move. So the relationship is asserted directly.
        """
        control = self.test_the_payload_sink_scanner_reports_the_shapes_that_defeated_the_old_one
        tree = ast.parse(textwrap.dedent(inspect.getsource(control)))
        fixtures = [n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name != control.__name__]
        self.assertGreaterEqual(len(fixtures), 13, "fixtures went missing")

        for fixture in fixtures:
            with self.subTest(fixture=fixture.name):
                params = [a.arg for a in fixture.args.args]
                self.assertNotIn(
                    "payload", params,
                    "this fixture hands the scanner a parameter named exactly "
                    "`payload`, which is one of its two taint seeds - so it "
                    "can no longer prove the scanner finds taint any OTHER "
                    "way, which is the whole point of the control",
                )
                # Every NAME anywhere in the receiver expression, not just a
                # bare `logger.error(...)`. A surviving mutant found this:
                # renaming a fixture's parameter so it reached its sink as
                # `logger.logger.error(...)` left the receiver an Attribute
                # rather than a Name, and the narrower comprehension this
                # replaces could not see it at all.
                receivers = {
                    name.id
                    for node in ast.walk(fixture)
                    if isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    for name in ast.walk(node.func.value)
                    if isinstance(name, ast.Name)
                }
                self.assertNotIn(
                    "logger", receivers,
                    "this fixture reaches its sink through a receiver spelled "
                    "`logger`, the name the pre-T-527.17 scanner required. A "
                    "control that only exercises the recognised spelling "
                    "cannot fail for the reason it exists",
                )

    def test_the_payload_sink_scanner_does_not_cry_wolf_over_escaped_ones(self):
        """The other half of the control. A scanner that reported RAW for
        everything would pass the case above and be equally useless - it would
        redden on the shipping code, which is the direction that gets a check
        deleted rather than fixed.

        The last four forms are the T-527.17 M2 fix. `%r`, `%a`, `{!r}` and
        `{!a}` escape exactly as `!r` does and were all reported RAW, so the
        failure message told the reader that four correctly-escaped spellings
        were remote forgery paths.
        """
        def every_escaped_form(payload, logger):
            logger.error(f"a {payload!r} b {payload!a} c {repr(payload)} "
                         f"d {ascii(payload)}")

        forms = [form for _, form, _ in _payload_sinks(every_escaped_form)]
        self.assertNotIn("RAW", forms, forms)
        self.assertEqual(["!a", "!r", "ascii()", "repr()"], sorted(forms))

    def test_the_other_two_formatting_syntaxes_escape_too(self):
        """T-527.17 M2. Each of these was reported RAW before the scanner
        learned to read a format string positionally."""
        def percent_repr(msg, sink):
            sink.error("bad: %r" % msg.payload)

        def percent_ascii(msg, sink):
            sink.error("bad: %a" % msg.payload)

        def percent_lazy_repr(msg, sink):
            sink.error("bad: %r", msg.payload)

        def format_repr(msg, sink):
            sink.error("bad: {!r}".format(msg.payload))

        def format_ascii(msg, sink):
            sink.error("bad: {0!a}".format(msg.payload))

        def literal_stars_are_not_a_star_width(msg, sink):
            # `if "*" in fmt` bailed on this and called correctly-escaped code
            # RAW. Only a star inside a specifier consumes an argument.
            sink.error("*** rejected %r ***" % msg.payload)

        expected = {"percent_repr": "%r", "percent_ascii": "%a",
                    "percent_lazy_repr": "%r", "format_repr": "{!r}",
                    "format_ascii": "{!a}",
                    "literal_stars_are_not_a_star_width": "%r"}
        for shape in (percent_repr, percent_ascii, percent_lazy_repr,
                      format_repr, format_ascii,
                      literal_stars_are_not_a_star_width):
            with self.subTest(shape=shape.__name__):
                forms = [form for _, form, _ in _payload_sinks(shape)]
                self.assertEqual([expected[shape.__name__]], forms)

    def test_a_mixed_format_string_is_matched_by_POSITION_not_by_presence(self):
        """The reason pass 3 parses specifiers in order instead of asking
        whether the string contains an `%r` anywhere.

        A presence test would call both of these safe. Only one of them is.
        """
        def payload_at_the_escaped_slot(msg, sink):
            sink.error("at %r on %s", msg.payload, "topic")

        def payload_at_the_raw_slot(msg, sink):
            sink.error("at %s on %r", msg.payload, "topic")

        def payload_SECOND_at_the_raw_slot(msg, sink):
            # The case the first pair cannot distinguish, and a surviving
            # mutant is what found it. In both fixtures above the payload is
            # argument ONE, so reading `convs[0]` instead of the specifier
            # that actually matches it gives the right answer by accident.
            # Here the two disagree: convs[0] is `r`, and the payload lands in
            # the `%s` slot.
            sink.error("at %r on %s", "topic", msg.payload)

        self.assertEqual(
            ["%r"], [f for _, f, _ in _payload_sinks(payload_at_the_escaped_slot)])
        self.assertEqual(
            ["RAW"], [f for _, f, _ in _payload_sinks(payload_at_the_raw_slot)])
        self.assertEqual(
            ["RAW"],
            [f for _, f, _ in _payload_sinks(payload_SECOND_at_the_raw_slot)],
            "the payload is in the %s slot; reading the first specifier in "
            "the string instead of the one that matches its position calls "
            "this escaped when it is not",
        )

    def test_the_sink_method_set_is_pinned_AS_A_SET_and_every_name_is_real(self):
        """Six of the eight names in _LOG_METHODS were pinned by nothing, and
        `fatal` was missing from it entirely - a live forgery path one word
        wide. A review swept the set per name and found both.

        The set is asserted as a WHOLE against a literal, not iterated over.
        Iterating would be self-referential: dropping a name would delete its
        own case and the test would stay green, which is how the omission
        survived in the first place. Each name is then checked twice - that
        logging.Logger really has it, so the set cannot accumulate names that
        do nothing, and that _sink_calls actually treats it as a sink, so a
        name can be in the set and still be unwired.
        """
        import logging

        self.assertEqual(
            ("critical", "debug", "error", "exception", "fatal", "info",
             "log", "warn", "warning"),
            tuple(sorted(_LOG_METHODS)),
            "the sink-method set changed. Adding a name is fine; REMOVING one "
            "opens a forgery path, which is what happened with `fatal`",
        )

        for method in sorted(_LOG_METHODS):
            with self.subTest(method=method):
                self.assertTrue(
                    callable(getattr(logging.Logger, method, None)),
                    f"logging.Logger has no {method}(), so this name in the "
                    f"set matches calls that are not log sinks at all",
                )
                tree = ast.parse(f"sink.{method}(f'raw: {{body}}')")
                self.assertEqual(
                    [method], [m for m, _, _ in _sink_calls(tree)],
                    f"{method} is in _LOG_METHODS but _sink_calls does not "
                    f"yield it, so nothing in the set reaches the scanner",
                )

    def test_taint_stops_at_a_numeric_conversion(self):
        """Without this the scanner reddens correct shipping code.

        mqtt.py binds `candidate = float(payload)` at 1367 and logs a value
        derived from it at 1405; `requested = int(payload)` and `brightness =
        int(payload)` are the same shape. An int cannot carry a newline, so
        propagating taint through one would report a forgery path that does
        not exist - and a check that cries wolf on shipping code is the kind
        that gets deleted rather than fixed.
        """
        def sanitised(msg, sink):
            candidate = float(msg.payload)
            sink.info(f"threshold now {candidate:.2f}cm")

        def sanitised_inline(msg, sink):
            sink.info(f"speed {int(msg.payload)}")

        self.assertEqual([], _payload_sinks(sanitised))
        self.assertEqual([], _payload_sinks(sanitised_inline))

    def test_the_real_on_message_is_clean_under_the_widened_scanner(self):
        """The widening is only trustworthy if it did not also start lying.

        Separate from the shipping-code test above it because that one asserts
        the RULE and this one asserts that widening the rule did not change
        the verdict on the code it has always covered. If this goes red while
        that one is green, the new passes have introduced a false positive.
        """
        forms = [(lineno, form, line) for lineno, form, line
                 in _payload_sinks(mqtt_mod.on_message) if form == "RAW"]
        self.assertEqual([], forms)

    def test_the_boundaries_the_scanner_DOES_NOT_see_are_pinned_as_such(self):
        """A guard that overstates its reach is what stops the next reader
        looking, so _payload_sinks()'s docstring enumerates what it cannot
        see - and a docstring is a claim until something checks it.

        These assert the LIMITATION. Each is a real forgery path that this
        scanner reports clean, and each is listed in that docstring. If one of
        these ever goes red, that is good news and the docstring is now wrong:
        delete the case and delete the corresponding bullet, together.
        """
        def sink_in_a_helper(msg, sink):
            _log_it(msg.payload)

        def taint_through_a_container(msg, sink):
            box = {}
            box["raw"] = msg.payload
            sink.error(f"via a dict: {box['raw']}")

        def taint_through_an_exception(msg, sink):
            try:
                value = int(msg.payload)
            except ValueError as exc:
                sink.exception(f"cannot parse: {exc}")

        def non_literal_format_string(msg, sink, template):
            sink.error(template % msg.payload)

        for shape in (sink_in_a_helper, taint_through_a_container,
                      taint_through_an_exception):
            with self.subTest(shape=shape.__name__):
                forms = [form for _, form, _ in _payload_sinks(shape)]
                self.assertNotIn(
                    "RAW", forms,
                    f"{shape.__name__} is now VISIBLE to the scanner. That is "
                    f"an improvement - remove this case and the matching "
                    f"bullet from _payload_sinks()'s docstring together, so "
                    f"the two cannot disagree about what it covers",
                )

        self.assertEqual(
            ["RAW"],
            [f for _, f, _ in _payload_sinks(non_literal_format_string)],
            "a format string the scanner cannot read must bail to RAW rather "
            "than to silence - guessing is how a false negative gets in",
        )

    def test_every_payload_sink_uses_the_canonical_repr_spelling(self):
        """Separate from the safety test above, and reported separately,
        because these two failures mean different things.

        `{payload!a}` is ascii(): it escapes \\n, \\r and \\x1b exactly as
        repr() does, so a sink written that way is NOT a forgery path and
        saying so would be false. What it is is a second spelling of a
        one-spelling rule, and mqtt.py's own comment states the rule as "!r" -
        so the codebase and the check agree on one form and a reader has one
        thing to look for.
        """
        odd = [(lineno, form, line) for lineno, form, line
               in _payload_sinks(mqtt_mod.on_message)
               if form not in _CANONICAL_FORMS and form != "RAW"]
        self.assertEqual(
            [], odd,
            "this sink escapes the payload safely but not in the form the rule "
            "is written as (!r). Either spell it !r or change the rule in "
            "mqtt.py's comment and in _CANONICAL_FORMS together.",
        )

    def test_an_ordinary_payload_still_reads_naturally(self):
        """repr() of a plain string is the quoted form the line already had, so
        the fix does not change what an operator reads for normal traffic - and
        the sibling suites that match on that shape stay valid.

        The topic is quoted too as of T-527.12. That is a real change to what
        an operator reads, so it is asserted here rather than left for someone
        to discover from a grep that stopped matching."""
        message = self._decode_record(LIGHT_COMMAND, b"ON")
        self.assertEqual(f"Decoded payload on '{LIGHT_COMMAND}': 'ON'", message)

    def test_a_topic_carrying_a_newline_cannot_forge_a_line(self):
        """END TO END, not source-level: the point of escaping the topic is
        what lands in the record, and every other test in this pair reads the
        source instead.

        A topic is where the forgery goes when the payload stops accepting it.
        paho does not validate an inbound topic against the filters it
        subscribed to - it hands over whatever the broker sent - so a
        non-conforming broker delivering `gardyn/light/command\\nFORGED` gets
        that newline into gardyn.log verbatim unless this line escapes it.
        """
        forged_topic = (LIGHT_COMMAND
                        + "\n2026-01-01 00:00:00,000 - mqtt - ERROR - forged")
        # The fixture control, same as its payload sibling above: the topic
        # really does carry a raw newline, so a green result below is the
        # escaping working rather than the input being harmless.
        self.assertIn("\n", forged_topic)

        message = self._decode_record(forged_topic, b"ON")
        self.assertNotIn("\n", message,
                         "a topic put a line break into gardyn.log")
        self.assertIn("\\n", message)

    def test_the_catch_all_handler_reprs_its_exception(self):
        """SOURCE-LEVEL, and that is a limitation rather than a preference.

        The scanner cannot reach the `e` half of this line: `except ... as e`
        is outside _bindings() on purpose, and whether a given exception's
        str() quotes its operand is a runtime property of that exception class
        rather than anything an AST can decide. See the EXCEPTION bullet on
        _payload_sinks(), which names this line.

        IT DOES REACH THE TOPIC HALF, as of T-527.12 - that half is
        machine-checked by test_no_log_line_in_on_message_interpolates_a_
        payload_raw and needs nothing from here. An earlier version of this
        docstring said the scanner could see neither, which stopped being true
        in the commit that widened the seed.

        So this asserts ONE conversion, on `e`, deliberately - matching the
        substring rather than the whole line, so that a change to the topic's
        spelling fails the scanner-driven tests (which describe it correctly)
        instead of failing here under a message about exceptions. That
        misdirection is the defect this repo already fixed once, in
        tests/test_water_interlock.py.

        And the spelling is all it asserts. It does NOT assert that no control
        character can reach gardyn.log from this call - logger.exception() also
        renders exc_info, whose final line is str(e), and nothing at this call
        site formats that.
        """
        import inspect

        source = inspect.getsource(mqtt_mod)
        handler = [line for line in source.splitlines()
                   if 'logger.exception(f"Error handling message on topic'
                   in line]
        # Locating the line is a separate failure from what it contains, and
        # they mean different things: one says the handler moved, the other
        # says it stopped escaping.
        self.assertEqual(
            1, len(handler),
            f"expected exactly one catch-all handler line, got {handler!r}")
        self.assertIn(
            "{e!r}", handler[0],
            "the catch-all handler interpolates its exception without repr(), "
            "so an exception whose message quotes a payload can forge a log "
            "line - see the EXCEPTION bullet on _payload_sinks()",
        )


class TopicIsATaintSeedTests(unittest.TestCase):
    """T-527.12. The scanner's widening from payload-only to payload-and-topic.

    Separate from the shipping-code assertions above because these test the
    RULE, and a rule that has no case which makes it fire is indistinguishable
    from an absent one. Every fixture here is a shape mqtt.py does not contain;
    that is the point - the shipping code is clean, so only a synthetic case
    can prove the widening fires at all.
    """

    def test_a_raw_topic_is_reported(self):
        """The positive control. Before T-527.12 every one of these scanned
        clean, which is exactly what made the gap invisible."""
        def raw_topic_in_an_fstring(msg, sink):
            sink.info(f"arrived on {msg.topic}")

        def raw_topic_through_a_bound_local(msg, sink):
            where = msg.topic
            sink.error(f"arrived on {where}")

        def raw_topic_in_lazy_logging(msg, sink):
            sink.error("arrived on %s", msg.topic)

        def raw_topic_via_a_derived_suffix(msg, sink):
            suffix = msg.topic.replace("gardyn/", "")
            sink.warning(f"suffix {suffix}")

        for shape in (raw_topic_in_an_fstring, raw_topic_through_a_bound_local,
                      raw_topic_in_lazy_logging, raw_topic_via_a_derived_suffix):
            with self.subTest(shape=shape.__name__):
                self.assertEqual(
                    ["RAW"], [f for _, f, _ in _payload_sinks(shape)],
                    f"{shape.__name__} puts a broker-chosen topic into the "
                    f"record unescaped and the scanner did not say so")

    def test_an_escaped_topic_is_not_reported_as_raw(self):
        """The negative control, and the direction that gets a check deleted:
        a rule that accuses correct code is a rule someone removes."""
        def escaped_topic(msg, sink):
            sink.info(f"arrived on {msg.topic!r}")

        def escaped_topic_lazily(msg, sink):
            sink.error("arrived on %r", msg.topic)

        for shape in (escaped_topic, escaped_topic_lazily):
            with self.subTest(shape=shape.__name__):
                forms = [f for _, f, _ in _payload_sinks(shape)]
                self.assertNotIn("RAW", forms)
                self.assertEqual(1, len(forms), forms)

    def test_the_seed_set_is_pinned_per_name(self):
        """_TAINT_SEEDS is the whole rule, and dropping one member is a
        one-word edit. Asserted AS A SET rather than by membership, so an
        addition has to be deliberate too - a seed that is wrong in the
        widening direction accuses correct code."""
        self.assertEqual({"payload", "topic"}, set(_TAINT_SEEDS))

    def test_the_topic_seed_reaches_the_attribute_not_only_the_bare_name(self):
        """A UNIT test of the predicate, and REDUNDANT with the control above -
        said here rather than left for the next reader to discover.

        Its first docstring justified it by claiming the fixtures above would
        stay green under an attribute-only narrowing "on their parameter
        name". False: all four take `(msg, sink)` and reference `msg.topic`,
        so narrowing _is_taint_seed to the Name branch reddens
        test_a_raw_topic_is_reported directly. Review caught it.

        Kept anyway, at one line, because it tests the predicate WITHOUT the
        scanner pipeline in between - so when both go red the pair says which
        layer moved. That is a smaller claim than the one it replaced.
        """
        tree = ast.parse("sink.info(f'on {msg.topic}')")
        self.assertTrue(any(_is_taint_seed(n) for n in ast.walk(tree)))


class AnUndecodableTopicIsDroppedNotFatalTests(unittest.TestCase):
    """T-527.12, from review. A topic whose bytes are not valid UTF-8 used to
    kill the process, and the mechanism is worth stating because nothing about
    it is visible at the call site.

    `msg.topic` is a paho PROPERTY that decodes on every access, and paho does
    not validate the bytes. on_message read it inside a `try` AND again inside
    that try's own `except UnicodeDecodeError` handler - so the handler raised
    the exception it was catching, out of on_message, out of loop_forever(),
    out of the process. `Restart=always` with `RestartSec=10` then gives a
    permanent ten-second restart loop with the grow light off, on a host with
    no physical recovery path.

    ~~Reachable on any unit whose durable session still holds the pre-9e00c2f
    `gardyn/#` wildcard.~~ WRONG, struck rather than deleted: 9e00c2f is the
    commit that INTRODUCED the durable session, in the same change that
    removed the wildcard, so no unit ever had both.

    So this is DEFENCE IN DEPTH, not the closure of a live path - the same
    distinction the `{e!r}` comment in mqtt.py had to be corrected to make.
    Under a conforming broker this client only ever receives the ten LOCALLY
    DERIVED topics its session has subscribed to. ~~"ten literal ASCII
    topics"~~ - struck 2026-08-12 for the same reason the equivalent sentence
    in mqtt.py was: nine of the ten are `BASE_TOPIC + "<ascii suffix>"` and
    BASE_TOPIC comes from a gitignored `.env`, so neither "literal" nor
    "ASCII" is a property this repo can assert about them. Locally derived is,
    and is the one the argument rests on. It is still worth having:
    paho hands over whatever arrived without validating it, the failure mode
    is the process exiting on a host with no recovery path, and the guard
    costs four lines.

    Pre-existing; found reviewing the escaping change to those same two lines.
    """

    class _PahoShapedMessage:
        """Decodes LAZILY, in the property, exactly as paho 2.0.0 does.

        A fixture holding an already-decoded string could not exhibit this at
        all - the whole defect is that the access is what raises, and that it
        raises every time.
        """

        def __init__(self, raw_topic, payload):
            self._topic, self.payload = raw_topic, payload
            self.qos, self.retain = 1, False

        @property
        def topic(self):
            return self._topic.decode("utf-8")

    def setUp(self):
        self.client = MagicMock()

    def test_a_conforming_topic_reaches_the_decode_line(self):
        """POSITIVE CONTROL for the two tests below, which assert an absence.

        Without it, a fixture that on_message rejects outright would make both
        of them pass while measuring nothing.

        NAMED FOR WHERE IT ACTUALLY REACHES, after review. It was
        `..._still_reaches_a_handler`, which is more than it shows: the decode
        line is emitted BEFORE dispatch, and under this suite's stubs
        `light_scheduler is None`, so the light command is refused and
        `client.publish` is never called. As a control against "the fixture is
        rejected at the door" it is sound; it cannot catch a fixture that bails
        after the decode line, and the name should not imply otherwise.
        """
        msg = self._PahoShapedMessage(LIGHT_COMMAND.encode(), b"ON")
        with self.assertLogs(mqtt_mod.logger, level="INFO") as captured:
            mqtt_mod.on_message(self.client, None, msg)
        self.assertTrue(
            any(r.getMessage().startswith("Decoded payload on ")
                for r in captured.records),
            [r.getMessage() for r in captured.records])

    def test_an_undecodable_topic_does_not_escape_on_message(self):
        msg = self._PahoShapedMessage(b"gardyn/light/\xff", b"ON")
        # The fixture control: the bytes really are undecodable, so a green
        # result is the guard working rather than the input being harmless.
        with self.assertRaises(UnicodeDecodeError):
            msg.topic

        try:
            mqtt_mod.on_message(self.client, None, msg)
        except UnicodeDecodeError as exc:
            self.fail(
                f"on_message let a UnicodeDecodeError escape ({exc}). paho "
                f"does not catch it, so it leaves loop_forever() and the "
                f"process exits into a ten-second Restart=always loop with "
                f"the light off")

    def test_a_topic_property_raising_AttributeError_does_not_escape_either(self):
        """The second arm of the guard, which shipped UNPINNED - the battery
        caught it surviving, and the reviewer who asked for the arm predicted
        it would.

        paho's property is `self._topic.decode("utf-8")`, so a `_topic` holding
        anything without `.decode` raises AttributeError there instead. Same
        process-exit route, one exception class over, and a guard presented as
        closing a class has to close it.

        Lower reachability than the UnicodeDecodeError arm, and deliberately
        not described as equal: it needs paho to hand over a non-bytes
        `_topic`, which nothing in the library does today.
        """
        class _BadTopicMessage:
            payload, qos, retain = b"ON", 1, False
            _topic = None

            @property
            def topic(self):
                return self._topic.decode("utf-8")

        msg = _BadTopicMessage()
        # Fixture control: the property really does raise, so a green result
        # below is the guard working rather than the input being harmless.
        with self.assertRaises(AttributeError):
            msg.topic

        try:
            mqtt_mod.on_message(self.client, None, msg)
        except AttributeError as exc:
            self.fail(
                f"on_message let an AttributeError escape ({exc}). paho does "
                f"not catch it, so it leaves loop_forever() and the process "
                f"exits into a ten-second Restart=always loop with the light "
                f"off - the same outcome as the UnicodeDecodeError arm")

    def test_the_drop_is_recorded_with_the_raw_bytes_escaped(self):
        """Dropping it silently would be its own defect: this is the one
        record that a hostile or misconfigured broker reached this device.

        AND IT IS THE ONLY THING COVERING THIS SINK. The scanner does not see
        this line - `_topic` reaches it as a string literal inside `getattr`,
        which is not a taint seed - so neither the raw-interpolation rule nor
        the canonical-`!r` rule reaches it, and every other sink in
        `on_message` is machine-checked. Hence all three control characters
        here rather than the newline alone: for this line the fixture IS the
        rule. Found by review 2026-08-12.
        """
        msg = self._PahoShapedMessage(
            b"gardyn/x\xff\nFORGED\rCR\x1b[2Jcleared", b"ON")
        with self.assertLogs(mqtt_mod.logger, level="ERROR") as captured:
            mqtt_mod.on_message(self.client, None, msg)
        messages = [r.getMessage() for r in captured.records]
        self.assertEqual(1, len(messages), messages)
        for raw, escaped in (("\r", "\\r"), ("\x1b", "\\x1b")):
            self.assertNotIn(raw, messages[0])
            self.assertIn(escaped, messages[0])
        self.assertNotIn("\n", messages[0],
                         "the dropped topic's raw bytes put a line break into "
                         "gardyn.log, which is the forgery this ticket closes")
        self.assertIn("\\n", messages[0])

    def test_a_payload_with_no_decode_does_not_escape_on_message_either(self):
        """T-527.29. The SIBLING of the topic guard, one statement below it,
        which kept `except UnicodeDecodeError` alone when the topic guard was
        widened.

        `payload = msg.payload.decode("utf-8")` takes the identical route out
        of the process when `.decode` is missing, and the asymmetry was the
        whole finding: the topic arm was added for a shape "nothing in the
        library does today", and this one was left for a shape with exactly
        the same reachability.

        Three shapes, because they fail at different places and a single case
        would not distinguish them: a None payload, a payload that is already
        a str (the shape a well-meaning double produces), and a message with
        no `payload` attribute at all - which is also the case that would make
        a naive handler re-raise while trying to report itself.
        """
        for label, msg in (
            ("payload=None",
             self._PahoShapedMessage(LIGHT_COMMAND.encode(), None)),
            ("payload is already str",
             self._PahoShapedMessage(LIGHT_COMMAND.encode(), "ON")),
        ):
            with self.subTest(shape=label):
                # Fixture control: the payload really has no `.decode`, so a
                # green result is the guard working rather than the input
                # being harmless.
                with self.assertRaises(AttributeError):
                    msg.payload.decode("utf-8")
                try:
                    with self.assertLogs(mqtt_mod.logger, level="ERROR"):
                        mqtt_mod.on_message(self.client, None, msg)
                except AttributeError as exc:
                    self.fail(
                        f"on_message let an AttributeError escape for "
                        f"{label} ({exc}). paho does not catch it, so it "
                        f"leaves loop_forever() and the process exits into a "
                        f"ten-second Restart=always loop with the light off")

        # The third shape gets its own block: `msg` has no `payload` at all,
        # so the HANDLER must not read it either. This is the case that would
        # reproduce T-527.12's process-killer inside the fix for it.
        no_payload = self._PahoShapedMessage(LIGHT_COMMAND.encode(), b"ON")
        del no_payload.payload
        with self.subTest(shape="no payload attribute"):
            # Fixture control, added 2026-08-12. The two loop shapes above
            # carried one and this block did not, while the commit message and
            # the docstring said all three did - a false coverage claim of the
            # cheapest kind, found by the review of bfef37f. It did not matter
            # in fact (a normal payload logs at INFO, so `assertLogs(ERROR)`
            # would raise rather than pass vacuously), which is exactly why it
            # was worth closing rather than re-wording: the claim now costs one
            # line to make true.
            with self.assertRaises(AttributeError):
                no_payload.payload
            try:
                with self.assertLogs(mqtt_mod.logger, level="ERROR") as caught:
                    mqtt_mod.on_message(self.client, None, no_payload)
            except AttributeError as exc:
                self.fail(
                    f"the handler re-read the attribute that raised, so the "
                    f"guard itself exits the process ({exc}) - the exact "
                    f"shape T-527.12 found at the topic guard")
            self.assertTrue(
                any("PAYLOAD has no usable bytes" in r.getMessage()
                    for r in caught.records),
                [r.getMessage() for r in caught.records])

    def test_an_UNEXPECTED_fault_in_either_decode_is_caught_and_named(self):
        """T-527.30 remediation. The two named arms at each guard closed two
        exception classes and the comments called that closing a class.

        Both reviews landed on the same sentence from opposite sides. The
        bfef37f review named it as an over-claim (F7): a `payload` property
        raising `TypeError`, or `MemoryError` out of `bytes.decode`, took the
        same route out of `on_message`, out of `loop_forever()` and out of the
        process as the two classes that ARE named - a ten-second
        Restart=always loop with the grow light off on a host with no physical
        recovery path. And the stated reason for refusing a blanket catch
        ("this runs before dispatch and has no business swallowing handler
        faults") was false: each `try` covers one or two statements and no
        dispatch code is reachable from inside either.

        So the class is now actually closed, at BOTH guards, with the named
        arms kept in front so their diagnosis survives. This test drives the
        arm that was added; the one below asserts the diagnoses stay apart,
        which is the property that makes keeping three arms rather than one
        worth the lines.
        """
        class _Exploding:
            def decode(self, *_a, **_k):
                raise ValueError("not a decode failure this module models")

        payload_side = self._PahoShapedMessage(LIGHT_COMMAND.encode(),
                                               _Exploding())
        # Fixture control: the fault has to be one NEITHER named arm catches,
        # or this measures the arms that were already there.
        with self.assertRaises(ValueError):
            payload_side.payload.decode("utf-8")
        with self.subTest(guard="payload"):
            try:
                with self.assertLogs(mqtt_mod.logger, level="ERROR") as caught:
                    mqtt_mod.on_message(self.client, None, payload_side)
            except ValueError as exc:
                self.fail(
                    f"on_message let a ValueError out of the payload decode "
                    f"({exc}); paho does not catch it, so the process exits")
            messages = [r.getMessage() for r in caught.records]
            self.assertTrue(any("UNEXPECTED" in m for m in messages), messages)

        class _ExplodingTopic:
            _topic = b"unused"
            payload = b"ON"
            qos, retain = 1, False

            @property
            def topic(self):
                raise ValueError("not a topic decode failure either")

        topic_side = _ExplodingTopic()
        with self.assertRaises(ValueError):
            topic_side.topic
        with self.subTest(guard="topic"):
            try:
                with self.assertLogs(mqtt_mod.logger, level="ERROR") as caught:
                    mqtt_mod.on_message(self.client, None, topic_side)
            except ValueError as exc:
                self.fail(
                    f"on_message let a ValueError out of the topic read "
                    f"({exc}); this is the T-527.12 process-exit route with a "
                    f"different exception class")
            messages = [r.getMessage() for r in caught.records]
            self.assertTrue(any("UNEXPECTED" in m for m in messages), messages)
            self.assertTrue(any("TOPIC" in m for m in messages), messages)

    def test_a_dropped_payload_does_not_reach_a_dispatch_branch(self):
        """The `return` in each drop arm, which nothing pinned.

        F4 of the bfef37f review, and the one finding there with a real blast
        radius. The battery carried three mutants for the new arm - delete,
        merge, re-read - and deleting its `return` SURVIVED all of them at
        zero named failing cases. Lose that statement and control falls
        through to the dispatch chain with `payload` UNBOUND, and three of the
        branches never read it, so they run to completion: the HA/status
        collision guard, `water/level/get`, and `pcb/temperature/get`. On the
        Pi that is a distance measurement or a sensor publish executing one
        statement after a log line saying the message was DROPPED. The other
        six raise UnboundLocalError into the catch-all - noisy and harmless,
        which is why nothing noticed.

        `pcb/temperature/get` is the cheapest of the three to observe: with
        the return in place the drop is logged and nothing is published; with
        it gone the branch publishes a temperature. The positive control below
        is what makes the absence mean something.
        """
        topic = (mqtt_mod.BASE_TOPIC + "/pcb/temperature/get").encode()

        # POSITIVE CONTROL. A branch that never publishes under ANY input
        # would make the assertion below pass while measuring nothing.
        healthy = self._PahoShapedMessage(topic, b"")
        mqtt_mod.on_message(self.client, None, healthy)
        published = [c.args[0] for c in self.client.publish.call_args_list]
        self.assertIn(
            mqtt_mod.BASE_TOPIC + "/pcb/temperature", published,
            "CONTROL FAILED: this branch does not publish even on a good "
            "payload, so the drop assertion below proves nothing")

        for label, payload in (("no .decode", None),
                               ("already a str", "20.0")):
            with self.subTest(shape=label):
                self.client.reset_mock()
                with self.assertLogs(mqtt_mod.logger, level="ERROR"):
                    mqtt_mod.on_message(
                        self.client, None,
                        self._PahoShapedMessage(topic, payload))
                self.assertEqual(
                    [], self.client.publish.call_args_list,
                    f"a message logged as DROPPED went on to run a dispatch "
                    f"branch and publish - the drop arm's `return` is gone, "
                    f"and `payload` was unbound for the whole chain")

    def test_the_topic_guards_two_arms_report_two_DIFFERENT_causes(self):
        """The topic-side pair of the payload test below, and the one the
        catch-all made necessary.

        Measured consequence of adding that arm, caught by re-running the
        battery rather than by reading the diff: mutant 27 ("narrow the guard
        back to UnicodeDecodeError") had been dying because a non-bytes
        `_topic` then escaped on_message and exited the process. With a
        catch-all present it no longer escapes - it is caught one arm lower
        and logged as UNEXPECTED - so the mutant SURVIVED at zero named cases
        and the narrowing became invisible. The property that remains, and
        that this pins, is the DIAGNOSIS: both named causes belong to the
        named arm and say "could not be decoded", because on this host that is
        the difference between an incident record pointing at the broker and
        one pointing nowhere.

        This is the shape the rules call widening what can SET a signal: the
        production change was right and it silently retired a test's only
        observable.
        """
        cases = {
            # UnicodeDecodeError - bytes that are not UTF-8.
            "undecodable topic bytes":
                (self._PahoShapedMessage(b"gardyn/light/\xff", b"ON"),
                 "could not be decoded"),
            # AttributeError - `_topic` holding something with no `.decode`,
            # which is the class the guard was widened for in 4601f55.
            "topic attribute is not bytes":
                (self._PahoShapedMessage(b"unused", b"ON"),
                 "could not be decoded"),
        }
        cases["topic attribute is not bytes"][0]._topic = "already a str"

        for label, (msg, expected) in cases.items():
            with self.subTest(cause=label):
                with self.assertLogs(mqtt_mod.logger, level="ERROR") as caught:
                    mqtt_mod.on_message(self.client, None, msg)
                messages = [r.getMessage() for r in caught.records]
                self.assertTrue(any(expected in m for m in messages), messages)
                self.assertFalse(
                    any("UNEXPECTED" in m for m in messages),
                    f"the {label!r} case fell through to the catch-all, so the "
                    f"named arm no longer covers it and the log no longer says "
                    f"what happened: {messages}")

    def test_the_three_payload_arms_report_three_DIFFERENT_causes(self):
        """What stops the catch-all being written as one `except Exception`.

        `gardyn.log` is the only thing an incident on this host is
        reconstructed from, and the three causes send a reader to three
        different places: undecodable bytes to the broker or a device, a
        missing `.decode` to the library having moved, and anything else to a
        fault this module has no model for. Merging any pair still CATCHES the
        fault, so a test asserting only "nothing escaped" cannot see it -
        which is why the mutants for the merges exist.
        """
        class _Exploding:
            def decode(self, *_a, **_k):
                raise ValueError("neither of the two named causes")

        cases = {
            "undecodable bytes": (b"\xff\xfe", "Likely binary"),
            "no .decode at all": (None, "PAYLOAD has no usable bytes"),
            "something else entirely": (_Exploding(), "UNEXPECTED"),
        }
        seen = {}
        for label, (payload, expected) in cases.items():
            with self.subTest(cause=label):
                msg = self._PahoShapedMessage(LIGHT_COMMAND.encode(), payload)
                with self.assertLogs(mqtt_mod.logger, level="ERROR") as caught:
                    mqtt_mod.on_message(self.client, None, msg)
                messages = [r.getMessage() for r in caught.records]
                self.assertTrue(any(expected in m for m in messages), messages)
                seen[label] = messages
        # And the other two markers must be ABSENT from each, or "different
        # cause" is satisfied by a line that says all three things at once.
        for label, messages in seen.items():
            others = [e for lbl, (_p, e) in cases.items()
                      if lbl != label for e in (e,)]
            for other in others:
                self.assertFalse(
                    any(other in m for m in messages),
                    f"the {label!r} arm also reported {other!r}, so the three "
                    f"arms do not tell a reader three different things: "
                    f"{messages}")

    def test_the_payload_TYPE_name_is_escaped_like_every_other_sink(self):
        """The operand the payload-sink scanner structurally cannot see.

        F5 of the bfef37f review, and it corrects a MECHANISM rather than an
        outcome. That commit said the scanner caught its first `%r` spelling
        of this log line because the line interpolates a payload-derived
        operand. It does not: `_TAINT_SEEDS` matches a Name or Attribute
        called `payload`/`topic`, and `getattr(msg, "payload", None)` passes
        "payload" as a STRING CONSTANT, which seeds nothing. Measured -
        `_tainted_names(on_message)` returns {'payload','topic','topic_suffix'}
        with no `payload_type` in it, and un-escaping `{payload_type!r}`
        SURVIVES the battery while un-escaping `{topic!r}` on the same
        physical line is killed. The line is scanned only because of `topic`.

        So the escaping of `payload_type` has no static reader, and this is
        its behavioural one. Not a live forgery path today - the value is
        `type(...).__name__` - but "not reachable through today's library" is
        the exact argument this ticket has spent four rounds declining to
        accept as safety, and the topic guard's equivalent invisible operand
        at least has a control-character fixture. This is that fixture.
        """
        exotic = type("Broken\nAug 12 03:00:00 mqtt - INFO - forged\x1b[31m",
                      (), {})
        msg = self._PahoShapedMessage(LIGHT_COMMAND.encode(), exotic())
        # Control: the name really does carry the characters, so a green
        # result is the escaping working rather than the fixture being tame.
        self.assertIn("\n", type(msg.payload).__name__)
        with self.assertLogs(mqtt_mod.logger, level="ERROR") as caught:
            mqtt_mod.on_message(self.client, None, msg)
        messages = [r.getMessage() for r in caught.records]
        self.assertEqual(
            [], [m for m in messages if "\n" in m or "\x1b" in m],
            f"a payload's TYPE NAME wrote a raw newline or escape into "
            f"gardyn.log, which forges a log line exactly as a raw payload "
            f"would: {messages!r}")
        self.assertTrue(any("\\n" in m for m in messages), messages)

    def test_the_payload_drop_is_reported_as_a_LIBRARY_fault_not_a_broker_one(self):
        """The two arms are kept separate on purpose, and this is what makes
        that separation checkable.

        An incident on this host is reconstructed from `gardyn.log` and
        nothing else, so a drop that says "likely binary" sends the reader to
        the broker and a device, while a payload with no `.decode` means the
        library moved underneath us. Merging the arms into
        `except (UnicodeDecodeError, AttributeError)` would still catch the
        fault and would silently give it the wrong cause - the wrong-cause
        notification being a defect this ticket has now hit seven times.
        """
        binary = self._PahoShapedMessage(LIGHT_COMMAND.encode(), b"\xff\xfe")
        with self.assertLogs(mqtt_mod.logger, level="ERROR") as captured:
            mqtt_mod.on_message(self.client, None, binary)
        self.assertTrue(
            any("Likely binary" in r.getMessage() for r in captured.records),
            [r.getMessage() for r in captured.records])

        broken = self._PahoShapedMessage(LIGHT_COMMAND.encode(), None)
        with self.assertLogs(mqtt_mod.logger, level="ERROR") as captured:
            mqtt_mod.on_message(self.client, None, broken)
        messages = [r.getMessage() for r in captured.records]
        self.assertTrue(
            any("PAYLOAD has no usable bytes" in m for m in messages), messages)
        self.assertFalse(
            any("Likely binary" in m for m in messages),
            f"a library shape change was reported as a binary payload, which "
            f"points the reader at the broker: {messages}")

    def test_on_message_survives_PAHOS_OWN_DISPATCH_not_just_a_direct_call(self):
        """The frame every other test in this class skips, and the reason a
        wrong comment about this guard survived two review rounds.

        Everything else here calls `mqtt_mod.on_message(...)` directly with
        `_PahoShapedMessage`, a double whose `topic` is a property that raises.
        A real `MQTTMessage` has `__slots__` and no such property, and - the
        part that matters - **paho reads `message.topic` ITSELF before calling
        the callback**, guarded against `UnicodeDecodeError` alone. So the
        double supplies exactly the shape the guard reads and skips the frame
        where a non-UnicodeDecodeError fault actually lands. Written from the
        same belief as the code, which is why every mutant died honestly while
        `mqtt.py` claimed to close a route it cannot reach.

        This drives the real `Client._handle_on_message`, which is the exact
        call `_handle_publish` makes on an inbound QoS-1 command.

        SKIPPED where paho is absent rather than failed - it is not installed
        on the development machine and a failure there would be a false
        finding about the environment. That makes this the second test whose
        evidence depends on the import helper telling absent from broken.
        """
        real_paho = _import_real_paho_client()
        if real_paho is None:
            self.skipTest("paho is not installed here; nothing to dispatch through")

        client = real_paho.Client(real_paho.CallbackAPIVersion.VERSION2)
        client.on_message = lambda _c, _u, msg: mqtt_mod.on_message(
            self.client, _u, msg)

        def drive(mutate):
            msg = real_paho.MQTTMessage(
                topic=(mqtt_mod.BASE_TOPIC + "/light/command").encode())
            msg.payload = b"ON"
            mutate(msg)
            client._handle_on_message(msg)

        # CONTROL. If a healthy message does not survive this path, every
        # absence below is meaningless.
        drive(lambda m: None)

        # THE ONE THAT MATTERS, and the only topic fault this guard can
        # actually receive: paho catches UnicodeDecodeError itself, hands the
        # message over, and mqtt.py re-reads `msg.topic` - which is the
        # T-527.12 process-killer, and it is closed.
        drive(lambda m: setattr(m, "_topic", b"gardyn/light/\xff"))

        # Payload faults DO reach us, because paho never touches `payload`
        # before the callback.
        for payload in (b"\xff\xfe", None, "already a str"):
            with self.subTest(payload=type(payload).__name__):
                drive(lambda m, p=payload: setattr(m, "payload", p))

        # And the honest negative: a non-bytes `_topic` raises INSIDE paho and
        # never reaches on_message at all. Asserted so the docstring in
        # mqtt.py stays checkable rather than being a claim about a library.
        with self.assertRaises(AttributeError):
            drive(lambda m: setattr(m, "_topic", "already a str"))

    def test_the_private_attribute_the_guard_reads_is_pinned_against_paho(self):
        """`getattr(msg, "_topic", None)` degrades SILENTLY, and this suite is
        structurally unable to notice - the fixture above sets `_topic`
        because the production code reads it, so both sides agree by
        construction. That is the shared-blind-spot case: a double written
        from the same belief as the code, where every mutant dies honestly.

        `_topic` is paho-private, so it can be renamed in a minor release with
        nothing in this repo objecting. The drop would then log
        `... could not be decoded: None`, which is exactly the incident record
        the comment at the guard calls "the only useful thing" - and the guard
        would still work, so nothing else would go red either.

        SKIPPED rather than FAILED where paho is absent (it is not installed on
        the development machine, and the whole suite stubs it out), because a
        failure there would be a false finding about the environment rather
        than about the code. A skip that is counted is the honest state; see
        the same reasoning in tests/test_suite_isolation.py.
        """
        real_paho = _import_real_paho_client()
        if real_paho is None:
            self.skipTest("paho is not installed here; nothing to pin against")
        message = real_paho.MQTTMessage(topic=b"gardyn/light/command")
        self.assertTrue(
            hasattr(message, "_topic"),
            "paho's MQTTMessage no longer carries `_topic`, so mqtt.py's "
            "guard now logs None instead of the bytes that arrived - see "
            "AnUndecodableTopicIsDroppedNotFatalTests")
        self.assertIsInstance(
            message._topic, bytes,
            "`_topic` is no longer bytes, so %r of it is not the escaped "
            "byte record the guard's comment promises")


class TheImportHelperTellsAbsentApartFromBroken(unittest.TestCase):
    """`_import_real_paho_client` returning None means "paho is not installed",
    and its only caller turns that straight into
    `skipTest("paho is not installed here")`.

    So a None for a paho that IS installed makes this suite state something
    false about the machine, in precisely the case where a human needed to
    know - and it does it by SKIPPING, which reads as green. That is the
    dead-instrument-reports-an-absence shape the rest of this file exists to
    guard against, sitting in the file's own helper.

    The class is written as a pair on purpose. The broken case alone proves
    nothing: an exception could be coming from the fixture rather than from
    the helper. The healthy case is the control that says the fixture can
    produce a real, importable paho, so the difference between the two rows is
    the helper's own behaviour and nothing else.
    """

    @staticmethod
    def _paho_on_disk(client_body, package_init=""):
        """A real `paho.mqtt.client` package on sys.path, with `client_body`
        as client.py and `package_init` as paho/__init__.py.

        `package_init` exists because the parent's body is what `find_spec`
        executes, and that is where the 094eac0 review found the helper
        misclassifying a broken install as an absent one. A default of "" is
        the healthy case every other user of this fixture wants.

        Every cached `paho*` module comes out of sys.modules for the duration
        and goes back afterwards: this suite plants a MagicMock at
        `paho.mqtt.client` for the length of mqtt.py's import, and
        `find_spec` answers from sys.modules before it ever reaches the disk.
        Without the eviction this fixture would measure the mock.
        """
        import contextlib
        import os
        import shutil
        import sys
        import tempfile

        @contextlib.contextmanager
        def _cm():
            saved_modules = {k: v for k, v in sys.modules.items()
                             if k == "paho" or k.startswith("paho.")}
            saved_path = list(sys.path)
            tmp = tempfile.mkdtemp(prefix="t527-paho-")
            try:
                pkg = os.path.join(tmp, "paho", "mqtt")
                os.makedirs(pkg)
                with open(os.path.join(tmp, "paho", "__init__.py"), "w") as h:
                    h.write(package_init)
                with open(os.path.join(pkg, "__init__.py"), "w"):
                    pass
                with open(os.path.join(pkg, "client.py"), "w") as handle:
                    handle.write(client_body)
                for name in saved_modules:
                    del sys.modules[name]
                sys.path.insert(0, tmp)
                yield tmp
            finally:
                sys.path[:] = saved_path
                for name in [k for k in list(sys.modules)
                             if k == "paho" or k.startswith("paho.")]:
                    del sys.modules[name]
                sys.modules.update(saved_modules)
                shutil.rmtree(tmp, ignore_errors=True)

        return _cm()

    def test_a_paho_that_imports_cleanly_is_returned(self):
        """CONTROL. Without this the test below cannot tell "the helper
        raised" from "the fixture cannot build an importable package at
        all"."""
        with self._paho_on_disk("MARKER = 'this is the fixture paho'\n"):
            module = _import_real_paho_client()
        self.assertIsNotNone(
            module, "the fixture failed to produce an importable paho, so the "
                    "broken-paho case below proves nothing")
        self.assertEqual(module.MARKER, "this is the fixture paho")

    def test_a_paho_that_is_present_and_fails_to_import_RAISES(self):
        """The finding. This returned None before the fix, and the caller then
        skipped with "paho is not installed here" - which was false."""
        body = "raise RuntimeError('this paho is installed and broken')\n"
        with self._paho_on_disk(body) as tmp:
            with self.assertRaises(PahoIsInstalledButUnusable) as caught:
                _import_real_paho_client()
        # The message has to carry the path, or the reader cannot tell WHICH
        # paho broke - the point of the distinction is actionability.
        self.assertIn(tmp, str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    def test_a_paho_whose_own_PARENT_fails_to_import_RAISES_too(self):
        """The branch the fix for the branch missed.

        `find_spec("paho.mqtt.client")` IMPORTS `paho` and `paho.mqtt` before
        it looks for `client`, so paho's `__init__` runs one guard EARLIER
        than the `exec_module` arm the 094eac0 commit hardened. Every failure
        below therefore reached the blanket `except (ImportError, ValueError,
        AttributeError): return None` and was reported to the caller as "paho
        is not installed here" - the identical false absence, one branch up,
        inside the fix for it. Found by the review of that commit; the three
        shapes were then measured rather than reasoned about, because they do
        NOT all raise the same class.
        """
        cases = {
            # ModuleNotFoundError, but naming a module that is not paho.
            "__init__ imports an absent module":
                "import definitely_absent_xyz\n",
            # Plain ImportError - NOT a ModuleNotFoundError, so a fix that
            # only special-cases MNFE still reports this one as an absence.
            "__init__ from-imports a missing name":
                "from os import definitely_absent_name\n",
            # Not an ImportError at all. This one was not caught by the old
            # handler either, and surfaced as a raw traceback naming nothing.
            "__init__ raises something else":
                "raise RuntimeError('this paho is installed and broken')\n",
        }
        for label, init_body in cases.items():
            with self.subTest(shape=label):
                with self._paho_on_disk("X = 1\n", package_init=init_body):
                    with self.assertRaises(PahoIsInstalledButUnusable) as caught:
                        _import_real_paho_client()
                # The words matter as much as the class: this string is what
                # stops a reader concluding the machine has no paho.
                self.assertIn("NOT the same as paho being absent",
                              str(caught.exception))

    @staticmethod
    def _tree_on_disk(files):
        """An arbitrary package LAYOUT on sys.path, for the shapes
        `_paho_on_disk` cannot express.

        That fixture always builds a well-formed `paho/mqtt/client.py` and
        varies only the file BODIES, which is why the malformed-layout states
        below went unmeasured until the review of 2a8c951 planted them.
        """
        import contextlib
        import os
        import shutil
        import sys
        import tempfile

        @contextlib.contextmanager
        def _cm():
            saved_modules = {k: v for k, v in sys.modules.items()
                             if k == "paho" or k.startswith("paho.")}
            saved_path = list(sys.path)
            tmp = tempfile.mkdtemp(prefix="t527-paho-tree-")
            try:
                for rel, body in files.items():
                    path = os.path.join(tmp, rel)
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w") as handle:
                        handle.write(body)
                for name in saved_modules:
                    del sys.modules[name]
                sys.path.insert(0, tmp)
                yield tmp
            finally:
                sys.path[:] = saved_path
                for name in [k for k in list(sys.modules)
                             if k == "paho" or k.startswith("paho.")]:
                    del sys.modules[name]
                sys.modules.update(saved_modules)
                shutil.rmtree(tmp, ignore_errors=True)

        return _cm()

    def test_a_MALFORMED_paho_layout_is_broken_rather_than_absent(self):
        """The three states the FIRST fix for this still called an absence.

        That fix classified on `exc.name`: a missing name that was paho or
        under it meant absence. The review of 2a8c951 measured why that cannot
        work - CPython folds a missing `__path__` into
        `ModuleNotFoundError(name=<the fullname you asked for>)`, so all three
        shapes below raise with a name starting "paho" while paho is plainly
        installed. Reading the name could not separate them from a real
        absence, and the comment claiming `AttributeError` reached the handler
        was wrong twice over: it never arrives, and the state it described was
        being answered as an absence.

        The current split asks the two questions separately instead, which is
        why these are now correct without any name arithmetic.
        """
        ok = {"paho/__init__.py": "", "paho/mqtt/__init__.py": "",
              "paho/mqtt/client.py": "MARKER = 1\n"}
        cases = {
            "paho.mqtt is a MODULE, not a package":
                {"paho/__init__.py": "", "paho/mqtt.py": "X = 1\n"},
            "`paho` itself is a MODULE, not a package":
                {"paho.py": "X = 1\n"},
            "__init__ imports a missing paho.* submodule":
                dict(ok, **{"paho/__init__.py": "import paho.nope\n"}),
        }
        for label, files in cases.items():
            with self.subTest(shape=label):
                with self._tree_on_disk(files):
                    with self.assertRaises(PahoIsInstalledButUnusable):
                        _import_real_paho_client()

    def test_the_absence_question_is_answered_without_EXECUTING_anything(self):
        """CONTROL for the design, not for a case.

        The whole split rests on `find_spec("paho")` being able to say "paho is
        there" without running paho's `__init__` - if it executed, a broken
        `__init__` would make the absence question unanswerable and the
        classifier would be back to guessing. There is no parent to import for
        a top-level name, so it locates only. Asserted directly, because it is
        a property of CPython rather than of this repo and a future version
        changing it would silently re-break the classification.
        """
        import importlib.util
        exploding = {"paho/__init__.py": "raise RuntimeError('boom')\n",
                     "paho/mqtt/__init__.py": "",
                     "paho/mqtt/client.py": "MARKER = 1\n"}
        with self._tree_on_disk(exploding):
            spec = importlib.util.find_spec("paho")
            self.assertIsNotNone(
                spec, "find_spec could not locate a paho whose __init__ "
                      "raises, so the absence question is being answered by "
                      "execution and the split above is unsound")
            # ...and the helper must still call this state BROKEN, not absent.
            with self.assertRaises(PahoIsInstalledButUnusable):
                _import_real_paho_client()

    def test_the_skip_branch_still_reports_a_genuine_absence_as_None(self):
        """The other half. Widening the raise until it swallows the absent
        case would be the same defect pointing the other way: the development
        machine has no paho, and a FAIL there is a false finding about the
        environment rather than about mqtt.py."""
        import sys

        # A package with no `client` submodule at all - present parent,
        # absent target, which is the shape of a genuine absence.
        with self._paho_on_disk("") as tmp:
            import os
            os.remove(os.path.join(tmp, "paho", "mqtt", "client.py"))
            for name in [k for k in list(sys.modules)
                         if k == "paho" or k.startswith("paho.")]:
                del sys.modules[name]
            self.assertIsNone(_import_real_paho_client())


class PahoIsInstalledButUnusable(Exception):
    """paho is on disk and did not import.

    A distinct state from "paho is absent", and deliberately an exception
    rather than a return value: the caller's only two options for a None are
    to skip or to fail, and both are wrong here. Skipping says the environment
    lacks paho, which is false; failing says mqtt.py is broken, which is also
    false. Raising says what is true - the instrument cannot run - and stops
    the suite reporting a green it has not earned.
    """


def _import_real_paho_client():
    """The REAL paho, not this suite's stub - or None if it is not installed.

    sys.modules holds a MagicMock for `paho.mqtt.client` for the length of
    mqtt.py's import, and a MagicMock satisfies `hasattr` for every name, so
    reading it here would make the assertions above true for free. This goes
    to the filesystem instead and refuses to answer from anything mocked.
    """
    import importlib.util

    # ASK "IS PAHO THERE" AND "DOES IT RUN" AS TWO SEPARATE QUESTIONS, because
    # only the first one can honestly answer ABSENT and it is the cheaper of
    # the two to get right.
    #
    # THE SHAPE THIS REPLACES, and why classifying by `exc.name` was a trap.
    # Until 2026-08-12 there was one blanket `except (ImportError, ValueError,
    # AttributeError): return None` here, and everything that went wrong in
    # paho's own `__init__` came back as "paho is not installed" - `find_spec`
    # IMPORTS the parent packages, so paho's `__init__` runs at this call, one
    # branch ABOVE the `exec_module` guard that was hardened for exactly this
    # (094eac0 review). The first fix for that read `exc.name` and asked
    # whether the missing name was paho or under it. The review of THAT fix
    # measured three states it still calls an absence, because CPython folds a
    # missing `__path__` into `ModuleNotFoundError(name=<the fullname you
    # asked for>)`:
    #
    #   paho.mqtt is a MODULE not a package   name='paho.mqtt.client'
    #   `paho` itself is a MODULE             name='paho.mqtt'
    #   __init__ imports a missing paho.*     name='paho.nope'
    #
    # All three start with "paho", all three mean paho IS installed, and the
    # name field cannot tell them from a genuine absence. So stop reading it.
    #
    # `find_spec("paho")` LOCATES the top-level package without EXECUTING
    # anything - there is no parent to import for a top-level name - so it
    # answers the absence question and nothing else can confound it. After it
    # succeeds, paho is on sys.path and every subsequent failure is an
    # installation that cannot run. Verified across nine planted states, with
    # absent and healthy as the two controls: 9/9 classified correctly, where
    # the `exc.name` version got three wrong.
    try:
        top_level = importlib.util.find_spec("paho")
    except Exception:
        # Nothing at all named `paho` that the import system will even look at.
        top_level = None
    if top_level is None:
        return None

    try:
        spec = importlib.util.find_spec("paho.mqtt.client")
    except Exception as exc:
        raise PahoIsInstalledButUnusable(
            f"paho is on sys.path and locating paho.mqtt.client failed, so "
            f"this suite cannot pin anything against it. This is NOT the same "
            f"as paho being absent, and must not be skipped as though it "
            f"were: {exc!r}") from exc
    if spec is None or spec.origin is None or not spec.origin.endswith(".py"):
        # NOT a raise, and the asymmetry is deliberate. This is the branch a
        # STUB reaches - this suite plants a MagicMock at `paho.mqtt.client`
        # for the length of mqtt.py's import, and a mock satisfies `hasattr`
        # for every name, so answering from it would make the assertions that
        # call this helper true for free. "There is no real .py module here"
        # is honestly an absence of the thing being pinned against, and a FAIL
        # would be a false finding about the environment.
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        # RAISE, do not return None. This is the EXECUTION half: a real .py
        # file was found on disk and failed to run. That is a broken
        # installation, not a missing one.
        #
        # ~~"Everything above this point is about LOCATING paho, where 'not
        # found' and 'found a stub' are both honestly reported as absence and
        # the caller's skip is true."~~ False, and the review of 094eac0
        # measured it: `find_spec` imports the parent packages, so paho's own
        # `__init__` runs up there and can fail in three ways that are all
        # brokenness rather than absence. The classification now happens at
        # BOTH points - see the comment on the `find_spec` call above - and
        # this arm covers only `paho.mqtt.client` itself failing to exec.
        #
        # `except Exception: return None` here made those two states
        # indistinguishable, and the caller then skips with the words "paho is
        # not installed here" - a false statement about the environment in
        # exactly the case where something is wrong. A dead instrument must
        # not report an absence; that is the whole premise this suite is built
        # on. Found by the T-527.27 review round, which probed it: healthy
        # paho PASS, `_topic` renamed FAIL, `_topic` as str FAIL, planted
        # MagicMock SKIP (correctly refused) - and import-raises SKIP, which
        # is this line.
        raise PahoIsInstalledButUnusable(
            f"paho was found at {spec.origin} and failed to import, so this "
            f"suite cannot pin anything against it. This is NOT the same as "
            f"paho being absent, and must not be skipped as though it were: "
            f"{exc!r}") from exc
    return module


if __name__ == "__main__":
    unittest.main()
