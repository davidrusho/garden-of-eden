#!/usr/bin/env python3
"""Mutation battery for bin/gardyn-check-upgrade-policy.py (T-484).

Two halves, because they answer two different questions.

  CONFIG_MUTANTS mutate the POLICY, not the code, and assert the checker goes
  red. Each one reproduces a real way unattended-upgrades gets disabled or
  subverted while a naive check still reports green - the release pin deleted,
  APT::Periodic::Enable set to 0, a strict whitelist that matches nothing, the
  timers masked. They run against a captured fixture, so nothing here writes a
  drop-in into a live /etc/apt/apt.conf.d, which is how the first pass of this
  audit was done and is not repeatable.

  CODE_MUTANTS mutate the checker itself and assert the unittest suite goes
  red. A green suite proves nothing until it has been shown capable of failing;
  a mutation that survives means the corresponding test is decorative.

THREE CONTROLS run before any result is read, because a mutation battery
inverts the usual false-all-clear. A mutant is scored by whether the run
FAILED, so a broken scorer reports every mutant caught - the most reassuring
output available, and the one that goes straight into a summary as proof of
rigour.

  Control A - the clean fixture must score GREEN and the clean tree's suite
              must pass. On its own this is worthless: it is scored by the same
              path that may be broken.
  Control B - a deliberately broken policy must score RED, and a deliberately
              broken assertion in the tree must make the suite RED.
  Control C - a mutation known to change nothing must be reported SURVIVED.
              A and B together still cannot catch a scorer that returns "red"
              unconditionally; C is the only one that can.

Run from the repo root:

    python3 tests/mutate_upgrade_policy.py

Exit 0 means every mutation behaved as declared. Exit 1 names the ones that did
not. Exit 2 means a control failed and no result in the run means anything.
"""
import copy
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = "bin/gardyn-check-upgrade-policy.py"
TESTS = "tests/test_upgrade_policy.py"
FIXTURE = REPO / "tests" / "fixtures" / "gardyn-upgrade-policy.json"

sys.path.insert(0, str(REPO / "tests"))


def load_checker():
    """Import the checker by path. bin/ is not a package and the file has a
    hyphen, and the module deliberately keeps `import apt` inside
    collect_state() so this works off-host."""
    import importlib.util
    # Inert, for the same reason the checker's own load_vendor is: without
    # this, running the battery exactly as its docstring documents
    # (`python3 tests/mutate_upgrade_policy.py`, no -B) writes bin/__pycache__
    # into the working tree. A harness that mutates files must not also leave
    # bytecode next to them - that is the stale-cache trap it exists to avoid,
    # aimed at the repo instead of the temp copy.
    sys.dont_write_bytecode = True
    spec = importlib.util.spec_from_file_location(
        "gardyn_check_upgrade_policy", REPO / SRC)
    if spec is None or spec.loader is None:  # pragma: no cover
        raise ImportError("cannot load %s" % SRC)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def purge_caches(root):
    for cached in pathlib.Path(root).rglob("__pycache__"):
        shutil.rmtree(cached, ignore_errors=True)


def load_base_state():
    """The live capture, plus the one policy line it is still missing.

    tests/fixtures/gardyn-upgrade-policy.json is a verbatim `--dump-state`
    capture from the Gardyn Pi. It is deliberately NOT edited, so it keeps
    recording the three keys the deployed config still leaves unset, every one
    of which resolves to a u-u default that works against this host:

      Remove-New-Unused-Dependencies  default True  -> autoremove runs
      OnlyOnACPower                   default True  -> a battery reading stops it
      Skip-Updates-On-Metered-Connections default True -> a metered link stops it

    The base state used for mutation is the capture with those three applied,
    i.e. the policy as it is meant to be once the fix is deployed.
    `test_upgrade_policy.py` asserts both halves: the raw capture reports
    exactly those three failures, and the corrected base reports none.
    """
    raw = json.loads(FIXTURE.read_text())
    base = copy.deepcopy(raw)
    for key in PENDING_CONFIG_FIXES:
        base["conf"][key] = "false"
    return raw, base


#: The keys the deployed config is missing. Named once so the battery, the
#: suite and the docstring above cannot drift apart.
PENDING_CONFIG_FIXES = (
    "Unattended-Upgrade::Remove-New-Unused-Dependencies",
    "Unattended-Upgrade::OnlyOnACPower",
    "Unattended-Upgrade::Skip-Updates-On-Metered-Connections",
)


