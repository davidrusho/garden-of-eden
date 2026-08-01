#!/usr/bin/env python3
# Reviewed: 2026-07-31 against 65621ff (T-484)
"""Verify one host's unattended-upgrades policy against unattended-upgrades'
own matching code.

READ THIS FIRST IF YOU ARE NOT ME. This script encodes ONE HOST'S policy, not a
general-purpose audit. `EXPECTED_ORIGINS`, `EXPECTED_PATTERNS`, `MUST_ALLOW`,
`is_boot_path` and the required-timer list are all specific to a headless
Raspberry Pi OS (bookworm) machine whose sources are Raspbian plus the
Raspberry Pi Foundation archive and whose only network path is Wi-Fi. Run it
unchanged on Ubuntu or Debian proper and it will print a wall of confident
FAILs that say nothing about your host - the origins will not match, the boot
path is named differently, and the boot-path packages do not exist. The
POLICY CONSTANTS block below is the part you would edit; everything under
`run_checks` is generic.

Why it exists. This machine's APT sources contain no Debian-Security origin -
only Raspbian and Raspberry Pi Foundation - while the stock
50unattended-upgrades matches `label=Debian-Security` and nothing else. Installed
as shipped, unattended-upgrades therefore enables itself, runs on schedule,
reports success and installs *nothing*, and every "is it configured?" check
still passes. This script exists because that failure is invisible.

What it does. Imports /usr/bin/unattended-upgrade (guarded by __main__, so the
import is inert beyond three lsb_release calls) and asks *its* functions the
questions, rather than reimplementing the matching and being wrong in a
different way. The checks themselves are pure functions of a `PolicyState`
snapshot, so the same code runs against live apt state on the Pi and against
captured fixtures in tests/ - which is the only way a mutation battery over
this policy can exist without writing drop-ins into a live /etc/apt.

Read-only. Takes no dpkg lock, installs nothing, and writes no bytecode. Two
caveats: apt.Cache() will rebuild /var/cache/apt/pkgcache.bin if it is stale AND
this runs as root, so prefer running it unprivileged.

Requires the system python3-apt; it will not import from a plain venv. Importing
this module does NOT require python3-apt - the apt imports live inside
collect_state() so the check layer stays testable off-host.

    gardyn-check-upgrade-policy.py                 # check live state
    gardyn-check-upgrade-policy.py --dump-state    # capture a fixture
    gardyn-check-upgrade-policy.py --state f.json  # check a captured fixture

Every check carries both controls. A check that can only return "pass" is not a
check - and the desired answer here is mostly an *absence*, which is exactly
where a dead test and a real all-clear produce identical output.
"""
import argparse
import fnmatch
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional

UU_PATH = "/usr/bin/unattended-upgrade"

# ===========================================================================
# POLICY CONSTANTS - this block is host-specific. See the header.
# ===========================================================================

EXPECTED_ORIGINS = {"Raspbian", "Raspberry Pi Foundation"}

#: The release pin, in full. Asserting only `origin=` is not enough: a pattern
#: carrying no `codename=` matches ANY suite from that origin, so a half-finished
#: "move to trixie" edit of sources.list makes cross-release packages eligible
#: unattended while every origin check stays green.
def expected_patterns(distro_codename):
    # type: (str) -> List[Dict[str, str]]
    return [
        {"origin": "Raspbian", "codename": distro_codename, "label": "Raspbian"},
        {"origin": "Raspberry Pi Foundation", "codename": distro_codename},
    ]


BOOT_PATH_PREFIXES = ("linux-image", "linux-headers",
                      "raspberrypi-kernel", "raspberrypi-bootloader")
BOOT_PATH_NAMES = frozenset({"raspi-firmware", "rpi-eeprom", "u-boot-rpi",
                             "firmware-brcm80211"})

#: Names that are not installed today but name the same artifacts. A
#: distribution that reintroduced them would reopen the hole silently.
BOOT_PATH_REGRESSION_GUARDS = ("linux-image-6.12.96+rpt-rpi-v6",
                               "raspberrypi-kernel", "raspberrypi-bootloader",
                               "u-boot-rpi")

