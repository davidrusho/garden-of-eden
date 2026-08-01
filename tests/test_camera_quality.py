"""Tests that fswebcam is told what JPEG quality to encode at (T-478).

The bug this covers IS AN ABSENCE, which inverts the usual test-writing
instinct. `_capture_and_publish()` built an fswebcam argv with no `--jpeg` flag
at all; the quality parameter was then never set and fell through to an
out-of-range default, visible in the frames themselves as `quality = 255` in
their own JPEG comment. Nothing errored, nothing logged, and the captures
looked perfect - they just cost ~748 KB per five-minute cycle on a host whose
uplink had collapsed to 802.11b rates.

So a test that asserts "argv contains the resolution" passes happily with the
flag gone, and so does one that asserts the capture succeeded. The assertions
here are specifically that the FLAG IS PRESENT and carries the configured
value, and the mutation battery's first mutant deletes it again.

Stubs come from tests.test_water_interlock, which owns the sys.modules hardware
stubs and the real `import mqtt`; a second stubbing module fights the first.

Run:  python3 -m unittest tests.test_camera_quality
"""

import importlib.util
import logging
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from tests.test_water_interlock import mqtt_mod

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_git_checkout():
    """Ask git, rather than looking for a .git DIRECTORY.

    In a linked worktree `.git` is a file containing a gitdir pointer, so an
    isdir() test reports "not a checkout" and silently skips a test that should
    have run.
    """
    import subprocess as sp
    try:
        return sp.run(["git", "rev-parse", "--is-inside-work-tree"],
                      cwd=_REPO_ROOT, stdout=sp.DEVNULL,
                      stderr=sp.DEVNULL).returncode == 0
    except (OSError, sp.SubprocessError):
        return False


class TestFswebcamArgv(unittest.TestCase):
    """What actually reaches the fswebcam process."""

    def setUp(self):
        self.client = MagicMock()
        self.check_call = patch.object(
            mqtt_mod.subprocess, "check_call").start()
        # _capture_and_publish opens the captured file afterwards; give it one.
        self.open_patch = patch("builtins.open", create=True).start()
        self.addCleanup(patch.stopall)

    def _capture(self, **kwargs):
        args = dict(
            client=self.client, label="upper", device="/dev/video0",
            resolution="1600x1200", quality=85,
            image_path="/tmp/u.jpg", topic="/image/upper_camera",
        )
        args.update(kwargs)
        mqtt_mod._capture_and_publish(
            args["client"], args["label"], args["device"], args["resolution"],
            args["quality"], args["image_path"], args["topic"],
        )
        return self.check_call.call_args.args[0]

    def test_the_jpeg_flag_is_present(self):
        # THE test. Without it the quality parameter is never set and gd
        # encodes at an out-of-range 255.
        self.assertIn("--jpeg", self._capture())

    def test_the_jpeg_flag_carries_the_configured_value(self):
        argv = self._capture(quality=85)
        self.assertEqual(argv[argv.index("--jpeg") + 1], "85")

    def test_a_different_quality_reaches_the_argv(self):
        # Guards against a hardcoded literal that happens to equal the default,
        # which would pass the test above while ignoring the setting entirely.
        argv = self._capture(quality=60)
        self.assertEqual(argv[argv.index("--jpeg") + 1], "60")

    def test_the_quality_is_a_string_in_the_argv(self):
        # subprocess rejects an int in an argv list with a TypeError, which the
        # function's own except-Exception would swallow into a log line - every
        # capture silently failing while nothing looks wrong.
        argv = self._capture()
        self.assertTrue(all(isinstance(a, str) for a in argv), argv)

    def test_the_existing_flags_are_untouched(self):
        # -S 2 -F 2 are the frame-skip and frame-average settings the measured
        # numbers were taken with; the resolution is deliberately NOT changed
        # by this ticket.
        argv = self._capture()
        self.assertEqual(argv[0], "fswebcam")
        for flag, value in (("-d", "/dev/video0"), ("-r", "1600x1200"),
                            ("-S", "2"), ("-F", "2")):
            with self.subTest(flag=flag):
                self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--no-banner", argv)
        self.assertEqual(argv[-1], "/tmp/u.jpg")