# ---------------------------------------------------------------------------
# CONFIG MUTANTS
#
# (name, mutate(state_dict), expected substring of a failing check label)
# An expectation of None declares "this must change nothing" - the survivor
# control. A mutant that fires the WRONG check is not a catch: the substring is
# what separates "the checker went red" from "the checker went red for the
# reason claimed".
# ---------------------------------------------------------------------------

def _set(key, value):
    def mutate(state):
        state["conf"][key] = value
    return mutate


def _drop(key):
    return _set(key, None)


def _patterns(*entries):
    def mutate(state):
        state["allowed_origins"] = list(entries)
    return mutate


def _blacklist_add(*entries):
    def mutate(state):
        state["blacklist"] = list(state["blacklist"]) + list(entries)
    return mutate


CONFIG_MUTANTS = [
    # --- F1: the release pin. A pattern with no codename matches ANY suite
    # from that origin, so a half-finished release move makes cross-release
    # packages eligible unattended.
    ("release pin stripped to bare origins",
     _patterns("origin=Raspbian", "origin=Raspberry Pi Foundation"),
     "release pin is intact"),
    ("codename dropped from one pattern",
     _patterns("origin=Raspbian,codename=bookworm,label=Raspbian",
               "origin=Raspberry Pi Foundation"),
     "release pin is intact"),
    ("label dropped from the Raspbian pattern",
     _patterns("origin=Raspbian,codename=bookworm",
               "origin=Raspberry Pi Foundation,codename=bookworm"),
     "release pin is intact"),
    ("half-finished move to the next release",
     _patterns("origin=Raspbian,codename=trixie,label=Raspbian",
               "origin=Raspberry Pi Foundation,codename=trixie"),
     "release pin is intact"),
    ("a third pattern appended",
     _patterns("origin=Raspbian,codename=bookworm,label=Raspbian",
               "origin=Raspberry Pi Foundation,codename=bookworm",
               "origin=Raspbian,codename=bookworm-staging,label=Raspbian"),
     "release pin is intact"),
    # get_allowed_origins() PREPENDS the legacy key's entries, so this widens
    # what is eligible without touching a single Origins-Pattern line.
    ("legacy Allowed-Origins key smuggles in a staging suite",
     lambda s: (s.__setitem__("legacy_allowed_origins",
                              ["Raspbian bookworm-staging"]),
                s.__setitem__("allowed_origins",
                              ["o=Raspbian,a=bookworm-staging"]
                              + list(s["allowed_origins"]))),
     "legacy Allowed-Origins"),
    ("fnmatch wildcard in the origin value",
     _patterns("origin=Raspb*,codename=bookworm,label=Raspbian",
               "origin=Raspberry Pi Foundation,codename=bookworm"),
     "fnmatch wildcard"),
    ("unexpanded substitution survives",
     _patterns("origin=Raspbian,codename=${distro_codename},label=Raspbian",
               "origin=Raspberry Pi Foundation,codename=${distro_codename}"),
     "substitutions expanded"),

    # --- F2: the autoremove path. do_auto_remove() calls mark_delete()
    # directly, so is_pkg_change_allowed() never runs: neither the blacklist
    # nor apt-mark hold protects a package against REMOVAL.
    ("Remove-New-Unused-Dependencies left at u-u's default (True)",
     _drop("Unattended-Upgrade::Remove-New-Unused-Dependencies"),
     "Remove-New-Unused-Dependencies"),
    ("Remove-New-Unused-Dependencies turned on explicitly",
     _set("Unattended-Upgrade::Remove-New-Unused-Dependencies", "true"),
     "Remove-New-Unused-Dependencies"),

    # --- F3: apt.systemd.daily reads APT::Periodic::Enable first and exit 0s.
    ("APT::Periodic::Enable 0 disables the whole mechanism",
     _set("APT::Periodic::Enable", "0"), "APT::Periodic::Enable"),
    ("apt-daily-upgrade.timer masked",
     lambda s: s["timers"].__setitem__("apt-daily-upgrade.timer", "masked"),
     "apt-daily-upgrade.timer is enabled"),
    ("apt-daily.timer disabled",
     lambda s: s["timers"].__setitem__("apt-daily.timer", "disabled"),
     "apt-daily.timer is enabled"),
    ("apt-daily.timer does not exist",
     lambda s: s["timers"].__setitem__("apt-daily.timer", ""),
     "apt-daily.timer is enabled"),
    ("update-package-lists switched off",
     _set("APT::Periodic::Update-Package-Lists", "0"),
     "Update-Package-Lists is on"),
    ("master switch off",
     _set("APT::Periodic::Unattended-Upgrade", "0"),
     "Unattended-Upgrade is on"),

    # --- F4: strict whitelist. The harmful state is strict + a narrow list;
    # an EMPTY whitelist turns the feature off, which is what the old check
    # guarded against and is the benign half.
    ("strict whitelist that matches nothing",
     lambda s: (s["conf"].__setitem__(
         "Unattended-Upgrade::Package-Whitelist-Strict", "true"),
         s.__setitem__("whitelist", ["nothing-matches-this$"])),
     "Package-Whitelist-Strict off"),
    ("strict whitelist with an empty list",
     _set("Unattended-Upgrade::Package-Whitelist-Strict", "true"),
     "Package-Whitelist-Strict off"),
    ("a whitelist appears without strict",
     lambda s: s.__setitem__("whitelist", ["openssl"]),
     "Package-Whitelist empty"),

    # --- F5: patterns and regexes u-u itself dies on. The old parser skipped
    # these silently, so the checker called a config fine that u-u raises on.
    ("unknown matcher token (u-u raises UnknownMatcherError)",
     _patterns("origin=Raspbian,arch=armhf,codename=bookworm,label=Raspbian",
               "origin=Raspberry Pi Foundation,codename=bookworm"),
     "entry parses"),
    ("token with no '=' (u-u raises ValueError)",
     _patterns("origin=Raspbian,bookworm,label=Raspbian",
               "origin=Raspberry Pi Foundation,codename=bookworm"),
     "entry parses"),
    ("token with two '=' (u-u raises ValueError)",
     _patterns("origin=Raspbian,codename=book=worm,label=Raspbian",
               "origin=Raspberry Pi Foundation,codename=bookworm"),
     "entry parses"),
    ("a glob written where the config says regex",
     _blacklist_add("*-firmware"), "pattern compiles"),

    # --- F6: the component narrowing. Only visible against the configured
    # package INDEXES; the installed versions' origins are all component=main
    # on this host, so the same assertion over that corpus passes.
    ("component=main added to both patterns",
     _patterns("origin=Raspbian,codename=bookworm,label=Raspbian,component=main",
               "origin=Raspberry Pi Foundation,codename=bookworm,component=main"),
     "EVERY configured package index"),
    # Only SOME indexes raise: the good pattern short-circuits for Raspbian,
    # so the RPF indexes are the only ones that reach the malformed one. That
    # mix is what made an earlier form of this check crash rather than report.
    ("a malformed pattern that only some indexes reach",
     _patterns("origin=Raspbian,codename=bookworm,label=Raspbian",
               "origin=Raspberry Pi Foundation,arch=armhf"),
     "EVERY configured package index"),
    ("an unallowed origin appears in the index list",
     lambda s: s["index_origins"].append(dict(
         origin="Debian", label="Debian-Security", component="main",
         archive="stable-security", codename="bookworm-security",
         site="security.debian.org", architecture="armhf", filename="x")),
     "EVERY configured package index"),

    # --- F7: multi-arch. `raspi-firmware$` does not match
    # `raspi-firmware:arm64`, and python-apt reports a foreign architecture's
    # packages that way. Only visible once is_boot_path() strips the qualifier.
    ("an arch-qualified boot package the anchored pattern misses",
     lambda s: s["installed"].append("raspi-firmware:arm64"),
     "BLOCKED  raspi-firmware:arm64"),

    # --- F10 replacement: the two-sided exact set. The retired heuristic was
    # "< 25% of installed", i.e. 161 packages against 18 real matches, so no
    # realistic over-block could reach it.
    ("blacklist swallows the interpreter stack",
     _blacklist_add("python3.*"), "allowed  python3"),
    ("blacklist swallows the Wi-Fi stack",
     _blacklist_add("network-manager$"), "allowed  network-manager"),
    # The one the old heuristic could NOT catch and no MUST_ALLOW entry names:
    # exactly one extra installed package blocked.
    ("blacklist over-blocks one unlisted package",
     _blacklist_add("bash$"), "nothing else installed"),
    ("blacklist emptied",
     lambda s: s.__setitem__("blacklist", []), "Package-Blacklist is non-empty"),

    # --- F12: apt-mark hold, the config's documented second layer.
    ("all holds cleared",
     lambda s: s.__setitem__("holds", []), "apt-mark holds are set"),
    ("a hold on something off the boot path",
     lambda s: s["holds"].append("bash"), "every held package is on the boot path"),

    # --- F13 and the rest of the boolean policy.
    ("Automatic-Reboot on",
     _set("Unattended-Upgrade::Automatic-Reboot", "true"), "Automatic-Reboot off"),
    ("Remove-Unused-Kernel-Packages left at u-u's default (True)",
     _drop("Unattended-Upgrade::Remove-Unused-Kernel-Packages"),
     "Remove-Unused-Kernel-Packages off"),
    ("InstallOnShutdown on",
     _set("Unattended-Upgrade::InstallOnShutdown", "yes"), "InstallOnShutdown off"),
    ("MinimalSteps off",
     _set("Unattended-Upgrade::MinimalSteps", "false"), "MinimalSteps on"),
    ("MinimalSteps off via the misspelled key u-u also ANDs",
     _set("Unattended-Upgrades::MinimalSteps", "0"), "misspelled key"),
    ("SyslogEnable left at u-u's default (False)",
     _drop("Unattended-Upgrade::SyslogEnable"), "SyslogEnable on"),
    ("Allow-downgrade on",
     _set("Unattended-Upgrade::Allow-downgrade", "on"), "Allow-downgrade off"),

    # --- review findings. Each of these produced a GREEN report before the
    # fix, on a policy under which u-u upgrades nothing or upgrades wrongly.
    ("matcher key capitalised (u-u compares case-sensitively)",
     _patterns("Origin=Raspbian,Codename=bookworm,Label=Raspbian",
               "Origin=Raspberry Pi Foundation,Codename=bookworm"),
     "entry parses"),
    ("codename given twice (u-u ANDs, so it matches neither)",
     _patterns("origin=Raspbian,codename=trixie,codename=bookworm,label=Raspbian",
               "origin=Raspberry Pi Foundation,codename=bookworm"),
     "entry parses"),
    ("a wildcard hidden behind a repeated key",
     _patterns("origin=*,origin=Raspbian,codename=bookworm,label=Raspbian",
               "origin=Raspberry Pi Foundation,codename=bookworm"),
     "entry parses"),
    ("alias collision: a= and suite= both given",
     _patterns("origin=Raspbian,a=oldstable,suite=stable,codename=bookworm,"
               "label=Raspbian",
               "origin=Raspberry Pi Foundation,codename=bookworm"),
     "entry parses"),
    # apt's StringToBool takes strtol(base 0) first, so these are booleans to
    # apt and were unrecognised words to the old checker - which resolved them
    # to the vendor default, and the vendor default is the required value.
    ("Automatic-Reboot on, spelled as hex",
     _set("Unattended-Upgrade::Automatic-Reboot", "0x1"), "Automatic-Reboot off"),
    ("InstallOnShutdown on, spelled with a leading zero",
     _set("Unattended-Upgrade::InstallOnShutdown", "01"),
     "InstallOnShutdown off"),
    ("Package-Whitelist-Strict on, spelled with a plus",
     _set("Unattended-Upgrade::Package-Whitelist-Strict", "+1"),
     "Package-Whitelist-Strict off"),
    # The gates that make a run exit as a SUCCESS having done nothing.
    ("Update-Days restricts u-u to one day a week",
     lambda s: s.__setitem__("update_days", ["Sun"]), "Update-Days"),
    ("OnlyOnACPower left at u-u's default (True)",
     _drop("Unattended-Upgrade::OnlyOnACPower"), "OnlyOnACPower off"),
    ("Skip-Updates-On-Metered-Connections left at u-u's default (True)",
     _drop("Unattended-Upgrade::Skip-Updates-On-Metered-Connections"),
     "Skip-Updates-On-Metered"),
    ("apt-daily-upgrade.service masked (the timer still reads enabled)",
     lambda s: s["timers"].__setitem__("apt-daily-upgrade.service", "masked"),
     "apt-daily-upgrade.service is not masked"),
    ("apt-daily.service masked",
     lambda s: s["timers"].__setitem__("apt-daily.service", "masked"),
     "apt-daily.service is not masked"),

    # --- CONTROL C: mutations that must change nothing. If either of these is
    # reported as caught, the scorer is broken and no verdict above is
    # trustworthy.
    ("CONTROL C: Remove-Unused-Dependencies absent (u-u's default is False, "
     "so absent is compliant)",
     _drop("Unattended-Upgrade::Remove-Unused-Dependencies"), None),
    ("CONTROL C: an unrelated package installed",
     lambda s: s["installed"].append("zzz-not-a-policy-package"), None),
    ("CONTROL C: a value spelled with a synonym apt accepts",
     _set("Unattended-Upgrade::SyslogEnable", "yes"), None),
]


