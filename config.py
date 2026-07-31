# config.py
#
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
