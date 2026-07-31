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

Design note: every parser here is a pure function over a string so the failure
branches the real host cannot be made to produce on demand — a missing
vcgencmd, a radio that has vanished from /proc/net/wireless, malformed hex —
are still covered by tests. main() is the only part that touches the system.
"""
from __future__ import annotations

import shutil
import subprocess
import sys

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
                  nm_error: str | None = None) -> str:
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

    record = format_record(
        throttled=throttled,
        temp_c=parse_soc_temp(_read("/sys/class/thermal/thermal_zone0/temp")),
        wireless=parse_proc_net_wireless(_read("/proc/net/wireless")),
        nm_state=parse_nmcli_state(nm_out),
        uptime_s=parse_uptime(_read("/proc/uptime")),
        mem_avail_mb=parse_mem_available_mb(_read("/proc/meminfo")),
        nm_error=nm_err,
    )
    print(record, flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
