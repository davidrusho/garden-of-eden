#!/usr/bin/env python3
"""Mutation battery for the fswebcam JPEG quality setting (T-478).

The defect being fixed IS AN ABSENCE - fswebcam was invoked with no `--jpeg`
flag at all - which makes this suite unusually easy to write uselessly. A test
that checks "the capture succeeded" or "the resolution is in the argv" passes
perfectly with the flag deleted again, because deleting it produces no error,
no log line and a visually identical photo. It only costs 4.4x the bytes.

So mutant [1] is the whole point of this file: remove `--jpeg` and confirm a
test goes RED. Everything else is supporting fire.

Run:  python3 tests/mutate_camera_quality.py

Two controls gate every result and BOTH must hold before any verdict is read. A
battery scores a mutant by whether the test run FAILED, so a broken scorer
reports every mutant caught - the most reassuring output available:

  CONTROL A  clean tree               -> must be GREEN
  CONTROL B  deliberately broken code -> must be RED
             (A alone is worthless: it is scored by the same path that may be
             broken, so only B proves the scorer can tell pass from fail.)

__pycache__ is purged before every run and PYTHONDONTWRITEBYTECODE=1 is set;
stderr is merged into stdout because unittest reports there; every anchor must
match exactly once; each mutated file is compared against the original, since a
replacement matching nothing exits successfully and looks like a survivor; and
the tree is asserted byte-identical at the end.
"""
import hashlib
import os
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MQTT = os.path.join(REPO, "mqtt.py")
CONFIG = os.path.join(REPO, "config.py")

SUITES = ["tests.test_camera_quality", "tests.test_water_interlock",
          "tests.test_retired_entities"]

# (file, label, anchor, replacement)
MUTANTS = [
    (MQTT,
     "REMOVE --jpeg entirely - the original bug, and a silent one",
     "'fswebcam', '-d', device, '-r', resolution, '--jpeg', str(quality),",
     "'fswebcam', '-d', device, '-r', resolution,"),

    (MQTT,
     "drop only the VALUE, leaving a dangling --jpeg flag",
     "'-r', resolution, '--jpeg', str(quality),",
     "'-r', resolution, '--jpeg',"),

    (MQTT,
     "hardcode the quality, ignoring the setting",
     "'--jpeg', str(quality),",
     "'--jpeg', '85',"),

    (MQTT,
     "pass the quality as an int, which subprocess rejects",
     "'--jpeg', str(quality),",
     "'--jpeg', quality,"),

    (MQTT,
     "give both cameras the UPPER quality, defeating the per-camera override",
     "LOWER_CAMERA_RESOLUTION, LOWER_CAMERA_JPEG_QUALITY,",
     "LOWER_CAMERA_RESOLUTION, UPPER_CAMERA_JPEG_QUALITY,"),

    (MQTT,
     "swap resolution and quality at the call site",
     "UPPER_CAMERA_RESOLUTION, UPPER_CAMERA_JPEG_QUALITY,",
     "UPPER_CAMERA_JPEG_QUALITY, UPPER_CAMERA_RESOLUTION,"),

    (CONFIG,
     "change the default from 85 to the out-of-range 255",
     "_JPEG_QUALITY_DEFAULT = 85",
     "_JPEG_QUALITY_DEFAULT = 255"),

    (CONFIG,
     "drop the range check, so 255 is accepted from the environment",
     "    if not _JPEG_QUALITY_MIN <= value <= _JPEG_QUALITY_MAX:",
     "    if False:"),

    (CONFIG,
     "widen the ceiling past fswebcam's documented 95",
     "_JPEG_QUALITY_MAX = 95",
     "_JPEG_QUALITY_MAX = 100"),

    (CONFIG,
     "admit -1, the 'automatic' factor that IS the bug on ARM",
     "_JPEG_QUALITY_MIN = 0",
     "_JPEG_QUALITY_MIN = -1"),

    (CONFIG,
     "raise on an unparseable value instead of falling back (crash loop)",
     "    except ValueError:\n"
     "        logging.getLogger(__name__).error(\n"
     '            "Unparseable %s=%r; using %s", var, raw, default)\n'
     "        return default",
     "    except ValueError:\n"
     "        raise"),

    (CONFIG,
     "ignore the per-camera override, collapsing both to the shared value",
     '    "LOWER_CAMERA_JPEG_QUALITY", CAMERA_JPEG_QUALITY)',
     '    "CAMERA_JPEG_QUALITY", CAMERA_JPEG_QUALITY)'),
]


