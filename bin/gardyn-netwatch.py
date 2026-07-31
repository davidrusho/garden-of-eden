#!/usr/bin/env python3
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
ICMP to two hosts plus a TCP connect to the broker. Two reasons for the mix:
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
"""
from __future__ import annotations

import json
import math
import os
import socket
import subprocess
import sys

# Pinged in order; the network is UP if ANY probe answers. Requiring one
# specific host to agree would make a broker reboot look identical to this
# Pi's radio failing. The question is "can this Pi reach the LAN at all", not
# "is one particular box up".
TARGETS = ("192.168.1.1", "192.168.1.204")

# An independent, non-ICMP modality: the MQTT broker this Pi actually talks
# to. If ICMP is filtered or rate-limited upstream, this still answers, and it
# tests the path that carries the grow-light commands rather than a proxy for
# it.
TCP_PROBE_HOST = "192.168.1.204"
TCP_PROBE_PORT = 1883

# The `preconfigured` wlan0 profile. Pinned by UUID rather than by name so a
# renamed connection fails loudly at reconnect time instead of silently
# activating some other profile.
WLAN_UUID = "11d51067-9d11-4257-822e-cf6744b9a997"

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
    except (ValueError, TypeError):
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
    """logfmt value: quote anything with a space, render None as a bare -."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return f'"{text}"' if " " in text else text


def format_record(action: str, reason: str, results: dict, uptime_s: float | None,
                  state: dict, outcome: str | None = None) -> str:
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
    for name in list(TARGETS) + [f"tcp_{TCP_PROBE_HOST}_{TCP_PROBE_PORT}"]:
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


def tcp_probe(host: str = TCP_PROBE_HOST, port: int = TCP_PROBE_PORT) -> bool | None:
    """True connected, False refused/unreachable/timed out, None could not try."""
    try:
        with socket.create_connection((host, port), timeout=TCP_TIMEOUT_S):
            return True
    except (socket.timeout, ConnectionRefusedError):
        # Refused means something answered — the path is up, the port is shut.
        return True
    except OSError:
        return False


def reconnect() -> str:
    """Reactivate the wlan0 profile. Returns a short outcome string.

    `nmcli connection up` — NOT `nmcli device reconnect`, which does not exist
    (nmcli 1.42.4 exits 2 with "argument not understood", so a watchdog built
    on it would look healthy while never recovering the link), and NOT `nmcli
    device reapply`, which reports success without re-running activation. Both
    were verified against the live host during T-473.3.
    """
    try:
        proc = subprocess.run(
            ["nmcli", "--wait", str(RECONNECT_NMCLI_WAIT_S),
             "connection", "up", "uuid", WLAN_UUID],
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
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
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
    uptime_s = parse_uptime(_read("/proc/uptime"))
    boot_id = read_boot_id(_read(BOOT_ID_PATH))
    state = load_state(_read(STATE_PATH))

    results: dict = {target: ping(target) for target in TARGETS}
    results[f"tcp_{TCP_PROBE_HOST}_{TCP_PROBE_PORT}"] = tcp_probe()

    reachable = any(v is True for v in results.values())
    measured = any(v is not None for v in results.values())

    if uptime_s is None:
        # No trustworthy clock means the ladder cannot be evaluated.
        print(format_record(ACT_STAND_DOWN, "no_uptime", results, None, state), flush=True)
        return 0

    if not reachable and not measured:
        # Nothing answered because nothing could be ASKED. That is not evidence
        # about the network, and acting on it would reboot a healthy host.
        print(format_record(ACT_STAND_DOWN, "no_probe_ran", results, uptime_s, state),
              flush=True)
        return 0

    action, reason, reconnect_now, new_state = decide(uptime_s, reachable, state, boot_id)

    if action == ACT_REBOOT:
        # Persist BEFORE ordering the reboot; this process may not run again.
        # If the write fails the cap is blind, so downgrade rather than reboot.
        if not save_state(STATE_PATH, new_state):
            print(format_record(ACT_REBOOT_SUPPRESSED, "state_unwritable", results,
                                uptime_s, new_state, "reconnect_skipped"), flush=True)
            return 0
        print(format_record(action, reason, results, uptime_s, new_state, "reboot_ordering"),
              flush=True)
        outcome = reboot()
        if outcome != "reboot_ordered":
            # The reboot did not happen; give the slot back or two failures
            # strand the ladder at reboot_suppressed forever.
            rolled = dict(new_state)
            rolled["consecutive_reboots"] = max(0, rolled["consecutive_reboots"] - 1)
            save_state(STATE_PATH, rolled)
            new_state = rolled
        print(format_record(action, reason, results, uptime_s, new_state, outcome), flush=True)
        return 0

    outcome = reconnect() if reconnect_now else None

    # Skip an unchanged write: a healthy host would otherwise fsync 720 times a
    # day onto the SD card, on a ticket whose sibling work exists to cut SD
    # writes.
    if new_state != state:
        save_state(STATE_PATH, new_state)
    print(format_record(action, reason, results, uptime_s, new_state, outcome), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
