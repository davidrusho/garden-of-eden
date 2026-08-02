#!/usr/bin/env python3
"""Mutation battery for gardyn-netwatch's configuration and reboot paths (T-490).

A green suite proves nothing until it has been shown capable of going red.
Each mutation below breaks ONE guarantee the tests claim to enforce; every one
must produce at least one failure. A survivor means the corresponding test is
decorative, OR the mutated code is redundant, OR the mutation never applied —
all three are findings, and the harness reports enough to tell them apart.

Two families are deliberately over-represented:

  * THE REBOOT PATH. This script can power-cycle the host it runs on, and a
    kill count that never touches the reboot decision is not coverage of this
    file. Every guard in front of `systemctl reboot` gets at least one mutant.

  * REINTRODUCTION, not just breakage. The T-490 policy is that the ping
    targets, the TCP host and the wlan0 UUID have NO working default, and
    every ordinary mutation tests code that is present. A default coming back
    is code being ADDED, which a suite that only exercises the loader would
    never notice — the loader simply stops being asked. Those mutants restore
    a hardcoded fallback and must still be caught.

    Those mutants use RFC 5737 documentation addresses and a synthetic UUID
    rather than the deployment's real ones. The point of T-490 was to stop
    publishing one LAN's topology from a public repository, and a battery that
    pasted it into the test harness in order to prove it was gone would have
    republished it by a different route. The detector under test matches the
    SHAPE of an address, so any literal exercises it.

Runs entirely inside a disposable copy of the repo; the real working tree is
never mutated.

    python3 tests/mutate_netwatch.py

Exit 0 all killed, 1 survivors, 2 a broken instrument (either control failed).
"""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# The save-before-reboot ordering, spelled out in both directions. `outcome =
# reboot()` is the line the write has to precede: the process may not run
# again, and a reboot the state file did not record is a reboot the cap cannot
# see.
_REBOOT_SAVE_FIRST = """        if not save_state(STATE_PATH, new_state):
            print(format_record(ACT_REBOOT_SUPPRESSED, "state_unwritable", results,
                                uptime_s, new_state, cfg, "reconnect_skipped"), flush=True)
            return 0
        print(format_record(action, reason, results, uptime_s, new_state, cfg,
                            "reboot_ordering"), flush=True)
        outcome = reboot()"""
_REBOOT_SAVE_LAST = """        print(format_record(action, reason, results, uptime_s, new_state, cfg,
                            "reboot_ordering"), flush=True)
        outcome = reboot()
        if not save_state(STATE_PATH, new_state):
            print(format_record(ACT_REBOOT_SUPPRESSED, "state_unwritable", results,
                                uptime_s, new_state, cfg, "reconnect_skipped"), flush=True)
            return 0"""
SRC = "bin/gardyn-netwatch.py"
UNIT = "services/etc/systemd/system/mqtt.service"
INSTALLER = "bin/install-systemd-units.sh"

# Taken verbatim from the source rather than retyped: this line is dense with
# backslashes and quotes, and a retyped anchor that silently matches nothing
# would report a survivor for a mutation that never happened.
_ESCAPE_LINE = '    escaped = (text.replace("\\\\", "\\\\\\\\")\n                   .replace(\'"\', \'\\\\"\')'
TEMPLATE = "services/etc/gardyn/netwatch.env.example"

TEST_MODULES = ("tests.test_netwatch", "tests.test_setup_units")

# Which suites can possibly see a change to which file. Running the bash-driven
# installer suite for every mutation of a Python module costs ~20s a head and
# measures nothing; running it for the unit-file mutations is the whole point.
SUITES_FOR = {
    SRC: ("tests.test_netwatch",),
    TEMPLATE: ("tests.test_netwatch",),
    UNIT: TEST_MODULES,
    INSTALLER: ("tests.test_setup_units",),
}

