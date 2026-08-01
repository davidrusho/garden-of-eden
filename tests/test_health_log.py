"""Tests for bin/gardyn-health-log.py (T-473.2).

Hardware-free — every parser is a pure function over a string, so the branches
the live Pi cannot be made to produce on demand are covered here and ONLY here:
a vanished radio, a missing vcgencmd, malformed hex, a truncated /proc row.
Those are exactly the readings that matter during an outage, so the suite is
the only place they are ever exercised.

Run from the repo root:

    python3 -m unittest tests.test_health_log
"""
import contextlib
import importlib.util
import io
import pathlib
import shlex
import unittest
from unittest import mock

# bin/ is not a package and the file has a hyphen, so import it by path.
_SRC = pathlib.Path(__file__).resolve().parents[1] / "bin" / "gardyn-health-log.py"
_spec = importlib.util.spec_from_file_location("gardyn_health_log", _SRC)
if _spec is None or _spec.loader is None:  # pragma: no cover - import plumbing
    raise ImportError(f"cannot load {_SRC}")
hl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hl)


class TestParseThrottled(unittest.TestCase):
    def test_all_clear(self):
        got = hl.parse_throttled("throttled=0x0")
        self.assertEqual(got["raw"], "0x0")
        self.assertFalse(any(got[n] for n in hl.THROTTLED_BITS.values()))

    def test_undervoltage_now_and_since_boot(self):
        # 0x50005 = bits 0, 2, 16, 18 -> the classic brownout signature.
        got = hl.parse_throttled("throttled=0x50005")
        self.assertTrue(got["undervolt_now"])
        self.assertTrue(got["throttled_now"])
        self.assertTrue(got["undervolt_since_boot"])
        self.assertTrue(got["throttled_since_boot"])
        self.assertFalse(got["arm_capped_now"])
        self.assertFalse(got["soft_temp_limit_since_boot"])

    def test_sticky_only_is_the_diagnostic_case(self):
        # 0x80000 = bit 19 only: nothing wrong NOW, but it happened earlier.
        # This is the reading that survives an outage nobody watched, and it
        # must not be reported as all-clear.
        got = hl.parse_throttled("0x80000")
        self.assertTrue(got["soft_temp_limit_since_boot"])
        self.assertFalse(got["soft_temp_limit_now"])

    def test_bare_hex_without_key(self):
        self.assertEqual(hl.parse_throttled("0x2")["raw"], "0x2")

    def test_whitespace_tolerated(self):
        self.assertTrue(hl.parse_throttled("  throttled=0x1\n  ")["undervolt_now"])

    def test_vcgencmd_absent_returns_unknown_not_clear(self):
        # The dangerous failure is reporting "no undervoltage" when the truth
        # is "nobody asked". unknown must NOT decode to a set of false flags.
        got = hl.parse_throttled(None)
        self.assertEqual(got["raw"], hl.UNKNOWN)
        self.assertEqual(got["error"], "no_output")
        self.assertNotIn("undervolt_now", got)

    def test_empty_output(self):
        self.assertEqual(hl.parse_throttled("")["error"], "empty")

    def test_unparseable_hex(self):
        self.assertEqual(hl.parse_throttled("throttled=banana")["error"], "unparseable")

    def test_never_raises_on_junk(self):
        for junk in ("=", "throttled=", "0x", "throttled=0xZZ", "\x00"):
            self.assertIn("raw", hl.parse_throttled(junk))


class TestParseSocTemp(unittest.TestCase):
    def test_millidegrees(self):
        self.assertAlmostEqual(hl.parse_soc_temp("41923\n"), 41.923)

    def test_missing_file(self):
        self.assertIsNone(hl.parse_soc_temp(None))

    def test_garbage(self):
        self.assertIsNone(hl.parse_soc_temp("warm"))


