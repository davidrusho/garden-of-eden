#!/usr/bin/env python3
# Reviewed: 2026-08-02 against 27a8165 (T-494) — read end to end. All four
# defects the T-490 review left open are fixed and each carries a test and a
# mutant: the port length bound in front of int(), an explicit UTF-8 read,
# control-character escaping in _fmt(), and RecursionError in load_state()'s
# caught tuple. Known and ACCEPTED: neither the script nor the installer can
# tell a correctly-shaped config from one naming hosts that no longer exist,
# and that config drives the reboot ladder — see the ticket.
# Reviewed: 2026-08-02 against a4bb303 (T-490)
# Reviewed: 2026-07-31 against bf5680f (T-473.4)
"""Network watchdog for the Gardyn Pi (T-473.4).

Emits ONE logfmt line per run to stdout, captured by systemd into the (now
persistent, T-473.1) journal. Read it back with:

    journalctl -t gardyn-netwatch --since -24h

Why this exists
---------------
On 2026-07-30 the Pi dropped off the network at ~16:30 and was not reachable
again until a power-cycle at 20:24 — over four hours, during which the grow
light ran 2.5 h past its schedule. The host itself never died: a timer stamp
written at 19:00:02 proves the previous boot was still executing scheduled
work three hours into the outage. systemd was alive throughout, which is
exactly why a HARDWARE watchdog would not have helped — it would have been
petted happily the whole time. The thing that was dead was the network, and
nothing was watching it.

The escalation ladder
---------------------
Deliberately conservative, because the failure action is a reboot and a reboot
of this host dark-cycles the plants until the schedule's 15-minute re-assert
fires. A false positive costs more than reacting slowly.

    healthy          any probe answers             -> clear state, do nothing
    unmeasurable     no probe could be RUN         -> stand down, log, do nothing
    first failure    nothing answers               -> reactivate wlan0
    still failing    ... on a backoff cadence      -> reactivate wlan0 again
    down >= 5 min    ... and every guard agrees    -> reboot

Guards in front of the reboot, each blocking a distinct way this could make
things worse:

  * `MIN_UPTIME_BEFORE_REBOOT_S` — never reboot a host that only just booted.
    Wi-Fi association and DHCP take time, and a watchdog firing inside that
    window would reboot a healthy Pi for being early.

  * `MAX_CONSECUTIVE_REBOOTS` — a reboot that does not restore the network is
    evidence that rebooting is not the fix; the usual cause is the AP being
    down, which no reboot of this Pi can help.

  * `HEALTHY_STREAK_TO_REARM` — the cap above is only worth having if a single
    lucky tick cannot reset it. A flapping link that comes back for one check
    every 15 minutes would otherwise re-arm the ladder indefinitely: measured
    at 96 reboots/day against a nominal cap of 2. Health must be SUSTAINED
    before the cap resets.

  * a failed state write downgrades a reboot to a suppressed one — see
    `save_state`. The cap lives in that file, so a reboot the file did not
    record is a reboot the cap cannot see.

Two probe modalities, and the difference between "no" and "don't know"
----------------------------------------------------------------------
ICMP to the configured hosts plus a TCP connect to the broker. Two reasons:
a single target makes any one host's reboot look like this Pi's radio dying,
and ICMP alone cannot distinguish a dead path from a filtered or rate-limited
one — so an ICMP-only watchdog bounces a perfectly good link whenever somebody
turns on ICMP policing upstream.

More important is the third state. A probe that could not be RUN — fork
failure, missing binary — is not evidence of anything, and treating it as
"unreachable" turns a local resource problem into a reboot. `probe()` returns
None for that case and the ladder stands down, the same way it already stands
down when `/proc/uptime` is unreadable.

Timekeeping note: every duration is derived from `/proc/uptime`, never the
wall clock. This host has no RTC and runs `fake-hwclock`, so its wall clock is
restored-then-corrected at boot and can jump arbitrarily the moment NTP
reaches it — and NTP reaching it is precisely what is not happening during the
outage this exists for. Cross-boot staleness is settled by comparing the
kernel's `boot_id` rather than by inferring it from uptime arithmetic, which
only ever caught the case where the stored value was LARGER.

Design note: `decide()` is a pure function of (uptime, reachability, prior
state, boot id). Every branch — including the reboot path, which cannot be
exercised on a live host without rebooting it — is reachable from a test.

Configuration, and why there is no default
------------------------------------------
The ping targets, the TCP probe host and the wlan0 profile UUID are one
particular LAN's topology, and this is a PUBLIC repository. They used to be
module constants, which meant a clone anywhere else inherited both somebody
else's addressing AND a reboot policy aimed at it — a watchdog that decides a
stranger's network is down and power-cycles the machine it is running on.

They now come from `/etc/gardyn/netwatch.env` (`services/etc/gardyn/
netwatch.env.example` is the template) and there is NO working fallback. Every
way of not having a complete, non-placeholder configuration — file absent,
unreadable, empty, a key missing, a value still holding the template's
CHANGEME — refuses to run and exits non-zero, so systemd marks the unit
failed. A silent fall back to a built-in target is precisely the failure this
exists to prevent, so "found no config" and "found a usable config" must never
produce the same behaviour.
"""
from __future__ import annotations