# ---------------------------------------------------------------------------
# CODE MUTANTS - (name, file, old, new). `old` must appear exactly once, or the
# mutation silently did nothing and a "survived" verdict would be meaningless.
# ---------------------------------------------------------------------------

CODE_MUTANTS = [
    ("release-pin comparison made vacuous", SRC,
     "    report.check(\"the release pin is intact: exactly the expected patterns, \"\n"
     "                 \"each complete (origin + codename + label)\",\n"
     "                 normalise(parsed),\n"
     "                 normalise(expected_patterns(state.distro_codename)))",
     "    report.check(\"the release pin is intact: exactly the expected patterns, \"\n"
     "                 \"each complete (origin + codename + label)\",\n"
     "                 True, True)"),
    ("legacy Allowed-Origins check disabled", SRC,
     '    report.check("no legacy Allowed-Origins entries (they PREPEND to the list)",\n'
     "                 list(state.legacy_allowed_origins), [])",
     '    report.check("no legacy Allowed-Origins entries (they PREPEND to the list)",\n'
     "                 [], [])"),
    ("origin-only assertion restored (the pre-T-484 check)", SRC,
     "    def normalise(dicts):\n        return sorted(sorted(d.items()) for d in dicts)",
     "    def normalise(dicts):\n"
     "        return sorted(sorted({'origin': d.get('origin')}.items())\n"
     "                      for d in dicts)"),
    ("APT::Periodic::Enable no longer consulted", SRC,
     '                 conf("APT::Periodic::Enable") != "0", True)',
     "                 True, True)"),
    ("timer states accepted unconditionally", SRC,
     "                     state_word in RUNNING_TIMER_STATES, True)",
     "                     True, True)"),
    ("the old strict-whitelist check restored (guards the benign half)", SRC,
     '    report.check("Package-Whitelist-Strict off",\n'
     '                 apt_bool(conf("Unattended-Upgrade::Package-Whitelist-Strict"),\n'
     "                          False), False)",
     '    report.check("Package-Whitelist-Strict off",\n'
     '                 apt_bool(conf("Unattended-Upgrade::Package-Whitelist-Strict"),\n'
     "                          False) and not state.whitelist, False)"),
    ("two-sided blacklist set comparison removed", SRC,
     '    report.check("the blacklist blocks the boot path and nothing else installed",\n'
     "                 blocked, sorted(derived_block))",
     '    report.check("the blacklist blocks the boot path and nothing else installed",\n'
     "                 blocked, blocked)"),
    ("index-origin corpus swapped for a vacuous one", SRC,
     '                 sorted("%s/%s/%s" % (k[0], k[2], k[5])\n'
     "                        for k, v in verdicts.items() if v is not True), [])",
     "                 [], [])"),
    ("index verdicts reduced to a set again (crashes on a mixed result)", SRC,
     '                 sorted("%s/%s/%s" % (k[0], k[2], k[5])\n'
     "                        for k, v in verdicts.items() if v is not True), [])",
     "                 sorted({v for v in verdicts.values()}), [True])"),
    ("architecture qualifier no longer stripped from a package name", SRC,
     '    bare = name.split(":")[0]', "    bare = name"),
    ("parse_pattern goes back to silently skipping bad tokens", SRC,
     "        if len(parts) != 2:\n"
     "            raise PatternError(\n"
     '                "token %r has %d \'=\' (u-u raises ValueError unpacking it)"\n'
     '                % (token.replace("%2C", ","), len(parts) - 1))',
     "        if len(parts) != 2:\n            continue"),
    ("parse_pattern accepts a matcher u-u does not know", SRC,
     "        if key not in KNOWN_KEYS:\n            raise PatternError(",
     "        if False:\n            raise PatternError("),
    ("blacklist regexes no longer compiled", SRC,
     "        try:\n            re.compile(entry)\n"
     "        except re.error as exc:\n"
     '            report.fail("blacklist pattern compiles: %r" % entry, str(exc))',
     "        try:\n            pass\n"
     "        except re.error as exc:\n"
     '            report.fail("blacklist pattern compiles: %r" % entry, str(exc))'),
    ("apt_bool ignores the vendor default for an unknown word", SRC,
     "    if lowered in _APT_FALSE:\n        return False\n    return default",
     "    if lowered in _APT_FALSE:\n        return False\n    return False"),
    ("Remove-New-Unused-Dependencies default flipped to the safe-looking one", SRC,
     '    ("Unattended-Upgrade::Remove-New-Unused-Dependencies", True, False,',
     '    ("Unattended-Upgrade::Remove-New-Unused-Dependencies", False, False,'),
    ("hold set no longer required to be non-empty", SRC,
     '    report.check("apt-mark holds are set (the documented second layer)",\n'
     "                 len(state.holds) > 0, True)",
     '    report.check("apt-mark holds are set (the documented second layer)",\n'
     "                 True, True)"),
    ("unexplained holds tolerated", SRC,
     '    report.check("every held package is on the boot path (no unexplained hold)",\n'
     "                 sorted(h for h in state.holds if not is_boot_path(h)), [])",
     '    report.check("every held package is on the boot path (no unexplained hold)",\n'
     "                 [], [])"),
    ("matcher exceptions swallowed instead of reported", SRC,
     '        except Exception as exc:                       # noqa: BLE001\n'
     '            self.fail(label, "%s: %s" % (type(exc).__name__, exc))\n'
     "            return None",
     "        except Exception:                              # noqa: BLE001\n"
     "            return None"),
    ("failures no longer recorded", SRC,
     "        if not ok:\n            self.failures.append(label)\n        return ok",
     "        return ok"),
    ("a failing run exits 0 anyway", SRC,
     '    if report.failures:\n        print("RESULT: %d FAILURE(S)" % len(report.failures))\n'
     '        for name in report.failures:\n            print("  - %s" % name)\n'
     "        return 1",
     '    if report.failures:\n        print("RESULT: %d FAILURE(S)" % len(report.failures))\n'
     '        for name in report.failures:\n            print("  - %s" % name)\n'
     "        return 0"),
    ("--state silently checks nothing", SRC,
     "            report = run_checks(state)\n        else:",
     "            report = Report()\n        else:"),

    # --- the review findings, each mutated back to the form that was green
    ("matcher key lowercased again (u-u is case-sensitive)", SRC,
     "        key, value = [p.strip().replace(\"%2C\", \",\") for p in parts]\n"
     "        if key not in KNOWN_KEYS:",
     "        key, value = [p.strip().replace(\"%2C\", \",\") for p in parts]\n"
     "        key = key.lower()\n        if key not in KNOWN_KEYS:"),
    ("a repeated matcher key silently overrides again", SRC,
     "        if canonical in out:\n            raise PatternError(",
     "        if False:\n            raise PatternError("),
    ("apt_bool loses the strtol path", SRC,
     "    parsed = _strtol_base0(raw)\n    if parsed in (0, 1):\n"
     "        return bool(parsed)",
     "    parsed = None\n    if parsed in (0, 1):\n        return bool(parsed)"),
    ("apt_bool strips before comparing words (apt does not)", SRC,
     "    lowered = raw.lower()", "    lowered = raw.strip().lower()"),
    ("strtol accepts a partially-converted string", SRC,
     '_DIGITS = {8: re.compile(r"[0-7]+\\Z"), 10: re.compile(r"[0-9]+\\Z"),\n'
     '           16: re.compile(r"[0-9a-fA-F]+\\Z")}',
     '_DIGITS = {8: re.compile(r"[0-7]+"), 10: re.compile(r"[0-9]+"),\n'
     '           16: re.compile(r"[0-9a-fA-F]+")}'),
    ("Update-Days no longer checked", SRC,
     "                 list(state.update_days), [])", "                 [], [])"),
    ("masked services tolerated", SRC,
     "                     state.timers.get(unit, \"\") != \"masked\", True)",
     "                     True, True)"),
    ("section 5 stops asserting the reference matcher agrees", SRC,
     '    report.check("reference_is_allowed_origin agrees with u-u on every index",\n'
     "                 sweep(\"index matcher\", reference_is_allowed_origin,\n"
     "                       vendor_origin, state.index_origins,\n"
     "                       lambda o: \"%s/%s\" % (o.origin, o.component),\n"
     "                       state.allowed_origins),\n"
     "                 [])",
     '    report.check("reference_is_allowed_origin agrees with u-u on every index",\n'
     "                 [], [])"),
    ("section 5 stops asserting the blacklist double agrees", SRC,
     "                 sweep(\"blacklist matcher\", reference_is_pkgname_in_blacklist,\n"
     "                       vendor_blacklist, state.installed, lambda n: n,\n"
     "                       state.blacklist),\n"
     "                 [])",
     "                 [], [])"),
    ("the local-origin short-circuit loses its label and site test", SRC,
     '    if (origin.component == "now" and origin.archive == "now"\n'
     "            and not origin.label and not origin.site):",
     '    if origin.component == "now" and origin.archive == "now":'),
    ("blacklist double uses re.search instead of re.match", SRC,
     "    return any(re.match(expr, pkgname) for expr in blacklist)",
     "    return any(re.search(expr, pkgname) for expr in blacklist)"),
    ("reference_or treats a raising pattern as a match", SRC,
     "    except Exception:                                  # noqa: BLE001\n"
     "        return False",
     "    except Exception:                                  # noqa: BLE001\n"
     "        return True"),
    ("state no longer round-trips deterministically", SRC,
     "        return json.dumps(asdict(self), indent=1, sort_keys=True)",
     "        return json.dumps(asdict(self), indent=1)"),
    ("a crashing run reports nothing", SRC,
     '        report.fail("the check run itself completed",\n'
     '                    "%s: %s" % (type(exc).__name__, exc))',
     "        pass"),
    ("--dump-state prints the report onto the fixture it is writing", SRC,
     "    if args.dump_state and not args.state:\n"
     "        print(collect_state(load_vendor()).to_json())\n        return 0",
     "    if False:\n"
     "        print(collect_state(load_vendor()).to_json())\n        return 0"),
]