class TestParseProcNetWireless(unittest.TestCase):
    # Real output, copied from the Pi on 2026-07-31.
    LIVE = (
        "Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE\n"
        " face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22\n"
        " wlan0: 0000   50.  -60.  -256        0      0      0  13937      0        0\n"
    )

    def test_live_row(self):
        got = hl.parse_proc_net_wireless(self.LIVE)
        self.assertTrue(got["present"])
        self.assertEqual(got["link"], 50)
        self.assertEqual(got["level_dbm"], -60)

    def test_trailing_dots_stripped(self):
        # The kernel writes "50." not "50"; a naive int() would raise here.
        self.assertEqual(hl.parse_proc_net_wireless(self.LIVE)["link"], 50)

    def test_radio_gone_is_distinct_from_disconnected(self):
        # THE incident signal: headers present, wlan0 absent -> driver died.
        # Must NOT be conflated with an associated-but-idle interface.
        headers = "\n".join(self.LIVE.splitlines()[:2]) + "\n"
        got = hl.parse_proc_net_wireless(headers)
        self.assertFalse(got["present"])
        self.assertNotIn("link", got)

    def test_present_but_unassociated(self):
        text = self.LIVE.replace("50.  -60.", " 0.    0.")
        got = hl.parse_proc_net_wireless(text)
        self.assertTrue(got["present"])
        self.assertEqual(got["link"], 0)

    def test_file_missing_entirely(self):
        self.assertFalse(hl.parse_proc_net_wireless(None)["present"])
        self.assertEqual(hl.parse_proc_net_wireless("")["error"], "no_output")

    def test_truncated_row(self):
        self.assertEqual(
            hl.parse_proc_net_wireless(" wlan0: 0000  50.\n")["error"], "short_row"
        )

    def test_other_interface_not_matched(self):
        # wlan1 present, wlan0 absent -> still "gone" for our purposes.
        self.assertFalse(
            hl.parse_proc_net_wireless(self.LIVE.replace("wlan0", "wlan1"))["present"]
        )

    def test_substring_interface_names_do_not_false_match(self):
        self.assertFalse(
            hl.parse_proc_net_wireless(" wlan0x: 0000 50. -60. -256\n")["present"]
        )


class TestParseNmcliState(unittest.TestCase):
    def test_connected(self):
        self.assertEqual(hl.parse_nmcli_state("connected\n"), "connected")

    def test_colon_separated(self):
        self.assertEqual(hl.parse_nmcli_state("connected:full\n"), "connected")

    def test_absent(self):
        self.assertEqual(hl.parse_nmcli_state(None), hl.UNKNOWN)
        self.assertEqual(hl.parse_nmcli_state("   "), hl.UNKNOWN)


class TestFormatRecord(unittest.TestCase):
    def test_healthy_line_omits_unset_flags(self):
        line = hl.format_record(
            hl.parse_throttled("throttled=0x0"),
            41.9,
            hl.parse_proc_net_wireless(TestParseProcNetWireless.LIVE),
            "connected",
        )
        self.assertIn("nm_state=connected", line)
        self.assertIn("wlan_level_dbm=-60", line)
        self.assertIn("soc_temp_c=41.9", line)
        self.assertIn("throttled=0x0", line)
        # A wall of flag pairs every 5 min would bury the one sample that
        # matters, so an all-clear line must carry NO flag keys at all.
        # Asserting `"false" not in line` is NOT enough: a bug that emits every
        # flag as true satisfies it while producing exactly the noise this
        # guards against. Found by mutation testing 2026-07-31.
        for flag in hl.THROTTLED_BITS.values():
            self.assertNotIn(flag, line, f"healthy line should not mention {flag}")
        self.assertNotIn("false", line)

    def test_set_flags_are_surfaced(self):
        line = hl.format_record(
            hl.parse_throttled("throttled=0x50005"), 41.9, {"present": True}, "connected"
        )
        self.assertIn("undervolt_since_boot=true", line)
        self.assertIn("throttled_now=true", line)

    def test_outage_shape_is_legible(self):
        line = hl.format_record(
            hl.parse_throttled(None), None, hl.parse_proc_net_wireless(""), hl.UNKNOWN
        )
        self.assertIn("wlan_present=false", line)
        self.assertIn("throttled=unknown", line)
        self.assertIn("soc_temp_c=-", line)

    def test_none_renders_as_dash_not_the_word_none(self):
        line = hl.format_record({"raw": "0x0"}, None, {"present": True}, "connected")
        self.assertNotIn("None", line)

    def test_is_single_line(self):
        line = hl.format_record(
            hl.parse_throttled("0x0"),
            41.9,
            hl.parse_proc_net_wireless(TestParseProcNetWireless.LIVE),
            "connected",
        )
        self.assertNotIn("\n", line)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Regression cover for the 2026-07-31 review (T-473.2). Each test below names