import errno
import json
import math
import os
import re
import socket
import subprocess
import sys
from typing import NamedTuple

# Overridable only so the test suite can point at a fixture; the deployed unit
# passes nothing and gets the real path. Same seam as GARDYN_UNIT_SRC_DIR in
# bin/install-systemd-units.sh.
CONFIG_PATH = os.environ.get("GARDYN_NETWATCH_CONFIG", "/etc/gardyn/netwatch.env")

KEY_TARGETS = "GARDYN_NETWATCH_PING_TARGETS"
KEY_TCP_HOST = "GARDYN_NETWATCH_TCP_HOST"
KEY_TCP_PORT = "GARDYN_NETWATCH_TCP_PORT"
KEY_WLAN_UUID = "GARDYN_NETWATCH_WLAN_UUID"

# Deliberately does NOT include the port: 1883 is the IANA-registered MQTT
# port and says nothing about anybody's topology, so it is the one field that
# may sensibly default. The three below are all site-specific.
REQUIRED_KEYS = (KEY_TARGETS, KEY_TCP_HOST, KEY_WLAN_UUID)

# The sentinel the shipped template uses. Rejecting it is what stops a
# copied-but-unedited config becoming a working-looking watchdog aimed at
# nothing: unedited placeholders would otherwise never answer a probe, which
# reads to the ladder as a total outage and escalates to a reboot.
PLACEHOLDER = "CHANGEME"

DEFAULT_TCP_PORT = 1883

# Two independent hosts, minimum. With one, that host rebooting is
# indistinguishable from this Pi's radio dying — which is the entire reason
# the ladder pings more than one thing, and is the same defect the duplicate
# check below refuses. A cap without a floor only closes half of it.
MIN_PING_TARGETS = 2

STATE_PATH = "/var/lib/gardyn-netwatch/state.json"
BOOT_ID_PATH = "/proc/sys/kernel/random/boot_id"

REBOOT_AFTER_DOWN_S = 300.0
MIN_UPTIME_BEFORE_REBOOT_S = 600.0
MAX_CONSECUTIVE_REBOOTS = 2
# 15 consecutive healthy checks at a 2-minute cadence = 30 minutes.
HEALTHY_STREAK_TO_REARM = 15
# After the first two attempts, reconnect only every Nth failing check. A
# reconnect DEACTIVATES and reactivates the link, dropping mqtt.py's broker
# session each time, so bouncing it every 2 minutes through an upstream outage
# is the watchdog becoming the outage.
RECONNECT_EVERY_N_FAILURES = 5

# No `-c`. ping(8): "If a packet count and deadline are both specified, and
# fewer than count packets are received by the time the deadline has arrived,
# it will also exit with code 1." With `-c 2 -w 3`, ONE lost reply from a
# perfectly reachable host exits 1 — verified on this host against the live
# gateway. Deadline-only means exit 0 <=> at least one reply, which is the
# question actually being asked.
PING_DEADLINE_S = "3"
TCP_TIMEOUT_S = 3.0
# nmcli(1): "If --wait option is not specified, the default timeout will be 90
# seconds." Killing the client at 60 s would pre-empt nmcli's own timeout and
# throw away its exit 3, so ask for less than we are willing to wait.
RECONNECT_NMCLI_WAIT_S = 45
RECONNECT_TIMEOUT_S = 60.0

