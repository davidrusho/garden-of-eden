"""Tests for bin/gardyn-netwatch.py (T-473.4).

Hardware-free. `decide()` is a pure function of (uptime, reachability, prior
state, boot id), which is the whole reason it exists in that shape: the reboot
branch cannot be exercised on a live host without rebooting it, and the
reboot-cap, just-booted and flapping-link branches cannot be exercised at all
without waiting out a real outage. This suite is the only place any of them
ever runs.

Run from the repo root:

    python3 -m unittest tests.test_netwatch
"""
import importlib.util
import io
import json
import pathlib
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

# bin/ is not a package and the file has a hyphen, so import it by path.
_SRC = pathlib.Path(__file__).resolve().parents[1] / "bin" / "gardyn-netwatch.py"
_spec = importlib.util.spec_from_file_location("gardyn_netwatch", _SRC)
if _spec is None or _spec.loader is None:  # pragma: no cover - import plumbing
    raise ImportError(f"cannot load {_SRC}")
nw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nw)

BOOT = "boot-aaaa"
TCP_KEY = f"tcp_{nw.TCP_PROBE_HOST}_{nw.TCP_PROBE_PORT}"


def state(first=None, failures=0, reconnects=0, reboots=0, streak=0, boot_id=BOOT):
    return {
        "boot_id": boot_id,
        "first_failure_uptime": first,
        "failures": failures,
        "reconnects": reconnects,
        "consecutive_reboots": reboots,
        "healthy_streak": streak,
    }


# --------------------------------------------------------------------------
# The three ladder cases the ticket names, plus what sits between them.
# --------------------------------------------------------------------------
class TestLadder(unittest.TestCase):
    def test_healthy_does_nothing(self):
        action, reason, rc, new = nw.decide(50_000.0, True, state(), BOOT)
        self.assertEqual(action, nw.ACT_NONE)
        self.assertIn("reachable", reason)
        self.assertFalse(rc)
        self.assertIsNone(new["first_failure_uptime"])

    def test_first_failure_reconnects_and_does_not_reboot(self):
        action, reason, rc, new = nw.decide(50_000.0, False, state(), BOOT)
        self.assertEqual(action, nw.ACT_RECONNECT)
        self.assertEqual(reason, "first_failure")
        self.assertTrue(rc)
        self.assertEqual(new["reconnects"], 1)
        self.assertEqual(new["first_failure_uptime"], 50_000.0)
        self.assertEqual(new["consecutive_reboots"], 0)

    def test_reconnect_succeeded_so_the_next_check_clears_the_outage(self):
        _, _, _, after = nw.decide(50_000.0, False, state(), BOOT)
        action, _, _, new = nw.decide(50_120.0, True, after, BOOT)
        self.assertEqual(action, nw.ACT_NONE)
        self.assertIsNone(new["first_failure_uptime"])
        self.assertEqual(new["reconnects"], 0)
        self.assertEqual(new["failures"], 0)

    def test_second_failure_still_reconnects(self):
        prior = state(first=50_000.0, failures=1, reconnects=1)
        action, _, rc, new = nw.decide(50_120.0, False, prior, BOOT)
        self.assertEqual(action, nw.ACT_RECONNECT)
        self.assertTrue(rc)
        self.assertEqual(new["reconnects"], 2)

    def test_third_failure_backs_off_instead_of_bouncing_the_link(self):
        """A reconnect deactivates and reactivates wlan0, dropping mqtt.py's
        broker session. Doing that every 2 minutes through an upstream outage
        is the watchdog becoming the outage."""
        prior = state(first=50_000.0, failures=2, reconnects=2)
        action, reason, rc, new = nw.decide(50_240.0, False, prior, BOOT)
        self.assertEqual(action, nw.ACT_WAIT)
        self.assertIn("backoff", reason)
        self.assertFalse(rc)
        self.assertEqual(new["reconnects"], 2)

    def test_backoff_retries_on_the_fifth_failure(self):
        prior = state(first=50_000.0, failures=4, reconnects=2)
        action, _, rc, new = nw.decide(50_000.0 + 8, False, prior, BOOT)
        self.assertEqual(action, nw.ACT_RECONNECT)
        self.assertTrue(rc)
        self.assertEqual(new["reconnects"], 3)

    def test_sustained_outage_reboots(self):
        prior = state(first=50_000.0, failures=9, reconnects=2)
        action, reason, rc, new = nw.decide(50_000.0 + nw.REBOOT_AFTER_DOWN_S,
                                            False, prior, BOOT)
        self.assertEqual(action, nw.ACT_REBOOT)
        self.assertIn("down_300s", reason)
        self.assertFalse(rc)
        self.assertEqual(new["consecutive_reboots"], 1)

    def test_reboot_threshold_is_not_reached_one_second_early(self):
        prior = state(first=50_000.0, failures=9, reconnects=2)
        action, _, _, _ = nw.decide(50_000.0 + nw.REBOOT_AFTER_DOWN_S - 1,
                                    False, prior, BOOT)
        self.assertNotEqual(action, nw.ACT_REBOOT)

    def test_reboot_requires_a_reconnect_to_have_been_tried_first(self):
        prior = state(first=50_000.0, failures=0, reconnects=0)
        action, reason, _, _ = nw.decide(50_000.0 + 9_999, False, prior, BOOT)
        self.assertEqual(action, nw.ACT_RECONNECT)
        self.assertEqual(reason, "first_failure")

    def test_reboot_clears_the_down_clock_and_the_attempt_counters(self):
        """Uptime resets at boot, so carrying a pre-reboot reading into the
        next boot's ladder would let it reboot again immediately."""
        prior = state(first=50_000.0, failures=9, reconnects=2)
        _, _, _, new = nw.decide(50_400.0, False, prior, BOOT)
        self.assertIsNone(new["first_failure_uptime"])
        self.assertEqual(new["reconnects"], 0)
        self.assertEqual(new["failures"], 0)