# the finding it pins. Three independent reviewers ran against commit 7d82d25;
# every case here was VERIFIED to fail before the fix, not taken on report.
# ---------------------------------------------------------------------------


class TestReviewFindings(unittest.TestCase):
    def test_f3_huge_temperature_does_not_raise(self):
        # int() is unbounded, the division is not. OverflowError escaped
        # `except ValueError` and killed main() before print(), so a junk
        # temperature destroyed the whole sample - including the throttle
        # flags, which are the reason the sampler exists.
        self.assertIsNone(hl.parse_soc_temp("9" * 400))

    def test_f1_unassociated_radio_reports_no_level_not_zero(self):
        # The kernel's nullstats fallback prints all-zeros with SPACE
        # separators (not dots) and noise=0 when not associated. Reporting
        # level_dbm=0 reads as a maximum-strength signal, so a graph would
        # spike UP at exactly the moment the link died.
        row = " wlan0: 0000    0     0     0        0      0      0      0\n"
        got = hl.parse_proc_net_wireless(row)
        self.assertTrue(got["present"])
        self.assertEqual(got["link"], 0)
        self.assertIsNone(got["level_dbm"])
        line = hl.format_record(hl.parse_throttled("0x0"), 41.9, got, "disconnected")
        self.assertIn("wlan_level_dbm=-", line)
        self.assertNotIn("wlan_level_dbm=0", line)

    def test_f4_bare_decimal_is_not_read_as_hex(self):
        # "50000" as hex is 0x50000 = undervolt_since_boot + throttled_since
        # _boot: a brownout that never happened, which is the worst-shaped
        # false positive this tool can emit.
        got = hl.parse_throttled("50000")
        self.assertEqual(got["error"], "not_hex")
        self.assertNotIn("undervolt_since_boot", got)

    def test_f4_bare_hex_still_accepted(self):
        self.assertEqual(hl.parse_throttled("0x50000")["raw"], "0x50000")

    def test_f5_three_field_row_keeps_its_link_and_level(self):
        # `noise` gated the length check but was never emitted, so a row
        # carrying a perfectly good link+level was thrown away. This is the
        # boundary the original test never touched: it fed a 2-field row,
        # short under either bound.
        got = hl.parse_proc_net_wireless(" wlan0: 0000   50.  -60.\n")
        self.assertNotIn("error", got)
        self.assertEqual(got["link"], 50)
        self.assertEqual(got["level_dbm"], -60)

    def test_f5_row_widths_never_raise(self):
        # The module promises it never raises; assert that across every width
        # rather than at one hand-picked point.
        for width in range(0, 12):
            row = " wlan0: " + " ".join(["0"] * width) + "\n"
            with self.subTest(width=width):
                self.assertIsInstance(hl.parse_proc_net_wireless(row), dict)

    def test_f6_colon_only_state_is_unknown_not_empty(self):
        # Returned "" and emitted a valueless logfmt key `nm_state= `.
        self.assertEqual(hl.parse_nmcli_state(":"), hl.UNKNOWN)
        self.assertEqual(hl.parse_nmcli_state("  :  "), hl.UNKNOWN)

    def test_f8_throttled_reads_only_the_first_line(self):
        self.assertEqual(hl.parse_throttled("throttled=0x0\nwarn: foo")["raw"], "0x0")

    def test_f2_run_failure_reasons_are_distinguishable(self):
        # A vcgencmd that exits non-zero (the shape of a missing video-group
        # membership) used to be byte-identical in the log to one that was
        # never installed - so the sampler's most valuable field could be dead
        # for months with nothing to indicate why.
        out, reason = hl._run(["definitely-not-a-real-binary-xyz"])
        self.assertIsNone(out)
        self.assertEqual(reason, "not_installed")
        out, reason = hl._run(["sh", "-c", "echo boom >&2; exit 3"])
        self.assertIsNone(out)
        self.assertTrue(reason.startswith("exit_3"), reason)
        self.assertIn("boom", reason)
        out, reason = hl._run(["sh", "-c", "sleep 5"], timeout=0.2)
        self.assertIsNone(out)
        self.assertEqual(reason, "timeout")
        out, reason = hl._run(["sh", "-c", "echo fine"])
        self.assertEqual(out.strip(), "fine")
        self.assertIsNone(reason)


