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
_LOG_METHODS = frozenset({
    "debug", "info", "warning", "warn", "error", "exception", "critical", "log",
})
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
    r"[-#0 +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[hlL]?"
    r"(?P<conv>[diouxXeEfFgGcrsa%])"
)


def _log_it(value):
    """Stand-in for a helper that logs, used by one fixture below.

    Never called - the fixture is read with inspect.getsource() and parsed,
    never executed - but defined so the module has no undefined name in it.
    """
    raise AssertionError("fixture helper is parsed, never run")


def _is_taint_seed(node):
    """The two places untrusted bytes ENTER, independent of what they get called.

    `msg.payload` is the real one - paho hands the callback an object and the
    bytes are an attribute of it. A bare name `payload` is kept because
    mqtt.py binds it at on_message's first statement and several tests below
    hand one straight in as a parameter.

    Seeding on the SOURCE rather than on the spelling is the whole T-527.17
    H2 fix: `body = msg.payload.decode()` is tainted because of where it came
    from, so a rename cannot disarm the rule.
    """
    return ((isinstance(node, ast.Name) and node.id == "payload")
            or (isinstance(node, ast.Attribute) and node.attr == "payload"))


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
    if "*" in fmt:
        return None
    convs = []
    for match in _PERCENT_SPEC.finditer(fmt):
        if match.group("conv") == "%":
            continue
        if match.group("key") is not None:
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
        is exactly that shape. It is not currently reachable - every int()/
        float() over the payload is either gated on `.isdigit()` or caught
        locally and logged with `!r` - but that is a property of today's
        branches, not of the guard. The blanket fix is `{e!r}`, and it is
        filed against T-527.12, which already touches this file.
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
        for conv, value in pairs:
            if _carries_taint(value, names) and unclaimed(value):
                claim(value, value.lineno, _SAFE_FORMAT.get(conv, "RAW"))

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

        def printed(msg, sink):
            print(f"stdout is captured into the journal too: {msg.payload}")

        def raised(msg, sink):
            # on_message wraps everything in `except Exception as e:` and logs
            # it, so a raised message is a sink one indirection away.
            raise ValueError(f"cannot parse {msg.payload}")

        for shape in (multi_line_fstring, percent_style_lazy_logging,
                      str_format, concatenation, str_conversion,
                      bound_intermediate, renamed_local, taint_through_a_chain,
                      taint_through_nested_blocks,
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

        expected = {"percent_repr": "%r", "percent_ascii": "%a",
                    "percent_lazy_repr": "%r", "format_repr": "{!r}",
                    "format_ascii": "{!a}"}
        for shape in (percent_repr, percent_ascii, percent_lazy_repr,
                      format_repr, format_ascii):
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
        the sibling suites that match on that shape stay valid."""
        message = self._decode_record(LIGHT_COMMAND, b"ON")
        self.assertEqual(f"Decoded payload on {LIGHT_COMMAND}: 'ON'", message)


if __name__ == "__main__":
    unittest.main()