# --------------------------------------------------------------------------
# The guards. None of these can be reached on a live host.
# --------------------------------------------------------------------------
class TestRebootGuards(unittest.TestCase):
    def test_just_booted_host_is_never_rebooted(self):
        prior = state(first=0.0, failures=9, reconnects=2)
        action, reason, _, _ = nw.decide(nw.MIN_UPTIME_BEFORE_REBOOT_S - 1,
                                         False, prior, BOOT)
        self.assertEqual(action, nw.ACT_REBOOT_SUPPRESSED)
        self.assertEqual(reason, "uptime_too_low")

    def test_uptime_guard_releases_once_the_host_has_been_up_long_enough(self):
        prior = state(first=0.0, failures=9, reconnects=2)
        action, _, _, _ = nw.decide(nw.MIN_UPTIME_BEFORE_REBOOT_S + 1,
                                    False, prior, BOOT)
        self.assertEqual(action, nw.ACT_REBOOT)

    def test_uptime_guard_releases_exactly_AT_the_threshold(self):
        """The guard reads 'up for at least MIN_UPTIME', so the boundary value
        itself must pass. Tests at +/-1 leave the exact case unpinned, and a
        `<` -> `<=` mutation survived the battery on that gap."""
        prior = state(first=0.0, failures=9, reconnects=2)
        action, _, _, _ = nw.decide(nw.MIN_UPTIME_BEFORE_REBOOT_S, False, prior, BOOT)
        self.assertEqual(action, nw.ACT_REBOOT)

    def test_reboot_cap_stops_an_endless_power_cycle(self):
        """Literal 2, not MAX_CONSECUTIVE_REBOOTS: a test written against the
        symbol moves with the constant, so widening the cap to 99 — an endless
        power-cycle, the worst defect this file can carry — stayed green
        through the whole suite. Caught by the mutation battery."""
        prior = state(first=50_000.0, failures=9, reconnects=2, reboots=2)
        action, reason, _, new = nw.decide(50_400.0, False, prior, BOOT)
        self.assertEqual(action, nw.ACT_REBOOT_SUPPRESSED)
        self.assertEqual(reason, "reboot_cap_reached")
        self.assertEqual(new["consecutive_reboots"], 2)

    def test_one_below_the_cap_still_reboots(self):
        prior = state(first=50_000.0, failures=9, reconnects=2, reboots=1)
        action, _, _, new = nw.decide(50_400.0, False, prior, BOOT)
        self.assertEqual(action, nw.ACT_REBOOT)
        self.assertEqual(new["consecutive_reboots"], 2)

    def test_a_suppressed_reboot_still_retries_the_cheap_fix(self):
        """Otherwise a capped ladder strands doing nothing at all."""
        prior = state(first=50_000.0, failures=9, reconnects=2, reboots=2)
        _, _, rc, new = nw.decide(50_400.0, False, prior, BOOT)
        self.assertTrue(rc)
        self.assertEqual(new["reconnects"], 3)

    def test_a_suppressed_reboot_honours_the_backoff(self):
        prior = state(first=50_000.0, failures=10, reconnects=3, reboots=2)
        _, _, rc, new = nw.decide(50_400.0, False, prior, BOOT)
        self.assertFalse(rc)
        self.assertEqual(new["reconnects"], 3)