class TestNewReadings(unittest.TestCase):
    def test_uptime(self):
        self.assertAlmostEqual(hl.parse_uptime("12345.67 98765.43\n"), 12345.67)

    def test_uptime_absent_or_junk(self):
        self.assertIsNone(hl.parse_uptime(None))
        self.assertIsNone(hl.parse_uptime(""))
        self.assertIsNone(hl.parse_uptime("banana\n"))

    def test_mem_available(self):
        text = "MemTotal:  437752 kB\nMemFree: 100 kB\nMemAvailable:  236032 kB\n"
        self.assertAlmostEqual(hl.parse_mem_available_mb(text), 230.5, places=1)

    def test_mem_available_missing_field(self):
        self.assertIsNone(hl.parse_mem_available_mb("MemTotal: 437752 kB\n"))
        self.assertIsNone(hl.parse_mem_available_mb(None))

    def test_uptime_reaches_the_line(self):
        line = hl.format_record({"raw": "0x0"}, 41.9, {"present": True},
                                "connected", uptime_s=12345.67, mem_avail_mb=230.5)
        self.assertIn("uptime_s=12345", line)
        self.assertIn("mem_avail_mb=230.5", line)


class TestMainEndToEnd(unittest.TestCase):
    """main() was covered by nothing at all - the largest gap in the suite.

    Parsing with shlex rather than substring-matching is what makes this
    structural: a line that is not logfmt fails to produce k=v tokens, so a
    changed separator or a lost quote is caught rather than tolerated.
    """

    def _run_main(self, runs, reads, push=None):
        buf = io.StringIO()
        # push_kuma is stubbed for EVERY main() test, not just the ones that
        # care. Left real, a developer with KUMA_PUSH_URL exported in their
        # shell would have the suite fire live heartbeats at the production
        # monitor and quietly hold it green - a test suite that falsifies the
        # very signal it is testing.
        with mock.patch.object(hl, "_run", side_effect=lambda cmd, timeout=5.0: runs(cmd)), \
             mock.patch.object(hl, "_read", side_effect=reads), \
             mock.patch.object(hl, "push_kuma", side_effect=push or (lambda *a, **k: "ok")), \
             contextlib.redirect_stdout(buf):
            rc = hl.main()
        return rc, buf.getvalue()

    HEALTHY_READS = staticmethod(lambda p: {
        "/sys/class/thermal/thermal_zone0/temp": "41923\n",
        "/proc/net/wireless": TestParseProcNetWireless.LIVE,
        "/proc/uptime": "12345.67 98765.43\n",
        "/proc/meminfo": "MemAvailable:  236032 kB\n",
    }.get(p))

    def test_emits_exactly_one_parseable_logfmt_line(self):
        rc, out = self._run_main(
            lambda cmd: ("throttled=0x0\n", None) if cmd[0] == "vcgencmd"
            else ("connected\n", None),
            self.HEALTHY_READS,
        )
        self.assertEqual(rc, 0)
        self.assertTrue(out.endswith("\n"), "line must be newline-terminated")
        self.assertEqual(len(out.splitlines()), 1, "must be exactly one line")
        rec = dict(t.partition("=")[::2] for t in shlex.split(out.strip()))
        self.assertEqual(rec["throttled"], "0x0")
        self.assertEqual(rec["soc_temp_c"], "41.9")
        self.assertEqual(rec["nm_state"], "connected")
        self.assertEqual(rec["wlan_level_dbm"], "-60")
        self.assertEqual(rec["uptime_s"], "12345")

    def test_state_with_spaces_survives_as_one_token(self):
        # "connected (local only)" and "connected (site only)" are real
        # NetworkManager global states (verified in nmcli's nm_state_to_string)
        # and are exactly the partial-connectivity cases this sampler is for.
        # Unquoted they would shred the line into bogus keys.
        rc, out = self._run_main(
            lambda cmd: ("throttled=0x0\n", None) if cmd[0] == "vcgencmd"
            else ("connected (local only)\n", None),
            self.HEALTHY_READS,
        )
        rec = dict(t.partition("=")[::2] for t in shlex.split(out.strip()))
        self.assertEqual(rec["nm_state"], "connected (local only)")
        self.assertEqual(len(out.splitlines()), 1)

    def test_total_outage_still_emits_a_line_with_reasons(self):
        # Everything missing is the shape during the very incident this
        # exists for. It must still print, and must say WHY each field is gone.
        rc, out = self._run_main(lambda cmd: (None, "not_installed"), lambda p: None)
        self.assertEqual(rc, 0)
        self.assertEqual(len(out.splitlines()), 1)
        rec = dict(t.partition("=")[::2] for t in shlex.split(out.strip()))
        self.assertEqual(rec["throttled"], "unknown")
        self.assertEqual(rec["throttled_error"], "not_installed")
        self.assertEqual(rec["wlan_present"], "false")
        self.assertEqual(rec["nm_error"], "not_installed")

    def test_junk_temperature_does_not_lose_the_sample(self):
        # The F3 crash, at the level that matters: an unparseable temperature
        # must not take the throttle flags down with it.
        rc, out = self._run_main(
            lambda cmd: ("throttled=0x50005\n", None) if cmd[0] == "vcgencmd"
            else ("connected\n", None),
            lambda p: "9" * 400 if "thermal" in p else self.HEALTHY_READS(p),
        )
        self.assertEqual(rc, 0)
        rec = dict(t.partition("=")[::2] for t in shlex.split(out.strip()))
        self.assertEqual(rec["soc_temp_c"], "-")
        self.assertEqual(rec["throttled"], "0x50005")
        self.assertEqual(rec["undervolt_since_boot"], "true")