# ---------------------------------------------------------------------------


def score_config(checker, base, mutate):
    """Return the sorted failing-check labels a mutated policy produces."""
    state = copy.deepcopy(base)
    mutate(state)
    report = checker.run_checks(checker.PolicyState.from_dict(state))
    return sorted(report.failures)


def run_suite(root):
    """Run the unittest suite against whatever is on disk in `root`.

    The __pycache__ purge and PYTHONDONTWRITEBYTECODE are load-bearing, not
    hygiene. The module under test is imported through spec_from_file_location,
    and CPython validates cached bytecode on (mtime-SECONDS, size) - so a
    mutation that preserves file size and is applied and reverted inside one
    second re-runs the PREVIOUS bytecode and the harness reports a verdict
    belonging to the wrong mutant. `-B` alone suppresses WRITING a cache, not
    READING a stale one.

    stderr is merged, not discarded: unittest reports there, and a harness that
    greps a blanked stream scores every mutant caught.
    """
    purge_caches(root)
    env = {"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin",
           "HOME": str(root)}
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", "tests.test_upgrade_policy"],
        cwd=root, capture_output=True, text=True, env=env,
        stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout + proc.stderr


def controls_config(checker, base):
    """Controls A and B for the config half."""
    clean = sorted(checker.run_checks(
        checker.PolicyState.from_dict(copy.deepcopy(base))).failures)
    if clean:
        print("CONTROL A FAILED - the clean fixture is already red:")
        for name in clean:
            print("    %s" % name)
        return False
    print("  control A  clean fixture scores GREEN")

    broken = score_config(checker, base,
                          _set("Unattended-Upgrade::Automatic-Reboot", "true"))
    if not broken:
        print("CONTROL B FAILED - a deliberately broken policy scored GREEN. "
              "The scorer cannot see failures; nothing below means anything.")
        return False
    print("  control B  a broken policy scores RED (%d failure(s))" % len(broken))
    return True