#: NEGATIVE CONTROL for over-blocking, chosen adversarially: the interpreter
#: stack this host's MQTT/camera service runs on, the security packages the
#: whole exercise exists to keep current, and the Wi-Fi stack - a headless
#: Wi-Fi-only host whose logged failure mode is "dropped off the network" must
#: keep its network daemons patchable. A stray "python3-.*", "libc.*" or
#: "network-.*" pattern would silently freeze these while every other check
#: stayed green. Blacklisting anything here is a policy decision that has to be
#: made deliberately, by editing this list first.
MUST_ALLOW = ["openssl", "libssl3", "openssh-server", "sudo", "systemd",
              "libc6", "linux-libc-dev", "linux-base", "python3",
              "python3-apt", "python3-dbus", "mosquitto-clients", "pigpio",
              "libcamera0", "ca-certificates", "initramfs-tools",
              "network-manager", "wpasupplicant", "raspberrypi-sys-mods",
              "raspberrypi-net-mods"]

#: The stock Debian patterns, kept as a regression test for the original
#: defect: a REAL origin from this host must NOT match them.
STOCK_DEBIAN_PATTERNS = [
    "origin=Debian,codename=bookworm,label=Debian",
    "origin=Debian,codename=bookworm,label=Debian-Security",
    "origin=Debian,codename=bookworm-security,label=Debian-Security",
]

#: apt.systemd.daily is what actually invokes unattended-upgrade. Both timers
#: masked or disabled is indistinguishable, from inside apt.conf, from a
#: perfectly configured host that never runs.
REQUIRED_TIMERS = ("apt-daily.timer", "apt-daily-upgrade.timer")
RUNNING_TIMER_STATES = ("enabled", "enabled-runtime")

# ===========================================================================
# VENDOR FACTS - transcribed from /usr/bin/unattended-upgrade 2.9.1+nmu3 and
# /usr/lib/apt/apt.systemd.daily. Each carries the line it came from, because a
# default asserted from memory is the way this whole class of check goes quietly
# wrong: the wrong default reports the config's ACTUAL behaviour incorrectly.
# ===========================================================================

#: key -> (u-u's own default, what this host requires)
BOOL_POLICY = [
    # Automatic-Reboot: :1660. A reboot resets the light PWM to 0.
    ("Unattended-Upgrade::Automatic-Reboot", False, False,
     "Automatic-Reboot off"),
    # Remove-Unused-Kernel-Packages: :2300, default True - must be SET off.
    ("Unattended-Upgrade::Remove-Unused-Kernel-Packages", True, False,
     "Remove-Unused-Kernel-Packages off (u-u default True - must be set)"),
    # Remove-Unused-Dependencies: :2325 and :2394, default FALSE.
    ("Unattended-Upgrade::Remove-Unused-Dependencies", False, False,
     "Remove-Unused-Dependencies off"),
    # Remove-New-Unused-Dependencies: :2406, default TRUE, and it is the `elif`
    # branch that runs precisely BECAUSE Remove-Unused-Dependencies is false.
    # do_auto_remove() (:1850) calls mark_delete() directly, so
    # is_pkg_change_allowed() is never reached: neither Package-Blacklist nor
    # `apt-mark hold` protects a package on the REMOVAL path, only on upgrade.
    # Left at its default, an automatic run may delete boot-path packages this
    # host has explicitly frozen against upgrade.
    ("Unattended-Upgrade::Remove-New-Unused-Dependencies", True, False,
     "Remove-New-Unused-Dependencies off (u-u default True; autoremove "
     "honours neither the blacklist nor apt-mark hold)"),
    # Allow-downgrade: :1708.
    ("Unattended-Upgrade::Allow-downgrade", False, False, "Allow-downgrade off"),
    # InstallOnShutdown: :2351. Would run dpkg during the deliberate reboots
    # this host's policy describes, unwatched, on an SD card.
    ("Unattended-Upgrade::InstallOnShutdown", False, False,
     "InstallOnShutdown off (dpkg during shutdown is unwatched)"),
    # MinimalSteps: :2463, default True, ANDed with the misspelled key.
    # False makes the upgrade one uninterruptible transaction, which widens the
    # SD-corruption window and defeats the graceful-stop handler.
    ("Unattended-Upgrade::MinimalSteps", True, True, "MinimalSteps on"),
    ("Unattended-Upgrades::MinimalSteps", True, True,
     "MinimalSteps on (u-u ANDs the misspelled key too)"),
    # SyslogEnable: :1622. No MTA here, so syslog is the only route by which a
    # failing run is ever seen.
    ("Unattended-Upgrade::SyslogEnable", False, True,
     "SyslogEnable on (the only audit trail on this host)"),
]