class TestParseSystemdTimespan(unittest.TestCase):
    """systemd 252 renders monotonic timestamps as a span, not raw microseconds.

    Every literal below was copied from the live Pi's `systemctl show --value
    -p LastTriggerUSecMonotonic` output rather than invented, because the whole
    risk in this parser is assuming a format the vendor does not actually emit.
    """

    def test_hours_and_seconds_live_sample(self):
        # gardyn-netwatch.timer, 2026-07-31. Note systemd OMITS a zero minutes
        # component, which is precisely what breaks a naive positional parser.
        self.assertAlmostEqual(hl.parse_systemd_timespan("1h 32.020246s"), 3632.020246)

    def test_minutes_and_milliseconds_live_sample(self):
        # systemd-tmpfiles-clean.timer, same host and moment.
        self.assertAlmostEqual(hl.parse_systemd_timespan("15min 203.146ms"), 900.203146)

    def test_bare_zero_means_never_fired_this_boot(self):
        # Every idle timer on the host reads exactly this.
        self.assertEqual(hl.parse_systemd_timespan("0"), 0.0)

    def test_min_is_not_confused_with_ms(self):
        # The one genuinely ambiguous pair: both units begin with "m", and
        # getting it backwards is a 60,000x error in the safe-looking direction.
        self.assertEqual(hl.parse_systemd_timespan("5min"), 300.0)
        self.assertAlmostEqual(hl.parse_systemd_timespan("5ms"), 0.005)

    def test_us_is_not_confused_with_s(self):
        self.assertAlmostEqual(hl.parse_systemd_timespan("500us"), 0.0005)

    def test_full_ladder(self):
        self.assertEqual(hl.parse_systemd_timespan("1d 2h 3min 4s"), 93784.0)

    def test_untrustworthy_input_is_none_not_a_guess(self):
        for raw in (None, "", "   ", "infinity", "banana", "1h banana",
                    "1h 30", "-5s", "h", "1hh"):
            with self.subTest(raw=raw):
                self.assertIsNone(hl.parse_systemd_timespan(raw),
                                  f"{raw!r} must not parse to a number")


