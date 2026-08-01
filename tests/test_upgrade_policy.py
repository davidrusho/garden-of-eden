"""Tests for bin/gardyn-check-upgrade-policy.py (T-484).

Hardware-free, and off-host by design. The checker's job is to notice that
unattended-upgrades is disabled or subverted, and every one of those states is
a *configuration* - which means the only honest way to exercise them is to
present the checker with that configuration and watch it go red. Doing that on
the live Pi means writing drop-ins into /etc/apt/apt.conf.d and hoping the
cleanup runs; doing it here means a captured state snapshot and a dict.

So the checks are pure functions of a `PolicyState` and this suite is the only
place the failing configurations are ever seen. The cases below are the ones
that made the original checker report green while u-u would have upgraded
nothing, upgraded across releases, or deleted a package the policy froze.

Run from the repo root:

    python3 -m unittest tests.test_upgrade_policy

The companion battery, tests/mutate_upgrade_policy.py, proves this suite can
fail - both by mutating the policy and by mutating the checker.
"""
import copy
import unittest

from tests import mutate_upgrade_policy as battery

cup = battery.load_checker()
RAW_CAPTURE, BASE = battery.load_base_state()


def failures(state_dict):
    """Failing check labels for a policy dict."""
    return cup.run_checks(cup.PolicyState.from_dict(state_dict)).failures


def mutated(mutate):
    state = copy.deepcopy(BASE)
    mutate(state)
    return failures(state)


# ---------------------------------------------------------------------------
# The controls. Named so the battery can anchor on the class.
# ---------------------------------------------------------------------------
class TestPolicyControls(unittest.TestCase):
    def test_the_intended_policy_is_green(self):
        # If this ever goes red, nothing else in the file means anything: every
        # other test asserts that a MUTATION turns a green baseline red.
        self.assertEqual(failures(copy.deepcopy(BASE)), [])

    def test_the_raw_live_capture_reports_the_one_outstanding_defect(self):
        # The fixture is a verbatim capture and is deliberately not edited.
        # The deployed config still leaves Remove-New-Unused-Dependencies
        # unset, which resolves to u-u's default of True. Delete this test when
        # the config line ships - and update the fixture in the same commit.
        got = failures(copy.deepcopy(RAW_CAPTURE))
        self.assertEqual(len(got), 1, got)
        self.assertIn("Remove-New-Unused-Dependencies", got[0])

    def test_a_check_that_cannot_fail_would_be_caught(self):
        # The negative control on the Report itself: a failing check must land
        # in .failures, or every assertion above is decorative.
        report = cup.Report()
        report.check("deliberately false", 1, 2)
        self.assertEqual(report.failures, ["deliberately false"])


# ---------------------------------------------------------------------------
# Every declared config mutation, driven from the battery's own table so the
# two cannot drift apart.
# ---------------------------------------------------------------------------
class TestDeclaredMutations(unittest.TestCase):
    def test_each_mutation_behaves_as_declared(self):
        for name, mutate, expected in battery.CONFIG_MUTANTS:
            with self.subTest(mutation=name):
                got = mutated(mutate)
                if expected is None:
                    self.assertEqual(got, [], "no-op mutant went red")
                else:
                    self.assertTrue(got, "mutation produced no failure")
                    self.assertTrue(
                        any(expected in f for f in got),
                        "expected a failure containing %r, got %r"
                        % (expected, got))


# ---------------------------------------------------------------------------
# The four HIGH findings, spelled out individually. The loop above covers them,
# but a named test is what a future reader greps for.
# ---------------------------------------------------------------------------
class TestReleasePin(unittest.TestCase):
    """F1: only `origin` was asserted, so the release pin could be deleted
    with zero FAILs. A pattern with no `codename=` matches any suite from that
    origin."""

    def test_bare_origins_are_rejected(self):
        got = mutated(battery._patterns("origin=Raspbian",
                                        "origin=Raspberry Pi Foundation"))
        self.assertTrue(any("release pin is intact" in f for f in got))

    def test_the_origin_names_still_look_right_to_the_old_check(self):
        # The point of F1: the mutation above leaves the origin-name assertion
        # completely happy. That check is not the one doing the work.
        state = copy.deepcopy(BASE)
        battery._patterns("origin=Raspbian",
                          "origin=Raspberry Pi Foundation")(state)
        got = failures(state)
        self.assertFalse(any("origins named are exactly" in f for f in got))

    def test_legacy_key_prepends_without_touching_origins_pattern(self):
        got = mutated(lambda s: (
            s.__setitem__("legacy_allowed_origins", ["Raspbian bookworm-staging"]),
            s.__setitem__("allowed_origins",
                          ["o=Raspbian,a=bookworm-staging"]
                          + list(s["allowed_origins"]))))
        self.assertTrue(any("legacy Allowed-Origins" in f for f in got))