class TestBothCamerasGetTheirOwnQuality(unittest.TestCase):
    """The publish loop wires a per-camera value through, not a module global."""

    def setUp(self):
        self.calls = []

        def record(client, label, device, resolution, quality, image_path, topic):
            self.calls.append((label, resolution, quality))

        patch.object(mqtt_mod, "_capture_and_publish", record).start()
        # publish_images() is `while True: ... sleep(...)`, so break out of it
        # after the first cycle rather than letting it spin.
        patch.object(mqtt_mod, "sleep",
                     side_effect=RuntimeError("stop")).start()
        self.addCleanup(patch.stopall)

    def test_each_camera_is_captured_with_its_own_quality(self):
        # The stubbed config gives upper 85 and lower 70 precisely so a single
        # shared value cannot satisfy this.
        with self.assertRaises(RuntimeError):
            mqtt_mod.publish_images(MagicMock())
        self.assertEqual(
            [(label, quality) for label, _, quality in self.calls],
            [("upper", 85), ("lower", 70)],
        )

    def test_resolution_still_travels_per_camera_too(self):
        with self.assertRaises(RuntimeError):
            mqtt_mod.publish_images(MagicMock())
        self.assertEqual([r for _, r, _ in self.calls], ["640x480", "640x480"])


class TestQualityConfigLoading(unittest.TestCase):
    """config.py's loader, read from disk rather than through the stub.

    tests.test_water_interlock replaces `config` with a stub module, so the real
    loader never runs in this process. This mirrors TestBandConfigValidation in
    that file: load config.py from its file under a private name, with dotenv
    stubbed so the developer's own .env cannot leak in.
    """

    CAMERA_VARS = ("CAMERA_JPEG_QUALITY", "UPPER_CAMERA_JPEG_QUALITY",
                   "LOWER_CAMERA_JPEG_QUALITY")

    def _load(self, **env):
        saved_dotenv = sys.modules.get("dotenv")
        sys.modules["dotenv"] = types.ModuleType("dotenv")
        sys.modules["dotenv"].load_dotenv = lambda *a, **k: None
        saved_env = {k: os.environ.get(k) for k in self.CAMERA_VARS}
        # Silence the deliberate error logs from the rejection cases.
        logging.disable(logging.CRITICAL)
        try:
            for k in self.CAMERA_VARS:
                os.environ.pop(k, None)
            for k, v in env.items():
                os.environ[k] = v
            spec = importlib.util.spec_from_file_location(
                "_cfg_camera_under_test", os.path.join(_REPO_ROOT, "config.py"))
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            return (m.CAMERA_JPEG_QUALITY, m.UPPER_CAMERA_JPEG_QUALITY,
                    m.LOWER_CAMERA_JPEG_QUALITY)
        finally:
            logging.disable(logging.NOTSET)
            for k, v in saved_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            if saved_dotenv is None:
                sys.modules.pop("dotenv", None)
            else:
                sys.modules["dotenv"] = saved_dotenv

    def test_the_default_is_85_when_unset(self):
        self.assertEqual(self._load(), (85, 85, 85))

    def test_a_shared_override_reaches_both_cameras(self):
        self.assertEqual(self._load(CAMERA_JPEG_QUALITY="75"), (75, 75, 75))

    def test_a_per_camera_override_is_honoured(self):
        self.assertEqual(
            self._load(CAMERA_JPEG_QUALITY="80", LOWER_CAMERA_JPEG_QUALITY="60"),
            (80, 80, 60),
        )

    def test_255_is_refused(self):
        # The exact value the frames reported. Accepting it from the
        # environment would reinstate the bug through a different door while
        # looking configured.
        self.assertEqual(self._load(CAMERA_JPEG_QUALITY="255"), (85, 85, 85))

    def test_minus_one_is_refused_even_though_fswebcam_documents_it(self):
        # fswebcam's man page lists -1 as legal ("automatic"), and -1 is
        # precisely the bug: it is the default the missing flag selected, and
        # it becomes 255 in an unsigned char on ARM.
        self.assertEqual(self._load(CAMERA_JPEG_QUALITY="-1")[0], 85)

    def test_values_above_fswebcams_range_are_refused(self):
        # fswebcam(1): "The compression factor is a value between 0 and 95".
        # It validates nothing itself - atoi() straight into gdImageJpeg - so
        # 96-100 would reach libjpeg and clamp to maximum quality, which is the
        # behaviour this ticket removes.
        for bad in ("96", "100", "101", "1000"):
            with self.subTest(value=bad):
                self.assertEqual(self._load(CAMERA_JPEG_QUALITY=bad)[0], 85)

    def test_the_documented_range_edges_are_accepted(self):
        # 0 and 95 are fswebcam's stated bounds. 0 is useless in practice but
        # it is legal, and inventing a narrower range here would be this
        # module second-guessing the tool it drives.
        self.assertEqual(self._load(CAMERA_JPEG_QUALITY="0")[0], 0)
        self.assertEqual(self._load(CAMERA_JPEG_QUALITY="95")[0], 95)

    def test_unparseable_values_fall_back_instead_of_raising(self):
        # An exception here is not a loud failure: mqtt.service carries
        # Restart=always with StartLimitIntervalSec=0, so it is a permanent
        # crash loop that takes the lights and the cameras down too.
        for bad in ("", "high", "85.5", "8 5"):
            with self.subTest(value=bad):
                self.assertEqual(self._load(CAMERA_JPEG_QUALITY=bad)[0], 85)

    def test_a_bad_shared_value_does_not_poison_a_good_per_camera_one(self):
        self.assertEqual(
            self._load(CAMERA_JPEG_QUALITY="255", UPPER_CAMERA_JPEG_QUALITY="70"),
            (85, 70, 85),
        )