# Must match TimeoutStartSec= in services/etc/systemd/system/gardyn-netwatch.service.
# Pinned here because the target COUNT is now operator-supplied, so the worst-
# case run time is no longer fixed at authoring time: a config listing enough
# ping targets would push a failing run past the unit's start timeout and get
# it killed mid-reconnect, every tick, silently.
UNIT_TIMEOUT_START_S = 90.0

ACT_NONE = "none"
ACT_WAIT = "wait"
ACT_RECONNECT = "reconnect"
ACT_REBOOT = "reboot"
ACT_REBOOT_SUPPRESSED = "reboot_suppressed"
ACT_STAND_DOWN = "stand_down"

EMPTY_STATE = {
    "boot_id": None,
    "first_failure_uptime": None,
    "failures": 0,
    "reconnects": 0,
    "consecutive_reboots": 0,
    "healthy_streak": 0,
}


class ConfigError(Exception):
    """A configuration that cannot be used. Carries a short greppable reason.

    Every raise site is a refusal to run, never a downgrade to a default —
    see the module docstring for why this file has no fallback topology.
    """

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class NetwatchConfig(NamedTuple):
    targets: tuple[str, ...]
    tcp_host: str
    tcp_port: int
    wlan_uuid: str

    @property
    def tcp_key(self) -> str:
        return f"tcp_{self.tcp_host}_{self.tcp_port}"


def probe_budget_s(target_count: int) -> float:
    """Worst-case wall time for one failing run with `target_count` pings.

    Every ping runs to its deadline (plus the subprocess backstop), then the
    TCP probe times out, then a reconnect is allowed its full budget.
    """
    return (target_count * (float(PING_DEADLINE_S) + 5.0)
            + TCP_TIMEOUT_S + RECONNECT_TIMEOUT_S)


def _max_ping_targets() -> int:
    """The largest target count whose worst-case run still fits the unit's
    start timeout. DERIVED, not chosen: bumping TimeoutStartSec= or a probe
    timeout moves this by itself instead of leaving a stale literal behind."""
    count = 1
    while probe_budget_s(count + 1) < UNIT_TIMEOUT_START_S:
        count += 1
    return count


MAX_PING_TARGETS = _max_ping_targets()

# A host token that is safe to hand to `ping` as argv and to render into a
# logfmt key. The FIRST character is the load-bearing part: a value beginning
# with `-` is read by ping(8) as an OPTION rather than a destination, so a
# target of `-V` or `--flood` would be an operator-supplied flag on a command
# this script runs. Whitespace and `#` are excluded for a second reason — they
# survive into `probe_<host>_<port>=` and produce a log line that no longer
# parses as logfmt, which matters because during an outage that line is the
# only artifact anyone reads back.
#
# Known limitation, deliberate: a bare-colon IPv6 literal (`::1`) is refused
# because the first character must be alphanumeric. The ping invocation here is
# IPv4-only in any case, and `fe80::1` is accepted.
_HOST_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]*\Z")

_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")