class TestParseShowProperties(unittest.TestCase):
    def test_live_sample(self):
        raw = ("LastTriggerUSec=Fri 2026-07-31 21:00:03 MDT\n"
               "LastTriggerUSecMonotonic=1h 32.020246s\n"
               "ActiveState=active\n"
               "UnitFileState=enabled\n")
        got = hl.parse_show_properties(raw)
        self.assertEqual(got["UnitFileState"], "enabled")
        self.assertEqual(got["LastTriggerUSecMonotonic"], "1h 32.020246s")

    def test_value_containing_equals_survives(self):
        got = hl.parse_show_properties("Environment=FOO=bar=baz\n")
        self.assertEqual(got["Environment"], "FOO=bar=baz")

    def test_missing_unit_yields_blank_not_absent_key(self):
        # `systemctl show` on a unit that does not exist exits 0 and prints an
        # EMPTY UnitFileState. That blank IS the signal, so it must survive.
        got = hl.parse_show_properties("UnitFileState=\nActiveState=inactive\n")
        self.assertEqual(got["UnitFileState"], "")
        self.assertIn("UnitFileState", got)

    def test_no_output(self):
        self.assertEqual(hl.parse_show_properties(None), {})


class TestEvaluateNetwatch(unittest.TestCase):
    """The four failure modes T-479 names, plus the three-valued verdict.

    None of these can be produced on the live Pi without disabling the real
    watchdog, so this class is the only place most of them are ever exercised.
    """

    HEALTHY_TIMER = {"UnitFileState": "enabled", "ActiveState": "active",
                     "LastTriggerUSecMonotonic": "1h 32.020246s"}
    OK_SERVICE = {"Result": "success"}
    UP = 3685.70  # /proc/uptime at the moment the timer sample above was taken

    def test_healthy_host(self):
        got = hl.evaluate_netwatch(self.HEALTHY_TIMER, self.OK_SERVICE, self.UP)
        self.assertIs(got["ok"], True)
        self.assertEqual(got["reason"], "ok")
        self.assertAlmostEqual(got["age_s"], 53.679754, places=3)

    def test_timer_disabled(self):
        got = hl.evaluate_netwatch({**self.HEALTHY_TIMER, "UnitFileState": "disabled",
                                    "ActiveState": "inactive"},
                                   self.OK_SERVICE, self.UP)
        self.assertIs(got["ok"], False)
        self.assertEqual(got["reason"], "timer_disabled")

    def test_timer_masked(self):
        got = hl.evaluate_netwatch({**self.HEALTHY_TIMER, "UnitFileState": "masked"},
                                   self.OK_SERVICE, self.UP)
        self.assertIs(got["ok"], False)
        self.assertEqual(got["reason"], "timer_masked")

    def test_timer_removed_entirely(self):
        got = hl.evaluate_netwatch({"UnitFileState": "", "ActiveState": "inactive"},
                                   {}, self.UP)
        self.assertIs(got["ok"], False)
        self.assertEqual(got["reason"], "timer_absent")

    def test_timer_stopped_but_still_enabled(self):
        got = hl.evaluate_netwatch({**self.HEALTHY_TIMER, "ActiveState": "inactive"},
                                   self.OK_SERVICE, self.UP)
        self.assertIs(got["ok"], False)
        self.assertEqual(got["reason"], "timer_inactive")

    def test_timer_failed(self):
        got = hl.evaluate_netwatch({**self.HEALTHY_TIMER, "ActiveState": "failed"},
                                   self.OK_SERVICE, self.UP)
        self.assertIs(got["ok"], False)
        self.assertEqual(got["reason"], "timer_failed")

    def test_last_run_failed_is_its_own_reason(self):
        # The timer is fine and firing; the run it triggers is not. Collapsing
        # this into "stale" would point the fix at the wrong unit.
        got = hl.evaluate_netwatch(self.HEALTHY_TIMER, {"Result": "timeout"}, self.UP)
        self.assertIs(got["ok"], False)
        self.assertEqual(got["reason"], "run_timeout")

    def test_active_but_no_longer_firing(self):
        # A wedged run: systemd still lists the timer as active, but the last
        # trigger is far older than the 2-minute cadence. The ONLY axis that
        # catches this, and the reason freshness is measured at all.
        got = hl.evaluate_netwatch({**self.HEALTHY_TIMER,
                                    "LastTriggerUSecMonotonic": "10min"},
                                   self.OK_SERVICE, 1200.0)
        self.assertIs(got["ok"], False)
        self.assertEqual(got["reason"], "stale")

    def test_just_inside_the_freshness_bound_is_healthy(self):
        got = hl.evaluate_netwatch({**self.HEALTHY_TIMER,
                                    "LastTriggerUSecMonotonic": "419s"},
                                   self.OK_SERVICE, 838.0)
        self.assertIs(got["ok"], True)

    def test_never_fired_is_forgiven_only_during_boot(self):
        booting = hl.evaluate_netwatch({**self.HEALTHY_TIMER,
                                        "LastTriggerUSecMonotonic": "0"},
                                       self.OK_SERVICE, 60.0)
        self.assertIs(booting["ok"], True)
        self.assertEqual(booting["reason"], "booting")

        settled = hl.evaluate_netwatch({**self.HEALTHY_TIMER,
                                        "LastTriggerUSecMonotonic": "0"},
                                       self.OK_SERVICE, 9000.0)
        self.assertIs(settled["ok"], False)
        self.assertEqual(settled["reason"], "never_triggered")

    def test_unreadable_clock_reports_up_with_the_blindness_named(self):
        # Enabled and active still say healthy; only freshness is blind. Both
        # directions are wrong here - a fabricated DOWN pages for a working
        # watchdog, a silent UP hides that one axis went dark.
        for timer, uptime in (({**self.HEALTHY_TIMER,
                               "LastTriggerUSecMonotonic": "infinity"}, self.UP),
                              (self.HEALTHY_TIMER, None),
                              ({**self.HEALTHY_TIMER,
                                "LastTriggerUSecMonotonic": "9h"}, 100.0)):
            with self.subTest(timer=timer, uptime=uptime):
                got = hl.evaluate_netwatch(timer, self.OK_SERVICE, uptime)
                self.assertIs(got["ok"], True)
                self.assertEqual(got["reason"], "age_unknown")
                self.assertIsNone(got["age_s"])

    def test_probe_failure_is_dont_know_not_no(self):
        # `systemctl` itself unrunnable. Neither verdict is honest, so the
        # caller must push nothing and let Kuma's timeout speak.
        got = hl.evaluate_netwatch({}, {}, self.UP, probe_error="not_installed")
        self.assertIsNone(got["ok"])
        self.assertEqual(got["reason"], "probe_not_installed")