# --------------------------------------------------------------------------
# The flapping-link defect: a single lucky tick must not re-arm the cap.
# Measured before the fix at 96 reboots/day against a nominal cap of 2.
# --------------------------------------------------------------------------
class TestHealthyStreak(unittest.TestCase):
    def test_one_healthy_tick_does_not_rearm_the_cap(self):
        prior = state(reboots=2)
        _, _, _, new = nw.decide(50_000.0, True, prior, BOOT)
        self.assertEqual(new["consecutive_reboots"], 2)
        self.assertEqual(new["healthy_streak"], 1)

    def test_sustained_health_rearms_the_cap(self):
        st = state(reboots=2)
        for _ in range(15):
            _, _, _, st = nw.decide(50_000.0, True, st, BOOT)
        self.assertEqual(st["healthy_streak"], 15)
        self.assertEqual(st["consecutive_reboots"], 0)

    def test_one_tick_short_of_the_streak_keeps_the_cap(self):
        st = state(reboots=2)
        for _ in range(14):
            _, _, _, st = nw.decide(50_000.0, True, st, BOOT)
        self.assertEqual(st["consecutive_reboots"], 2)

    def test_a_failure_resets_the_streak(self):
        st = state(reboots=1, streak=10)
        _, _, _, st = nw.decide(50_000.0, False, st, BOOT)
        self.assertEqual(st["healthy_streak"], 0)


# --------------------------------------------------------------------------
# Cross-boot state. Settled by boot_id, not by uptime arithmetic — the old
# `first > uptime` test only ever caught a stored value that was LARGER.
# --------------------------------------------------------------------------
class TestCrossBoot(unittest.TestCase):
    def test_state_from_a_previous_boot_is_discarded(self):
        prior = state(first=100.0, failures=9, reconnects=2, boot_id="boot-OLD")
        action, reason, _, new = nw.decide(700.0, False, prior, BOOT)
        self.assertEqual(new["first_failure_uptime"], 700.0)
        self.assertEqual(action, nw.ACT_RECONNECT)
        self.assertEqual(reason, "first_failure")
        self.assertEqual(new["failures"], 1)

    def test_the_reboot_cap_SURVIVES_a_boot_change(self):
        """The one field that must cross boots — it is the guard against this
        unit power-cycling the host forever."""
        prior = state(first=100.0, reconnects=2, reboots=2, boot_id="boot-OLD")
        _, _, _, new = nw.decide(700.0, False, prior, BOOT)
        self.assertEqual(new["consecutive_reboots"], 2)

    def test_same_boot_state_is_kept(self):
        prior = state(first=1_000.0, failures=1, reconnects=1, boot_id=BOOT)
        _, reason, _, new = nw.decide(1_250.0, False, prior, BOOT)
        self.assertEqual(new["first_failure_uptime"], 1_000.0)
        self.assertIn("retry", reason)

    def test_new_boot_id_is_recorded(self):
        _, _, _, new = nw.decide(50_000.0, False, state(boot_id="boot-OLD"), BOOT)
        self.assertEqual(new["boot_id"], BOOT)

    def test_a_larger_stored_uptime_is_still_clamped(self):
        """Belt and braces for a same-boot_id state written by a clock that
        somehow ran backwards."""
        prior = state(first=50_000.0, failures=1, reconnects=2, boot_id=BOOT)
        _, _, _, new = nw.decide(30.0, False, prior, BOOT)
        self.assertEqual(new["first_failure_uptime"], 30.0)


# --------------------------------------------------------------------------
# Multi-tick simulation: the shape of test that would have caught the
# flapping-cap defect, the unbounded-loop defect and the reordering defect in
# one go. Every ladder test above is a single decide() call with a hand-built
# prior; this drives real sequences.
# --------------------------------------------------------------------------
class TestTickSequences(unittest.TestCase):
    TICK = 120.0

    def _run(self, reach_at, hours=24, start_uptime=700.0):
        st = dict(nw.EMPTY_STATE)
        st["boot_id"] = BOOT
        uptime = start_uptime
        boot = BOOT
        reboots = 0
        t = 0.0
        while t < hours * 3600:
            action, _, _, st = nw.decide(uptime, reach_at(t), st, boot)
            if action == nw.ACT_REBOOT:
                reboots += 1
                boot = f"boot-{reboots}"
                uptime = 0.0
            else:
                uptime += self.TICK
            t += self.TICK
        return reboots

    def test_continuous_outage_stops_at_the_cap(self):
        self.assertEqual(self._run(lambda t: False), 2)

    def test_a_flapping_link_does_not_reboot_forever(self):
        """Before the healthy-streak guard this produced 96, 96 and 48 reboots
        a day for a 15/30/60-minute flap. The cap only bounded a CONTINUOUS
        outage, and a marginal radio is at least as likely as a dead one."""
        for period_min in (15, 30, 60):
            with self.subTest(period_min=period_min):
                reboots = self._run(
                    lambda t, p=period_min: (t % (p * 60)) < self.TICK)
                self.assertLessEqual(reboots, 2)

    def test_a_healthy_network_never_reboots(self):
        self.assertEqual(self._run(lambda t: True), 0)

    def test_recovery_after_an_outage_rearms_then_a_new_outage_can_reboot(self):
        # Down for 2h, up for 2h (well past the 30-min streak), down again.
        def profile(t):
            return 7200 <= t < 14400
        self.assertGreater(self._run(profile, hours=6), 2)

    def test_first_reboot_lands_no_earlier_than_the_threshold(self):
        st = dict(nw.EMPTY_STATE)
        st["boot_id"] = BOOT
        uptime = 700.0
        for tick in range(200):
            action, _, _, st = nw.decide(uptime, False, st, BOOT)
            if action == nw.ACT_REBOOT:
                self.assertGreaterEqual(uptime - 700.0, nw.REBOOT_AFTER_DOWN_S)
                return
            uptime += self.TICK
        self.fail("no reboot within 200 ticks of a continuous outage")