def parse_env(raw: str | None) -> dict:
    """Parse a systemd EnvironmentFile-shaped KEY=VALUE body.

    Deliberately lenient about SHAPE and strict about CONTENT: a stray line
    is skipped here and the missing-key check below is what refuses to run.
    Splitting it that way keeps every refusal in one place, so there is no
    path where a malformed line produces a partial config that still runs.
    """
    out: dict = {}
    if not raw:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def build_config(env: dict) -> NetwatchConfig:
    """Validate a parsed environment into a config, or refuse.

    Raises ConfigError for every incomplete or implausible input. There is no
    branch that returns a partially-populated config and no branch that
    substitutes a value the operator did not write.
    """
    for key in REQUIRED_KEYS:
        value = (env.get(key) or "").strip()
        if not value:
            raise ConfigError("config_missing_key", f"{key} is missing or empty")
        if PLACEHOLDER in value.upper():
            raise ConfigError("config_placeholder",
                              f"{key} still holds the template placeholder")

    targets = tuple(t for t in re.split(r"[,\s]+", env[KEY_TARGETS].strip()) if t)
    if not targets:
        raise ConfigError("config_no_targets", f"{KEY_TARGETS} names no host")
    for target in targets:
        if not _HOST_RE.match(target):
            raise ConfigError("config_bad_target",
                              f"{KEY_TARGETS} contains an implausible host")
    if len(targets) < MIN_PING_TARGETS:
        raise ConfigError("config_too_few_targets",
                          f"{KEY_TARGETS} needs at least {MIN_PING_TARGETS} "
                          "independent hosts")
    if len(set(targets)) != len(targets):
        # Two entries that are the same host are one modality wearing a hat:
        # that host rebooting then looks exactly like this Pi's radio dying,
        # which is the specific confusion the multi-target design removes.
        raise ConfigError("config_duplicate_targets",
                          f"{KEY_TARGETS} repeats a host")
    if len(targets) > MAX_PING_TARGETS:
        raise ConfigError(
            "config_too_many_targets",
            f"{KEY_TARGETS} lists {len(targets)}; more than {MAX_PING_TARGETS} "
            f"can exceed the unit's {UNIT_TIMEOUT_START_S:.0f}s start timeout")

    raw_port = (env.get(KEY_TCP_PORT) or "").strip()
    if raw_port:
        if PLACEHOLDER in raw_port.upper():
            raise ConfigError("config_placeholder",
                              f"{KEY_TCP_PORT} still holds the template placeholder")
        # `int()` alone accepts "1_883", "+1883" and Arabic-Indic digits, none
        # of which anybody meant to write in a config file.
        if not (raw_port.isascii() and raw_port.isdigit()):
            raise ConfigError("config_bad_port",
                              f"{KEY_TCP_PORT} is not a plain decimal integer")
        # BEFORE the conversion, because the conversion is what raises.
        # `isdigit()` is true for any number of digits, and past CPython's
        # 4300-digit cap `int()` raises ValueError - which is not a
        # ConfigError, so it escapes main()'s handler and exits 1 with a
        # traceback and NO journal line. That is the same failure shape the
        # UnicodeDecodeError catch removed from _read(): the operator loses
        # the named reason this whole refusal vocabulary exists to give them.
        #
        # Five, because 65535 is five digits - so a sixth character can only be
        # zero padding or garbage, and a config file carrying either is a typo
        # rather than an intent. Checking the converted VALUE instead would be
        # checking a number that was never produced.
        if len(raw_port) > 5:
            raise ConfigError("config_bad_port",
                              f"{KEY_TCP_PORT} is longer than any port can be")
        port = int(raw_port, 10)
        if not 1 <= port <= 65535:
            raise ConfigError("config_bad_port",
                              f"{KEY_TCP_PORT} is outside 1-65535")
    else:
        port = DEFAULT_TCP_PORT

    uuid = env[KEY_WLAN_UUID].strip()
    if not _UUID_RE.match(uuid):
        # `nmcli connection up uuid <x>` with a connection NAME here fails
        # every reconnect with nothing but the journal to show for it, so
        # catch the shape at config time rather than during an outage.
        raise ConfigError("config_bad_uuid",
                          f"{KEY_WLAN_UUID} is not a UUID")

    tcp_host = env[KEY_TCP_HOST].strip()
    if not _HOST_RE.match(tcp_host):
        raise ConfigError("config_bad_tcp_host",
                          f"{KEY_TCP_HOST} is not a plausible host")

    return NetwatchConfig(targets=targets, tcp_host=tcp_host,
                          tcp_port=port, wlan_uuid=uuid)


def load_config(path: str) -> NetwatchConfig:
    """Read and validate the config file, or refuse. Never returns a default."""
    raw = _read(path)
    if raw is None:
        raise ConfigError("config_unreadable", f"cannot read {path}")
    env = parse_env(raw)
    if not env:
        raise ConfigError("config_empty", f"{path} defines no settings")
    return build_config(env)