def controls_code(root):
    """Controls A and B for the code half."""
    rc, out = run_suite(root)
    if rc != 0:
        print("CONTROL A FAILED - the clean tree's suite is red:\n" + out)
        return False
    print("  control A  clean tree's suite passes")

    target = root / TESTS
    original = target.read_text()
    anchor = "class TestPolicyControls(unittest.TestCase):"
    if original.count(anchor) != 1:
        print("CONTROL B FAILED - cannot find the anchor to break.")
        return False
    target.write_text(original.replace(
        anchor,
        anchor + "\n    def test_deliberately_broken_control(self):\n"
                 "        self.assertEqual(1, 2)  # control B"))
    rc, out = run_suite(root)
    target.write_text(original)
    if rc == 0:
        print("CONTROL B FAILED - a deliberately broken assertion still "
              "scored GREEN. The runner is not running these tests.")
        return False
    print("  control B  a broken assertion makes the suite RED")
    return True


def main():
    checker = load_checker()
    raw, base = load_base_state()

    print("== config half: mutate the POLICY, the checker must go red ==")
    if not controls_config(checker, base):
        return 2

    # The capture is checked in unedited; this records what it is still
    # missing, so a future capture that silently fixes or breaks something is
    # visible here rather than only in the suite.
    raw_failures = sorted(checker.run_checks(
        checker.PolicyState.from_dict(copy.deepcopy(raw))).failures)
    print("  note: the raw live capture reports %d failure(s): %s"
          % (len(raw_failures), raw_failures))
    print()

    bad = []
    for name, mutate, expected in CONFIG_MUTANTS:
        failures = score_config(checker, base, mutate)
        if expected is None:
            if failures:
                print("  WRONGLY CAUGHT  %s -> %s" % (name, failures))
                bad.append("%s (no-op mutant was scored as caught)" % name)
            else:
                print("  survived (as declared)  %s" % name)
            continue
        if not failures:
            print("  SURVIVED  %s" % name)
            bad.append("%s (policy mutation produced no failure)" % name)
        elif not any(expected in f for f in failures):
            print("  WRONG CHECK  %s -> expected %r, got %s"
                  % (name, expected, failures))
            bad.append("%s (fired the wrong check)" % name)
        else:
            print("  killed    %s  [%d failure(s)]" % (name, len(failures)))

    print()
    print("== code half: mutate the CHECKER, the suite must go red ==")
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp) / "repo"
        shutil.copytree(REPO, root,
                        ignore=shutil.ignore_patterns(".git", "__pycache__"))
        pristine = pathlib.Path(tmp) / "pristine"
        shutil.copytree(root, pristine)

        if not controls_code(root):
            return 2
        print()

        for name, relpath, old, new in CODE_MUTANTS:
            target = root / relpath
            original = target.read_text()
            # Counted in Python, never with grep -F, which treats each line of
            # a multi-line pattern as a separate alternative and would report a
            # match for an anchor that does not exist as written.
            occurrences = original.count(old)
            if occurrences != 1:
                print("  ANCHOR  %s: appears %dx, not 1" % (name, occurrences))
                bad.append("%s (anchor not unique - the mutation never applied)"
                           % name)
                continue
            target.write_text(original.replace(old, new))
            # Prove the edit landed. A replace that matched nothing is silent.
            if target.read_text() == (pristine / relpath).read_text():
                target.write_text(original)
                print("  NO-DIFF  %s: file unchanged after mutation" % name)
                bad.append("%s (mutation produced no diff)" % name)
                continue
            rc, out = run_suite(root)
            target.write_text(original)
            if rc == 0:
                print("  SURVIVED  %s" % name)
                bad.append("%s (code mutation survived the suite)" % name)
            else:
                print("  killed    %s" % name)

        # The tree must be byte-identical to where it started. Compared as a
        # set in BOTH directions - a one-way walk of `root` can see a changed
        # or added file but is structurally blind to a file that was DELETED,
        # which is the restoration failure a mutation harness is most likely
        # to produce.
        def manifest(base):
            base = pathlib.Path(base)
            return {str(p.relative_to(base)): p.read_bytes()
                    for p in base.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts}

        after, before = manifest(root), manifest(pristine)
        drift = sorted(set(before) ^ set(after))
        drift += sorted(k for k in set(before) & set(after)
                        if before[k] != after[k])
        if drift:
            print("\nTREE NOT RESTORED - added, removed or changed: %s" % drift)
            return 2
        print("  tree restored byte-identical (%d files, compared both ways)"
              % len(after))

    print()
    if bad:
        print("%d MUTATION(S) DID NOT BEHAVE AS DECLARED:" % len(bad))
        for name in bad:
            print("  - %s" % name)
        print("\nA survivor has three explanations, not one: the test is weak, "
              "the harness never applied the mutation, or the mutated code is "
              "redundant and genuinely changes nothing - in which case delete "
              "it rather than test it.")
        return 1
    print("all %d config and %d code mutations behaved as declared"
          % (len(CONFIG_MUTANTS), len(CODE_MUTANTS)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