# (name, file, old, new) — `old` must appear exactly once in `file`.
MUTATIONS = [
    # ---------------------------------------------------------------- config
    ("missing key silently defaulted instead of refused", SRC,
     '        if not value:\n            raise ConfigError("config_missing_key", f"{key} is missing or empty")',
     '        if False:\n            raise ConfigError("config_missing_key", f"{key} is missing or empty")'),
    ("template placeholder accepted as a real value", SRC,
     "        if PLACEHOLDER in value.upper():", "        if False:"),
    ("absent config file treated as an empty one", SRC,
     '    if raw is None:\n        raise ConfigError("config_unreadable", f"cannot read {path}")',
     '    if raw is None:\n        raw = ""'),
    ("empty config file accepted", SRC,
     '    if not env:\n        raise ConfigError("config_empty", f"{path} defines no settings")',
     '    if not env:\n        env = dict(env)'),
    ("empty target list accepted", SRC,
     '    if not targets:\n        raise ConfigError("config_no_targets", f"{KEY_TARGETS} names no host")',
     '    if not targets:\n        pass'),
    ("duplicate targets accepted (two probes, one host)", SRC,
     "    if len(set(targets)) != len(targets):", "    if False:"),
    ("target cap removed - a failing run can outlive the start timeout", SRC,
     "    if len(targets) > MAX_PING_TARGETS:", "    if False:"),
    ("target cap widened past the run budget", SRC,
     "MAX_PING_TARGETS = _max_ping_targets()", "MAX_PING_TARGETS = 99"),
    ("port range check dropped", SRC,
     "        if not 1 <= port <= 65535:", "        if False:"),
    ("non-numeric port coerced to the default instead of refused", SRC,
     '            raise ConfigError("config_bad_port",\n                              f"{KEY_TCP_PORT} is not a plain decimal integer")',
     "            port = DEFAULT_TCP_PORT\n        elif False:\n            pass"),
    ("a connection NAME accepted where a UUID belongs", SRC,
     "    if not _UUID_RE.match(uuid):", "    if False:"),
    ("uuid trailing anchor dropped - a uuid with junk appended passes", SRC,
     r'    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")',
     r'    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")'),
    ("config failure downgraded to a quiet successful tick", SRC,
     '        print(f"gardyn-netwatch: refusing to run: {exc.detail}", file=sys.stderr,\n'
     "              flush=True)\n        return 2",
     '        print(f"gardyn-netwatch: refusing to run: {exc.detail}", file=sys.stderr,\n'
     "              flush=True)\n        return 0"),
    ("config loaded AFTER the probes run", SRC,
     "    try:\n        cfg = load_config(CONFIG_PATH)",
     "    ping('probe-before-config-check')\n    try:\n        cfg = load_config(CONFIG_PATH)"),

    ("host shape check removed - a target ping reads as a FLAG is accepted", SRC,
     "        if not _HOST_RE.match(target):", "        if False:"),
    ("host shape check loosened to allow a leading dash", SRC,
     r'_HOST_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]*\Z")',
     r'_HOST_RE = re.compile(r"\A[A-Za-z0-9-][A-Za-z0-9._:-]*\Z")'),
    ("tcp host shape check removed", SRC,
     "    if not _HOST_RE.match(tcp_host):", "    if False:"),
    ("minimum target count dropped - one host answers for the whole LAN", SRC,
     "    if len(targets) < MIN_PING_TARGETS:", "    if False:"),
    ("minimum target count lowered to one", SRC,
     "MIN_PING_TARGETS = 2", "MIN_PING_TARGETS = 1"),
    ("port strictness dropped back to a bare int()", SRC,
     "        if not (raw_port.isascii() and raw_port.isdigit()):",
     "        if False:"),
    ("undecodable config file raises instead of refusing", SRC,
     "    except (OSError, UnicodeDecodeError):\n        return None",
     "    except OSError:\n        return None"),
    ("undecodable bytes SALVAGED into a config value", SRC,
     '        with open(path, encoding="utf-8") as handle:',
     '        with open(path, encoding="utf-8", errors="replace") as handle:'),
    ("logfmt quoting no longer escapes an embedded quote", SRC,
     _ESCAPE_LINE, "    escaped = (text\n                   .replace('x', 'x')"),
    # --------------------------------------------------- installer preflight
    ("installer arms the watchdog with no config file", INSTALLER,
     '    if [ ! -f "$NETWATCH_CONFIG" ] || [ ! -s "$NETWATCH_CONFIG" ]; then\n'
     '        echo "$NETWATCH_CONFIG is missing or empty"\n'
     '        return 0\n'
     '    fi',
     '    if false; then\n'
     '        echo "$NETWATCH_CONFIG is missing or empty"\n'
     '        return 0\n'
     '    fi'),
    ("installer accepts a zero-byte config", INSTALLER,
     '    if [ ! -f "$NETWATCH_CONFIG" ] || [ ! -s "$NETWATCH_CONFIG" ]; then',
     '    if [ ! -e "$NETWATCH_CONFIG" ]; then'),
    ("installer downgrades the missing config to a warning", INSTALLER,
     '                record_failure "$u: NOT enabled - $problem.',
     '                log_warn "$u: NOT enabled - $problem.'),
    ("installer default config path silently changed", INSTALLER,
     'NETWATCH_CONFIG="${GARDYN_NETWATCH_CONFIG:-/etc/gardyn/netwatch.env}"',
     'NETWATCH_CONFIG="${GARDYN_NETWATCH_CONFIG:-/etc/gardyn/netwatch.env.bak}"'),

    # ------------------------------------------- reintroduced hardcoding
    # These ADD code back rather than break code that is present, which is the
    # only way to pin a policy of absence.
    ("hardcoded ping targets restored as a fallback", SRC,
     "    targets = tuple(t for t in re.split(r\"[,\\s]+\", env[KEY_TARGETS].strip()) if t)",
     "    targets = tuple(t for t in re.split(r\"[,\\s]+\", env.get(KEY_TARGETS, \"192.0.2.1,192.0.2.9\").strip()) if t)"),
    ("hardcoded tcp probe host restored as a default argument", SRC,
     "def tcp_probe(host: str, port: int) -> bool | None:",
     'def tcp_probe(host: str = "192.0.2.9", port: int = 1883) -> bool | None:'),
    ("hardcoded wlan uuid restored as a default argument", SRC,
     "def reconnect(wlan_uuid: str) -> str:",
     'def reconnect(wlan_uuid: str = "99999999-8888-7777-6666-555555555555") -> str:'),
    ("module-level topology constant restored", SRC,
     'STATE_PATH = "/var/lib/gardyn-netwatch/state.json"',
     'TARGETS = ("192.0.2.1", "192.0.2.9")\nSTATE_PATH = "/var/lib/gardyn-netwatch/state.json"'),
    ("template shipped pre-filled with a real address", TEMPLATE,
     "GARDYN_NETWATCH_TCP_HOST=CHANGEME-broker-address",
     "GARDYN_NETWATCH_TCP_HOST=192.0.2.9"),
    ("template shipped pre-filled with a real uuid", TEMPLATE,
     "GARDYN_NETWATCH_WLAN_UUID=CHANGEME-00000000-0000-0000-0000-000000000000",
     "GARDYN_NETWATCH_WLAN_UUID=99999999-8888-7777-6666-555555555555"),

    # --------------------------------------------------------- THE REBOOT PATH
    # The destructive action. Each mutant here is a way the host gets power-
    # cycled when it should not have been, or the guard that stopped it goes
    # missing.
    ("reboot ordered without a reconnect ever being tried", SRC,
     "    reboot_earned = reconnects >= 1 and down_for >= REBOOT_AFTER_DOWN_S",
     "    reboot_earned = down_for >= REBOOT_AFTER_DOWN_S"),
    ("down-threshold removed: reboot on the first failing check", SRC,
     "    reboot_earned = reconnects >= 1 and down_for >= REBOOT_AFTER_DOWN_S",
     "    reboot_earned = reconnects >= 1"),
    ("down-threshold widened to nothing", SRC,
     "REBOOT_AFTER_DOWN_S = 300.0", "REBOOT_AFTER_DOWN_S = 1.0"),
    ("just-booted guard removed - reboots a Pi for being early", SRC,
     "        if uptime_s < MIN_UPTIME_BEFORE_REBOOT_S:", "        if False:"),
    ("consecutive-reboot cap removed - endless power cycle", SRC,
     '        if new_state["consecutive_reboots"] >= MAX_CONSECUTIVE_REBOOTS:',
     "        if False:"),
    ("consecutive-reboot cap widened to 99", SRC,
     "MAX_CONSECUTIVE_REBOOTS = 2", "MAX_CONSECUTIVE_REBOOTS = 99"),
    ("one lucky healthy tick re-arms the cap (the flapping-link defect)", SRC,
     "HEALTHY_STREAK_TO_REARM = 15", "HEALTHY_STREAK_TO_REARM = 1"),
    ("unwritable state no longer suppresses the reboot", SRC,
     "        if not save_state(STATE_PATH, new_state):", "        if False:"),
    # A MOVE, not an addition: leaving the original write in place and adding a
    # second one changes nothing, and the harness would score it a survivor for
    # a defect it never actually created.
    ("cap increment written AFTER the reboot is ordered", SRC,
     _REBOOT_SAVE_FIRST, _REBOOT_SAVE_LAST),
    ("poweroff instead of reboot - permanently dark garden", SRC,
     '        proc = subprocess.run(["systemctl", "reboot"],',
     '        proc = subprocess.run(["systemctl", "poweroff"],'),
    ("a failed reboot reported as ordered", SRC,
     '    return "reboot_ordered" if proc.returncode == 0 else f"reboot_exit_{proc.returncode}"',
     '    return "reboot_ordered"'),
    ("stand-down on an unmeasurable network replaced by escalation", SRC,
     "    if not reachable and not measured:", "    if False:"),
    ("missing uptime no longer stands down", SRC,
     "    if uptime_s is None:\n        # No trustworthy clock", "    if False:\n        # No trustworthy clock"),

    # ------------------------------------------- the T-494 refusal-shape fixes
    #
    # All three restore a defect whose damage is to the REFUSAL, not to the
    # ladder: an operator loses either the named reason or the config itself,
    # on a host with no physical recovery path where the journal line is the
    # only thing anybody can act on.
    ("port length bound removed - a long all-digit port is a TRACEBACK, "
     "not a named refusal", SRC,
     "        if len(raw_port) > 5:", "        if False:"),
    ("load_state stops catching RecursionError, so a corrupt state file "
     "takes the whole ladder down", SRC,
     "    except (ValueError, TypeError, RecursionError):",
     "    except (ValueError, TypeError):"),
    ("config read back to the LOCALE encoding - a valid UTF-8 config is "
     "refused as unreadable under a C locale", SRC,
     '        with open(path, encoding="utf-8") as handle:',
     "        with open(path) as handle:"),
    ("logfmt no longer escapes control characters - one record splits into "
     "three with an injected field", SRC,
     '                   .replace("\\n", "\\\\n")\n'
     '                   .replace("\\r", "\\\\r")\n'
     '                   .replace("\\t", "\\\\t"))',
     "                   )"),
    ("the quoting decision stops seeing the escape, so a control character "
     "slips past for not being a space", SRC,
     '    if escaped != text or " " in text or \'"\' in text:',
     '    if " " in text or \'"\' in text:'),

    # ------------------------------------------------------ mqtt.service Type=
    ("mqtt.service back to the default Type (broken start reports success)", UNIT,
     "Type=exec\nUser=gardyn", "User=gardyn"),
    ("mqtt.service pinned to Type=simple explicitly", UNIT,
     "Type=exec\nUser=gardyn", "Type=simple\nUser=gardyn"),
]

