"""Tests for bin/gardyn-health-log.py (T-473.2).

Hardware-free — every parser is a pure function over a string, so the branches
the live Pi cannot be made to produce on demand are covered here and ONLY here:
a vanished radio, a missing vcgencmd, malformed hex, a truncated /proc row.
Those are exactly the readings that matter during an outage, so the suite is
the only place they are ever exercised.

Run from the repo root:

    python3 -m unittest tests.test_health_log
"""
import importlib.util
import pathlib
import unittest

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