#: apt.conf key aliases as match_whitelist_string() reads them (:820-850).
KEY_ALIASES = {"o": "origin", "l": "label", "a": "archive", "suite": "archive",
               "n": "codename", "c": "component", "site": "site"}
KNOWN_KEYS = frozenset(KEY_ALIASES.values()) | frozenset(KEY_ALIASES)

#: apt's StringToBool: case-insensitive word lists, plus the integers 0 and 1;
#: ANYTHING else silently returns the caller's default. Verified against
#: apt_pkg.config.find_b on the host, not read off a doc page.
_APT_TRUE = frozenset({"yes", "true", "with", "on", "enable", "1"})
_APT_FALSE = frozenset({"no", "false", "without", "off", "disable", "0"})


def apt_bool(raw, default):
    # type: (Optional[str], bool) -> bool
    """Resolve an apt.conf scalar the way apt_pkg.config.find_b would."""
    if raw is None:
        return default
    token = raw.strip().lower()
    if token in _APT_TRUE:
        return True
    if token in _APT_FALSE:
        return False
    return default


class PatternError(ValueError):
    """A pattern u-u itself would reject, or silently never match."""


def parse_pattern(pat):
    # type: (str) -> Dict[str, str]
    """Token-parse one Origins-Pattern entry the way u-u's matcher reads it.

    Deliberately as strict as match_whitelist_string() (:804-861): u-u raises
    on a token with no `=` and on a token with two, and raises
    UnknownMatcherError on a key it does not know. The previous version of this
    function skipped all three, which is a parser/vendor disagreement in the
    worst direction - the checker calls a config fine that u-u dies on.
    """
    stripped = pat.strip()
    if not stripped:
        raise PatternError("empty pattern; u-u warns and matches nothing")
    # u-u html-quotes an escaped comma before splitting, so "\," is a literal
    # comma in a value rather than a token separator.
    quoted = stripped.replace("\\,", "%2C")
    out = {}  # type: Dict[str, str]
    for token in quoted.split(","):
        parts = token.split("=")
        if len(parts) != 2:
            raise PatternError(
                "token %r has %d '=' (u-u raises ValueError unpacking it)"
                % (token.replace("%2C", ","), len(parts) - 1))
        key, value = [p.strip().replace("%2C", ",") for p in parts]
        key = key.lower()
        if key not in KNOWN_KEYS:
            raise PatternError(
                "unknown matcher %r (u-u raises UnknownMatcherError)" % key)
        out[KEY_ALIASES.get(key, key)] = value
    return out


def is_boot_path(name):
    # type: (str) -> bool
    """Packages whose failure costs a physical trip: they either prevent boot
    or remove the only network path into a headless, Wi-Fi-only host.

    The architecture qualifier is stripped first. python-apt reports a foreign
    architecture's packages as `name:arch`, and this host has arm64 as a
    foreign architecture with arm64 kernel packages installed, so an exact-name
    test against the bare name silently misses them.
    """
    bare = name.split(":")[0]
    return bare.startswith(BOOT_PATH_PREFIXES) or bare in BOOT_PATH_NAMES


# ===========================================================================
# STATE - everything the checks read, in one snapshot that can be captured to
# JSON and replayed. Nothing below here touches apt.
# ===========================================================================


@dataclass
class Origin:
    """Duck-types both apt.package.Origin and apt_pkg.PackageFile for the
    attributes match_whitelist_string() reads."""
    origin: str = ""
    label: str = ""
    component: str = ""
    archive: str = ""
    codename: str = ""
    site: str = ""
    architecture: str = ""
    filename: str = ""