# --------------------------------------------------------------------------
# State file.
# --------------------------------------------------------------------------
class TestLoadState(unittest.TestCase):
    def test_missing_file_is_a_clean_slate(self):
        self.assertEqual(nw.load_state(None), nw.EMPTY_STATE)

    def test_corrupt_json_is_a_clean_slate(self):
        self.assertEqual(nw.load_state('{"first_failure_uptime": '), nw.EMPTY_STATE)

    def test_non_object_json_is_a_clean_slate(self):
        self.assertEqual(nw.load_state("[1, 2, 3]"), nw.EMPTY_STATE)

    def test_round_trip(self):
        got = nw.load_state(json.dumps(state(first=12.5, failures=4, reconnects=3,
                                             reboots=1, streak=2)))
        self.assertEqual(got["first_failure_uptime"], 12.5)
        self.assertEqual(got["failures"], 4)
        self.assertEqual(got["reconnects"], 3)
        self.assertEqual(got["consecutive_reboots"], 1)
        self.assertEqual(got["healthy_streak"], 2)
        self.assertEqual(got["boot_id"], BOOT)

    def test_negative_and_wrong_typed_fields_fall_back(self):
        got = nw.load_state(json.dumps({
            "first_failure_uptime": -5, "reconnects": "many",
            "consecutive_reboots": -1, "boot_id": 17,
        }))
        self.assertIsNone(got["first_failure_uptime"])
        self.assertEqual(got["reconnects"], 0)
        self.assertEqual(got["consecutive_reboots"], 0)
        self.assertIsNone(got["boot_id"])

    def test_booleans_are_not_accepted_as_counts(self):
        """bool subclasses int; True would otherwise load as 1 reboot."""
        self.assertEqual(nw.load_state(json.dumps({"consecutive_reboots": True}))
                         ["consecutive_reboots"], 0)

    def test_non_finite_uptime_in_the_state_file_is_rejected(self):
        self.assertIsNone(nw.load_state('{"first_failure_uptime": 1e400}')
                          ["first_failure_uptime"])

    def test_zero_is_a_legitimate_first_failure_uptime(self):
        self.assertEqual(nw.load_state('{"first_failure_uptime": 0}')
                         ["first_failure_uptime"], 0.0)

    def test_save_then_load_survives_a_real_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(pathlib.Path(tmp) / "sub" / "state.json")
            self.assertTrue(nw.save_state(path, state(first=7.5, reboots=1)))
            got = nw.load_state(pathlib.Path(path).read_text())
            self.assertEqual(got["first_failure_uptime"], 7.5)
            self.assertEqual(got["consecutive_reboots"], 1)

    def test_save_state_reports_FAILURE_on_an_unwritable_path(self):
        """The return value is load-bearing: the reboot cap lives in this
        file, so the reboot path must be able to tell that it did not land.
        The earlier version returned None and the caller rebooted anyway."""
        self.assertFalse(nw.save_state("/proc/nope/state.json", state()))


class TestParseUptime(unittest.TestCase):
    def test_normal(self):
        self.assertAlmostEqual(nw.parse_uptime("12345.67 98765.43"), 12345.67)

    def test_garbage_and_empty(self):
        for raw in (None, "", "not-a-number", "   "):
            self.assertIsNone(nw.parse_uptime(raw))

    def test_non_finite_is_rejected(self):
        """NaN compares False against every threshold, so it would fall
        through all three reboot guards and then crash at int(nan)."""
        for raw in ("nan 0", "inf 0", "-inf 0", "1e400 0"):
            self.assertIsNone(nw.parse_uptime(raw))

    def test_negative_is_rejected(self):
        self.assertIsNone(nw.parse_uptime("-5 0"))