class TestEnvTemplateShipsTheSetting(unittest.TestCase):
    """The template is how the next deployment gets the setting at all.

    Its own test because a widened `.env` ignore silently un-shipping the
    template is a known trap: the repo publishes cleanly and cannot be
    configured, and no secret scan complains.
    """

    def test_the_template_documents_the_quality(self):
        with open(os.path.join(_REPO_ROOT, ".env-dist")) as fh:
            body = fh.read()
        self.assertIn("CAMERA_JPEG_QUALITY=85", body)

    @unittest.skipUnless(_is_git_checkout(),
                         "not a git checkout - nothing to ask git about")
    def test_the_template_is_git_tracked(self):
        # Skipped rather than failed outside a checkout. This ran as a hard
        # assertion first and produced a FALSE RED inside a `git archive` copy,
        # which is the worst outcome for a test whose whole job is to make a
        # packaging mistake visible.
        #
        # The skip predicate ASKS GIT rather than looking for a .git directory.
        # First attempt used os.path.isdir(".git") and skipped in this repo's
        # own worktree, where .git is a FILE pointing at the real gitdir - a
        # test that silently opts out is barely better than one that is absent.
        import subprocess as sp
        proc = sp.run(["git", "ls-files", ".env-dist"], cwd=_REPO_ROOT,
                      stdout=sp.PIPE, stderr=sp.PIPE, text=True)
        # Checked separately, so "git failed" cannot masquerade as "untracked".
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout.strip(), ".env-dist",
                         ".env-dist is not tracked - a fresh clone would have "
                         "no template to copy, and no secret scan would notice")


if __name__ == "__main__":
    unittest.main()