@dataclass
class PolicyState:
    distro_codename: str = ""
    #: get_allowed_origins() output - post-substitution, legacy entries first.
    allowed_origins: List[str] = field(default_factory=list)
    #: The raw legacy key. It PREPENDS to the list above (:787-796), so an entry
    #: here widens what is eligible without changing any Origins-Pattern line.
    legacy_allowed_origins: List[str] = field(default_factory=list)
    #: One entry per configured Packages file. This, not the set of origins the
    #: INSTALLED versions came from, is what u-u gates candidate versions on
    #: (ver_in_allowed_origin, :996). The installed-versions view collapses to
    #: component=main on this host and therefore cannot see a component
    #: narrowing that permanently excludes non-free, contrib and rpi.
    index_origins: List[Origin] = field(default_factory=list)
    installed: List[str] = field(default_factory=list)
    holds: List[str] = field(default_factory=list)
    blacklist: List[str] = field(default_factory=list)
    whitelist: List[str] = field(default_factory=list)
    #: Raw apt.conf scalars. None means the key is ABSENT, which is the case
    #: the vendor defaults exist to resolve.
    conf: Dict[str, Optional[str]] = field(default_factory=dict)
    #: systemctl is-enabled per unit; "" means the unit does not exist.
    timers: Dict[str, str] = field(default_factory=dict)

    def to_json(self):
        # type: () -> str
        return json.dumps(asdict(self), indent=1, sort_keys=True)

    @classmethod
    def from_dict(cls, data):
        # type: (dict) -> "PolicyState"
        data = dict(data)
        data["index_origins"] = [Origin(**o) for o in data.get("index_origins", [])]
        return cls(**data)


# --- reference matchers ----------------------------------------------------
# Used only when the vendor module is unavailable (i.e. in tests, off-host).
# They are transcribed from unattended-upgrade, and `check_matchers_agree`
# below re-derives that claim against the real vendor functions on every live
# run - so a divergence surfaces on the host rather than rotting in the suite.

def reference_is_allowed_origin(origin, allowed_origins):
    # type: (Origin, List[str]) -> bool
    if (origin.component == "now" and origin.archive == "now"
            and not origin.label and not origin.site):
        return True
    for allowed in allowed_origins:
        if _reference_match(allowed, origin):
            return True
    return False


def _reference_match(pattern, origin):
    # type: (str, Origin) -> bool
    parsed = parse_pattern(pattern)   # raises exactly where u-u raises
    for key, value in parsed.items():
        if not fnmatch.fnmatch(getattr(origin, key) or "", value):
            return False
    return bool(parsed)


def reference_is_pkgname_in_blacklist(pkgname, blacklist):
    # type: (str, List[str]) -> bool
    return any(re.match(expr, pkgname) for expr in blacklist)


# ===========================================================================
# REPORT
# ===========================================================================


class Report:
    def __init__(self):
        self.failures = []  # type: List[str]
        self.lines = []     # type: List[str]

    def emit(self, text=""):
        self.lines.append(text)

    def section(self, title):
        self.emit()
        self.emit("=== %s ===" % title)

    def check(self, label, got, want):
        ok = got == want
        self.emit("  [%s] %s: got=%r want=%r"
                  % ("PASS" if ok else "FAIL", label, got, want))
        if not ok:
            self.failures.append(label)
        return ok

    def fail(self, label, why):
        self.emit("  [FAIL] %s: %s" % (label, why))
        self.failures.append(label)

    def guard(self, label, fn, *args):
        """Run a matcher, turning any exception into a FAIL row.

        Without this an UnknownMatcherError, a ValueError from a malformed
        token or an re.error from a glob written where a regex was meant
        aborts the run mid-section: the exit code is still non-zero, but the
        remaining sections never execute and a human scanning for [FAIL] sees
        none. The crash is the loud half; the checks that never ran are the
        quiet half.
        """
        try:
            return fn(*args)
        except Exception as exc:                       # noqa: BLE001
            self.fail(label, "%s: %s" % (type(exc).__name__, exc))
            return None

    def text(self):
        return "\n".join(self.lines)