class TestAutoremovePath(unittest.TestCase):
    """F2: do_auto_remove() calls mark_delete() directly, so neither
    Package-Blacklist nor `apt-mark hold` protects a package from REMOVAL. The
    key that governs it defaults to True and was never checked."""

    def test_absent_key_resolves_to_the_vendor_default_and_fails(self):
        got = mutated(battery._drop(
            "Unattended-Upgrade::Remove-New-Unused-Dependencies"))
        self.assertTrue(any("Remove-New-Unused-Dependencies" in f for f in got))

    def test_remove_unused_dependencies_default_is_false_not_true(self):
        # F8: u-u's own default (:2325, :2394) is False, so an absent key is
        # compliant. The old checker passed True and would have reported a
        # compliant host as broken.
        got = mutated(battery._drop(
            "Unattended-Upgrade::Remove-Unused-Dependencies"))
        self.assertEqual(got, [])


class TestMasterSwitches(unittest.TestCase):
    """F3: APT::Periodic::Enable "0" makes apt.systemd.daily exit before it
    reads anything else, and a masked timer is invisible from apt.conf."""

    def test_periodic_enable_zero_is_caught(self):
        got = mutated(battery._set("APT::Periodic::Enable", "0"))
        self.assertTrue(any("APT::Periodic::Enable" in f for f in got))

    def test_the_other_two_switches_still_read_on(self):
        # Why F3 mattered: with Enable=0 the two switches the old checker did
        # examine are both still "1".
        state = copy.deepcopy(BASE)
        battery._set("APT::Periodic::Enable", "0")(state)
        got = failures(state)
        self.assertFalse(any("Update-Package-Lists" in f for f in got))
        self.assertFalse(any("master switch" in f for f in got))

    def test_masked_timer_is_caught(self):
        got = mutated(lambda s: s["timers"].__setitem__(
            "apt-daily-upgrade.timer", "masked"))
        self.assertTrue(any("apt-daily-upgrade.timer" in f for f in got))


class TestStrictWhitelist(unittest.TestCase):
    """F4: the old check asserted `not (strict and whitelist_empty)`. An empty
    whitelist turns the feature OFF, so it guarded the benign half; the
    harmful state is strict plus a list that matches nothing."""

    def test_strict_with_a_narrow_list_is_caught(self):
        got = mutated(lambda s: (
            s["conf"].__setitem__(
                "Unattended-Upgrade::Package-Whitelist-Strict", "true"),
            s.__setitem__("whitelist", ["nothing-matches-this$"])))
        self.assertTrue(any("Package-Whitelist-Strict off" in f for f in got))

    def test_the_old_condition_would_not_have_fired(self):
        state = copy.deepcopy(BASE)
        state["conf"]["Unattended-Upgrade::Package-Whitelist-Strict"] = "true"
        state["whitelist"] = ["nothing-matches-this$"]
        strict = cup.apt_bool(
            state["conf"]["Unattended-Upgrade::Package-Whitelist-Strict"], False)
        self.assertFalse(strict and not state["whitelist"])   # the old check
        self.assertTrue(strict)                               # the new one


# ---------------------------------------------------------------------------
# The parser, which is where the checker and the vendor were allowed to
# disagree.
# ---------------------------------------------------------------------------
class TestParsePattern(unittest.TestCase):
    def test_full_pattern(self):
        self.assertEqual(
            cup.parse_pattern("origin=Raspbian,codename=bookworm,label=Raspbian"),
            {"origin": "Raspbian", "codename": "bookworm", "label": "Raspbian"})

    def test_short_keys_are_normalised(self):
        self.assertEqual(cup.parse_pattern("o=Debian,a=stable,n=bookworm,c=main"),
                         {"origin": "Debian", "archive": "stable",
                          "codename": "bookworm", "component": "main"})

    def test_suite_is_an_alias_for_archive(self):
        # match_whitelist_string accepts "a", "suite" and "archive" for the
        # same field; the old alias table omitted "suite", so a pattern using
        # it parsed into a key nothing compares.
        self.assertEqual(cup.parse_pattern("suite=oldstable"),
                         {"archive": "oldstable"})

    def test_whitespace_is_insignificant(self):
        self.assertEqual(cup.parse_pattern(" origin = Raspbian , n = bookworm "),
                         {"origin": "Raspbian", "codename": "bookworm"})

    def test_escaped_comma_stays_inside_the_value(self):
        # u-u html-quotes "\," before splitting, so it is a literal comma in a
        # value and not a token separator.
        self.assertEqual(cup.parse_pattern("label=Foo\\, Inc,origin=Foo"),
                         {"label": "Foo, Inc", "origin": "Foo"})

    def test_token_without_equals_raises(self):
        with self.assertRaises(cup.PatternError):
            cup.parse_pattern("origin=Raspbian,bookworm")

    def test_token_with_two_equals_raises(self):
        with self.assertRaises(cup.PatternError):
            cup.parse_pattern("origin=Raspbian,codename=book=worm")

    def test_unknown_matcher_raises(self):
        with self.assertRaises(cup.PatternError):
            cup.parse_pattern("origin=Raspbian,arch=armhf")

    def test_empty_pattern_raises(self):
        with self.assertRaises(cup.PatternError):
            cup.parse_pattern("   ")


