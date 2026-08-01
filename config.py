# config.py
#
# Reviewed: 2026-08-01 against 3181aac (T-478)
# Reviewed: 2026-07-31 against b0f8f92 (T-472)
import math
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# MQTT configurations
BROKER = os.getenv("MQTT_BROKER", "localhost")
PORT = int(os.getenv("MQTT_PORT", "1883"))
KEEP_ALIVE_INTERVAL = int(os.getenv("MQTT_KEEPALIVE_INTERVAL", "60"))

# Topic configurations
VERSION = os.getenv("MQTT_VERSION", "1.0.0")
IDENTIFIER = os.getenv("MQTT_IDENTIFIER", "gardyn-xx")
MODEL= os.getenv("MQTT_DEVICE_MODEL", "gardyn 3.0")
BASE_TOPIC = os.getenv("MQTT_BASETOPIC", "gardyn")

USERNAME = os.getenv("MQTT_USERNAME")
PASSWORD = os.getenv("MQTT_PASSWORD")

SENSOR_TYPE = os.getenv('SENSOR_TYPE')

# Distance at which the reservoir counts as low. Set to 0 (or leave unset) to
# disable low-water checking entirely, which also lifts the pump interlock.
#
# CAVEAT: on the calibration in upstream PR #90 a FULL tank reads ~10-12 cm and
# a near-empty one ~23 cm (measured on a Gardyn 4.0), so a threshold of 11 sits
# at the full-tank reading rather than near the empty end. Re-measure against
# the real reservoir before trusting it - see T-472/T-299.
WATER_LOW_CM = float(os.getenv("WATER_LOW_CM", 0)) or None

# Plausibility band for the reservoir ultrasonic, in cm. A reading outside this
# range is treated as NO READING rather than as a distance.
#
# This is the load-bearing safety control, not a smoothing nicety. gpiozero
# returns None on a no-echo and the sensor is built with ignore={None}, so a
# disconnected or silent sensor is not reported as an error - spurious edges on
# the floating pin fill the averaging queue and the median latches somewhere
# arbitrary. Observed on this unit: values ranging from 0.09 cm to ~83 cm over
# three days with no hardware attached at all. Both ends of that range look
# like a perfectly valid distance to a bare threshold comparison, so without a
# band the low-water alert is a coin flip that reports either a false all-clear
# or a false alarm depending on where the garbage happened to settle.
#
# Defaults from upstream PR #90: below 3 cm is a sensor error or overflow,
# above 25 cm is out of range or empty.
#
# The MINIMUM is the safety-critical half and is validated below. A sensor that
# has never echoed reads exactly 0.0 cm - gpiozero's queue stays empty and the
# median of an empty queue is 0.0 - and 0.0 is the FULL-TANK end of the scale.
# Nothing else in the system can tell that apart from a genuinely full
# reservoir, so a minimum of 0 would turn a dead sensor into a permanent "tank
# full" and let the pump run dry: the exact failure this band exists to prevent.
#
# The MAXIMUM must sit above the real empty-tank reading with margin, or a
# genuinely empty reservoir is discarded as implausible and the low-water alarm
# goes unavailable instead of firing. On the PR #90 calibration empty is ~23 cm
# against a default max of 25, which is thin - re-measure on the real hardware
# (T-299) rather than trusting these numbers.
_WATER_BAND_DEFAULTS = (3.0, 25.0)


def _load_water_band():
    """Read the band from env, falling back to defaults on anything unsafe.

    Deliberately does NOT raise. This module is imported by mqtt.py, whose
    systemd unit carries Restart=always with StartLimitIntervalSec=0, so an
    exception here is not a loud failure - it is a permanent 10-second crash
    loop that takes the lights, the cameras and the pump down with it, and is
    recoverable only over SSH. A bad band degrades to the safe default plus a
    logged error instead.
    """
    import logging

    raw_min = os.getenv("WATER_VALID_MIN_CM")
    raw_max = os.getenv("WATER_VALID_MAX_CM")
    default_min, default_max = _WATER_BAND_DEFAULTS

    try:
        low = float(raw_min) if raw_min not in (None, "") else default_min
        high = float(raw_max) if raw_max not in (None, "") else default_max
    except ValueError:
        logging.getLogger(__name__).error(
            "Unparseable water band (min=%r max=%r); using defaults %s-%s cm",
            raw_min, raw_max, default_min, default_max,
        )
        return default_min, default_max

    problems = []
    if not (math.isfinite(low) and math.isfinite(high)):
        problems.append("non-finite bound")
    # > 0, not >= 0: a minimum of zero admits the 0.0 a dead sensor reports.
    if low <= 0:
        problems.append("minimum must be greater than zero")
    if low >= high:
        problems.append("minimum must be below maximum")

    if problems:
        logging.getLogger(__name__).error(
            "Refusing unsafe water band %r-%r (%s); using defaults %s-%s cm",
            raw_min, raw_max, "; ".join(problems), default_min, default_max,
        )
        return default_min, default_max

    return low, high


WATER_VALID_MIN_CM, WATER_VALID_MAX_CM = _load_water_band()