# --------------------------------------------------------------------------
# Probes. The tri-state is the point: "could not ask" is not "the answer is
# no", and collapsing them turns a local fault into a reboot.
# --------------------------------------------------------------------------
class TestPing(unittest.TestCase):
    def test_reply_is_reachable(self):
        with mock.patch.object(nw.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            self.assertIs(nw.ping("192.168.1.1"), True)
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["ping", "-w", nw.PING_DEADLINE_S, "192.168.1.1"])

    def test_no_count_flag_is_passed(self):
        """ping(8): with a count AND a deadline, receiving fewer than count
        packets before the deadline ALSO exits 1. Verified on the host:
        `ping -c 2 -w 1 <live gateway>` returns 1. With a deadline alone,
        exit 0 <=> at least one reply, which is the question being asked."""
        with mock.patch.object(nw.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            nw.ping("192.168.1.1")
        self.assertNotIn("-c", run.call_args[0][0])

    def test_exit_1_is_no_answer(self):
        with mock.patch.object(nw.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertIs(nw.ping("192.168.1.1"), False)

    def test_exit_2_is_could_not_measure(self):
        """ping exits 2 for errors that say nothing about the network."""
        with mock.patch.object(nw.subprocess, "run", return_value=mock.Mock(returncode=2)):
            self.assertIsNone(nw.ping("192.168.1.1"))

    def test_timeout_is_could_not_measure(self):
        with mock.patch.object(nw.subprocess, "run",
                               side_effect=nw.subprocess.TimeoutExpired("ping", 8)):
            self.assertIsNone(nw.ping("192.168.1.1"))

    def test_missing_binary_or_fork_failure_is_could_not_measure(self):
        """Under a memory limit a fork failure raises OSError(ENOMEM). Reading
        that as 'the LAN is down' would reboot a healthy host."""
        with mock.patch.object(nw.subprocess, "run", side_effect=OSError(12, "ENOMEM")):
            self.assertIsNone(nw.ping("192.168.1.1"))


class TestTcpProbe(unittest.TestCase):
    def test_connect_is_reachable(self):
        with mock.patch.object(nw.socket, "create_connection", return_value=mock.MagicMock()):
            self.assertIs(nw.tcp_probe(), True)

    def test_refused_is_still_reachable(self):
        """Something answered — the path is up and the port is shut."""
        with mock.patch.object(nw.socket, "create_connection",
                               side_effect=ConnectionRefusedError()):
            self.assertIs(nw.tcp_probe(), True)

    def test_unreachable_is_false(self):
        with mock.patch.object(nw.socket, "create_connection",
                               side_effect=OSError(113, "no route")):
            self.assertIs(nw.tcp_probe(), False)

    def test_uses_the_broker_and_a_bounded_timeout(self):
        with mock.patch.object(nw.socket, "create_connection",
                               return_value=mock.MagicMock()) as conn:
            nw.tcp_probe()
        self.assertEqual(conn.call_args[0][0], (nw.TCP_PROBE_HOST, nw.TCP_PROBE_PORT))
        self.assertEqual(conn.call_args[1]["timeout"], nw.TCP_TIMEOUT_S)


class TestReconnect(unittest.TestCase):
    def test_uses_connection_up_with_the_pinned_uuid(self):
        """`nmcli device reconnect` does not exist in 1.42.4; it exits 2. A
        watchdog built on it would look healthy and never recover the link.
        `nmcli device reapply` reports success without re-activating."""
        with mock.patch.object(nw.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(nw.reconnect(), "reconnect_ok")
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["nmcli", "--wait", str(nw.RECONNECT_NMCLI_WAIT_S),
                                "connection", "up", "uuid", nw.WLAN_UUID])
        self.assertNotIn("reconnect", argv)
        self.assertNotIn("reapply", argv)

    def test_nmcli_wait_is_below_the_subprocess_backstop(self):
        """Otherwise the subprocess SIGKILLs nmcli before it can report its
        own documented exit 3, and the diagnostic is lost."""
        self.assertLess(nw.RECONNECT_NMCLI_WAIT_S, nw.RECONNECT_TIMEOUT_S)

    def test_failure_timeout_and_oserror_are_reported_distinctly(self):
        with mock.patch.object(nw.subprocess, "run", return_value=mock.Mock(returncode=4)):
            self.assertEqual(nw.reconnect(), "reconnect_exit_4")
        with mock.patch.object(nw.subprocess, "run",
                               side_effect=nw.subprocess.TimeoutExpired("nmcli", 60)):
            self.assertEqual(nw.reconnect(), "reconnect_timeout")
        with mock.patch.object(nw.subprocess, "run", side_effect=OSError(2, "gone")):
            self.assertEqual(nw.reconnect(), "reconnect_oserror_2")


class TestReboot(unittest.TestCase):
    """The highest-consequence function in the file, and it previously had
    ZERO coverage — a mutation to `systemctl poweroff` survived the whole
    suite, which would permanently power off a headless garden controller."""

    def test_orders_a_reboot_and_nothing_else(self):
        with mock.patch.object(nw.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(nw.reboot(), "reboot_ordered")
        self.assertEqual(run.call_args[0][0], ["systemctl", "reboot"])

    def test_never_powers_off_and_never_forces(self):
        with mock.patch.object(nw.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            nw.reboot()
        argv = run.call_args[0][0]
        self.assertNotIn("poweroff", argv)
        self.assertNotIn("halt", argv)
        self.assertNotIn("--force", argv)

    def test_non_zero_exit_is_NOT_reported_as_ordered(self):
        """A reboot that did not happen must not burn a slot off the cap; two
        of those would strand the ladder at reboot_suppressed forever."""
        with mock.patch.object(nw.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertEqual(nw.reboot(), "reboot_exit_1")

    def test_exceptions_are_reported_not_raised(self):
        with mock.patch.object(nw.subprocess, "run", side_effect=OSError(2, "gone")):
            self.assertTrue(nw.reboot().startswith("reboot_failed_"))


# --------------------------------------------------------------------------
# End to end through main().
# --------------------------------------------------------------------------
class TestMain(unittest.TestCase):
    def _run_main(self, uptime, probe_result, prior, tmpdir, tcp=None,
                  reboot_outcome="reboot_ordered", boot_id=BOOT):
        path = str(pathlib.Path(tmpdir) / "state.json")
        if prior is not None:
            nw.save_state(path, prior)

        def fake_read(p):
            if p == "/proc/uptime":
                return None if uptime is None else f"{uptime} 0"
            if p == nw.BOOT_ID_PATH:
                return boot_id
            if p == path:
                return pathlib.Path(path).read_text() if pathlib.Path(path).exists() else None
            return None

        buf = io.StringIO()
        with mock.patch.object(nw, "STATE_PATH", path), \
             mock.patch.object(nw, "_read", side_effect=fake_read), \
             mock.patch.object(nw, "ping", side_effect=lambda _t: probe_result), \
             mock.patch.object(nw, "tcp_probe",
                               return_value=probe_result if tcp is None else tcp), \
             mock.patch.object(nw, "reconnect", return_value="reconnect_ok") as rc, \
             mock.patch.object(nw, "reboot", return_value=reboot_outcome) as rb, \
             redirect_stdout(buf):
            rv = nw.main()
        self.assertEqual(rv, 0)
        after = (nw.load_state(pathlib.Path(path).read_text())
                 if pathlib.Path(path).exists() else None)
        return buf.getvalue(), rc, rb, after

    def test_healthy_run_touches_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, rc, rb, _ = self._run_main(50_000, True, None, tmp)
            self.assertIn("action=none", out)
            self.assertIn("reachable=true", out)
            rc.assert_not_called()
            rb.assert_not_called()

    def test_a_healthy_run_CLEARS_an_escalation_left_on_disk(self):
        """decide()'s clearing is unit-tested; that it ever reaches disk was
        not, and a mutation skipping the healthy-path write survived."""
        with tempfile.TemporaryDirectory() as tmp:
            prior = state(first=50_000.0, failures=9, reconnects=3)
            _, _, _, after = self._run_main(50_000, True, prior, tmp)
            assert after is not None
            self.assertIsNone(after["first_failure_uptime"])
            self.assertEqual(after["reconnects"], 0)

    def test_first_failure_reconnects_and_records_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, rc, rb, after = self._run_main(50_000, False, None, tmp)
            self.assertIn("action=reconnect", out)
            self.assertIn("outcome=reconnect_ok", out)
            rc.assert_called_once()
            rb.assert_not_called()
            assert after is not None
            self.assertEqual(after["reconnects"], 1)

    def test_sustained_outage_orders_a_reboot(self):
        with tempfile.TemporaryDirectory() as tmp:
            prior = state(first=50_000.0, failures=9, reconnects=2)
            out, rc, rb, after = self._run_main(50_400, False, prior, tmp)
            self.assertIn("action=reboot", out)
            self.assertIn("outcome=reboot_ordered", out)
            rb.assert_called_once()
            rc.assert_not_called()
            assert after is not None
            self.assertEqual(after["consecutive_reboots"], 1)

    def test_the_cap_increment_is_ON_DISK_BEFORE_reboot_is_called(self):
        """Not merely written at some point — written first. The process may
        not run again, and a reboot the file did not record is a reboot the
        cap cannot see. Asserting print-order did not test this: moving the
        save below reboot() survived the old suite."""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(pathlib.Path(tmp) / "state.json")
            nw.save_state(path, state(first=50_000.0, failures=9, reconnects=2))
            seen = {}

            def spy_reboot():
                seen["on_disk"] = nw.load_state(pathlib.Path(path).read_text())
                return "reboot_ordered"

            def fake_read(p):
                if p == "/proc/uptime":
                    return "50400 0"
                if p == nw.BOOT_ID_PATH:
                    return BOOT
                return pathlib.Path(path).read_text()

            with mock.patch.object(nw, "STATE_PATH", path), \
                 mock.patch.object(nw, "_read", side_effect=fake_read), \
                 mock.patch.object(nw, "ping", return_value=False), \
                 mock.patch.object(nw, "tcp_probe", return_value=False), \
                 mock.patch.object(nw, "reboot", side_effect=spy_reboot), \
                 redirect_stdout(io.StringIO()):
                nw.main()
            self.assertEqual(seen["on_disk"]["consecutive_reboots"], 1)

    def test_an_unwritable_state_file_SUPPRESSES_the_reboot(self):
        """The unbounded-loop defect: with the write silently swallowed, the
        cap never persisted and the host rebooted every ~10 minutes forever —
        measured at 10 reboots across 60 ticks against a nominal cap of 2."""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(pathlib.Path(tmp) / "state.json")
            nw.save_state(path, state(first=50_000.0, failures=9, reconnects=2))

            def fake_read(p):
                if p == "/proc/uptime":
                    return "50400 0"
                if p == nw.BOOT_ID_PATH:
                    return BOOT
                return pathlib.Path(path).read_text()

            buf = io.StringIO()
            with mock.patch.object(nw, "STATE_PATH", path), \
                 mock.patch.object(nw, "_read", side_effect=fake_read), \
                 mock.patch.object(nw, "ping", return_value=False), \
                 mock.patch.object(nw, "tcp_probe", return_value=False), \
                 mock.patch.object(nw, "save_state", return_value=False), \
                 mock.patch.object(nw, "reboot") as rb, \
                 redirect_stdout(buf):
                nw.main()
            rb.assert_not_called()
            self.assertIn("action=reboot_suppressed", buf.getvalue())
            self.assertIn("reason=state_unwritable", buf.getvalue())

    def test_a_failed_reboot_gives_the_cap_slot_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            prior = state(first=50_000.0, failures=9, reconnects=2)
            out, _, _, after = self._run_main(50_400, False, prior, tmp,
                                              reboot_outcome="reboot_exit_1")
            self.assertIn("outcome=reboot_exit_1", out)
            assert after is not None
            self.assertEqual(after["consecutive_reboots"], 0)

    def test_capped_run_reconnects_instead_of_rebooting(self):
        with tempfile.TemporaryDirectory() as tmp:
            prior = state(first=50_000.0, failures=9, reconnects=2, reboots=2)
            out, rc, rb, _ = self._run_main(50_400, False, prior, tmp)
            self.assertIn("action=reboot_suppressed", out)
            self.assertIn("reason=reboot_cap_reached", out)
            rb.assert_not_called()
            rc.assert_called_once()

    def test_missing_uptime_stands_down(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, rc, rb, _ = self._run_main(None, False, None, tmp)
            self.assertIn("reason=no_uptime", out)
            self.assertIn("action=stand_down", out)
            rc.assert_not_called()
            rb.assert_not_called()

    def test_no_probe_could_run_stands_down(self):
        """'Could not ask' is not 'the answer is no'. Acting on it would
        reboot a healthy host over a local resource problem."""
        with tempfile.TemporaryDirectory() as tmp:
            prior = state(first=50_000.0, failures=9, reconnects=2)
            out, rc, rb, _ = self._run_main(50_400, None, prior, tmp, tcp=None)
            self.assertIn("action=stand_down", out)
            self.assertIn("reason=no_probe_ran", out)
            rc.assert_not_called()
            rb.assert_not_called()

    def test_one_probe_answering_is_enough(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, rc, rb, _ = self._run_main(50_000, False, None, tmp, tcp=True)
            self.assertIn("action=none", out)
            rc.assert_not_called()
            rb.assert_not_called()

    def test_all_probes_are_attempted_not_just_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(pathlib.Path(tmp) / "state.json")
            asked = []
            with mock.patch.object(nw, "STATE_PATH", path), \
                 mock.patch.object(nw, "_read",
                                   side_effect=lambda p: "50000 0" if p == "/proc/uptime"
                                   else (BOOT if p == nw.BOOT_ID_PATH else None)), \
                 mock.patch.object(nw, "ping",
                                   side_effect=lambda t: asked.append(t) or True), \
                 mock.patch.object(nw, "tcp_probe", return_value=True) as tcp, \
                 redirect_stdout(io.StringIO()):
                nw.main()
            self.assertEqual(asked, list(nw.TARGETS))
            tcp.assert_called_once()


class TestFormatRecord(unittest.TestCase):
    def test_per_probe_results_are_all_present(self):
        results = {t: True for t in nw.TARGETS}
        results[TCP_KEY] = True
        line = nw.format_record("none", "reachable", results, 50_000.0, state())
        for target in nw.TARGETS:
            self.assertIn(f"probe_{target}=true", line)
        self.assertIn(f"probe_{TCP_KEY}=true", line)

    def test_a_failing_probe_renders_as_false_not_omitted(self):
        """During an outage this line is the only artifact anyone reads. A
        mutation hardcoding reachable=true survived the old suite, which had
        no test asserting any false value in a rendered line."""
        results = {nw.TARGETS[0]: False, nw.TARGETS[1]: None, TCP_KEY: False}
        line = nw.format_record("reconnect", "first_failure", results, 50_000.0, state())
        self.assertIn("reachable=false", line)
        self.assertIn(f"probe_{nw.TARGETS[0]}=false", line)
        self.assertIn(f"probe_{nw.TARGETS[1]}=-", line)

    def test_an_unmeasured_probe_does_not_count_as_reachable(self):
        line = nw.format_record("stand_down", "no_probe_ran",
                                {t: None for t in nw.TARGETS}, 50_000.0, state())
        self.assertIn("reachable=false", line)

    def test_counters_are_rendered_from_state(self):
        line = nw.format_record("wait", "backoff_3",
                                {}, 50_000.0, state(failures=3, reconnects=2, reboots=1,
                                                    streak=4))
        self.assertIn("failures=3", line)
        self.assertIn("reconnects=2", line)
        self.assertIn("consecutive_reboots=1", line)
        self.assertIn("healthy_streak=4", line)

    def test_none_renders_as_a_bare_dash(self):
        self.assertIn("uptime_s=-", nw.format_record("stand_down", "no_uptime", {},
                                                     None, state()))

    def test_values_with_spaces_are_quoted(self):
        self.assertIn('outcome="a b"', nw.format_record("none", "r", {}, 1.0, state(), "a b"))


class TestSafetyConstants(unittest.TestCase):
    """The thresholds are the safety envelope, so pin them as literals.

    This looks tautological and is not: every behavioural test that referenced
    a constant symbolically moved with it, which let a mutation widening the
    reboot cap to 99 survive the entire suite.
    """

    def test_thresholds(self):
        self.assertEqual(nw.REBOOT_AFTER_DOWN_S, 300.0)
        self.assertEqual(nw.MIN_UPTIME_BEFORE_REBOOT_S, 600.0)
        self.assertEqual(nw.MAX_CONSECUTIVE_REBOOTS, 2)
        self.assertEqual(nw.HEALTHY_STREAK_TO_REARM, 15)
        self.assertEqual(nw.RECONNECT_EVERY_N_FAILURES, 5)

    def test_min_uptime_exceeds_the_down_threshold_plus_one_tick(self):
        """Not decorative. It is what keeps a stale down-clock from producing
        a reboot with less than REBOOT_AFTER_DOWN_S of OBSERVED downtime on
        the current boot. boot_id now settles that directly, but the
        inequality is the second line of defence and was previously an
        undocumented accident that three pinned constants did not express."""
        self.assertGreater(nw.MIN_UPTIME_BEFORE_REBOOT_S,
                           nw.REBOOT_AFTER_DOWN_S + 120)

    def test_probe_budget_fits_inside_the_timer_period(self):
        """TimeoutStartSec=90 in the unit, against a 120s OnCalendar period."""
        worst = (len(nw.TARGETS) * (float(nw.PING_DEADLINE_S) + 5.0)
                 + nw.TCP_TIMEOUT_S + nw.RECONNECT_TIMEOUT_S)
        self.assertLess(worst, 90)

    def test_two_distinct_targets_plus_an_independent_modality(self):
        """len()==2 alone is satisfied by a duplicated tuple, and 'contains
        the gateway' is satisfied by a WAN address. Pin distinctness and that
        both are on this LAN."""
        self.assertEqual(len(set(nw.TARGETS)), 2)
        for target in nw.TARGETS:
            self.assertTrue(target.startswith("192.168.1."), target)
        self.assertEqual(nw.TCP_PROBE_PORT, 1883)

    def test_state_lives_on_a_boot_persistent_path(self):
        """The reboot cap is worthless if state dies with the reboot. /tmp is
        tmpfs on this host AND the unit sets PrivateTmp=yes, which would lose
        it every RUN."""
        self.assertTrue(nw.STATE_PATH.startswith("/var/lib/"), nw.STATE_PATH)

    def test_wlan_uuid_is_the_preconfigured_profile(self):
        """Verified against the live host: this UUID is the `preconfigured`
        802-11-wireless profile on wlan0. A wrong one fails every reconnect
        with nothing but the journal to show it."""
        self.assertEqual(nw.WLAN_UUID, "11d51067-9d11-4257-822e-cf6744b9a997")


class TestDecideIsPure(unittest.TestCase):
    def test_decide_does_not_mutate_its_input(self):
        """Claimed in both files' docstrings and asserted nowhere; a mutation
        aliasing new_state to the input survived."""
        prior = state(first=50_000.0, failures=9, reconnects=2, reboots=1)
        snapshot = json.dumps(prior, sort_keys=True)
        nw.decide(50_400.0, False, prior, BOOT)
        self.assertEqual(json.dumps(prior, sort_keys=True), snapshot)


if __name__ == "__main__":
    unittest.main()