class TestAptBool(unittest.TestCase):
    """apt's StringToBool, verified against apt_pkg.config.find_b on the host
    rather than read off a doc page: word lists plus the integers 0 and 1, and
    ANYTHING else silently returns the caller's default."""

    def test_true_words(self):
        for word in ("yes", "true", "with", "on", "enable", "1",
                     "TRUE", " True "):
            self.assertTrue(cup.apt_bool(word, False), word)

    def test_false_words(self):
        for word in ("no", "false", "without", "off", "disable", "0", "FALSE"):
            self.assertFalse(cup.apt_bool(word, True), word)

    def test_unrecognised_value_falls_back_to_the_default(self):
        for word in ("2", "-1", "bogus", ""):
            self.assertTrue(cup.apt_bool(word, True), word)
            self.assertFalse(cup.apt_bool(word, False), word)

    def test_absent_key_uses_the_default(self):
        self.assertTrue(cup.apt_bool(None, True))
        self.assertFalse(cup.apt_bool(None, False))


class TestIsBootPath(unittest.TestCase):
    def test_prefixes(self):
        for name in ("linux-image-6.12.75+rpt-rpi-v6", "linux-headers-rpi-v7",
                     "raspberrypi-kernel", "raspberrypi-bootloader-something"):
            self.assertTrue(cup.is_boot_path(name), name)

    def test_exact_names(self):
        for name in ("raspi-firmware", "rpi-eeprom", "u-boot-rpi",
                     "firmware-brcm80211"):
            self.assertTrue(cup.is_boot_path(name), name)

    def test_architecture_qualifier_is_stripped(self):
        # F7. python-apt reports a foreign architecture's packages as
        # `name:arch`, and this host runs armhf with arm64 as a foreign
        # architecture. Without the strip these fall out of the derived set
        # entirely, so the checker never asks whether they are blocked.
        for name in ("raspi-firmware:arm64", "rpi-eeprom:arm64",
                     "u-boot-rpi:arm64", "firmware-brcm80211:arm64"):
            self.assertTrue(cup.is_boot_path(name), name)

    def test_ordinary_packages_are_not_on_the_boot_path(self):
        for name in ("bash", "python3", "network-manager", "linux-libc-dev",
                     "linux-base", "firmware-atheros"):
            self.assertFalse(cup.is_boot_path(name), name)


class TestReferenceMatchers(unittest.TestCase):
    """The off-host stand-ins for u-u's own matchers. A live run re-derives
    this agreement against the vendor on the real corpus (section 5), so this
    covers the shapes the host does not happen to have."""

    def test_all_tokens_must_match(self):
        origin = cup.Origin(origin="Raspbian", label="Raspbian",
                            component="main", archive="oldstable",
                            codename="bookworm", site="raspbian.example")
        self.assertTrue(cup.reference_is_allowed_origin(
            origin, ["origin=Raspbian,codename=bookworm"]))
        self.assertFalse(cup.reference_is_allowed_origin(
            origin, ["origin=Raspbian,codename=trixie"]))

    def test_component_narrowing_excludes_other_components(self):
        non_free = cup.Origin(origin="Raspbian", label="Raspbian",
                              component="non-free", archive="oldstable",
                              codename="bookworm", site="raspbian.example")
        self.assertFalse(cup.reference_is_allowed_origin(
            non_free, ["origin=Raspbian,codename=bookworm,component=main"]))

    def test_wildcard_trusts_anything(self):
        anything = cup.Origin(origin="Some Random Repo", component="main",
                              archive="unstable", codename="sid", site="x")
        self.assertTrue(cup.reference_is_allowed_origin(anything, ["origin=*"]))

    def test_the_local_dpkg_status_pseudo_origin_is_always_allowed(self):
        local = cup.Origin(component="now", archive="now")
        self.assertTrue(cup.reference_is_allowed_origin(local, []))

    def test_blacklist_is_a_regex_match_not_a_substring(self):
        self.assertTrue(cup.reference_is_pkgname_in_blacklist(
            "linux-image-6.12", ["linux-image-.*"]))
        # re.match anchors at the start only, which is why the config's
        # trailing `$` matters and why an unanchored pattern over-blocks.
        self.assertTrue(cup.reference_is_pkgname_in_blacklist(
            "rpi-eeprom-update", ["rpi-eeprom"]))
        self.assertFalse(cup.reference_is_pkgname_in_blacklist(
            "rpi-eeprom-update", ["rpi-eeprom$"]))