# ===========================================================================
# CHECKS - pure functions of (PolicyState, matchers) -> Report
# ===========================================================================


def check_origins(state, report):
    report.section("1. Origins-Pattern, as u-u itself resolves it")
    for entry in state.allowed_origins:
        report.emit("    %s" % entry)

    report.check("Origins-Pattern resolves to at least one pattern",
                 bool(state.allowed_origins), True)
    # get_allowed_origins() puts the legacy key's entries FIRST, so a single
    # `Allowed-Origins { "Raspbian bookworm-staging"; }` line widens what is
    # eligible while leaving every Origins-Pattern line untouched and every
    # origin-name check green.
    report.check("no legacy Allowed-Origins entries (they PREPEND to the list)",
                 list(state.legacy_allowed_origins), [])

    parsed = []
    for entry in state.allowed_origins:
        try:
            parsed.append(parse_pattern(entry))
        except PatternError as exc:
            report.fail("Origins-Pattern entry parses: %r" % entry, str(exc))

    origins_named = {p.get("origin") for p in parsed}
    report.check("every pattern names an origin",
                 all(p.get("origin") for p in parsed) and bool(parsed), True)
    report.check("origins named are exactly the two this host actually has",
                 origins_named, EXPECTED_ORIGINS)
    # Values are matched with fnmatch, so a wildcard ANYWHERE - not only in the
    # origin - trusts repos that do not exist yet.
    report.check("no fnmatch wildcard in any pattern value",
                 any(ch in value
                     for p in parsed for value in p.values() for ch in "*?["),
                 False)
    report.check("no pattern admits a Debian origin",
                 any((p.get("origin") or "").lower() == "debian"
                     for p in parsed), False)
    report.check("substitutions expanded (no literal ${...} survived)",
                 any("${" in a for a in state.allowed_origins), False)

    # THE release pin, asserted whole. Checking only `origin` leaves the
    # codename and label free to be deleted with zero FAILs, and a pattern with
    # no codename matches any suite from that origin.
    def normalise(dicts):
        return sorted(sorted(d.items()) for d in dicts)

    report.check("the release pin is intact: exactly the expected patterns, "
                 "each complete (origin + codename + label)",
                 normalise(parsed),
                 normalise(expected_patterns(state.distro_codename)))
    return report


def check_index_origins(state, report, origin_allowed):
    report.section("2. Matching this host's real package indexes")
    real = [o for o in state.index_origins if o.origin]
    for o in sorted(real, key=lambda o: (o.origin, o.component, o.architecture)):
        report.emit("    origin=%-26r label=%-26r component=%-10r arch=%-7r"
                    % (o.origin, o.label, o.component, o.architecture))

    report.check("at least one real package index was examined",
                 len(real) > 0, True)

    verdicts = {}
    for o in real:
        key = (o.origin, o.label, o.component, o.archive, o.codename,
               o.architecture)
        if key not in verdicts:
            verdicts[key] = report.guard(
                "index origin matches: %s/%s/%s" % (o.origin, o.component,
                                                    o.architecture),
                origin_allowed, o, state.allowed_origins)

    for key, ok in sorted(verdicts.items(), key=lambda kv: str(kv[0])):
        report.emit("      %-24s %-10s %-7s -> allowed=%s"
                    % (key[0], key[2], key[5], ok))

    # This is the check a component narrowing has to trip. The installed
    # versions' origins are all component=main on this host, so the same
    # assertion against THAT corpus passes while non-free, contrib and rpi are
    # permanently excluded - firmware-brcm80211 has a non-free candidate.
    #
    # Named, not reduced to a set of verdicts: a matcher that raised leaves a
    # None here, and `sorted({True, None})` is a TypeError - a check that
    # crashes on exactly the malformed config it exists to report.
    report.check("EVERY configured package index is allowed (no component or "
                 "architecture silently excluded)",
                 sorted("%s/%s/%s" % (k[0], k[2], k[5])
                        for k, v in verdicts.items() if v is not True), [])
    report.check("the origins across those indexes are exactly the two expected",
                 {k[0] for k in verdicts}, EXPECTED_ORIGINS)

    if not real:
        report.fail("negative control", "no real origin found to test against")
        return report

    probe = real[0]
    report.check("a real origin does NOT match the stock Debian patterns "
                 "(reproduces the defect)",
                 report.guard("negative control", origin_allowed, probe,
                              STOCK_DEBIAN_PATTERNS), False)
    report.check("...and the SAME object DOES match ours "
                 "(so the pattern changed, not the object)",
                 report.guard("positive control", origin_allowed, probe,
                              state.allowed_origins), True)
    return report


