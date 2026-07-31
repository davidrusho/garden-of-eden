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

    def _run_main(self, runs, reads):
        buf = io.StringIO()
        with mock.patch.object(hl, "_run", side_effect=lambda cmd, timeout=5.0: runs(cmd)), \
             mock.patch.object(hl, "_read", side_effect=reads), \
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