class TestMatcherCrashesBecomeFailures(unittest.TestCase):
    """F5: an UnknownMatcherError, a ValueError from a malformed token or an
    re.error from a glob used to abort the run mid-section. The exit code was
    still non-zero, but the later sections never ran and a human scanning for
    [FAIL] saw none."""

    def test_a_bad_pattern_does_not_stop_the_later_sections(self):
        state = copy.deepcopy(BASE)
        battery._patterns("origin=Raspbian,arch=armhf",
                          "origin=Raspberry Pi Foundation,codename=bookworm")(state)
        report = cup.run_checks(cup.PolicyState.from_dict(state))
        self.assertTrue(any("entry parses" in f for f in report.failures))
        # Section 4 must still have produced rows.
        self.assertIn("4. Does it actually run", report.text())
        # The guard must NAME the raising call and quote the exception, not
        # merely let a downstream comparison go red on a None. Without this
        # assertion the whole except branch can be deleted and every other
        # test here still passes, because the outer checks fail anyway - the
        # diagnostic is the only thing lost, and the diagnostic is the point.
        self.assertTrue(any("index origin matches" in f
                            for f in report.failures))
        self.assertIn("PatternError: unknown matcher 'arch'", report.text())

    def test_a_partially_reachable_bad_pattern_reports_rather_than_crashes(self):
        # The matchers short-circuit on the first pattern that matches, so a
        # malformed SECOND pattern is reached by some indexes and not others.
        # The mixed result is the case that has to be reported rather than
        # raised - a check that dies on the malformed config it exists to
        # detect is the worst of both.
        state = copy.deepcopy(BASE)
        battery._patterns("origin=Raspbian,codename=bookworm,label=Raspbian",
                          "origin=Raspberry Pi Foundation,arch=armhf")(state)
        report = cup.run_checks(cup.PolicyState.from_dict(state))
        self.assertTrue(any("EVERY configured package index" in f
                            for f in report.failures))
        self.assertIn("Raspberry Pi Foundation/main/armhf", report.text())

    def test_a_glob_in_the_blacklist_is_reported_not_raised(self):
        state = copy.deepcopy(BASE)
        battery._blacklist_add("*-firmware")(state)
        report = cup.run_checks(cup.PolicyState.from_dict(state))
        self.assertTrue(any("pattern compiles" in f for f in report.failures))
        self.assertIn("4. Does it actually run", report.text())
        self.assertTrue(any(f.startswith("blacklist match ")
                            for f in report.failures))
        self.assertIn("nothing to repeat", report.text())


class TestCommandLine(unittest.TestCase):
    """The --state seam, which is what makes a captured policy checkable at
    all. Nothing else here drives main(), so an exit code that stopped
    reflecting the failure count would be invisible."""

    def test_state_file_with_failures_exits_nonzero(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cup.main(["--state", str(battery.FIXTURE)])
        self.assertEqual(rc, 1)
        self.assertIn("RESULT: 1 FAILURE(S)", out.getvalue())
        self.assertIn("Remove-New-Unused-Dependencies", out.getvalue())

    def test_a_compliant_state_file_exits_zero(self):
        import contextlib
        import io
        import json
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json") as handle:
            json.dump(BASE, handle)
            handle.flush()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cup.main(["--state", handle.name])
        self.assertEqual(rc, 0)
        self.assertIn("all checks passed", out.getvalue())


class TestModuleIsImportableOffHost(unittest.TestCase):
    def test_apt_is_not_imported_at_module_scope(self):
        # The whole suite depends on this: python3-apt does not exist on the
        # machine these tests are written on, so `import apt` has to live
        # inside collect_state().
        import sys as _sys
        self.assertNotIn("apt", _sys.modules)
        self.assertNotIn("apt_pkg", _sys.modules)

    def test_load_vendor_disables_bytecode_writing(self):
        # F9: SourceFileLoader.load_module() cached bytecode beside the source,
        # so running as root created /usr/bin/__pycache__ under a root-owned
        # bin - and the module's own docstring called itself read-only.
        # exec_module() alone would still cache; sys.dont_write_bytecode is the
        # part that stops it.
        import inspect
        source = inspect.getsource(cup.load_vendor)
        self.assertIn("sys.dont_write_bytecode = True", source)
        self.assertIn("exec_module", source)
        self.assertNotIn("load_module()", source.split('"""')[-1])


if __name__ == "__main__":
    unittest.main()