class TestFormatPush(unittest.TestCase):
    def test_healthy_becomes_up_with_the_age(self):
        status, msg = hl.format_push({"ok": True, "reason": "ok", "age_s": 53.7})
        self.assertEqual(status, "up")
        self.assertEqual(msg, "netwatch ok (last run 53s ago)")

    def test_fault_becomes_down_and_names_it(self):
        status, msg = hl.format_push({"ok": False, "reason": "timer_masked",
                                      "age_s": None})
        self.assertEqual(status, "down")
        self.assertEqual(msg, "netwatch timer_masked")

    def test_unmeasurable_pushes_nothing(self):
        self.assertIsNone(hl.format_push({"ok": None, "reason": "probe_timeout"}))

    def test_message_is_ascii(self):
        # The homelab's shared kuma_push() helper URL-encodes only spaces, so a
        # non-ASCII character 400s the push and drops the heartbeat silently.
        # This sender encodes properly, but the constraint is kept so the
        # message stays portable to that helper.
        for reason in ("ok", "timer_masked", "run_timeout", "never_triggered"):
            _, msg = hl.format_push({"ok": False, "reason": reason, "age_s": 1.0})
            msg.encode("ascii")


class TestPushKuma(unittest.TestCase):
    class _Response:
        def __init__(self, body):
            self._body = body.encode()

        def read(self, n=None):
            return self._body[:n] if n else self._body

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def _push(self, body="{\"ok\":true}", url="http://kuma.example/api/push/TOKEN",
              raises=None):
        seen = {}

        def fake_urlopen(target, timeout=None):
            seen["url"] = target
            if raises is not None:
                raise raises
            return self._Response(body)

        with mock.patch.object(hl.urllib.request, "urlopen", fake_urlopen):
            outcome = hl.push_kuma(url, "up", "netwatch ok")
        return outcome, seen.get("url")

    def test_accepted_push(self):
        outcome, url = self._push()
        self.assertEqual(outcome, "ok")
        self.assertIn("status=up", url)

    def test_no_url_configured_is_a_skip_not_a_failure(self):
        self.assertEqual(hl.push_kuma(None, "up", "x"), "skipped_no_url")
        self.assertEqual(hl.push_kuma("", "up", "x"), "skipped_no_url")

    def test_http_200_with_ok_false_is_a_failure(self):
        # Kuma answers a REJECTED push with 200 and {"ok":false}. Trusting the
        # status code alone is the documented way this goes silently wrong.
        outcome, _ = self._push(body='{"ok":false,"msg":"Monitor not found"}')
        self.assertEqual(outcome, "failed_not_ok")

    def test_unparseable_body_is_a_failure(self):
        outcome, _ = self._push(body="<html>502 Bad Gateway</html>")
        self.assertEqual(outcome, "failed_bad_body")

    def test_baked_in_template_query_is_stripped(self):
        # Kuma's UI shows the push URL complete with ?status=up&msg=OK&ping=.
        # Stored verbatim and appended to, the duplicate keys make Kuma record
        # msg="[object Object]" while returning 200 the whole time.
        _, url = self._push(url="http://kuma.example/api/push/TOKEN?status=up&msg=OK&ping=")
        self.assertEqual(url.count("status="), 1)
        self.assertEqual(url.count("msg="), 1)

    def test_transport_failure_never_leaks_the_token(self):
        # HTTPError.__str__ embeds the full URL. The journal on this host is
        # persistent, so a leaked token would survive reboots.
        outcome, _ = self._push(
            raises=hl.urllib.error.HTTPError(
                "http://kuma.example/api/push/SUPERSECRETTOKEN", 404,
                "Not Found", {}, None))
        self.assertEqual(outcome, "failed_HTTPError")
        self.assertNotIn("SUPERSECRETTOKEN", outcome)

    def test_network_down_is_reported_not_raised(self):
        outcome, _ = self._push(raises=OSError("Network is unreachable"))
        self.assertEqual(outcome, "failed_OSError")