UPPER_CAMERA_DEVICE = os.getenv("UPPER_CAMERA_DEVICE", "/dev/video0")
LOWER_CAMERA_DEVICE = os.getenv("LOWER_CAMERA_DEVICE", "/dev/video2")
UPPER_IMAGE_PATH = os.getenv("UPPER_IMAGE_PATH", "/tmp/upper_camera.jpg")
LOWER_IMAGE_PATH = os.getenv("LOWER_IMAGE_PATH", "/tmp/lower_camera.jpg")
CAMERA_RESOLUTION = os.getenv("CAMERA_RESOLUTION", "640x480")
# Per-camera overrides; fall back to the shared CAMERA_RESOLUTION when unset.
UPPER_CAMERA_RESOLUTION = os.getenv("UPPER_CAMERA_RESOLUTION", CAMERA_RESOLUTION)
LOWER_CAMERA_RESOLUTION = os.getenv("LOWER_CAMERA_RESOLUTION", CAMERA_RESOLUTION)
IMAGE_INTERVAL_SECONDS = int(os.getenv("IMAGE_INTERVAL_SECONDS", "3600"))

# JPEG quality for the camera captures, 0-95 (T-478).
#
# The range is fswebcam's, not ours. Its man page: "--jpeg <factor>  Set JPEG as
# the output image format. The compression factor is a value between 0 and 95,
# or -1 for automatic. This is the default format, with a factor of -1."
#
# _capture_and_publish() invoked fswebcam with no --jpeg flag, so it took that
# documented -1 default - and on this hardware -1 does NOT mean "automatic". In
# fswebcam.c the field is `char compression;` (line 180), set to -1 at line 785
# and passed straight to gdImageJpeg() at line 507 with no validation. Plain
# `char` is UNSIGNED on ARM, so -1 stores as 255, gd never takes its
# quality-is-negative "use the default" branch, and hands 255 to libjpeg, which
# clamps it to 100. Maximum quality, on every frame.
#
# The frames say so themselves: `file(1)` on both live captures reports
# `CREATOR: gd-jpeg v1.0 (using IJG JPEG v62), quality = 255` - gd prints the
# value it was handed, which is why the artefact names the bug exactly. That is
# why a 640x480 frame cost 326 KB.
#
# Passing an explicit in-range value sidesteps the whole thing. -1 is therefore
# NOT an accepted value here even though fswebcam documents it: "automatic" is
# the bug.
#
# Measured on the Pi with identical -S 2 -F 2 flags, so the comparison is fair:
#
#   lower 640x480    no flag 325,070 | q95 151,093 | q85 83,003 | q75 66,197
#   upper 1600x1200  no flag 440,713 | q95 187,254 | q85 90,381 | q75 76,754
#
# Per cycle that is ~748 KB against ~169 KB at q85 - a 4.4x reduction for no
# visible loss on a plant camera. It matters because this burst every five
# minutes is the only sustained TX load on a host whose uplink has collapsed to
# 802.11b rates with a 13% tx-failure rate, and a blocking publish stalls all
# MQTT processing including keepalive handling (see mqtt.py's flash_lights
# note). 88 keep-alive timeouts and ~28 network outages a day are the symptom
# this is a hypothesis for - unproven, but free and reversible.
#
# 85 rather than 95 because 95 only buys back half the saving. Resolution and
# IMAGE_INTERVAL_SECONDS are deliberately NOT touched: the retired T-309
# timelapse SHA-256 dedups consecutive frames, so a longer interval could dedup
# away real growth, and quality has no such interaction.
_JPEG_QUALITY_DEFAULT = 85
# fswebcam's own documented bounds. See the docstring below before widening.
_JPEG_QUALITY_MIN = 0
_JPEG_QUALITY_MAX = 95


def _load_jpeg_quality(var, default):
    """Read a JPEG quality from env, falling back to `default` on anything unusable.

    The range check is the point of the setting rather than defensive padding,
    and it is fswebcam's documented 0-95 rather than a guess. fswebcam does no
    validation of its own - `config->compression = atoi(options)` straight into
    gdImageJpeg() - so anything this module lets through is passed to libgd
    unchecked. Both ends matter:

    - Above 95 is outside the documented range. libjpeg would clamp to 100,
      which is the maximum-quality behaviour this ticket exists to stop.
    - -1 is documented and legal, and is REFUSED anyway: it selects the
      "automatic" path that stores as 255 on ARM and produces the bug.

    Deliberately does NOT raise, for the same reason _load_water_band() does
    not: mqtt.py's systemd unit carries Restart=always with
    StartLimitIntervalSec=0, so a ValueError here is not a loud failure but a
    permanent crash loop that takes the lights and the cameras with it.
    """
    import logging

    raw = os.getenv(var)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        logging.getLogger(__name__).error(
            "Unparseable %s=%r; using %s", var, raw, default)
        return default
    if not _JPEG_QUALITY_MIN <= value <= _JPEG_QUALITY_MAX:
        logging.getLogger(__name__).error(
            "Refusing out-of-range %s=%r (fswebcam accepts %s-%s); using %s",
            var, raw, _JPEG_QUALITY_MIN, _JPEG_QUALITY_MAX, default)
        return default
    return value


CAMERA_JPEG_QUALITY = _load_jpeg_quality("CAMERA_JPEG_QUALITY", _JPEG_QUALITY_DEFAULT)
# Per-camera overrides; fall back to the shared CAMERA_JPEG_QUALITY when unset,
# mirroring the resolution settings above so the two cameras can differ.
UPPER_CAMERA_JPEG_QUALITY = _load_jpeg_quality(
    "UPPER_CAMERA_JPEG_QUALITY", CAMERA_JPEG_QUALITY)
LOWER_CAMERA_JPEG_QUALITY = _load_jpeg_quality(
    "LOWER_CAMERA_JPEG_QUALITY", CAMERA_JPEG_QUALITY)