def config_error_record(exc: ConfigError, path: str) -> str:
    """The one log line emitted when there is no config to run against.

    Shaped like the ordinary record so `journalctl -t gardyn-netwatch` greps
    the same way, but it cannot use format_record(): the per-probe keys are
    derived from the config that just failed to load.
    """
    parts = [
        ("action", ACT_STAND_DOWN),
        ("reason", exc.reason),
        ("config_path", path),
        ("detail", exc.detail),
    ]
    return " ".join(f"{k}={_fmt(v)}" for k, v in parts)


def read_boot_id(raw: str | None) -> str | None:
    """The kernel's per-boot UUID. Changes on every boot, so it makes
    cross-boot staleness DECIDABLE instead of inferred from uptime."""
    if not raw:
        return None
    text = raw.strip()
    return text or None


def load_state(raw: str | None) -> dict:
    """Parse the persisted state, falling back to a clean slate.

    Never raises. A watchdog that dies on a corrupt state file is a watchdog
    that stops watching, and this file is written by a process that may be
    interrupted by the very reboot it just ordered.
    """
    state = dict(EMPTY_STATE)
    if not raw:
        return state
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        # RecursionError, because json raises THAT rather than ValueError on a
        # deeply nested document - so without it the "never raises" promise
        # above was not literally true, and a corrupt state file took the whole
        # ladder down with it.
        return state
    if not isinstance(parsed, dict):
        return state

    value = parsed.get("first_failure_uptime")
    if (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0):
        state["first_failure_uptime"] = float(value)

    boot_id = parsed.get("boot_id")
    if isinstance(boot_id, str) and boot_id.strip():
        state["boot_id"] = boot_id.strip()

    for key in ("failures", "reconnects", "consecutive_reboots", "healthy_streak"):
        count = parsed.get(key)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            state[key] = count
    return state


