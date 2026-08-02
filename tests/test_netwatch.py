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
import os
import pathlib
import re
import subprocess
import sys
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

REPO = pathlib.Path(__file__).resolve().parents[1]
TEMPLATE = REPO / "services" / "etc" / "gardyn" / "netwatch.env.example"
UNIT = REPO / "services" / "etc" / "systemd" / "system" / "gardyn-netwatch.service"

BOOT = "boot-aaaa"
CONFIG = "/fixture/netwatch.env"

# RFC 5737 TEST-NET-1 and a synthetic UUID. Deliberately not the deployment's
# real values: this suite must not become the place the addressing that was
# just lifted out of the script quietly reappears in a public repository.
CFG_TEXT = (
    "# a comment\n"
    "\n"
    "GARDYN_NETWATCH_PING_TARGETS=192.0.2.1,192.0.2.9\n"
    "GARDYN_NETWATCH_TCP_HOST=192.0.2.9\n"
    "GARDYN_NETWATCH_TCP_PORT=1883\n"
    "GARDYN_NETWATCH_WLAN_UUID=11111111-2222-3333-4444-555555555555\n"
)
CFG = nw.build_config(nw.parse_env(CFG_TEXT))
TCP_KEY = CFG.tcp_key


def cfg_text(**overrides) -> str:
    """CFG_TEXT with keys replaced or removed (pass None to drop a key)."""
    env = nw.parse_env(CFG_TEXT)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return "".join(f"{k}={v}\n" for k, v in env.items())


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
    def test_never_raises_holds_for_RecursionError_too(self):
        """`json.loads` raises RecursionError - not ValueError - on a deeply
        nested document, and RecursionError was not in the caught tuple.

        The docstring promises "never raises", and a watchdog that dies on a
        corrupt state file is a watchdog that stops watching. It fails in the
        SAFE direction (the unit exits non-zero, no reboot is ordered), but it
        costs the whole ladder on a host with no physical recovery path, for a
        file this process writes moments before an intentional reboot.

        Asserted by raising it from `json.loads` rather than by feeding real
        nesting. The genuine trigger needs roughly 150,000 levels, the exact
        depth is C-STACK dependent rather than governed by
        `sys.setrecursionlimit` (which the C scanner ignores), and a fixture
        tuned to overflow one machine's stack can hard-crash another instead
        of raising - so the real document would make this suite flaky on the
        small-stack ARM host it describes. What is pinned here is the handler,
        which is the part that was missing.
        """
        with mock.patch.object(nw.json, "loads", side_effect=RecursionError):
            self.assertEqual(dict(nw.EMPTY_STATE), nw.load_state('{"a": 1}'))

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
            self.assertIs(nw.ping("192.0.2.1"), True)
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["ping", "-w", nw.PING_DEADLINE_S, "192.0.2.1"])

    def test_no_count_flag_is_passed(self):
        """ping(8): with a count AND a deadline, receiving fewer than count
        packets before the deadline ALSO exits 1. Verified on the host:
        `ping -c 2 -w 1 <live gateway>` returns 1. With a deadline alone,
        exit 0 <=> at least one reply, which is the question being asked."""
        with mock.patch.object(nw.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            nw.ping("192.0.2.1")
        self.assertNotIn("-c", run.call_args[0][0])

    def test_exit_1_is_no_answer(self):
        with mock.patch.object(nw.subprocess, "run", return_value=mock.Mock(returncode=1)):
            self.assertIs(nw.ping("192.0.2.1"), False)

    def test_exit_2_is_could_not_measure(self):
        """ping exits 2 for errors that say nothing about the network."""
        with mock.patch.object(nw.subprocess, "run", return_value=mock.Mock(returncode=2)):
            self.assertIsNone(nw.ping("192.0.2.1"))

    def test_timeout_is_could_not_measure(self):
        with mock.patch.object(nw.subprocess, "run",
                               side_effect=nw.subprocess.TimeoutExpired("ping", 8)):
            self.assertIsNone(nw.ping("192.0.2.1"))

    def test_missing_binary_or_fork_failure_is_could_not_measure(self):
        """Under a memory limit a fork failure raises OSError(ENOMEM). Reading
        that as 'the LAN is down' would reboot a healthy host."""
        with mock.patch.object(nw.subprocess, "run", side_effect=OSError(12, "ENOMEM")):
            self.assertIsNone(nw.ping("192.0.2.1"))


class TestTcpProbe(unittest.TestCase):
    def test_connect_is_reachable(self):
        with mock.patch.object(nw.socket, "create_connection", return_value=mock.MagicMock()):
            self.assertIs(nw.tcp_probe(CFG.tcp_host, CFG.tcp_port), True)

    def test_refused_is_still_reachable(self):
        """Something answered — the path is up and the port is shut."""
        with mock.patch.object(nw.socket, "create_connection",
                               side_effect=ConnectionRefusedError()):
            self.assertIs(nw.tcp_probe(CFG.tcp_host, CFG.tcp_port), True)

    def test_network_level_errors_are_a_real_no(self):
        import errno as _errno
        for err in (_errno.ENETUNREACH, _errno.EHOSTUNREACH, _errno.ETIMEDOUT,
                    _errno.ENETDOWN):
            with self.subTest(errno=err):
                with mock.patch.object(nw.socket, "create_connection",
                                       side_effect=OSError(err, "network")):
                    self.assertIs(nw.tcp_probe(CFG.tcp_host, CFG.tcp_port), False)

    def test_local_resource_failures_are_could_not_measure(self):
        """EMFILE/ENOMEM say nothing about reachability. Reading them as
        'the LAN is down' is the same defect the tri-state in ping() closes —
        and this probe matters more, because when wlan0 is down both ICMP
        probes report don't-know and this one carries the whole decision."""
        import errno as _errno
        for err in (_errno.EMFILE, _errno.ENFILE, _errno.ENOMEM, _errno.EACCES):
            with self.subTest(errno=err):
                with mock.patch.object(nw.socket, "create_connection",
                                       side_effect=OSError(err, "local")):
                    self.assertIsNone(nw.tcp_probe(CFG.tcp_host, CFG.tcp_port))

    def test_timeout_is_a_real_no(self):
        with mock.patch.object(nw.socket, "create_connection",
                               side_effect=nw.socket.timeout()):
            self.assertIs(nw.tcp_probe(CFG.tcp_host, CFG.tcp_port), False)

    def test_uses_the_broker_and_a_bounded_timeout(self):
        with mock.patch.object(nw.socket, "create_connection",
                               return_value=mock.MagicMock()) as conn:
            nw.tcp_probe(CFG.tcp_host, CFG.tcp_port)
        self.assertEqual(conn.call_args[0][0], (CFG.tcp_host, CFG.tcp_port))
        self.assertEqual(conn.call_args[1]["timeout"], nw.TCP_TIMEOUT_S)


class TestReconnect(unittest.TestCase):
    def test_uses_connection_up_with_the_pinned_uuid(self):
        """`nmcli device reconnect` does not exist in 1.42.4; it exits 2. A
        watchdog built on it would look healthy and never recover the link.
        `nmcli device reapply` reports success without re-activating."""
        with mock.patch.object(nw.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as run:
            self.assertEqual(nw.reconnect(CFG.wlan_uuid), "reconnect_ok")
        argv = run.call_args[0][0]
        self.assertEqual(argv, ["nmcli", "--wait", str(nw.RECONNECT_NMCLI_WAIT_S),
                                "connection", "up", "uuid", CFG.wlan_uuid])
        self.assertNotIn("reconnect", argv)
        self.assertNotIn("reapply", argv)

    def test_nmcli_wait_is_below_the_subprocess_backstop(self):
        """Otherwise the subprocess SIGKILLs nmcli before it can report its
        own documented exit 3, and the diagnostic is lost."""
        self.assertLess(nw.RECONNECT_NMCLI_WAIT_S, nw.RECONNECT_TIMEOUT_S)

    def test_failure_timeout_and_oserror_are_reported_distinctly(self):
        with mock.patch.object(nw.subprocess, "run", return_value=mock.Mock(returncode=4)):
            self.assertEqual(nw.reconnect(CFG.wlan_uuid), "reconnect_exit_4")
        with mock.patch.object(nw.subprocess, "run",
                               side_effect=nw.subprocess.TimeoutExpired("nmcli", 60)):
            self.assertEqual(nw.reconnect(CFG.wlan_uuid), "reconnect_timeout")
        with mock.patch.object(nw.subprocess, "run", side_effect=OSError(2, "gone")):
            self.assertEqual(nw.reconnect(CFG.wlan_uuid), "reconnect_oserror_2")


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
                  reboot_outcome="reboot_ordered", boot_id=BOOT,
                  config_text=CFG_TEXT, expect_rv=0):
        path = str(pathlib.Path(tmpdir) / "state.json")
        if prior is not None:
            nw.save_state(path, prior)

        def fake_read(p):
            if p == CONFIG:
                return config_text
            if p == "/proc/uptime":
                return None if uptime is None else f"{uptime} 0"
            if p == nw.BOOT_ID_PATH:
                return boot_id
            if p == path:
                return pathlib.Path(path).read_text() if pathlib.Path(path).exists() else None
            return None

        buf = io.StringIO()
        with mock.patch.object(nw, "STATE_PATH", path), \
             mock.patch.object(nw, "CONFIG_PATH", CONFIG), \
             mock.patch.object(nw, "_read", side_effect=fake_read), \
             mock.patch.object(nw, "ping", side_effect=lambda _t: probe_result), \
             mock.patch.object(nw, "tcp_probe",
                               return_value=probe_result if tcp is None else tcp), \
             mock.patch.object(nw, "reconnect", return_value="reconnect_ok") as rc, \
             mock.patch.object(nw, "reboot", return_value=reboot_outcome) as rb, \
             redirect_stdout(buf):
            rv = nw.main()
        self.assertEqual(rv, expect_rv)
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
                if p == CONFIG:
                    return CFG_TEXT
                if p == "/proc/uptime":
                    return "50400 0"
                if p == nw.BOOT_ID_PATH:
                    return BOOT
                return pathlib.Path(path).read_text()

            with mock.patch.object(nw, "STATE_PATH", path), \
                 mock.patch.object(nw, "CONFIG_PATH", CONFIG), \
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
                if p == CONFIG:
                    return CFG_TEXT
                if p == "/proc/uptime":
                    return "50400 0"
                if p == nw.BOOT_ID_PATH:
                    return BOOT
                return pathlib.Path(path).read_text()

            buf = io.StringIO()
            with mock.patch.object(nw, "STATE_PATH", path), \
                 mock.patch.object(nw, "CONFIG_PATH", CONFIG), \
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
                 mock.patch.object(nw, "CONFIG_PATH", CONFIG), \
                 mock.patch.object(nw, "_read",
                                   side_effect=lambda p: CFG_TEXT if p == CONFIG
                                   else ("50000 0" if p == "/proc/uptime"
                                         else (BOOT if p == nw.BOOT_ID_PATH else None))), \
                 mock.patch.object(nw, "ping",
                                   side_effect=lambda t: asked.append(t) or True), \
                 mock.patch.object(nw, "tcp_probe", return_value=True) as tcp, \
                 redirect_stdout(io.StringIO()):
                nw.main()
            self.assertEqual(asked, list(CFG.targets))
            tcp.assert_called_once()


class TestMainRefusesWithoutConfig(unittest.TestCase):
    """Fail closed, and fail LOUDLY.

    The dangerous shape is not a crash — it is a run that looks like an
    ordinary quiet tick. So all three are asserted together: nothing is
    probed, nothing is written, and the exit status is non-zero so systemd
    marks the unit failed instead of logging a success.
    """

    def _run(self, config_text):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(pathlib.Path(tmp) / "state.json")
            buf, err = io.StringIO(), io.StringIO()
            with mock.patch.object(nw, "STATE_PATH", path), \
                 mock.patch.object(nw, "CONFIG_PATH", CONFIG), \
                 mock.patch.object(nw, "_read",
                                   side_effect=lambda p: config_text if p == CONFIG
                                   else None), \
                 mock.patch.object(nw, "ping") as ping, \
                 mock.patch.object(nw, "tcp_probe") as tcp, \
                 mock.patch.object(nw, "reconnect") as rc, \
                 mock.patch.object(nw, "reboot") as rb, \
                 mock.patch.object(nw, "save_state") as save, \
                 mock.patch("sys.stderr", err), \
                 redirect_stdout(buf):
                rv = nw.main()
            for probe in (ping, tcp, rc, rb, save):
                probe.assert_not_called()
            self.assertFalse(pathlib.Path(path).exists())
            return rv, buf.getvalue(), err.getvalue()

    def test_a_missing_config_file_refuses(self):
        rv, out, err = self._run(None)
        self.assertNotEqual(rv, 0)
        self.assertIn("reason=config_unreadable", out)
        self.assertIn(CONFIG, out)
        self.assertIn("refusing to run", err)

    def test_an_empty_config_file_refuses(self):
        rv, out, _ = self._run("\n# nothing here\n")
        self.assertNotEqual(rv, 0)
        self.assertIn("reason=config_empty", out)

    def test_a_config_missing_one_key_refuses(self):
        rv, out, _ = self._run(cfg_text(GARDYN_NETWATCH_WLAN_UUID=None))
        self.assertNotEqual(rv, 0)
        self.assertIn("reason=config_missing_key", out)
        self.assertIn(nw.KEY_WLAN_UUID, out)

    def test_a_malformed_config_refuses(self):
        rv, out, _ = self._run(cfg_text(GARDYN_NETWATCH_TCP_PORT="not-a-port"))
        self.assertNotEqual(rv, 0)
        self.assertIn("reason=config_bad_port", out)

    def test_the_unedited_template_refuses(self):
        rv, out, _ = self._run(TEMPLATE.read_text())
        self.assertNotEqual(rv, 0)
        self.assertIn("reason=config_placeholder", out)

    def test_a_complete_config_does_NOT_refuse(self):
        """The positive control for this whole class. Without it every test
        above is satisfied by a main() that refuses unconditionally."""
        with tempfile.TemporaryDirectory() as tmp:
            path = str(pathlib.Path(tmp) / "state.json")
            with mock.patch.object(nw, "STATE_PATH", path), \
                 mock.patch.object(nw, "CONFIG_PATH", CONFIG), \
                 mock.patch.object(nw, "_read",
                                   side_effect=lambda p: CFG_TEXT if p == CONFIG
                                   else ("50000 0" if p == "/proc/uptime"
                                         else (BOOT if p == nw.BOOT_ID_PATH else None))), \
                 mock.patch.object(nw, "ping", return_value=True) as ping, \
                 mock.patch.object(nw, "tcp_probe", return_value=True), \
                 redirect_stdout(io.StringIO()) as buf:
                rv = nw.main()
        self.assertEqual(rv, 0)
        self.assertEqual(ping.call_count, len(CFG.targets))
        self.assertIn("action=none", buf.getvalue())


class TestFormatRecord(unittest.TestCase):
    def test_per_probe_results_are_all_present(self):
        results = {t: True for t in CFG.targets}
        results[TCP_KEY] = True
        line = nw.format_record("none", "reachable", results, 50_000.0, state(), CFG)
        for target in CFG.targets:
            self.assertIn(f"probe_{target}=true", line)
        self.assertIn(f"probe_{TCP_KEY}=true", line)

    def test_a_failing_probe_renders_as_false_not_omitted(self):
        """During an outage this line is the only artifact anyone reads. A
        mutation hardcoding reachable=true survived the old suite, which had
        no test asserting any false value in a rendered line."""
        results = {CFG.targets[0]: False, CFG.targets[1]: None, TCP_KEY: False}
        line = nw.format_record("reconnect", "first_failure", results, 50_000.0,
                                state(), CFG)
        self.assertIn("reachable=false", line)
        self.assertIn(f"probe_{CFG.targets[0]}=false", line)
        self.assertIn(f"probe_{CFG.targets[1]}=-", line)

    def test_an_unmeasured_probe_does_not_count_as_reachable(self):
        line = nw.format_record("stand_down", "no_probe_ran",
                                {t: None for t in CFG.targets}, 50_000.0, state(), CFG)
        self.assertIn("reachable=false", line)

    def test_counters_are_rendered_from_state(self):
        line = nw.format_record("wait", "backoff_3", {}, 50_000.0,
                                state(failures=3, reconnects=2, reboots=1, streak=4), CFG)
        self.assertIn("failures=3", line)
        self.assertIn("reconnects=2", line)
        self.assertIn("consecutive_reboots=1", line)
        self.assertIn("healthy_streak=4", line)

    def test_none_renders_as_a_bare_dash(self):
        self.assertIn("uptime_s=-", nw.format_record("stand_down", "no_uptime", {},
                                                     None, state(), CFG))

    def test_an_embedded_quote_is_escaped_not_emitted_raw(self):
        """An operator-supplied string reaches this through a ConfigError
        detail. A raw quote closes the field early, so the line still parses -
        into the wrong fields, which is worse than one that visibly does not."""
        self.assertEqual(nw._fmt('a "weird" v'), '"a \\"weird\\" v"')
        self.assertEqual(nw._fmt("back\\slash here"), '"back\\\\slash here"')

    def test_values_with_spaces_are_quoted(self):
        self.assertIn('outcome="a b"',
                      nw.format_record("none", "r", {}, 1.0, state(), CFG, "a b"))

    def test_a_control_character_cannot_SPLIT_the_record(self):
        """A raw newline in a value ends the log line and turns the remainder
        into a second record with an injected field. The quoting rule keys off
        a space and a quote, and a newline is neither - so the escape hatch the
        embedded-quote test closes is still open by a different route.

        During an outage this line is the only artifact anyone reads back, and
        one record arriving as three is worse than one that visibly does not
        parse, for the same reason a mis-parsed field is worse than a broken
        one: it reads as data.
        """
        for raw in ("a\nb", "a\rb", "a\tb", "a\x00b", "a\x1bb"):
            with self.subTest(raw=raw):
                rendered = nw._fmt(raw)
                self.assertEqual(
                    1, len(rendered.splitlines()),
                    f"{raw!r} rendered as {rendered!r}, which spans lines")
                for ch in "\n\r\t":
                    self.assertNotIn(ch, rendered)

    def test_a_newline_in_a_record_VALUE_stays_on_one_line(self):
        """The property one level up from _fmt: the whole rendered record."""
        line = nw.format_record("none", "r", {}, 1.0, state(), CFG, "a\nb")
        self.assertEqual(1, len(line.splitlines()))


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
        """TimeoutStartSec=90 in the unit, against a 120s OnCalendar period.

        The target COUNT is operator-supplied now, so this is checked at the
        cap rather than against whatever this deployment happens to list."""
        self.assertLess(nw.probe_budget_s(nw.MAX_PING_TARGETS),
                        nw.UNIT_TIMEOUT_START_S)

    def test_the_target_cap_is_DERIVED_from_the_budget_not_chosen(self):
        """One more target must not fit. A cap that is merely smaller than the
        budget allows would drift silently when a timeout changes, and this is
        the constant that decides whether a failing run is killed mid-
        reconnect on every single tick."""
        self.assertGreaterEqual(nw.probe_budget_s(nw.MAX_PING_TARGETS + 1),
                                nw.UNIT_TIMEOUT_START_S)
        self.assertGreaterEqual(nw.MAX_PING_TARGETS, 2)

    def test_the_pinned_unit_timeout_matches_the_unit_FILE(self):
        """UNIT_TIMEOUT_START_S is a copy of a value systemd owns. A copy that
        nothing compares is a copy that goes stale, and the failure is
        invisible: the cap keeps computing from a timeout that is no longer
        deployed."""
        match = re.search(r"^TimeoutStartSec=(\d+)\s*$", UNIT.read_text(),
                          re.MULTILINE)
        self.assertIsNotNone(match, "no TimeoutStartSec= in the unit file")
        assert match is not None
        self.assertEqual(float(match.group(1)), nw.UNIT_TIMEOUT_START_S)

    def test_the_default_tcp_port_is_the_registered_mqtt_one(self):
        """The one field allowed a default, precisely because 1883 is IANA's
        and reveals nothing about anybody's topology."""
        self.assertEqual(nw.DEFAULT_TCP_PORT, 1883)

    def test_state_lives_on_a_boot_persistent_path(self):
        """The reboot cap is worthless if state dies with the reboot. /tmp is
        tmpfs on this host AND the unit sets PrivateTmp=yes, which would lose
        it every RUN."""
        self.assertTrue(nw.STATE_PATH.startswith("/var/lib/"), nw.STATE_PATH)


# --------------------------------------------------------------------------
# Configuration. Every branch here is a REFUSAL, and every one of them is
# unreachable on the deployed host — which has a correct config file and will
# therefore exercise none of this ever again.
# --------------------------------------------------------------------------
class TestParseEnv(unittest.TestCase):
    def test_comments_blanks_and_quotes(self):
        env = nw.parse_env('# note\n\n A = " x " \nB=\'y\'\nexport C=z\n')
        self.assertEqual(env, {"A": " x ", "B": "y", "C": "z"})

    def test_a_line_without_an_equals_is_skipped_not_fatal(self):
        self.assertEqual(nw.parse_env("junk\nA=1\n"), {"A": "1"})

    def test_a_value_may_contain_an_equals(self):
        self.assertEqual(nw.parse_env("A=b=c\n"), {"A": "b=c"})

    def test_empty_and_none(self):
        self.assertEqual(nw.parse_env(None), {})
        self.assertEqual(nw.parse_env(""), {})


class TestBuildConfig(unittest.TestCase):
    def test_the_happy_path(self):
        cfg = nw.build_config(nw.parse_env(CFG_TEXT))
        self.assertEqual(cfg.targets, ("192.0.2.1", "192.0.2.9"))
        self.assertEqual(cfg.tcp_host, "192.0.2.9")
        self.assertEqual(cfg.tcp_port, 1883)
        self.assertEqual(cfg.wlan_uuid, "11111111-2222-3333-4444-555555555555")
        self.assertEqual(cfg.tcp_key, "tcp_192.0.2.9_1883")

    def test_targets_may_be_space_separated(self):
        cfg = nw.build_config(nw.parse_env(
            cfg_text(GARDYN_NETWATCH_PING_TARGETS="192.0.2.1 192.0.2.9")))
        self.assertEqual(cfg.targets, ("192.0.2.1", "192.0.2.9"))

    def _refuses(self, reason, **overrides):
        with self.assertRaises(nw.ConfigError) as ctx:
            nw.build_config(nw.parse_env(cfg_text(**overrides)))
        self.assertEqual(ctx.exception.reason, reason)
        return ctx.exception

    def test_each_required_key_missing_is_refused(self):
        for key in nw.REQUIRED_KEYS:
            with self.subTest(key=key):
                exc = self._refuses("config_missing_key", **{key: None})
                self.assertIn(key, exc.detail)

    def test_each_required_key_EMPTY_is_refused(self):
        """Not the same branch as absent: a key present with a blank value is
        what a half-finished edit leaves behind."""
        for key in nw.REQUIRED_KEYS:
            with self.subTest(key=key):
                self._refuses("config_missing_key", **{key: "   "})

    def test_a_template_placeholder_left_in_place_is_refused(self):
        """The whole point of the CHANGEME sentinel. Unedited placeholders
        would answer no probe, which reads to the ladder as a total outage and
        escalates — a copied template would REBOOT the host on a cadence."""
        for key in nw.REQUIRED_KEYS:
            with self.subTest(key=key):
                self._refuses("config_placeholder",
                              **{key: f"{nw.PLACEHOLDER}-something"})

    def test_a_placeholder_in_the_optional_port_is_also_refused(self):
        self._refuses("config_placeholder",
                      GARDYN_NETWATCH_TCP_PORT=nw.PLACEHOLDER)

    def test_a_target_list_of_only_separators_is_refused(self):
        self._refuses("config_no_targets", GARDYN_NETWATCH_PING_TARGETS=" , , ")

    def test_a_repeated_target_is_refused(self):
        """Two entries that are one host is one modality wearing a hat: that
        host rebooting then looks exactly like this Pi's radio dying."""
        self._refuses("config_duplicate_targets",
                      GARDYN_NETWATCH_PING_TARGETS="192.0.2.1,192.0.2.1")

    def test_a_single_target_is_refused(self):
        """One host answering for the whole LAN means that host rebooting is
        indistinguishable from this Pi's radio dying. The base version pinned
        two DISTINCT targets as a module constant; with the value now supplied
        by an operator, the floor has to be enforced rather than assumed."""
        self._refuses("config_too_few_targets",
                      GARDYN_NETWATCH_PING_TARGETS="192.0.2.1")

    def test_the_minimum_is_two_and_the_cap_leaves_room_for_it(self):
        self.assertEqual(nw.MIN_PING_TARGETS, 2)
        self.assertGreaterEqual(nw.MAX_PING_TARGETS, nw.MIN_PING_TARGETS)

    def test_a_target_that_ping_would_read_as_a_FLAG_is_refused(self):
        """These reach `ping` as argv. A value starting with `-` is an option,
        not a destination, so an operator-supplied config would be choosing
        flags for a command this script runs — and a flag like `-V` exits 0,
        which the probe reads as `reachable` forever."""
        for bad in ("-V", "--flood", "-w", "-f"):
            with self.subTest(target=bad):
                self._refuses("config_bad_target",
                              GARDYN_NETWATCH_PING_TARGETS=f"{bad},192.0.2.9")

    def test_a_trailing_COMMENT_on_the_target_list_is_refused(self):
        """`# gw` on the end of a value is not stripped - systemd does not
        strip it either - and whitespace is a target SEPARATOR here, so the
        line silently becomes three targets of which `#` is one. It would then
        land in the logfmt key as `probe_#_...`, producing a record that no
        longer parses. Refusing the token is the only place to catch it."""
        self._refuses("config_bad_target",
                      GARDYN_NETWATCH_PING_TARGETS="192.0.2.1 # gw")

    def test_whitespace_between_targets_is_still_just_a_separator(self):
        """The control for the test above: the refusal must come from the `#`,
        not from whitespace, or space-separated lists stop working."""
        cfg = nw.build_config(nw.parse_env(cfg_text(
            GARDYN_NETWATCH_PING_TARGETS="192.0.2.1\t192.0.2.9")))
        self.assertEqual(cfg.targets, ("192.0.2.1", "192.0.2.9"))

    def test_an_implausible_tcp_host_is_refused(self):
        """Same reasoning, and it bites harder: an unusable tcp_host resolves
        to a permanent `None`, so the one probe the docstring says carries the
        whole decision when wlan0 is down is silently dead."""
        for bad in ("192.0.2.9 # broker", "-V", "", "  "):
            with self.subTest(host=bad):
                with self.assertRaises(nw.ConfigError) as ctx:
                    nw.build_config(nw.parse_env(cfg_text(
                        GARDYN_NETWATCH_TCP_HOST=bad)))
                self.assertIn(ctx.exception.reason,
                              ("config_bad_tcp_host", "config_missing_key"))

    def test_a_hostname_is_still_accepted(self):
        """The shape check must not force IP literals - the deployment may
        move to names, and over-tightening here would refuse a valid config."""
        cfg = nw.build_config(nw.parse_env(cfg_text(
            GARDYN_NETWATCH_PING_TARGETS="gw.example,broker.example",
            GARDYN_NETWATCH_TCP_HOST="broker.example")))
        self.assertEqual(cfg.targets, ("gw.example", "broker.example"))

    def test_more_targets_than_the_run_budget_allows_is_refused(self):
        many = ",".join(f"192.0.2.{n}" for n in range(1, nw.MAX_PING_TARGETS + 2))
        self._refuses("config_too_many_targets", GARDYN_NETWATCH_PING_TARGETS=many)

    def test_exactly_the_cap_is_accepted(self):
        many = ",".join(f"192.0.2.{n}" for n in range(1, nw.MAX_PING_TARGETS + 1))
        cfg = nw.build_config(nw.parse_env(
            cfg_text(GARDYN_NETWATCH_PING_TARGETS=many)))
        self.assertEqual(len(cfg.targets), nw.MAX_PING_TARGETS)

    def test_a_malformed_or_out_of_range_port_is_refused(self):
        # "1_883", "+1883" and Arabic-Indic digits are all accepted by a bare
        # int(), and none of them is something anyone meant to write.
        for bad in ("1883x", "", "-1", "0", "65536", "1e3", " 18 83",
                    "1_883", "+1883", "\u0661\u0668\u0668\u0663"):
            if bad == "":
                continue  # empty means "use the default", covered below
            with self.subTest(port=bad):
                self._refuses("config_bad_port", GARDYN_NETWATCH_TCP_PORT=bad)

    def test_an_absurdly_long_all_digit_port_is_REFUSED_not_a_traceback(self):
        """`isdigit()` passes for any number of digits, and `int()` then raises
        ValueError past CPython's 4300-digit conversion cap - which is NOT a
        ConfigError, so it escapes main()'s handler and exits 1 with a
        traceback and NO journal line.

        That is byte-for-byte the failure shape the UnicodeDecodeError fix
        removed from _read(): the operator gets a stack trace instead of the
        named reason the whole refusal vocabulary exists to give them.
        """
        for digits in (4301, 5000):
            with self.subTest(digits=digits):
                self._refuses("config_bad_port",
                              GARDYN_NETWATCH_TCP_PORT="1" * digits)

    def test_the_port_length_bound_is_what_a_port_can_actually_be(self):
        """65535 is five digits, so a sixth can only be padding or garbage.

        The bound is on LENGTH rather than on the converted value because the
        conversion is the thing that raises - checking the number afterwards is
        checking a value that was never produced.
        """
        cfg = nw.build_config(nw.parse_env(
            cfg_text(GARDYN_NETWATCH_TCP_PORT="65535")))
        self.assertEqual(cfg.tcp_port, 65535)
        # Zero-padded past five characters is refused rather than silently
        # accepted. This is the ONLY class of input whose treatment the bound
        # changes, and a config file carrying it is a typo, not an intent.
        for padded in ("001883", "0001883", "000001"):
            with self.subTest(padded=padded):
                self._refuses("config_bad_port",
                              GARDYN_NETWATCH_TCP_PORT=padded)
        # Five characters still converts, padding and all.
        self.assertEqual(
            1883,
            nw.build_config(nw.parse_env(
                cfg_text(GARDYN_NETWATCH_TCP_PORT="01883"))).tcp_port)

    def test_an_omitted_port_takes_the_registered_default(self):
        cfg = nw.build_config(nw.parse_env(cfg_text(GARDYN_NETWATCH_TCP_PORT=None)))
        self.assertEqual(cfg.tcp_port, nw.DEFAULT_TCP_PORT)

    def test_a_blank_port_takes_the_default_rather_than_failing(self):
        cfg = nw.build_config(nw.parse_env(cfg_text(GARDYN_NETWATCH_TCP_PORT="  ")))
        self.assertEqual(cfg.tcp_port, nw.DEFAULT_TCP_PORT)

    def test_a_connection_NAME_where_a_uuid_belongs_is_refused(self):
        """`nmcli connection up uuid <name>` fails every reconnect with
        nothing but the journal to show for it. Catch the shape at load time,
        not during the outage it was supposed to fix."""
        for bad in ("preconfigured", "11111111-2222-3333-4444", "not-a-uuid",
                    "11111111222233334444555555555555",
                    # A real UUID with junk appended: only the TRAILING anchor
                    # rejects this, and re.match() makes the leading one
                    # redundant, so this is the case that pins \Z.
                    "11111111-2222-3333-4444-555555555555-extra",
                    "prefix-11111111-2222-3333-4444-555555555555"):
            with self.subTest(uuid=bad):
                self._refuses("config_bad_uuid", GARDYN_NETWATCH_WLAN_UUID=bad)


class TestLoadConfig(unittest.TestCase):
    def test_a_missing_file_is_refused_not_defaulted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(pathlib.Path(tmp) / "absent.env")
            with self.assertRaises(nw.ConfigError) as ctx:
                nw.load_config(path)
            self.assertEqual(ctx.exception.reason, "config_unreadable")

    def test_an_empty_file_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "empty.env"
            path.write_text("# only comments\n\n")
            with self.assertRaises(nw.ConfigError) as ctx:
                nw.load_config(str(path))
            self.assertEqual(ctx.exception.reason, "config_empty")

    def test_a_file_that_is_not_valid_TEXT_is_refused_not_raised(self):
        """A traceback is not a refusal: it exits 1 with nothing on the
        journal, so the named reason the operator needs never appears. The
        decode happens outside load_state(), so this also unblocks that
        function's "never raises" promise for a corrupted state file."""
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "binary.env"
            path.write_bytes(b"GARDYN_NETWATCH_PING_TARGETS=192.0.2.1\xff\n")
            with self.assertRaises(nw.ConfigError) as ctx:
                nw.load_config(str(path))
            self.assertEqual(ctx.exception.reason, "config_unreadable")

    def test_a_real_file_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "netwatch.env"
            path.write_text(CFG_TEXT)
            self.assertEqual(nw.load_config(str(path)), CFG)

    def test_the_config_read_names_its_encoding_rather_than_inheriting_one(self):
        """`open()` with no `encoding=` uses the LOCALE's encoding.

        systemd units run with a minimal environment, and under a C/POSIX
        locale Python did not coerce to C.UTF-8 the preferred encoding is
        ASCII - so a config carrying any non-ASCII byte raises
        UnicodeDecodeError, is caught as "unreadable", and a PERFECTLY VALID
        config is refused with `config_unreadable`.

        Asserted on the mechanism, because a running interpreter cannot change
        its own locale; the end-to-end proof under a real ASCII locale is the
        subprocess test below.
        """
        seen = {}
        real_open = open

        def recording_open(path, *args, **kwargs):
            seen["encoding"] = kwargs.get("encoding")
            return real_open(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "netwatch.env"
            path.write_text(CFG_TEXT)
            with mock.patch("builtins.open", recording_open):
                nw._read(str(path))
        self.assertEqual(
            "utf-8", seen.get("encoding"),
            "_read() inherits the locale encoding; a valid UTF-8 config is "
            "then refused as unreadable under a C locale")

    def test_a_valid_utf8_config_is_accepted_under_a_REAL_ascii_locale(self):
        """End-to-end, in a subprocess whose locale is genuinely ASCII.

        Carries its own positive control: if the environment does not actually
        produce an ASCII preferred encoding, the case proves nothing and is
        skipped rather than passing quietly.
        """
        env = dict(os.environ, LC_ALL="C", LANG="C",
                   PYTHONCOERCECLOCALE="0", PYTHONUTF8="0")
        control = subprocess.run(
            [sys.executable, "-c",
             "import locale; print(locale.getpreferredencoding(False))"],
            env=env, capture_output=True, text=True)
        if "ascii" not in control.stdout.strip().lower().replace(
                "ansi_x3.4-1968", "ascii"):
            self.skipTest(
                f"locale did not go ASCII (got {control.stdout.strip()!r}); "
                "this platform cannot exercise the failing case")

        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "netwatch.env"
            # An em dash, exactly as the shipped template carries.
            path.write_bytes(
                ("# targets — an em dash, as the template has\n"
                 + CFG_TEXT).encode("utf-8"))
            proc = subprocess.run(
                [sys.executable, "-c",
                 "import importlib.util,sys\n"
                 f"s=importlib.util.spec_from_file_location('nw', {str(_SRC)!r})\n"
                 "m=importlib.util.module_from_spec(s); s.loader.exec_module(m)\n"
                 f"c=m.load_config({str(path)!r}); print(c.tcp_host)"],
                env=env, capture_output=True, text=True)
        self.assertEqual(
            0, proc.returncode,
            "a valid UTF-8 config was refused under a C locale:\n"
            + proc.stdout + proc.stderr)
        self.assertEqual("192.0.2.9", proc.stdout.strip())

    def test_the_shipped_template_itself_is_not_pure_ascii(self):
        """The positive control for the pair above: were the template ever to
        become plain ASCII, a locale-dependent read would stop being able to
        refuse it and both tests would pass while proving nothing."""
        with self.assertRaises(UnicodeDecodeError):
            TEMPLATE.read_bytes().decode("ascii")

    def test_the_error_record_names_the_reason_and_the_path(self):
        exc = nw.ConfigError("config_unreadable", "cannot read /etc/x")
        line = nw.config_error_record(exc, "/etc/x")
        self.assertIn("action=stand_down", line)
        self.assertIn("reason=config_unreadable", line)
        self.assertIn("config_path=/etc/x", line)
        self.assertIn('detail="cannot read /etc/x"', line)


class TestNoTopologyIsBakedIn(unittest.TestCase):
    """The policy itself, pinned against RE-INTRODUCTION rather than breakage.

    Every other test here would still pass if somebody added a fallback
    default alongside the config loader — the loader would simply never be
    asked. These scan the shipped source instead, so the deleted code cannot
    come back quietly.
    """

    IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    UUID = re.compile(r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b")

    def test_the_script_contains_no_ip_address_and_no_uuid(self):
        text = pathlib.Path(nw.__file__ or str(_SRC)).read_text()
        self.assertEqual(self.IPV4.findall(text), [])
        self.assertEqual(self.UUID.findall(text), [])

    def test_the_probe_helpers_take_no_default_target(self):
        """A default argument is the back door: main() would keep passing the
        config while any other caller silently got a built-in host."""
        import inspect
        for func, params in ((nw.tcp_probe, ("host", "port")),
                             (nw.reconnect, ("wlan_uuid",))):
            sig = inspect.signature(func)
            for name in params:
                with self.subTest(func=func.__name__, param=name):
                    self.assertIs(sig.parameters[name].default,
                                  inspect.Parameter.empty)

    def test_the_module_exposes_no_topology_constant(self):
        for gone in ("TARGETS", "TCP_PROBE_HOST", "WLAN_UUID"):
            self.assertFalse(hasattr(nw, gone),
                             f"{gone} is back as a module-level default")


class TestShippedTemplate(unittest.TestCase):
    def test_the_template_exists_and_documents_every_required_key(self):
        text = TEMPLATE.read_text()
        for key in nw.REQUIRED_KEYS + (nw.KEY_TCP_PORT,):
            self.assertIn(f"{key}=", text, key)

    def test_the_template_itself_is_REFUSED(self):
        """Copying it into place unedited must not produce a working watchdog.
        This is the test that makes 'no working default' true of the artifact
        an operator actually handles, rather than only of the code."""
        with self.assertRaises(nw.ConfigError) as ctx:
            nw.build_config(nw.parse_env(TEMPLATE.read_text()))
        self.assertEqual(ctx.exception.reason, "config_placeholder")

    def test_the_real_config_is_ignored_and_the_template_is_NOT(self):
        """Both directions, because each failure is silent in its own way.

        Ignoring too little publishes one LAN's topology from a public repo.
        Ignoring too much swallows the template and leaves a repository that
        passes every secret scan and cannot be configured by anybody.
        """
        import shutil
        import subprocess
        if shutil.which("git") is None or not (REPO / ".git").exists():
            self.skipTest("no git checkout to interrogate")

        def ignored(path):
            return subprocess.run(["git", "check-ignore", "-q", path],
                                  cwd=REPO, capture_output=True).returncode == 0

        # Positive control: prove check-ignore can say IGNORED here at all,
        # or a clean sweep below would mean nothing.
        self.assertTrue(ignored("definitely-a-secret.env"),
                        "control failed - check-ignore reports nothing ignored")

        for secret in ("netwatch.env", "services/etc/gardyn/netwatch.env",
                       ".env", "sub/dir/x.env"):
            with self.subTest(path=secret):
                self.assertTrue(ignored(secret), f"{secret} is committable")

        for template in ("services/etc/gardyn/netwatch.env.example", ".env-dist"):
            with self.subTest(path=template):
                self.assertFalse(ignored(template),
                                 f"{template} is ignored - the repo cannot be set up")

        tracked = subprocess.run(["git", "ls-files"], cwd=REPO,
                                 capture_output=True, text=True).stdout.split()
        self.assertIn("services/etc/gardyn/netwatch.env.example", tracked)
        self.assertEqual([f for f in tracked if f.endswith(".env")], [])

    def test_the_template_carries_no_real_address_or_uuid(self):
        text = TEMPLATE.read_text()
        for line in text.splitlines():
            if not line.startswith("GARDYN_NETWATCH_"):
                continue
            key, _, value = line.partition("=")
            if key == nw.KEY_TCP_PORT:
                continue
            with self.subTest(key=key):
                self.assertIn(nw.PLACEHOLDER, value.upper())


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
