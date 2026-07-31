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
WATER_VALID_MIN_CM = float(os.getenv("WATER_VALID_MIN_CM", "3.0"))
WATER_VALID_MAX_CM = float(os.getenv("WATER_VALID_MAX_CM", "25.0"))

UPPER_CAMERA_DEVICE = os.getenv("UPPER_CAMERA_DEVICE", "/dev/video0")
LOWER_CAMERA_DEVICE = os.getenv("LOWER_CAMERA_DEVICE", "/dev/video2")
UPPER_IMAGE_PATH = os.getenv("UPPER_IMAGE_PATH", "/tmp/upper_camera.jpg")
LOWER_IMAGE_PATH = os.getenv("LOWER_IMAGE_PATH", "/tmp/lower_camera.jpg")
CAMERA_RESOLUTION = os.getenv("CAMERA_RESOLUTION", "640x480")
# Per-camera overrides; fall back to the shared CAMERA_RESOLUTION when unset.
UPPER_CAMERA_RESOLUTION = os.getenv("UPPER_CAMERA_RESOLUTION", CAMERA_RESOLUTION)
LOWER_CAMERA_RESOLUTION = os.getenv("LOWER_CAMERA_RESOLUTION", CAMERA_RESOLUTION)
IMAGE_INTERVAL_SECONDS = int(os.getenv("IMAGE_INTERVAL_SECONDS", "3600"))