def parse_uptime(raw: str | None) -> float | None:
    """Seconds from /proc/uptime.

    Non-finite values are rejected: `float("nan")` compares False against every
    threshold, so a NaN would fall through all three reboot guards and then
    die at `int(nan)` — a crash on the one path that must not misfire.
    """
    if not raw:
        return None
    try:
        value = float(raw.split()[0])
    except (ValueError, IndexError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def decide(uptime_s: float, reachable: bool, state: dict,
           boot_id: str | None = None) -> tuple[str, str, bool, dict]:
    """Pure escalation logic. Returns (action, reason, reconnect_now, state).

    `reconnect_now` is separate from `action` because a suppressed reboot must
    still retry the cheap fix on the backoff cadence — folding that into the
    action would either lose the suppression in the log or strand the ladder
    doing nothing at all.
    """
    new_state = dict(EMPTY_STATE)
    new_state["boot_id"] = boot_id
    new_state["consecutive_reboots"] = state.get("consecutive_reboots", 0)

    if reachable:
        streak = state.get("healthy_streak", 0) + 1
        new_state["healthy_streak"] = streak
        # Sustained health, not a single lucky tick, is what re-arms the cap.
        if streak >= HEALTHY_STREAK_TO_REARM:
            new_state["consecutive_reboots"] = 0
        return ACT_NONE, f"reachable_streak_{streak}", False, new_state

    # A boot_id mismatch means this state was written by a previous boot, so
    # its uptime-derived fields measure a clock that no longer exists.
    stale = state.get("boot_id") != boot_id
    first = None if stale else state.get("first_failure_uptime")
    reconnects = 0 if stale else state.get("reconnects", 0)
    failures = 0 if stale else state.get("failures", 0)

    if first is None or first > uptime_s:
        first = uptime_s
    new_state["first_failure_uptime"] = first
    down_for = uptime_s - first

    failures += 1
    new_state["failures"] = failures
    new_state["reconnects"] = reconnects
    new_state["healthy_streak"] = 0

    # Reconnect on the first two failing checks, then back off. Bouncing the
    # link every 2 minutes through an upstream outage drops mqtt.py's session
    # each time for no benefit.
    wants_reconnect = failures <= 2 or failures % RECONNECT_EVERY_N_FAILURES == 0

    reboot_earned = reconnects >= 1 and down_for >= REBOOT_AFTER_DOWN_S
    if reboot_earned:
        if uptime_s < MIN_UPTIME_BEFORE_REBOOT_S:
            if wants_reconnect:
                new_state["reconnects"] = reconnects + 1
            return ACT_REBOOT_SUPPRESSED, "uptime_too_low", wants_reconnect, new_state
        if new_state["consecutive_reboots"] >= MAX_CONSECUTIVE_REBOOTS:
            if wants_reconnect:
                new_state["reconnects"] = reconnects + 1
            return ACT_REBOOT_SUPPRESSED, "reboot_cap_reached", wants_reconnect, new_state

        new_state["consecutive_reboots"] += 1
        # A reboot restarts the clock: uptime resets, so the down-timer and
        # the attempt counters must not carry a pre-reboot reading into the
        # next boot's ladder.
        new_state["first_failure_uptime"] = None
        new_state["reconnects"] = 0
        new_state["failures"] = 0
        return ACT_REBOOT, f"down_{int(down_for)}s", False, new_state

    if wants_reconnect:
        new_state["reconnects"] = reconnects + 1
        reason = "first_failure" if reconnects == 0 else f"retry_{failures}"
        return ACT_RECONNECT, reason, True, new_state

    return ACT_WAIT, f"backoff_{failures}", False, new_state


def _fmt(value) -> str:
    """logfmt value: quote anything with a space, render None as a bare -.

    Embedded quotes and backslashes are escaped rather than emitted raw. An
    operator-supplied string reaches this via a ConfigError detail, and an
    unescaped quote closes the field early — producing a line that parses, but
    into the wrong fields, which is worse than one that visibly does not.
    """
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    # CONTROL CHARACTERS are escaped for the same reason the quote is. The
    # quoting rule keys off a space and a quote, and a newline is neither - so
    # a raw newline ENDS the record and turns its remainder into a second line
    # carrying an injected field. One record arriving as three reads as data
    # rather than as damage, and this line is the only artifact anybody reads
    # back during an outage.
    #
    # Escaped BEFORE the quoting decision, so a value whose only oddity is a
    # control character still gets quoted rather than slipping past for not
    # containing a space.
    escaped = (text.replace("\\", "\\\\")
                   .replace('"', '\\"')
                   .replace("\n", "\\n")
                   .replace("\r", "\\r")
                   .replace("\t", "\\t"))
    # Anything else below 0x20, plus DEL. A bare ESC can retint or reposition
    # the terminal of whoever is reading the journal back.
    escaped = "".join(
        ch if (ch >= " " and ch != "\x7f") else f"\\x{ord(ch):02x}"
        for ch in escaped)
    if escaped != text or " " in text or '"' in text:
        return f'"{escaped}"'
    return text


def format_record(action: str, reason: str, results: dict, uptime_s: float | None,
                  state: dict, cfg: NetwatchConfig,
                  outcome: str | None = None) -> str:
    """Render one logfmt line. Key order is stable so the output greps well.

    During an outage this line is the only artifact anyone reads back, so the
    per-probe results are all rendered, including the false ones.
    """
    parts = [
        ("action", action),
        ("reason", reason),
        ("reachable", any(v is True for v in results.values())),
        ("uptime_s", None if uptime_s is None else int(uptime_s)),
        ("failures", state.get("failures", 0)),
        ("reconnects", state.get("reconnects", 0)),
        ("consecutive_reboots", state.get("consecutive_reboots", 0)),
        ("healthy_streak", state.get("healthy_streak", 0)),
    ]
    for name in list(cfg.targets) + [cfg.tcp_key]:
        parts.append((f"probe_{name}", results.get(name)))
    if outcome:
        parts.append(("outcome", outcome))
    return " ".join(f"{k}={_fmt(v)}" for k, v in parts)


def ping(target: str) -> bool | None:
    """True answered, False no answer, None could not measure.

    The third state is the point. A missing binary or a fork failure is not
    evidence that the network is down, and collapsing it into False turns a
    local resource problem into a reboot of a healthy host.
    """
    try:
        proc = subprocess.run(
            ["ping", "-w", PING_DEADLINE_S, target],
            capture_output=True, text=True, timeout=float(PING_DEADLINE_S) + 5.0,
        )
    except subprocess.TimeoutExpired:
        return None
    except OSError:
        return None
    if proc.returncode == 0:
        return True
    # ping(8) exits 1 for "no reply" and 2 for any other error (bad argument,
    # unknown host). Only the first is evidence about the network.
    return False if proc.returncode == 1 else None


# Errno values that are a genuine answer ABOUT THE NETWORK. Anything else from
# socket.create_connection is a local failure — running out of file
# descriptors or memory says nothing about reachability, and reading it as
# "the LAN is down" is the same defect the tri-state in ping() exists to close.
NETWORK_ERRNOS = frozenset({
    errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN, errno.EHOSTDOWN,
    errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED, errno.ENETRESET,
})


def tcp_probe(host: str, port: int) -> bool | None:
    """True connected, False a network-level no, None could not measure.

    Both arguments are REQUIRED. A default here would be a built-in target
    reintroduced through the back door — the caller could then omit it and
    get a probe aimed at somebody else's broker with nothing to show for it.

    This probe carries more weight than it looks. When wlan0 is down there is
    no route, so `ping` exits 2 and both ICMP probes report "don't know" —
    measured in the T-473.4 drill. The decision to reconnect rested entirely
    on this one, which is exactly why its own error classification has to be
    as careful as ping's.
    """
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT_S):
            return True
    except ConnectionRefusedError:
        # Something answered: the path is up and the port is shut. That is a
        # reachable network, which is the question being asked.
        return True
    except socket.timeout:
        return False
    except socket.gaierror:
        # Name resolution — not applicable to a literal address, but it would
        # be a local/DNS fault rather than evidence about the path.
        return None
    except OSError as exc:
        return False if exc.errno in NETWORK_ERRNOS else None