def check_blacklist(state, report, name_blacklisted):
    report.section("3. Package-Blacklist vs the host's real boot + radio path")
    for entry in state.blacklist:
        report.emit("    pattern: %s" % entry)

    report.check("Package-Blacklist is non-empty", bool(state.blacklist), True)
    # Blacklist entries are Python regexes, not globs - the config file says so
    # in its own comment, which is exactly why a "*-firmware" ends up here. u-u
    # would raise re.error at match time, mid-run.
    for entry in state.blacklist:
        try:
            re.compile(entry)
        except re.error as exc:
            report.fail("blacklist pattern compiles: %r" % entry, str(exc))

    derived_block = [n for n in state.installed if is_boot_path(n)]
    report.emit("    derived from installed set: %s" % derived_block)
    report.check("the derived boot/radio set is non-empty "
                 "(else this section is vacuous)",
                 len(derived_block) > 0, True)

    for name in derived_block:
        report.check("BLOCKED  %s" % name,
                     report.guard("blacklist match %s" % name,
                                  name_blacklisted, name, state.blacklist),
                     True)
    for name in BOOT_PATH_REGRESSION_GUARDS:
        report.check("BLOCKED  %s (not installed; regression guard)" % name,
                     report.guard("blacklist match %s" % name,
                                  name_blacklisted, name, state.blacklist),
                     True)
    for name in MUST_ALLOW:
        report.check("allowed  %s" % name,
                     report.guard("blacklist match %s" % name,
                                  name_blacklisted, name, state.blacklist),
                     False)

    # Two-sided and exact, which the old "< 25% of installed" heuristic was
    # not: on this host that threshold sat at 161 packages against 18 real
    # matches, so every over-block broad enough to cross it was already caught
    # by a named MUST_ALLOW entry firing first. This says the blacklist blocks
    # the boot path and NOTHING else installed - it fires on a pattern that
    # swallows one extra package, which is the realistic mistake.
    blocked = sorted(n for n in state.installed
                     if reference_or(name_blacklisted, n, state.blacklist))
    report.check("the blacklist blocks the boot path and nothing else installed",
                 blocked, sorted(derived_block))

    # The config names `apt-mark hold` as its second layer. Never verifying it
    # left that claim half-checked. Two-sided: holds must exist, and every hold
    # must be explicable - an unexplained hold silently freezes a package
    # against upgrade with every other check green.
    report.check("apt-mark holds are set (the documented second layer)",
                 len(state.holds) > 0, True)
    report.check("every held package is on the boot path (no unexplained hold)",
                 sorted(h for h in state.holds if not is_boot_path(h)), [])
    report.check("every held package is blacklisted too (the two layers agree)",
                 sorted(h for h in state.holds
                        if not reference_or(name_blacklisted, h,
                                            state.blacklist)), [])
    return report


def reference_or(matcher, name, blacklist):
    """Call a matcher that may raise; a raising pattern counts as no match.

    The raising case is already reported by the compile check above, so this
    only exists to stop one bad pattern from aborting the set comparisons.
    """
    try:
        return bool(matcher(name, blacklist))
    except Exception:                                  # noqa: BLE001
        return False