# Control B: a deliberately broken assertion that MUST make the suite red. If
# this scores green the scorer is broken and every "killed" above is a lie.
CONTROL_B = ("tests/test_netwatch.py",
             "        self.assertEqual(nw.REBOOT_AFTER_DOWN_S, 300.0)",
             "        self.assertEqual(nw.REBOOT_AFTER_DOWN_S, -1.0)")


def run_suite(root: Path, modules: tuple = TEST_MODULES) -> tuple[int, str]:
    """Run the suites against whatever is on disk. Returns (rc, merged output).

    The __pycache__ purge is load-bearing, not hygiene: the module under test
    is loaded through SourceFileLoader, which validates cached bytecode on
    (mtime-SECONDS, size). A mutation that preserves file size and is applied
    and reverted inside one second would otherwise re-run the PREVIOUS
    bytecode and return a verdict belonging to the wrong mutant. `-B`
    suppresses WRITING a cache, not READING a stale one.

    stdout and stderr are merged because unittest reports on stderr; grepping
    stdout alone is how a battery ends up scoring every mutant against no
    output at all.
    """
    for cached in root.rglob("__pycache__"):
        shutil.rmtree(cached, ignore_errors=True)
    proc = subprocess.run(
        [sys.executable, "-B", "-m", "unittest", *modules],
        cwd=root, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
             "PYTHONDONTWRITEBYTECODE": "1", "HOME": str(root)})
    return proc.returncode, proc.stdout + proc.stderr