def reconnect(wlan_uuid: str) -> str:
    """Reactivate the wlan0 profile. Returns a short outcome string.

    The UUID is REQUIRED and has no default, for the same reason tcp_probe's
    host does: activating a profile identified by a value this repository
    guessed is the one action here that changes a stranger's machine.

    `nmcli connection up` — NOT `nmcli device reconnect`, which does not exist
    (nmcli 1.42.4 exits 2 with "argument not understood", so a watchdog built
    on it would look healthy while never recovering the link), and NOT `nmcli
    device reapply`, which reports success without re-running activation. Both
    were verified against the live host during T-473.3.
    """
    try:
        proc = subprocess.run(
            ["nmcli", "--wait", str(RECONNECT_NMCLI_WAIT_S),
             "connection", "up", "uuid", wlan_uuid],
            capture_output=True, text=True, timeout=RECONNECT_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return "reconnect_timeout"
    except OSError as exc:
        return f"reconnect_oserror_{exc.errno}"
    return "reconnect_ok" if proc.returncode == 0 else f"reconnect_exit_{proc.returncode}"


def reboot() -> str:
    """Order a reboot. Returns "reboot_ordered" ONLY on a zero exit.

    A zero exit means ENQUEUED, not completed — systemctl(1) documents the
    command as asynchronous. That is the strongest signal available, and it
    still has to be checked: a non-zero exit (logind refusing, D-Bus wedged)
    with the counter already incremented would burn a slot off the reboot cap
    for a reboot that never happened, and two of those permanently strand the
    ladder at `reboot_suppressed`.
    """
    try:
        proc = subprocess.run(["systemctl", "reboot"],
                              capture_output=True, text=True, timeout=30.0)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return f"reboot_failed_{type(exc).__name__}"
    return "reboot_ordered" if proc.returncode == 0 else f"reboot_exit_{proc.returncode}"


def _read(path: str) -> str | None:
    """Read a file, or None if it cannot be read AS TEXT.

    UnicodeDecodeError is caught alongside OSError because a file that exists
    and is unreadable is the same fact to every caller here, and the decode
    happens OUTSIDE load_state() — so without this, `load_state`'s "never
    raises" promise is false for a state file corrupted into invalid UTF-8,
    and a bad config file exits 1 with a traceback instead of 2 with a named
    reason on the journal.

    Deliberately NOT `errors="replace"`: measured, that ACCEPTS the corrupt
    config and yields a ping target with a U+FFFD in it. Refusing to read is
    the whole point; salvaging bytes is strictly worse than not reading them.

    The encoding is NAMED rather than inherited. `open()` with no `encoding=`
    uses the locale's, and a systemd unit runs with a minimal environment - so
    under a C/POSIX locale Python did not coerce to C.UTF-8 the preferred
    encoding is ASCII, and a perfectly valid UTF-8 config is then refused as
    `config_unreadable`. The template this repository ships contains em
    dashes, so the file the README says to copy is exactly the file that would
    be rejected.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def save_state(path: str, state: dict) -> bool:
    """Write state atomically. Returns True only if it is durably on disk.

    The return value is load-bearing: the reboot cap lives in this file, so a
    reboot the file did not record is a reboot the cap cannot see. Callers on
    the reboot path MUST check it — an earlier version swallowed the error and
    rebooted anyway, which gave an unbounded reboot loop (measured at 10
    reboots across 60 ticks against a nominal cap of 2) whenever the file was
    readable but no longer writable.
    """
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as handle:
            json.dump(state, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        # fsync the DIRECTORY too: without it the rename is durable only after
        # the filesystem's own commit interval, and this file is written
        # moments before an intentional reboot.
        dirfd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        return True
    except OSError:
        return False


def main() -> int:
    # FIRST, and fail closed. Nothing below is safe to attempt against a
    # topology this process had to invent, so no probe is run, no state is
    # written, and the exit status is non-zero so systemd marks the unit
    # failed rather than logging a stand-down that looks like a quiet tick.
    try:
        cfg = load_config(CONFIG_PATH)
    except ConfigError as exc:
        print(config_error_record(exc, CONFIG_PATH), flush=True)
        print(f"gardyn-netwatch: refusing to run: {exc.detail}", file=sys.stderr,
              flush=True)
        return 2

    uptime_s = parse_uptime(_read("/proc/uptime"))
    boot_id = read_boot_id(_read(BOOT_ID_PATH))
    state = load_state(_read(STATE_PATH))

    results: dict = {target: ping(target) for target in cfg.targets}
    results[cfg.tcp_key] = tcp_probe(cfg.tcp_host, cfg.tcp_port)

    reachable = any(v is True for v in results.values())
    measured = any(v is not None for v in results.values())

    if uptime_s is None:
        # No trustworthy clock means the ladder cannot be evaluated.
        print(format_record(ACT_STAND_DOWN, "no_uptime", results, None, state, cfg),
              flush=True)
        return 0

    if not reachable and not measured:
        # Nothing answered because nothing could be ASKED. That is not evidence
        # about the network, and acting on it would reboot a healthy host.
        print(format_record(ACT_STAND_DOWN, "no_probe_ran", results, uptime_s, state, cfg),
              flush=True)
        return 0

    action, reason, reconnect_now, new_state = decide(uptime_s, reachable, state, boot_id)

    if action == ACT_REBOOT:
        # Persist BEFORE ordering the reboot; this process may not run again.
        # If the write fails the cap is blind, so downgrade rather than reboot.
        if not save_state(STATE_PATH, new_state):
            print(format_record(ACT_REBOOT_SUPPRESSED, "state_unwritable", results,
                                uptime_s, new_state, cfg, "reconnect_skipped"), flush=True)
            return 0
        print(format_record(action, reason, results, uptime_s, new_state, cfg,
                            "reboot_ordering"), flush=True)
        outcome = reboot()
        if outcome != "reboot_ordered":
            # The reboot did not happen; give the slot back or two failures
            # strand the ladder at reboot_suppressed forever.
            rolled = dict(new_state)
            rolled["consecutive_reboots"] = max(0, rolled["consecutive_reboots"] - 1)
            save_state(STATE_PATH, rolled)
            new_state = rolled
        print(format_record(action, reason, results, uptime_s, new_state, cfg, outcome),
              flush=True)
        return 0

    outcome = reconnect(cfg.wlan_uuid) if reconnect_now else None

    # Skip an unchanged write: a healthy host would otherwise fsync 720 times a
    # day onto the SD card, on a ticket whose sibling work exists to cut SD
    # writes.
    if new_state != state:
        save_state(STATE_PATH, new_state)
    print(format_record(action, reason, results, uptime_s, new_state, cfg, outcome),
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
