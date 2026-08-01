#!/usr/bin/env python3
# Reviewed: 2026-07-31 against 7d82d25 (T-473.2)
"""Periodic host-health sample for the Gardyn Pi (T-473.2).

Emits ONE logfmt line per run to stdout, which systemd captures into the
(now persistent, T-473.1) journal. Read it back with:

    journalctl -t gardyn-health --since -24h

Why this exists
---------------
The 2026-07-30 outage left no recoverable root cause: journald was volatile
and nothing sampled the two things that would have discriminated between the
candidate explanations. Those two things are:

  * `vcgencmd get_throttled` bits 16-19, which are STICKY since boot and would
    settle the brownout question outright — a rail sag that wedged the SDIO
    radio while the CPU survived is a documented Zero W failure, and it is
    indistinguishable after the fact from an AP-side deauth unless somebody
    recorded the undervoltage flag while it was still set.

  * whether wlan0 is present-and-associated vs present-but-not-passing-traffic
    vs gone from /proc/net/wireless entirely. Those imply different fixes and
    look identical from the outside once the host is unreachable.

Watching the watchdog (T-479)
----------------------------
This sampler also carries the external heartbeat for `gardyn-netwatch.timer`,
and it is deliberately THIS unit that carries it rather than netwatch itself.
Netwatch is the unit that reboots the Pi; its own service file says a watchdog
must not become the outage, and putting an HTTP call inside the one unit whose
job is to work while the network is down would be exactly that. This sampler
is not safety-critical, already runs on a fixed cadence, and can read every
failure mode that matters straight out of systemd:

    UnitFileState    -> disabled, masked, or the unit removed entirely
    ActiveState      -> failed, or simply stopped
    LastTriggerUSec* -> active but no longer firing (a wedged run)
    Result           -> the last run itself failed

The verdict is pushed to an Uptime Kuma push monitor, because the gap T-479
exists to close is that a dead watchdog and a healthy network write the same
thing to the journal: nothing. Only an OFF-host observer can tell silence
from health. Kuma's own timeout covers the case where this sampler dies too.

Timekeeping note: freshness is derived from LastTriggerUSecMonotonic against
/proc/uptime, never the wall clock, for the same reason netwatch avoids it —
this host has no RTC and runs fake-hwclock, so its wall clock is restored
then corrected at boot and can jump arbitrarily once NTP lands.

Design note: every parser here is a pure function over a string so the failure
branches the real host cannot be made to produce on demand — a missing
vcgencmd, a radio that has vanished from /proc/net/wireless, malformed hex —
are still covered by tests. main() is the only part that touches the system.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

# Bit meanings per the official Raspberry Pi OS documentation
# (raspberrypi.com/documentation/computers/os.html, "get_throttled").
# Bits 0-3 are CURRENT conditions; bits 16-19 are "has occurred" flags that
# latch until reboot — the sticky half is the diagnostically valuable one,
# because it survives the window in which nobody was watching.
THROTTLED_BITS = {
    0: "undervolt_now",
    1: "arm_capped_now",
    2: "throttled_now",
    3: "soft_temp_limit_now",
    16: "undervolt_since_boot",
    17: "arm_capped_since_boot",
    18: "throttled_since_boot",
    19: "soft_temp_limit_since_boot",
}

UNKNOWN = "unknown"

# --- Watching the netwatch watchdog (T-479) ---------------------------------

NETWATCH_TIMER = "gardyn-netwatch.timer"
NETWATCH_SERVICE = "gardyn-netwatch.service"

# Netwatch fires every 120s. This allows roughly three missed ticks before
# calling it stale, which matters because the freshness test is the ONLY one
# of the four that can produce a false alarm: a stopped, masked or disabled
# timer is a categorical reading, while "how long since it last fired" has to
# tolerate AccuracySec=10s, a slow run on a single-core ARMv6 host, and the
# sampler happening to land immediately before a tick rather than after one.
# Being generous here costs detection latency the categorical checks do not
# need, and buys the monitor's credibility, which a single false page spends.
NETWATCH_MAX_AGE_S = 420.0

# The push URL carries a secret token, so it is supplied by the environment
# (EnvironmentFile=-/etc/gardyn/kuma-netwatch.env) and never committed — this
# repository is public. Absent, the sampler simply does not push, and Kuma's
# own timeout is what reports the silence.
PUSH_URL_ENV = "KUMA_PUSH_URL"
PUSH_TIMEOUT_S = 5.0

# systemd renders monotonic timestamps as a human-readable span rather than
# raw microseconds ("1h 32.020246s", "15min 203.146ms", or a bare "0" for a
# timer that has not fired this boot). Units per systemd's format_timespan();
# `month` and `min` must precede `ms`/`m`-initial alternatives so the longest
# unit wins at each position.
_TIMESPAN_UNITS = {
    "y": 31557600.0, "month": 2629800.0, "w": 604800.0, "d": 86400.0,
    "h": 3600.0, "min": 60.0, "s": 1.0, "ms": 1e-3, "us": 1e-6,
}
_TIMESPAN_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*(y|month|w|d|h|min|ms|us|s)")


def parse_systemd_timespan(raw: str | None) -> float | None:
    """Seconds from a systemd-rendered timespan, or None if untrustworthy.

    Strict on purpose. A lenient parser that ignored trailing junk would turn
    "infinity" or a future systemd's new rendering into a plausible number,
    and a wrong freshness reading is worse than an admitted unknown: it either
    pages for a healthy watchdog or, far worse, reports a dead one as fresh.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    if text == "0":
        # Not "zero seconds ago" — systemd's way of saying it never fired
        # during this boot. The caller decides whether that is a fault, since
        # it is perfectly normal for the first two minutes after a reboot.
        return 0.0
    total = 0.0
    pos = 0
    for match in _TIMESPAN_TOKEN.finditer(text):
        if text[pos:match.start()].strip():
            return None
        total += float(match.group(1)) * _TIMESPAN_UNITS[match.group(2)]
        pos = match.end()
    if pos == 0 or text[pos:].strip():
        return None
    return total