def apply_once(target: Path, old: str, new: str) -> str | None:
    """Replace `old` with `new`, or return why it could not be done.

    The anchor must match EXACTLY ONCE. A `sed` that matches nothing exits 0,
    and a battery built on one reports the untouched tree as a surviving
    mutant — a survivor for a mutation that never happened.
    """
    original = target.read_text()
    count = original.count(old)
    if count != 1:
        return f"anchor appears {count}x, not 1"
    target.write_text(original.replace(old, new))
    return None


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        shutil.copytree(REPO, root,
                        ignore=shutil.ignore_patterns("__pycache__", "venv"))

        # --- CONTROL A: the clean tree must score GREEN -------------------
        rc, out = run_suite(root)
        if rc != 0:
            print("CONTROL A FAILED - the clean tree is already red, so no "
                  "mutant verdict below could mean anything.\n" + out[-4000:])
            return 2
        total = re.search(r"Ran (\d+) tests", out)
        print(f"CONTROL A ok: clean tree GREEN ({total.group(1) if total else '?'} tests)")

        # --- CONTROL B: a broken assertion must score RED -----------------
        # Control A alone is worthless: it is scored by the same code path,
        # so a scorer that can only ever return "caught" passes it happily.
        ctl_path = root / CONTROL_B[0]
        ctl_original = ctl_path.read_text()
        problem = apply_once(ctl_path, CONTROL_B[1], CONTROL_B[2])
        if problem:
            print(f"CONTROL B FAILED - could not inject the broken assertion: {problem}")
            return 2
        rc, out = run_suite(root)
        ctl_path.write_text(ctl_original)
        if rc == 0:
            print("CONTROL B FAILED - a deliberately broken assertion scored "
                  "GREEN. The scorer cannot see failures; every result is a lie.")
            return 2
        print("CONTROL B ok: broken assertion scored RED - the scorer works\n")

        # --- the battery --------------------------------------------------
        pristine = {rel: (root / rel).read_text()
                    for rel in {m[1] for m in MUTATIONS}}
        survived, broken = [], []
        for name, relpath, old, new in MUTATIONS:
            target = root / relpath
            problem = apply_once(target, old, new)
            if problem:
                print(f"  BROKEN    {name}: {problem}")
                broken.append(name)
                continue
            # Prove the edit actually landed rather than trusting the write.
            if target.read_text() == pristine[relpath]:
                print(f"  BROKEN    {name}: file unchanged after mutation")
                broken.append(name)
                target.write_text(pristine[relpath])
                continue

            rc, out = run_suite(root, SUITES_FOR[relpath])
            target.write_text(pristine[relpath])

            detail = re.search(r"FAILED \((.*)\)", out)
            if rc == 0:
                print(f"  SURVIVED  {name}")
                survived.append(name)
            else:
                print(f"  killed    {name}  [{detail.group(1) if detail else 'error'}]")

        # --- the tree must be byte-identical again -------------------------
        drifted = [rel for rel, text in pristine.items()
                   if (root / rel).read_text() != text]
        print()
        if drifted:
            print(f"RESTORE FAILED - still mutated: {drifted}")
            return 2
        print("all mutated files restored byte-identical")

        if broken:
            print(f"\n{len(broken)} MUTATION(S) COULD NOT BE APPLIED - the battery "
                  "did not measure them:")
            for name in broken:
                print(f"  - {name}")
        if survived:
            print(f"\n{len(survived)} MUTATION(S) SURVIVED - each is either a "
                  "decorative test or redundant code:")
            for name in survived:
                print(f"  - {name}")
        if survived or broken:
            return 1
        print(f"\nall {len(MUTATIONS)} mutations killed")
        return 0


if __name__ == "__main__":
    sys.exit(main())
