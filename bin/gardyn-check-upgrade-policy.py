#!/usr/bin/env python3
"""Verify this host's unattended-upgrades policy against unattended-upgrades'
own matching code.

Why it exists. This machine's APT sources contain no Debian-Security origin —
only Raspbian and Raspberry Pi Foundation — while the stock
50unattended-upgrades matches `label=Debian-Security` and nothing else. Installed
as shipped, unattended-upgrades therefore enables itself, runs on schedule,
reports success and installs *nothing*, and every "is it configured?" check
still passes. This script exists because that failure is invisible.

What it does. Imports /usr/bin/unattended-upgrade (guarded by __main__, so the
import is inert beyond three lsb_release calls) and asks *its* functions the
questions, rather than reimplementing the matching and being wrong in a
different way.

Read-only. Takes no dpkg lock and installs nothing. One caveat: apt.Cache() will
rebuild /var/cache/apt/pkgcache.bin if it is stale AND this runs as root, so
prefer running it unprivileged.

Requires the system python3-apt; it will not import from a plain venv.

Every check carries both controls. A check that can only return "pass" is not a
check — and the desired answer here is mostly an *absence*, which is exactly
where a dead test and a real all-clear produce identical output.
"""
import sys
from importlib.machinery import SourceFileLoader

import apt
import apt_pkg

UU_PATH = "/usr/bin/unattended-upgrade"

# Root-owned path; exec'ing it is how we get the vendor's real matchers.
uu = SourceFileLoader("uu", UU_PATH).load_module()

apt_pkg.init_config()
apt_pkg.init_system()

failures = []


def check(label, got, want):
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")
    if not ok:
        failures.append(label)


def fail(label, why):
    print(f"  [FAIL] {label}: {why}")
    failures.append(label)


# --- 1. Origins ------------------------------------------------------------
# The stock patterns are wrong here. Assert what the policy IS, structurally,
# not merely that one bad substring is absent: `o=Debian,...` (short keys),
# `origin = Debian` (spaces), and the legacy Allowed-Origins key all express
# the same thing while defeating a substring test, and `o=*` defeats everything
# because values are matched with fnmatch.
print("=== 1. Origins-Pattern, as u-u itself resolves it ===")
try:
    allowed = uu.get_allowed_origins()
except Exception as exc:
    # substitute() uses Template.substitute, so a bad ${var} raises rather than
    # returning the unexpanded string. Without this guard that is a traceback
    # and NO failure is reported at all.
    allowed = []
    fail("Origins-Pattern parses", f"{type(exc).__name__}: {exc}")

for a in allowed:
    print(f"    {a}")

EXPECTED_ORIGINS = {"Raspbian", "Raspberry Pi Foundation"}
KEY_ALIASES = {"o": "origin", "l": "label", "a": "archive",
               "n": "codename", "c": "component", "site": "site"}


def parse_pattern(pat):
    """Token-parse one Origins-Pattern entry the way u-u's matcher reads it:
    comma-separated key=value, short keys allowed, whitespace insignificant."""
    out = {}
    for tok in pat.split(","):
        if "=" not in tok:
            continue
        k, _, v = tok.partition("=")
        k = k.strip().lower()
        out[KEY_ALIASES.get(k, k)] = v.strip()
    return out


parsed = [parse_pattern(a) for a in allowed]
origins_named = {p.get("origin") for p in parsed}

check("every pattern names an origin",
      all(p.get("origin") for p in parsed) and bool(parsed), True)
check("origins named are exactly the two this host actually has",
      origins_named, EXPECTED_ORIGINS)
# fnmatch means a wildcard anywhere trusts repos that do not exist yet.
check("no fnmatch wildcard in any origin value (o=* would trust ANY repo)",
      any(ch in (p.get("origin") or "") for p in parsed for ch in "*?["), False)
check("no pattern admits a Debian origin",
      any((p.get("origin") or "").lower() == "debian" for p in parsed), False)
check("substitutions expanded (no literal ${...} survived)",
      any("${" in a for a in allowed), False)

# --- 2. Do those patterns match this host's REAL packages? ------------------
print()
print("=== 2. Matching real installed packages ===")
cache = apt.Cache()

seen = {}   # (origin, label, component) -> allowed?, so a disagreeing
            # component or architecture cannot be hidden by last-write-wins.
for pkg in cache:
    inst = pkg.installed
    if inst is None:
        continue
    for origin in inst.origins:
        if not origin.origin:
            continue          # ('', 'now', '') = the local dpkg status file
        key = (origin.origin, origin.label, origin.component)
        if key not in seen:
            seen[key] = uu.is_allowed_origin(origin, allowed)

for (o, l, c), ok in sorted(seen.items()):
    print(f"    origin={o!r:26s} label={l!r:26s} component={c!r:10s} -> allowed={ok}")

check("at least one real origin was examined", len(seen) > 0, True)
check("EVERY real origin on this host is allowed (no component/arch disagrees)",
      sorted({ok for ok in seen.values()}), [True])
check("the origins seen are exactly the two expected",
      {o for (o, _, _) in seen}, EXPECTED_ORIGINS)