def check_runtime(state, report):
    report.section("4. Does it actually run, and stay constrained?")

    def conf(key, default=None):
        return state.conf.get(key, default)

    # apt.systemd.daily reads APT::Periodic::Enable FIRST and exit 0s on "0",
    # before Update-Package-Lists or Unattended-Upgrade are ever consulted. One
    # line disables the whole mechanism and leaves both switches below reading
    # "1", which is how a green report and a machine that never upgrades again
    # coexist.
    report.check("APT::Periodic::Enable is not 0 (0 makes apt.systemd.daily "
                 "exit before anything runs)",
                 conf("APT::Periodic::Enable", "1") != "0", True)
    report.check("APT::Periodic::Update-Package-Lists is on",
                 conf("APT::Periodic::Update-Package-Lists", "0"), "1")
    report.check("APT::Periodic::Unattended-Upgrade is on (master switch)",
                 conf("APT::Periodic::Unattended-Upgrade", "0"), "1")

    # Same class of hole one layer down: apt.conf cannot tell you the timer
    # that invokes apt.systemd.daily has been masked.
    for unit in REQUIRED_TIMERS:
        state_word = state.timers.get(unit, "")
        report.check("%s is enabled (masked or disabled is invisible from "
                     "apt.conf)" % unit,
                     state_word in RUNNING_TIMER_STATES, True)

    # A strict whitelist that matches nothing upgrades nothing, forever. The
    # previous check asserted `not (strict and whitelist_empty)` - but an empty
    # whitelist turns the feature OFF (is_pkgname_in_whitelist, :1014), so it
    # guarded the benign half and could not fire on the harmful one. This host
    # runs no whitelist at all, so assert that directly, which covers both.
    report.check("Package-Whitelist-Strict off",
                 apt_bool(conf("Unattended-Upgrade::Package-Whitelist-Strict"),
                          False), False)
    report.check("Package-Whitelist empty (this host runs no whitelist)",
                 list(state.whitelist), [])

    for key, vendor_default, required, label in BOOL_POLICY:
        report.check("%s [u-u default %s]" % (label, vendor_default),
                     apt_bool(conf(key), vendor_default), required)
    return report


def check_matchers_agree(state, report, vendor_origin, vendor_blacklist):
    """Live-only: the reference matchers used by the off-host tests must still
    agree with the vendor's own, on this host's real corpus."""
    report.section("5. Reference matchers still agree with the vendor's")
    origin_mismatch = [
        (o.origin, o.component) for o in state.index_origins
        if reference_is_allowed_origin(o, state.allowed_origins)
        != vendor_origin(o, state.allowed_origins)]
    report.check("reference_is_allowed_origin agrees with u-u on every index",
                 origin_mismatch, [])
    name_mismatch = [
        n for n in state.installed
        if reference_is_pkgname_in_blacklist(n, state.blacklist)
        != vendor_blacklist(n, state.blacklist)]
    report.check("reference_is_pkgname_in_blacklist agrees with u-u on every "
                 "installed package", name_mismatch, [])
    return report


def run_checks(state, origin_allowed=None, name_blacklisted=None):
    # type: (PolicyState, Optional[Callable], Optional[Callable]) -> Report
    """Every check, against a snapshot. No apt, no network, no filesystem."""
    report = Report()
    origin_allowed = origin_allowed or reference_is_allowed_origin
    name_blacklisted = name_blacklisted or reference_is_pkgname_in_blacklist
    check_origins(state, report)
    check_index_origins(state, report, origin_allowed)
    check_blacklist(state, report, name_blacklisted)
    check_runtime(state, report)
    return report


# ===========================================================================
# COLLECTION - the only part that needs apt, python3-apt and the Pi.
# ===========================================================================

CONF_KEYS = ["APT::Periodic::Enable",
             "APT::Periodic::Update-Package-Lists",
             "APT::Periodic::Unattended-Upgrade",
             "Unattended-Upgrade::Package-Whitelist-Strict"] + \
            [key for key, _d, _r, _l in BOOL_POLICY]