def parse_show_properties(raw: str | None) -> dict:
    """Parse `systemctl show -p ...` KEY=VALUE output into a dict.

    Values may legitimately contain "=", so split once only. A missing key is
    reported as absent rather than defaulted, because `systemctl show` prints
    an EMPTY UnitFileState for a unit that does not exist and still exits 0 —
    so "the key came back blank" and "the unit is gone" are the same reading
    and both have to reach the caller intact.
    """
    out: dict = {}
    for line in (raw or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            out[key.strip()] = value.strip()
    return out


def evaluate_netwatch(timer: dict, service: dict, uptime_s: float | None,
                      probe_error: str | None = None) -> dict:
    """Decide whether the netwatch watchdog is actually running.

    Returns {"ok": True|False|None, "reason": str, "age_s": float|None, ...}.

    The three-valued `ok` is the same distinction netwatch itself draws
    between "no" and "don't know". None means the probe could not be RUN, and
    the caller must then push NOTHING — inventing an UP laundries a real fault
    green, inventing a DOWN pages for a broken `systemctl`. Silence is honest,
    and Kuma's interval already converts sustained silence into a DOWN.
    """
    enabled = timer.get("UnitFileState", "")
    active = timer.get("ActiveState", "")
    result = service.get("Result", "")
    verdict = {"enabled": enabled or None, "active": active or None,
               "result": result or None, "age_s": None}

    if probe_error:
        return {**verdict, "ok": None, "reason": f"probe_{probe_error}"}

    # Ordered most-categorical first, so the reason names the actual fault
    # rather than a downstream symptom of it.
    if not enabled:
        return {**verdict, "ok": False, "reason": "timer_absent"}
    if enabled not in ("enabled", "enabled-runtime"):
        # Covers masked, disabled, static and anything a future systemd adds.
        # An allowlist rather than a list of known-bad states on purpose: a
        # state nobody anticipated must read as "not running", never as
        # healthy-by-default. There was a dedicated `masked` branch above this
        # one until mutation testing showed it was dead code emitting the same
        # string this line already produces.
        return {**verdict, "ok": False, "reason": f"timer_{enabled}"}
    if active != "active":
        return {**verdict, "ok": False, "reason": f"timer_{active or UNKNOWN}"}
    if result and result != "success":
        # The timer is fine; the run it triggered is not. Distinct fault,
        # distinct fix, so it must not be collapsed into "stale" below.
        return {**verdict, "ok": False, "reason": f"run_{result}"}

    last = parse_systemd_timespan(timer.get("LastTriggerUSecMonotonic"))
    if last is None or uptime_s is None:
        # Enabled and active are still measurable and still say healthy; only
        # the freshness axis is blind. Reporting UP with the blindness named
        # beats manufacturing either verdict from a clock we cannot read.
        return {**verdict, "ok": True, "reason": "age_unknown"}
    if last == 0.0:
        if uptime_s <= NETWATCH_MAX_AGE_S:
            return {**verdict, "ok": True, "reason": "booting"}
        return {**verdict, "ok": False, "reason": "never_triggered"}

    age = uptime_s - last
    if age < 0:
        # Monotonic clock ahead of /proc/uptime is not physically meaningful;
        # treat it as an unreadable clock rather than a suspiciously fresh one.
        return {**verdict, "ok": True, "reason": "age_unknown"}
    verdict["age_s"] = age
    if age > NETWATCH_MAX_AGE_S:
        return {**verdict, "ok": False, "reason": "stale"}
    return {**verdict, "ok": True, "reason": "ok"}


def format_push(verdict: dict) -> tuple[str, str] | None:
    """Map a verdict onto (status, msg) for Kuma, or None to push nothing."""
    if verdict.get("ok") is None:
        return None
    status = "up" if verdict["ok"] else "down"
    msg = f"netwatch {verdict['reason']}"
    age = verdict.get("age_s")
    if age is not None:
        msg += f" (last run {int(age)}s ago)"
    return status, msg


def push_kuma(url: str | None, status: str, msg: str,
              timeout: float = PUSH_TIMEOUT_S) -> str:
    """Push one heartbeat. Returns an outcome token and NEVER raises.

    Three traps this homelab has already paid for, all avoided here:

      * A push URL copied out of Kuma's UI carries a `?status=up&msg=OK&ping=`
        template. Appending more parameters to it produces duplicate keys,
        which Kuma stringifies as "[object Object]" while returning HTTP 200 —
        silent corruption. The query string is stripped before rebuilding.

      * Kuma answers a REJECTED push with HTTP 200 and `{"ok":false}`, so the
        status code is necessary but nowhere near sufficient; the body decides.

      * The URL contains the push token. Only the exception's class name is
        ever reported — HTTPError's str() would put the full URL, token and
        all, into a journal that is now persistent.
    """
    if not url:
        return "skipped_no_url"
    base = url.partition("?")[0]
    query = urllib.parse.urlencode({"status": status, "msg": msg, "ping": ""})
    try:
        with urllib.request.urlopen(f"{base}?{query}", timeout=timeout) as response:
            body = response.read(512).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - a sampler must still print its line
        return f"failed_{type(exc).__name__}"
    try:
        if json.loads(body).get("ok") is True:
            return "ok"
    except (ValueError, AttributeError):
        return "failed_bad_body"
    return "failed_not_ok"


def parse_throttled(raw: str | None) -> dict:
    """Decode `vcgencmd get_throttled` output.

    Accepts "throttled=0x50005", a bare "0x0", or surrounding whitespace.
    Returns {"raw": <str>, <flag>: bool, ...} on success, or
    {"raw": "unknown", "error": <reason>} when the value cannot be trusted.
    Never raises: a diagnostic that dies on bad input records nothing, which
    is the exact failure this script exists to prevent.
    """
    if raw is None:
        return {"raw": UNKNOWN, "error": "no_output"}
    text = raw.strip()
    if not text:
        return {"raw": UNKNOWN, "error": "empty"}
    text = text.splitlines()[0].strip()
    if "=" in text:
        value = text.split("=", 1)[1].strip()
    else:
        # Bare form is accepted, but ONLY with an explicit 0x prefix. Without
        # this guard a decimal "50000" parses as 0x50000 and reports
        # undervolt_since_boot + throttled_since_boot for an event that never
        # happened - a false brownout is the worst possible false positive here.
        if not text.lower().startswith("0x"):
            return {"raw": UNKNOWN, "error": "not_hex"}
        value = text
    try:
        bits = int(value, 16)
    except ValueError:
        return {"raw": UNKNOWN, "error": "unparseable"}
    if bits < 0:
        return {"raw": UNKNOWN, "error": "negative"}
    out: dict = {"raw": hex(bits)}
    for bit, name in THROTTLED_BITS.items():
        out[name] = bool(bits & (1 << bit))
    return out


def parse_soc_temp(raw: str | None) -> float | None:
    """Millidegrees from /sys/class/thermal/thermal_zone0/temp -> degrees C.

    Preferred over `vcgencmd measure_temp` because it needs no video-group
    membership, so the sampler can drop privileges if that is ever wanted.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        # int() is unbounded but the division is not: a 400-digit reading
        # raises OverflowError, which `except ValueError` does NOT catch. That
        # killed main() before print(), losing the throttle flags too - i.e. a
        # junk temperature destroyed the sample this script exists to take.
        return int(text) / 1000.0
    except (ValueError, OverflowError):
        return None


def parse_proc_net_wireless(raw: str | None, iface: str = "wlan0") -> dict:
    """Pull one interface's row out of /proc/net/wireless.

    The three outcomes are deliberately distinct, because they imply different
    fixes and the whole point of this sampler is to tell them apart later:

      present=False            -> the radio is GONE from the kernel's view
                                  (driver crash / SDIO wedge). This is the
                                  reading that would have identified the
                                  07-30 outage.
      present=True, link=0     -> interface exists but is not associated.
      present=True, link>0     -> associated; level is the signal in dBm.
    """
    if not raw:
        return {"present": False, "error": "no_output"}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        name, _, rest = line.partition(":")
        if name.strip() != iface:
            continue
        # Fields: status link level noise ... Values carry a trailing "."
        # (e.g. "50.  -60.  -256") which int() rejects, so strip it.
        fields = rest.split()
        if len(fields) < 3:
            return {"present": True, "error": "short_row"}

        def num(tok):
            try:
                return int(tok.rstrip("."))
            except ValueError:
                return None

        link = num(fields[1])
        if not link:
            # Unassociated: cfg80211_wireless_stats() returns NULL and the row
            # is printed as all-zeros by the nullstats fallback. Reporting
            # level_dbm=0 here would read as a maximum-strength signal.
            return {"present": True, "link": link, "level_dbm": None}
        return {"present": True, "link": link, "level_dbm": num(fields[2])}
    return {"present": False}


def parse_uptime(raw: str | None) -> float | None:
    """Seconds from /proc/uptime.

    The single most valuable field for the incident class this exists for: a
    host that is alive-but-unreachable vs one that rebooted are indistinguish-
    able afterwards without it, and it is also what makes the sticky throttle
    bits interpretable (they are "since boot", so they mean nothing unless you
    know whether a boot intervened).
    """
    if not raw:
        return None
    try:
        return float(raw.split()[0])
    except (ValueError, IndexError):
        return None


def parse_mem_available_mb(raw: str | None) -> float | None:
    """MemAvailable from /proc/meminfo, in MiB."""
    if not raw:
        return None
    for line in raw.splitlines():
        if line.startswith("MemAvailable:"):
            parts = line.split()
            if len(parts) >= 2:
                try:
                    return int(parts[1]) / 1024.0
                except ValueError:
                    return None
    return None


def parse_nmcli_state(raw: str | None) -> str:
    """First field of `nmcli -t -f STATE general` (e.g. "connected")."""
    if not raw:
        return UNKNOWN
    first = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    return first.split(":")[0].strip() or UNKNOWN


def _fmt(value) -> str:
    """logfmt value: quote anything with a space, render None as a bare -."""
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return f'"{text}"' if " " in text else text


def format_record(throttled: dict, temp_c: float | None, wireless: dict,
                  nm_state: str, uptime_s: float | None = None,
                  mem_avail_mb: float | None = None,
                  nm_error: str | None = None,
                  netwatch: dict | None = None,
                  push_outcome: str | None = None) -> str:
    """Render one logfmt line. Key order is stable so the output greps well."""
    parts = [
        ("nm_state", nm_state),
        ("wlan_present", wireless.get("present", False)),
        ("wlan_link", wireless.get("link")),
        ("wlan_level_dbm", wireless.get("level_dbm")),
        ("soc_temp_c", None if temp_c is None else round(temp_c, 1)),
        ("throttled", throttled.get("raw", UNKNOWN)),
        ("uptime_s", None if uptime_s is None else int(uptime_s)),
        ("mem_avail_mb", None if mem_avail_mb is None else round(mem_avail_mb, 1)),
    ]
    if netwatch is not None:
        age = netwatch.get("age_s")
        parts += [
            ("netwatch_ok", netwatch.get("ok")),
            ("netwatch_reason", netwatch.get("reason")),
            ("netwatch_age_s", None if age is None else int(age)),
        ]
    if push_outcome:
        parts.append(("kuma_push", push_outcome))
    if nm_error:
        parts.append(("nm_error", nm_error))
    # Only surface the throttle flags that are actually SET. A line of eight
    # false= pairs every 5 minutes buries the one sample that matters.
    for name in THROTTLED_BITS.values():
        if throttled.get(name):
            parts.append((name, True))
    for key in ("error",):
        if throttled.get(key):
            parts.append((f"throttled_{key}", throttled[key]))
        if wireless.get(key):
            parts.append((f"wlan_{key}", wireless[key]))
    return " ".join(f"{k}={_fmt(v)}" for k, v in parts)


def _run(cmd: list[str], timeout: float = 5.0) -> tuple[str | None, str | None]:
    """Run a command. Returns (stdout, failure_reason); exactly one is None.

    The reason is reported rather than collapsed, because "not installed",
    "exited non-zero" and "timed out" call for completely different responses
    and used to be indistinguishable in the log. The concrete case: if the
    service user were not in the `video` group, vcgencmd exits non-zero with
    "VCHI initialization failed" and every line would read exactly like one
    from a host where vcgencmd was never installed.

    Bounded so a wedged binary cannot stall the sampler - which matters most
    precisely when the host is sick.
    """
    if not shutil.which(cmd[0]):
        return None, "not_installed"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, "timeout"
    except OSError as exc:
        return None, f"oserror_{exc.errno}"
    if proc.returncode != 0:
        detail = (proc.stderr or "").strip().splitlines()
        head = detail[0][:60].replace(" ", "_") if detail else ""
        return None, f"exit_{proc.returncode}" + (f"_{head}" if head else "")
    return proc.stdout, None


def _read(path: str) -> str | None:
    try:
        with open(path) as handle:
            return handle.read()
    except OSError:
        return None


def main() -> int:
    thr_out, thr_err = _run(["vcgencmd", "get_throttled"])
    throttled = parse_throttled(thr_out)
    if thr_out is None and thr_err:
        # Keep WHY the reading is missing, not merely that it is.
        throttled["error"] = thr_err

    nm_out, nm_err = _run(["nmcli", "-t", "-f", "STATE", "general"])

    uptime_s = parse_uptime(_read("/proc/uptime"))

    # T-479: watch the watchdog. `systemctl show` exits 0 for a unit that does
    # not exist, so a failure here really does mean the probe could not run.
    timer_out, timer_err = _run([
        "systemctl", "show", NETWATCH_TIMER,
        "-p", "UnitFileState", "-p", "ActiveState", "-p", "LastTriggerUSecMonotonic",
    ])
    service_out, _ = _run(["systemctl", "show", NETWATCH_SERVICE, "-p", "Result"])
    netwatch = evaluate_netwatch(
        parse_show_properties(timer_out),
        parse_show_properties(service_out),
        uptime_s,
        probe_error=timer_err,
    )

    push = format_push(netwatch)
    if push is None:
        push_outcome = "skipped_unmeasurable"
    else:
        push_outcome = push_kuma(os.environ.get(PUSH_URL_ENV), *push)

    record = format_record(
        throttled=throttled,
        temp_c=parse_soc_temp(_read("/sys/class/thermal/thermal_zone0/temp")),
        wireless=parse_proc_net_wireless(_read("/proc/net/wireless")),
        nm_state=parse_nmcli_state(nm_out),
        uptime_s=uptime_s,
        mem_avail_mb=parse_mem_available_mb(_read("/proc/meminfo")),
        nm_error=nm_err,
        netwatch=netwatch,
        push_outcome=push_outcome,
    )
    print(record, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