def purge_pycache():
    for root, dirs, _ in os.walk(REPO):
        if ".git" in root:
            continue
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(os.path.join(root, d), ignore_errors=True)


def run_suites():
    purge_pycache()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    combined, ok = [], True
    for suite in SUITES:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", suite],
            cwd=REPO, env=env, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
        )
        combined.append(f"--- {suite} (rc={proc.returncode}) ---\n{proc.stdout}")
        ok = ok and proc.returncode == 0
    return ok, "\n".join(combined)


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def main():
    sources = {MQTT: open(MQTT).read(), CONFIG: open(CONFIG).read()}
    shas = {p: sha(p) for p in sources}

    print("=" * 70)
    print("CONTROL A - clean tree must be GREEN")
    ok, out = run_suites()
    print(f"  clean tree: {'GREEN' if ok else 'RED'}")
    if not ok:
        print(out[-3000:])
        print("\nABORT: clean tree is not green. No mutant verdict is readable.")
        return 2

    print("=" * 70)
    print("CONTROL B - a deliberately broken tree must be RED")
    broken = sources[MQTT].replace("'--jpeg', str(quality),",
                                   "'--CONTROL-B-BROKEN', str(quality),")
    if broken == sources[MQTT]:
        print("ABORT: control-B anchor did not match. Harness is broken.")
        return 2
    open(MQTT, "w").write(broken)
    ok_b, _ = run_suites()
    open(MQTT, "w").write(sources[MQTT])
    print(f"  broken tree: {'GREEN' if ok_b else 'RED'}")
    if ok_b:
        print("\nABORT: the suites passed a deliberately broken tree.")
        print("The scorer cannot tell pass from fail; every verdict below")
        print("would be meaningless. Fix the harness before reading anything.")
        return 2

    print("=" * 70)
    print("BOTH CONTROLS OK - mutant verdicts are readable\n")

    killed, survived = 0, []
    for i, (path, label, old, new) in enumerate(MUTANTS, 1):
        src = sources[path]
        count = src.count(old)
        if count != 1:
            print(f"  [{i:2}] HARNESS ERROR ({count} anchor matches): {label}")
            survived.append((label, f"anchor matched {count}x, not 1"))
            continue

        open(path, "w").write(src.replace(old, new, 1))
        if open(path).read() == src:
            print(f"  [{i:2}] HARNESS ERROR (file unchanged): {label}")
            survived.append((label, "mutation did not apply"))
            open(path, "w").write(src)
            continue

        ok_m, _ = run_suites()
        open(path, "w").write(src)

        if ok_m:
            print(f"  [{i:2}] SURVIVED  {label}")
            survived.append((label, "suites stayed green"))
        else:
            killed += 1
            print(f"  [{i:2}] killed    {label}")

    print("\n" + "=" * 70)
    print(f"RESULT: {killed}/{len(MUTANTS)} killed")

    restored = all(sha(p) == shas[p] for p in sources)
    print(f"tree restored byte-identical: {restored}")

    if survived:
        print("\nSURVIVORS - a survivor is a question about the CODE and the")
        print("HARNESS, not only about the suite. Three explanations: the test")
        print("is weak, the mutation never applied, or the mutated code is")
        print("redundant and genuinely changes nothing.")
        for label, why in survived:
            print(f"  - {label}: {why}")

    return 0 if (killed == len(MUTANTS) and restored) else 1


if __name__ == "__main__":
    sys.exit(main())