def load_vendor(path=UU_PATH):
    """Import the vendor script by path, without writing bytecode into it.

    SourceFileLoader.load_module() - the previous form - caches bytecode beside
    the source, so running this as root created /usr/bin/__pycache__, a
    dpkg-unowned directory under a root-owned bin. `sys.dont_write_bytecode` is
    the part that actually prevents it; exec_module() alone would still cache.
    load_module() is also deprecated in favour of exec_module().
    """
    import importlib.machinery
    import importlib.util
    sys.dont_write_bytecode = True
    # The loader must be named explicitly: /usr/bin/unattended-upgrade has no
    # .py suffix, and spec_from_file_location() returns None rather than
    # raising when it cannot infer a loader from the extension.
    loader = importlib.machinery.SourceFileLoader("uu", path)
    spec = importlib.util.spec_from_file_location("uu", path, loader=loader)
    if spec is None or spec.loader is None:
        raise ImportError("cannot load %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect_state(uu):
    # type: (object) -> PolicyState
    import apt
    import apt_pkg
    import subprocess

    apt_pkg.init_config()
    apt_pkg.init_system()

    # `exists`, not a sentinel default: absent is a distinct state from
    # present-and-empty, and it is the one the vendor defaults resolve.
    conf = {}
    for key in CONF_KEYS:
        conf[key] = (apt_pkg.config.find(key)
                     if apt_pkg.config.exists(key) else None)

    index_origins = []
    for pkg_file in apt_pkg.Cache(None).file_list:
        index_origins.append(Origin(
            origin=pkg_file.origin or "", label=pkg_file.label or "",
            component=pkg_file.component or "", archive=pkg_file.archive or "",
            codename=pkg_file.codename or "", site=pkg_file.site or "",
            architecture=pkg_file.architecture or "",
            filename=pkg_file.filename or ""))

    cache = apt.Cache()
    installed = sorted(p.name for p in cache if p.installed is not None)

    holds = []
    try:
        out = subprocess.run(["apt-mark", "showhold"], capture_output=True,
                             text=True, check=True).stdout
        holds = sorted(line.strip() for line in out.splitlines() if line.strip())
    except Exception as exc:                           # noqa: BLE001
        print("  [WARN] apt-mark showhold failed: %s" % exc, file=sys.stderr)

    timers = {}
    for unit in REQUIRED_TIMERS:
        proc = subprocess.run(["systemctl", "is-enabled", unit],
                              capture_output=True, text=True)
        timers[unit] = proc.stdout.strip()

    try:
        allowed = uu.get_allowed_origins()
    except Exception as exc:                           # noqa: BLE001
        # substitute() uses Template.substitute, so a bad ${var} raises rather
        # than returning the unexpanded string. Reported as a state we can
        # still check against, not a traceback.
        print("  [WARN] get_allowed_origins() raised: %s: %s"
              % (type(exc).__name__, exc), file=sys.stderr)
        allowed = []

    return PolicyState(
        distro_codename=uu.get_distro_codename(),
        allowed_origins=list(allowed),
        legacy_allowed_origins=list(apt_pkg.config.value_list(
            "Unattended-Upgrade::Allowed-Origins")),
        index_origins=index_origins,
        installed=installed,
        holds=holds,
        blacklist=list(apt_pkg.config.value_list(
            "Unattended-Upgrade::Package-Blacklist")),
        whitelist=list(apt_pkg.config.value_list(
            "Unattended-Upgrade::Package-Whitelist")),
        conf=conf,
        timers=timers,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", metavar="FILE",
                        help="check a captured state file instead of live apt")
    parser.add_argument("--dump-state", action="store_true",
                        help="print the collected state as JSON and exit")
    args = parser.parse_args(argv)

    if args.state:
        with open(args.state) as handle:
            state = PolicyState.from_dict(json.load(handle))
        report = run_checks(state)
    else:
        uu = load_vendor()
        state = collect_state(uu)
        if args.dump_state:
            print(state.to_json())
            return 0
        report = run_checks(state, uu.is_allowed_origin,
                            uu.is_pkgname_in_blacklist)
        check_matchers_agree(state, report, uu.is_allowed_origin,
                             uu.is_pkgname_in_blacklist)

    print(report.text())
    print()
    if report.failures:
        print("RESULT: %d FAILURE(S)" % len(report.failures))
        for name in report.failures:
            print("  - %s" % name)
        return 1
    print("RESULT: all checks passed, both controls behaved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