# NEGATIVE CONTROL, and a regression test for the original defect: take a REAL
# origin off this host and match it against the STOCK Debian patterns. It must
# NOT match — that mismatch is why the as-shipped install upgraded nothing.
STOCK_DEBIAN_PATTERNS = [
    "origin=Debian,codename=bookworm,label=Debian",
    "origin=Debian,codename=bookworm,label=Debian-Security",
    "origin=Debian,codename=bookworm-security,label=Debian-Security",
]
real_origin = None
for pkg in cache:
    if pkg.installed is None:
        continue
    for origin in pkg.installed.origins:
        if origin.origin:
            real_origin = origin
            break
    if real_origin:
        break

if real_origin is None:
    fail("negative control", "no real origin found to test against")
else:
    check("a real origin does NOT match the stock Debian patterns "
          "(reproduces the defect)",
          uu.is_allowed_origin(real_origin, STOCK_DEBIAN_PATTERNS), False)
    check("...and the SAME object DOES match ours "
          "(so the pattern changed, not the object)",
          uu.is_allowed_origin(real_origin, allowed), True)

# --- 3. Blacklist ----------------------------------------------------------
# Derived from what is actually installed, not a hardcoded wishlist: a fixed
# list cannot notice a boot or radio package this host gains later.
print()
print("=== 3. Package-Blacklist vs the host's real boot + radio path ===")
blacklist = apt_pkg.config.value_list("Unattended-Upgrade::Package-Blacklist")
for b in blacklist:
    print(f"    pattern: {b}")


def is_boot_path(name):
    """Packages whose failure costs a physical trip: they either prevent boot
    or remove the only network path into a headless, Wi-Fi-only host."""
    return (name.startswith(("linux-image", "linux-headers",
                             "raspberrypi-kernel", "raspberrypi-bootloader"))
            or name in {"raspi-firmware", "rpi-eeprom", "u-boot-rpi",
                        "firmware-brcm80211"})


installed = sorted(p.name for p in cache if p.installed is not None)
derived_block = [n for n in installed if is_boot_path(n)]
print(f"    derived from installed set: {derived_block}")
check("the derived boot/radio set is non-empty (else this section is vacuous)",
      len(derived_block) > 0, True)

for name in derived_block:
    check(f"BLOCKED  {name}", uu.is_pkgname_in_blacklist(name, blacklist), True)

# Names not installed today but which name the same artifacts, plus a
# versioned kernel, which is what actually appears in an upgrade set.
for name in ("linux-image-6.12.96+rpt-rpi-v6", "raspberrypi-kernel",
             "raspberrypi-bootloader", "u-boot-rpi"):
    check(f"BLOCKED  {name} (not installed; regression guard)",
          uu.is_pkgname_in_blacklist(name, blacklist), True)

# NEGATIVE CONTROL for over-blocking. Chosen adversarially: the interpreter
# stack this host's MQTT/camera service runs on, plus the security packages the
# whole exercise exists to keep current. A stray "python3-.*" or "libc.*"
# pattern would silently freeze these while every check above stayed green.
must_allow = ["openssl", "libssl3", "openssh-server", "sudo", "systemd",
              "libc6", "linux-libc-dev", "linux-base", "python3",
              "python3-apt", "python3-dbus", "mosquitto-clients", "pigpio",
              "libcamera0", "ca-certificates", "initramfs-tools"]
for name in must_allow:
    check(f"allowed  {name}", uu.is_pkgname_in_blacklist(name, blacklist), False)

blocked_count = sum(1 for n in installed
                    if uu.is_pkgname_in_blacklist(n, blacklist))
print(f"    blacklist matches {blocked_count} of {len(installed)} installed packages")
check("blacklist is not swallowing the whole system",
      blocked_count < len(installed) * 0.25, True)

# --- 4. Will it run at all, and stay inside the lines? ---------------------
# Everything above is moot if the master switch is off or a strict whitelist
# matches nothing — both of which leave every check above green.
print()
print("=== 4. Does it actually run, and stay constrained? ===")
check("APT::Periodic::Update-Package-Lists is on",
      apt_pkg.config.find("APT::Periodic::Update-Package-Lists", "0"), "1")
check("APT::Periodic::Unattended-Upgrade is on (master switch)",
      apt_pkg.config.find("APT::Periodic::Unattended-Upgrade", "0"), "1")

whitelist = apt_pkg.config.value_list("Unattended-Upgrade::Package-Whitelist")
check("no strict whitelist silently disabling everything",
      apt_pkg.config.find_b("Unattended-Upgrade::Package-Whitelist-Strict",
                            False) and not whitelist, False)

# Defaults below mirror u-u's own, so deleting a line is reported the way u-u
# would actually behave rather than the way we hope it would.
check("Automatic-Reboot off (u-u default False)",
      apt_pkg.config.find_b("Unattended-Upgrade::Automatic-Reboot", False), False)
check("Remove-Unused-Kernel-Packages off (u-u default True — must be set)",
      apt_pkg.config.find_b(
          "Unattended-Upgrade::Remove-Unused-Kernel-Packages", True), False)
check("Remove-Unused-Dependencies off",
      apt_pkg.config.find_b(
          "Unattended-Upgrade::Remove-Unused-Dependencies", True), False)
check("Allow-downgrade off",
      apt_pkg.config.find_b("Unattended-Upgrade::Allow-downgrade", False), False)
# No MTA here, so syslog is the only route by which a failing run is ever seen.
check("SyslogEnable on (the only audit trail on this host)",
      apt_pkg.config.find_b("Unattended-Upgrade::SyslogEnable", False), True)

print()
if failures:
    print(f"RESULT: {len(failures)} FAILURE(S)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("RESULT: all checks passed, both controls behaved")
sys.exit(0)
