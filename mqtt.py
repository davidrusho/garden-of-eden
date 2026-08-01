# mqtt.py
#
# Reviewed: 2026-07-31 against b0f8f92 (T-472)
import math
import subprocess
import threading
from threading import Timer
import logging
import paho.mqtt.client as mqtt
import base64
import json
# import picamera
# import cv2
from time import sleep
from config import USERNAME, PASSWORD, BROKER, PORT, KEEP_ALIVE_INTERVAL, BASE_TOPIC, IDENTIFIER, MODEL, VERSION, WATER_LOW_CM, WATER_VALID_MIN_CM, WATER_VALID_MAX_CM, UPPER_CAMERA_DEVICE, LOWER_CAMERA_DEVICE, UPPER_IMAGE_PATH, LOWER_IMAGE_PATH, CAMERA_RESOLUTION, UPPER_CAMERA_RESOLUTION, LOWER_CAMERA_RESOLUTION, IMAGE_INTERVAL_SECONDS

from gpiozero import Button  # Import gpiozero Button
from gpiozero.pins.pigpio import PiGPIOFactory

from app.sensors.light.light import Light
from app.sensors.pump.pump import Pump
from app.sensors.pcb_temp.pcb_temp import get_pcb_temperature
from app.sensors.temperature.temperature import temperature_sensor
from app.sensors.humidity.humidity import humidity_sensor
from app.sensors.distance.distance import Distance, MeasurementError

# Configure logging.
#
# force=True is load-bearing. Importing anything from the `app` package pulls in
# a chain that installs a root StreamHandler first, and basicConfig() is a no-op
# once the root logger already has handlers — so without force= the FileHandler
# below is never instantiated, gardyn.log stays 0 bytes, and the journal gets
# logging.BASIC_FORMAT with no timestamps.
logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("gardyn.log"),  # Log to a file
        logging.StreamHandler()  # Log to the console (stdout)
    ],
    force=True,
)

# NOTE: the light module raises its OWN logger to INFO at import (see
# app/sensors/light/light.py). It is deliberately not done here: this file
# cannot be imported without paho/gpiozero/pigpio, so a policy set here is
# unreachable from the test suite, and the root WARNING above would otherwise
# discard every light command before it reaches a handler.
logger = logging.getLogger(__name__)

# Retained availability topic backing every entity's availability_config.
# Published "online" on connect; the broker publishes "offline" from the LWT
# when this client stops answering keepalives (~1.5x KEEP_ALIVE_INTERVAL).
STATUS_TOPIC = BASE_TOPIC + "/status"

# Retained trust topic for the reservoir ultrasonic specifically.
#
# STATUS_TOPIC answers "is this controller alive". It cannot answer "can the
# reservoir reading be believed", and the two are independent: a perfectly
# healthy controller reporting a fabricated distance is exactly the failure this
# exists to make visible. The water entities carry BOTH topics in an
# availability list, so they go unavailable if either the controller dies or the
# reading stops being trustworthy.
WATER_STATUS_TOPIC = BASE_TOPIC + "/water/status"

# Topics this client actually consumes, with the QoS to subscribe at.
#
# This replaces a BASE_TOPIC + "/#" wildcard. That wildcard made the broker echo
# every JPEG this process publishes straight back to it: the two image topics
# are ~0.7 MB per cycle in daylight and roughly double that at night (dark
# sensor noise compresses badly at 1600x1200), which measured as essentially
# 100% of inbound Wi-Fi on a single-antenna Zero W, with paho allocating the
# whole frame on the network thread each time.
#
# Commands are QoS 1 so that, paired with the durable session below, the broker
# queues them while this client is offline instead of discarding them. The
# */get topics are QoS 0 request triggers — nothing in HA publishes to them
# (they were declared via `command_topic` on sensor entities, which HA ignores),
# but they stay subscribed so a manual mosquitto_pub still works.
COMMAND_SUBSCRIPTIONS = [
    (BASE_TOPIC + "/light/command", 1),
    (BASE_TOPIC + "/light/brightness/set", 1),
    (BASE_TOPIC + "/pump/command", 1),
    (BASE_TOPIC + "/pump/speed/set", 1),
    (BASE_TOPIC + "/water/low/cm/set", 1),
    (BASE_TOPIC + "/water/level/get", 0),
    (BASE_TOPIC + "/pcb/temperature/get", 0),
    (BASE_TOPIC + "/temperature/get", 0),
    (BASE_TOPIC + "/humidity/get", 0),
]

# set to INFO, for to capture mqtt messages at info-level messages.
logger.setLevel(logging.WARNING)

# Initialize devices
pin_factory = PiGPIOFactory()

pump = Pump(pin_factory=pin_factory)
light = Light(pin_factory=pin_factory)
distance_sensor = Distance(pin_factory=pin_factory)

# default on brightness
brightness  = 50
speed       = 100
sec_per_min = 60
min_per_hr  = 60

# publish twice an hour
publish_frequency = sec_per_min * min_per_hr / 2

# Button GPIO setup using gpiozero
button_pin = 13
button = Button(button_pin, pin_factory=pin_factory, bounce_time=0.2, hold_time=2)  # hold_time = 2 seconds for long press detection

# Button press bookkeeping. Light/pump state is read from the hardware rather
# than tracked in shadow variables — see toggle_light()/toggle_pump().
double_press_time = 1  # Time to detect a double press (in seconds)
press_count = 0
double_press_timer = None

# State publishing.
#
# All four state topics are retained. Without retain, HA has nothing to subscribe
# to on restart and the entity sits at `unknown` until something changes it —
# which is why the pump entity read `unknown` for its entire history, and why a
# service restart left HA showing a stale "on" for a light that PWMLED had just
# driven to 0. Retained state means HA gets the truth the moment it subscribes.
def publish_light_state(client):
    """Publish the light's ACTUAL duty cycle, not a shadow variable."""
    duty = light.get_brightness()
    client.publish(BASE_TOPIC + "/light/state", "ON" if duty > 0 else "OFF", retain=True)
    client.publish(BASE_TOPIC + "/light/brightness/state", str(int(round(duty))), retain=True)

def publish_pump_state(client):
    """Publish the pump's ACTUAL duty cycle, not a shadow variable."""
    duty = pump.get_speed()
    client.publish(BASE_TOPIC + "/pump/state", "ON" if duty > 0 else "OFF", retain=True)
    client.publish(BASE_TOPIC + "/pump/speed/state", str(int(round(duty))), retain=True)

# Button press callbacks.
#
# These read hardware state rather than a shadow variable. light_state/pump_state
# used to be module globals that only the button path updated, so after any HA
# command the shadow was stale and the next button press just re-sent the state
# the device was already in — the press appeared to do nothing.
def toggle_light():
    if light.get_brightness() > 0:
        logger.info("Toggling Light OFF")
        light.off()
    else:
        logger.info("Toggling Light ON")
        light.set_duty_cycle(brightness)
    publish_light_state(client)

def toggle_pump():
    if pump.get_speed() > 0:
        logger.info("Toggling Pump OFF")
        pump.off()
    else:
        logger.info("Toggling Pump ON")
        # Through start_pump() so the physical button inherits the low-water
        # interlock. It used to call pump.set_speed() directly, which meant a
        # double-press could run the pump dry no matter what the sensor said.
        start_pump(speed, client)
    publish_pump_state(client)

def handle_button_press():
    global press_count, double_press_timer

    press_count += 1

    if press_count == 1:
        # Start a timer to detect if a second press occurs within the double press time window
        double_press_timer = Timer(double_press_time, handle_single_press)
        double_press_timer.start()
    elif press_count == 2:
        # If a second press occurs, cancel the single press action and trigger the double press action
        if double_press_timer:
            double_press_timer.cancel()
        handle_double_press()
        press_count = 0
    else:
        # press_count is written from two threads — this gpiozero callback and
        # the Timer thread running handle_single_press — and every reset used to
        # live inside the ==1 / ==2 branches. So a count of 3+ was an absorbing
        # state: no branch matched, nothing reset it, no timer got armed, and
        # every later press only incremented further, leaving the button dead
        # until the service restarted. This else makes 3+ recoverable: swallow
        # the extra press and rearm on the next one.
        logger.info(f"Ignoring press {press_count} inside the double-press window")
        press_count = 0

def handle_single_press():
    global press_count
    toggle_light()  # Single press toggles the light
    press_count = 0

def handle_double_press():
    toggle_pump()  # Double press toggles the pump

# Set button event for press detection
button.when_pressed = handle_button_press

# helpers
def _flash_lights_blocking(times=3, delay=0.3):
    original_brightness = light.get_brightness()  # Save the brightness (0–100 scale)
    was_on = original_brightness > 0  # If >0%, we consider it "on"

    logger.info(f"Flashing lights {times} times. Original brightness: {original_brightness}%")

    for _ in range(times):
        light.off()
        sleep(delay)
        light.set_brightness(100)  # Flash full brightness for maximum visibility
        sleep(delay)
    # Restore original state
    if was_on:
        light.set_brightness(original_brightness)
    else:
        light.off()

def flash_lights(times=3, delay=0.3):
    """Run the flash on its own thread.

    The only caller is the low-water abort inside on_message, which paho runs on
    the network thread. Sleeping 2 * times * delay there (1.8s by default) stalls
    all MQTT processing for the duration — including keepalive handling on a link
    that has no headroom to spare.
    """
    threading.Thread(
        target=_flash_lights_blocking, args=(times, delay), daemon=True
    ).start()

def safe_distance_measure():
    """Return a TRUSTWORTHY reservoir distance in cm, or None.

    None means "no usable reading" and must never be treated as a safe one.
    Every caller has to branch on it; this function will not substitute a
    number, and it will not report a reading it cannot vouch for.

    Two rejection paths, and the second is the one that matters:

    - The measurement itself failed, or the device was busy.
    - The measurement succeeded and is IMPLAUSIBLE. gpiozero returns None on a
      no-echo and DistanceSensor ignores Nones, so a disconnected sensor does
      not raise - spurious edges on the floating pin fill the averaging queue
      and the median latches at an arbitrary value that looks like a real
      distance. On this unit that value has been anywhere from 0.09 cm to
      ~83 cm. Only the band separates that from a reading off real water.

    The old recovery branch is gone. It rebuilt the global Distance without
    closing the old one, and gpiozero reserves GPIO 19/26 per pin factory, so
    the constructor could only ever raise GPIOPinInUse - it was guaranteed to
    fail, and unreachable regardless, since gpiozero never raises
    MeasurementError from this path in the first place.
    """
    # measure_once(), not measure(). Reading `.distance` returns the median of
    # gpiozero's own rolling nine-sample queue, so it is already smoothed;
    # measure()'s repeat sampling only adds meaning if it sleeps between reads,
    # and this runs on the paho network thread and on the pigpio callback
    # thread, neither of which may be stalled for the best part of a second.
    samples = distance_sensor.sample_count()
    if samples == 0:
        # Not the same thing as an implausible reading. gpiozero's sampler has
        # produced nothing yet - the process just started, or the sensor has
        # never echoed - and the 0.0 it would hand back is indistinguishable
        # from a full tank. Report "no reading" without claiming to know
        # anything, and let the caller decline to publish a verdict at all.
        logger.info("Distance sampler has no readings yet")
        return None

    try:
        distance = distance_sensor.measure_once()
    except MeasurementError as e:
        logger.warning(f"Distance measure failed: {e}")
        return None

    if not (WATER_VALID_MIN_CM <= distance <= WATER_VALID_MAX_CM):
        logger.warning(
            f"Discarding implausible water reading {distance:.2f}cm "
            f"(outside {WATER_VALID_MIN_CM:.2f}-{WATER_VALID_MAX_CM:.2f}cm) - "
            f"treating as NO reading, not as a safe one"
        )
        return None

    return distance


def publish_water_sensor_status(client, trustworthy):
    """Publish whether the reservoir reading can currently be believed."""
    payload = "online" if trustworthy else "offline"
    client.publish(WATER_STATUS_TOPIC, payload, qos=1, retain=True)

def publish_water_low_mode(client):
    if WATER_LOW_CM not in (None, 0):
        mode = "Enabled"
    else:
        mode = "Disabled"
    logger.info(f"Publishing water low mode: {mode}")
    client.publish(BASE_TOPIC + "/water/low/mode", mode, retain=True)


def _threshold_is_acceptable(value):
    """Whether a proposed low-water threshold may be applied.

    Rejects the two shapes that silently disarm the interlock:

    - Non-finite. float("nan") parses fine, is not in (None, 0) so the mode
      publishes "Enabled", and then `distance > nan` is False for every reading
      - so the alarm never fires AND start_pump() starts the pump every time.
      A fail-OPEN on a safety interlock, reachable by any mosquitto_pub.
    - Outside the plausibility band. A threshold below the band minimum can
      never be exceeded by a valid reading, pinning the alarm on and refusing
      the pump forever; one above the maximum can never be reached, pinning it
      off. Both look like a working configuration.

    Zero is allowed and means "disabled" - an explicit opt-out, surfaced by the
    separate water/low/mode entity.

    Note the isfinite() check is defence in depth rather than the sole barrier:
    every comparison against nan is False, so the band test below rejects
    non-finite values on its own. Deleting isfinite() does not change behaviour
    today - it is kept so the intent survives anyone later loosening the band.
    """
    if not math.isfinite(value):
        return False
    if value == 0:
        return True
    return WATER_VALID_MIN_CM <= value <= WATER_VALID_MAX_CM


def publish_water_low_threshold(client):
    """Publish the threshold currently in effect, so HA and this process agree."""
    # Publish 0 rather than returning when checking is disabled. Returning left
    # the previous number retained, so HA's slider snapped back to it and
    # displayed a threshold the interlock was not using - and the case where
    # that matters most is precisely this one, since a disabled threshold means
    # the pump interlock is bypassed entirely.
    effective = 0.0 if WATER_LOW_CM in (None, 0) else WATER_LOW_CM
    logger.info(f"Publishing water low threshold: {effective:.2f}cm")
    client.publish(BASE_TOPIC + "/water/low/cm", f"{effective:.2f}", retain=True)


_UNSET = object()


def update_water_low_state(client, distance=_UNSET):
    """Evaluate the low-water threshold and publish the binary sensor state.

    Pass `distance` when the caller has already taken a trustworthy reading, so
    one refresh cycle drives one physical measurement rather than two.

    On an untrustworthy reading this publishes NOTHING. It deliberately does not
    fall back to OFF: OFF means "the reservoir is fine", and asserting that on
    the strength of a reading we just rejected is precisely the false all-clear
    this whole change exists to remove. The entity is held unavailable through
    WATER_STATUS_TOPIC instead, so HA shows unknown rather than a stale claim.
    """
    if WATER_LOW_CM in (None, 0):
        # Explicitly opted out. The separate water/low/mode entity surfaces
        # "Disabled", so OFF here is disclosed rather than misleading.
        client.publish(BASE_TOPIC + "/water/low/state", "OFF", retain=True)
        logger.info("Water low checking disabled, setting water low state to OFF")
        return

    if distance is _UNSET:
        distance = safe_distance_measure()

    if distance is None:
        logger.warning(
            "Water low state NOT updated - no trustworthy reading. "
            "Leaving the entity unavailable rather than asserting OFF."
        )
        return

    if distance > WATER_LOW_CM:
        client.publish(BASE_TOPIC + "/water/low/state", "ON", retain=True)
        logger.info(f"Updated water low state to ON (distance {distance:.2f}cm > {WATER_LOW_CM:.2f}cm)")
    else:
        client.publish(BASE_TOPIC + "/water/low/state", "OFF", retain=True)
        logger.info(f"Updated water low state to OFF (distance {distance:.2f}cm <= {WATER_LOW_CM:.2f}cm)")


def refresh_water_state(client):
    """Take one reading and publish every topic derived from it, consistently.

    Single owner of the read -> trust -> level -> threshold sequence, so the
    trust topic, the level and the low-water state can never disagree about
    which measurement they came from.

    Returns the trustworthy distance in cm, or None.
    """
    distance = safe_distance_measure()

    if distance is None:
        publish_water_sensor_status(client, False)
        logger.warning(
            "Reservoir reading is not trustworthy - water entities marked unavailable"
        )
        return None

    logger.info(f"Publishing Water Level: {distance:.2f}cm")
    # Retained, like the other state topics, so HA gets it on subscribe instead
    # of sitting at `unknown`. Safe to retain only because the retained trust
    # topic travels with it - a stale level cannot be read as current while
    # water/status says offline. The water_level discovery payload also carries
    # expire_after, which bounds staleness at HA's end if this process stops
    # refreshing without the trust topic ever flipping.
    client.publish(BASE_TOPIC + "/water/level", f"{distance:.2f}", qos=1, retain=True)
    update_water_low_state(client, distance)
    # Trust is asserted LAST, after the values it vouches for are on the wire.
    # Published first, it would make the entities available for the gap before
    # the new level lands, and what HA would show in that gap is the previous
    # retained value - stale, and looking current.
    publish_water_sensor_status(client, True)
    return distance


def start_pump(target_speed, client):
    """The only path that energises the pump. Fails CLOSED.

    Upstream #83 asked for a safeguard that "should exist regardless of how it's
    called" and was closed without it: the guard lived inside the pump/command
    branch only, so pump/speed/set and the physical button's double-press both
    started the pump with no water check at all. Every caller routes here now.

    Refuses on an untrustworthy reading as well as on a genuinely low one. An
    unknown water level is not a safe water level, and running a hydroponic pump
    dry is the failure this interlock exists to prevent.

    Returns True if the pump was started.
    """
    if WATER_LOW_CM in (None, 0):
        pump.set_speed(target_speed)
        return True

    distance = refresh_water_state(client)

    if distance is None:
        logger.warning("Refusing to start pump: no trustworthy reservoir reading")
        flash_lights()
        return False

    if distance > WATER_LOW_CM:
        logger.warning(
            f"Refusing to start pump: water too low "
            f"({distance:.2f}cm > {WATER_LOW_CM:.2f}cm)"
        )
        flash_lights()
        return False

    pump.set_speed(target_speed)
    return True

# https://www.home-assistant.io/integrations/mqtt/#discovery-messages
#  Note: homeassistant/<component>/[<node_id>/]<object_id>/config.
#  User device_class for auto suggestion on HA card picks
def send_discovery_messages(client):
    device_info = {
        "identifiers": [IDENTIFIER],
        "name": BASE_TOPIC,
        "manufacturer": "gardyn-of-eden",
        "model": MODEL,
        "sw_version": VERSION,
    }

    # Availability / LWT. Every entity follows the retained STATUS_TOPIC, which
    # the broker itself flips to "offline" when this service stops sending
    # keepalives. Without it a dead Pi leaves HA showing the last known values
    # forever, so a lost light command looks identical to a working one.
    availability_config = {
        "availability_topic": STATUS_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
    }

    # Water entities answer to two conditions, not one: the controller must be
    # alive AND the reservoir reading must be believable. HA expresses that as an
    # `availability` LIST with mode "all" — "payload_available must be received
    # on all configured availability topics before the entity is marked online".
    #
    # It has to be a list rather than an extra key, because the MQTT integration
    # docs are explicit that `availability_topic` "must not be used together with
    # `availability`". So these payloads take this dict INSTEAD of
    # availability_config, never merged with it.
    #
    # payload_available / payload_not_available go INSIDE each entry, not
    # alongside. HA reads them from the per-entry dict for the list form and
    # only falls back to the top-level keys for the availability_topic form, so
    # top-level values here would be silently ignored - working today purely
    # because "online"/"offline" happen to be the per-entry defaults. Stating
    # them per entry means changing the shared payloads cannot quietly strand
    # these two entities as permanently unavailable.
    water_availability_config = {
        "availability": [
            {
                "topic": STATUS_TOPIC,
                "payload_available": "online",
                "payload_not_available": "offline",
            },
            {
                "topic": WATER_STATUS_TOPIC,
                "payload_available": "online",
                "payload_not_available": "offline",
            },
        ],
        "availability_mode": "all",
    }

    def publish_config(topic, payload, availability=None):
        client.publish(
            topic,
            json.dumps({**payload, **(availability or availability_config)}),
            retain=True,
        )

    # Config for Light
    TEMP_CONFIG_TOPIC = "homeassistant/light/gardyn/"+IDENTIFIER+"_light/config"
    temp_config_payload = {
        "name": "Light",
        "unique_id": IDENTIFIER + "_light",
        "platform": "mqtt",
        "state_topic": BASE_TOPIC + "/light/state",
        "command_topic": BASE_TOPIC + "/light/command",
        "brightness_state_topic": BASE_TOPIC + "/light/brightness/state",
        "brightness_command_topic": BASE_TOPIC + "/light/brightness/set",
        "brightness_scale": 100,
        # HA's `qos` applies to both what it subscribes at and what it publishes
        # at. QoS 1 is what lets the broker hold a command for this client while
        # it is briefly offline — but only in combination with the durable
        # session set up in __main__; QoS 1 alone only guarantees delivery to the
        # broker, which then has no subscriber to hand it to.
        "qos": 1,
        "device": device_info
    }
    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload)

    #Config for Pump (as a light with speed control, for example)
    # todo: maybe use fan instead....
    TEMP_CONFIG_TOPIC = "homeassistant/light/gardyn/"+IDENTIFIER+"_pump/config"
    temp_config_payload = {
        "name": "Pump",
        "unique_id": IDENTIFIER + "_pump",
        "platform": "mqtt",
        # `device_class: fan` was here; `light` has no device_class, so HA
        # discarded it. Dropped rather than left as a false signal.
        "state_topic": BASE_TOPIC + "/pump/state",
        "command_topic": BASE_TOPIC + "/pump/command",

        "brightness_state_topic": BASE_TOPIC + "/pump/speed/state",
        "brightness_command_topic": BASE_TOPIC + "/pump/speed/set",
        "brightness_scale": 100,

        # if using fan....
	# "percentage_state_topic": BASE_TOPIC + "/pump/speed/state",
	# "percentage_command_topic": BASE_TOPIC + "/pump/speed/set",
	# "speed_range_min": 1,
	# "speed_range_max": 100,
        "icon": "mdi:water-pump",
        "qos": 1,
        "device": device_info
    }
    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload)

    #Config for Temperature from PCB
    TEMP_CONFIG_TOPIC = "homeassistant/sensor/gardyn/"+IDENTIFIER+"_pcb_temp/config"
    temp_config_payload = {
        "name": "PCB Temperature",
        "unique_id": IDENTIFIER + "_pcb_temp",
        "state_topic": BASE_TOPIC + "/pcb/temperature",
        "unit_of_measurement": "°C",
        "device_class": "temperature",
        "device": device_info
    }
    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload)

    #Config for Temperature Sensor
    TEMP_CONFIG_TOPIC = "homeassistant/sensor/gardyn/"+IDENTIFIER+"_temperature/config"
    temp_config_payload = {
        "name": "Temperature",
        "unique_id": IDENTIFIER + "_temperature",
        "state_topic": BASE_TOPIC + "/temperature",
        # `command_topic` was here. `sensor` is read-only, so HA discards it and
        # never publishes to temperature/get — the on_message branch for that
        # topic is reachable only from a manual mosquitto_pub.
        "unit_of_measurement": "°C",
        "device_class": "temperature",
        "device": device_info
    }
    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload)

    #Config for Humidity Sensor
    TEMP_CONFIG_TOPIC = "homeassistant/sensor/gardyn/"+IDENTIFIER+"_humidity/config"
    temp_config_payload = {
        "name": "Humidity",
        "unique_id": IDENTIFIER + "_humidity",
        "state_topic": BASE_TOPIC + "/humidity",
        # `command_topic` dropped — read-only domain, see Temperature above.
        "unit_of_measurement": "%",
        "device_class": "humidity",
        "device": device_info
    }
    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload)


    #Config for Water Level Sensor
    TEMP_CONFIG_TOPIC = "homeassistant/sensor/gardyn/"+IDENTIFIER+"_water_level/config"

    temp_config_payload = {
        "name": "Water Level",
        "unique_id": IDENTIFIER + "_water_level",
        "state_topic": BASE_TOPIC + "/water/level",
        # `command_topic` dropped — read-only domain, see Temperature above.
        "unit_of_measurement": "cm",
        "device_class": "distance",
        # Backstop for the one staleness case the trust topic cannot catch: the
        # process stays healthy and MQTT keeps working while the SENSOR quietly
        # stops changing. gpiozero's queue keeps its last samples indefinitely,
        # so a latched value still passes the plausibility band and still gets
        # vouched for. Slightly over twice the 30-minute publish cadence, so one
        # missed cycle is tolerated and two are not.
        "expire_after": 3900,
        "device": device_info
    }
    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload, water_availability_config)

    # Config for Water Low Binary Sensor
    TEMP_CONFIG_TOPIC = f"homeassistant/binary_sensor/gardyn/{IDENTIFIER}_water_low/config"
    temp_config_payload = {
        "name": "Water Low",
        "unique_id": IDENTIFIER + "_water_low",
        "platform": "mqtt",
        "state_topic": BASE_TOPIC + "/water/low/state",
        "device_class": "problem",
        "payload_on": "ON",
        "payload_off": "OFF",
        "device": device_info
    }
    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload, water_availability_config)

    # Config for Water Low Threshold (current value)
        # Config for Water Low CM Set Number
    TEMP_CONFIG_TOPIC = f"homeassistant/number/gardyn/{IDENTIFIER}_water_low_cm/config"
    temp_config_payload = {
        "name": "Set Water Low Threshold",
        "unique_id": IDENTIFIER + "_water_low_cm",
        "platform": "mqtt",
        "state_topic": BASE_TOPIC + "/water/low/cm",
        "command_topic": BASE_TOPIC + "/water/low/cm/set",
        # Range tracks the plausibility band, not an arbitrary ceiling. It used
        # to stop at 15, which put the whole 15-25 cm span out of reach from HA
        # - and on the PR #90 calibration (full ~10-12 cm, empty ~23 cm) that
        # span is exactly where a threshold separating full from empty belongs.
        # 0 stays reachable as the explicit "disabled" value.
        "min": 0,
        "max": WATER_VALID_MAX_CM,
        "step": 0.5,
        "qos": 1,
        "unit_of_measurement": "cm",
        "device_class": "distance",
        "device": device_info
    }
    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Config for Water Low Mode (Enabled/Disabled)
    TEMP_CONFIG_TOPIC = f"homeassistant/sensor/gardyn/{IDENTIFIER}_water_low_mode/config"
    temp_config_payload = {
        "name": "Water Low Mode",
        "unique_id": IDENTIFIER + "_water_low_mode",
        "platform": "mqtt",
        "state_topic": BASE_TOPIC + "/water/low/mode",
        "icon": "mdi:toggle-switch",  # Optional: or use mdi:alert for dramatic effect
        "device": device_info
    }
    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Discovery configuration for Camera A (image entity)
    TEMP_CONFIG_TOPIC = "homeassistant/image/gardyn/" + IDENTIFIER + "_upper_camera/config"
    temp_config_payload = {
        "name": "Upper Camera",
        "unique_id": IDENTIFIER + "_upper_camera",
        "image_topic": BASE_TOPIC + "/image/upper_camera",
        # publish_images() sends raw JPEG bytes, so decoding must be OFF.
        # "b64" is a valid value for image_encoding, NOT for encoding — setting
        # it here makes HA try bytes.decode("b64") on arrival and raise
        # "unknown encoding: b64", which drops every frame.
        "encoding": "",
        "content_type": "image/jpeg",
        "object_id": IDENTIFIER + "_upper_camera",
        "device": device_info
    }
    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload)

    # Discovery configuration for Camera B (image entity)
    TEMP_CONFIG_TOPIC = "homeassistant/image/gardyn/" + IDENTIFIER + "_lower_camera/config"
    temp_config_payload = {
        "name": "Lower Camera",
        "unique_id": IDENTIFIER + "_lower_camera",
        "image_topic": BASE_TOPIC + "/image/lower_camera",
        # publish_images() sends raw JPEG bytes, so decoding must be OFF.
        # "b64" is a valid value for image_encoding, NOT for encoding — setting
        # it here makes HA try bytes.decode("b64") on arrival and raise
        # "unknown encoding: b64", which drops every frame.
        "encoding": "",
        "content_type": "image/jpeg",
        "object_id": IDENTIFIER + "_lower_camera",
        "device": device_info
    }
    publish_config(TEMP_CONFIG_TOPIC, temp_config_payload)

def on_connect(client, userdata, flags, rc, properties=None):
    logger.warning(f"Connected with result code {rc}")
    # Explicit topic list, not BASE_TOPIC + "/#" — see COMMAND_SUBSCRIPTIONS.
    client.subscribe(COMMAND_SUBSCRIPTIONS)
    # Retract the water trust topic BEFORE announcing the device online.
    #
    # gardyn/water/status is retained and has no will attached, so a reading
    # this process vouched for before it died is still sitting on the broker
    # saying "trustworthy" - along with the retained level it vouched for. Come
    # back up and announce gardyn/status online first, and both availability
    # topics read online, so HA marks the water entities available and displays
    # that old level as a current reading until the fresh one lands. Retracting
    # first means trust has to be re-earned by an actual measurement.
    publish_water_sensor_status(client, False)
    # Clear the retained "offline" the broker may have left from the last death.
    # Publish before discovery so HA never sees an entity announced unavailable.
    client.publish(STATUS_TOPIC, "online", qos=1, retain=True)
    send_discovery_messages(client)
    publish_water_low_mode(client)
    # Publish the threshold this process is actually running with. It used to be
    # published only from the water/low/cm/set handler, so a value set at runtime
    # survived in the retained topic while the service reloaded WATER_LOW_CM from
    # .env on restart — HA showed one number and the interlock used another.
    publish_water_low_threshold(client)
    # Announce real device state on every (re)connect. Retained, so HA gets it
    # immediately on subscribe instead of sitting at `unknown`.
    publish_light_state(client)
    publish_pump_state(client)
    # Re-read the reservoir on every (re)connect. The publish loop only fires
    # every 30 minutes and its thread survives a reconnect behind the once-only
    # guard in start_publisher_threads(), so without this a reconnect would leave
    # the water entities sitting on whatever the trust topic last said - for up
    # to half an hour, and across a restart, forever.
    # Guarded, and it must stay guarded: start_publisher_threads() is the next
    # line, so anything escaping here would leave the temperature, humidity,
    # PCB, camera and water loops permanently unstarted while gardyn/status sits
    # at "online" and the device looks perfectly healthy.
    try:
        refresh_water_state(client)
    except Exception as e:
        logger.exception(f"Failed to refresh water state on connect: {e}")
    start_publisher_threads(client)

def on_message(client, userdata, msg):
    global brightness, speed, WATER_LOW_CM

    try:
        payload = msg.payload.decode("utf-8").strip()
        logger.debug(f"Decoded payload on {msg.topic}: '{payload}'")
    except UnicodeDecodeError:
        logger.error(f"Failed to decode message on topic {msg.topic}. Likely binary.")
        return

    topic_suffix = msg.topic.replace(BASE_TOPIC + "/", "")

    try:
        # === Pump Logic ===
        if topic_suffix == "pump/command":
            if payload.upper() == "ON":
                # The interlock now lives in start_pump(), which also publishes
                # the water topics from the same reading it decided on.
                start_pump(speed, client)
                publish_pump_state(client)
            elif payload.upper() == "OFF":
                pump.off()
                publish_pump_state(client)

        elif topic_suffix == "pump/speed/set" and payload.isdigit():
            requested = int(payload)
            if requested == 0:
                # Stopping is never gated on a water reading.
                speed = requested
                pump.off()
            elif pump.get_speed() > 0:
                # Already running: this is a speed change, not a start, so it
                # must not be refused. Refusing it meant that with water low the
                # only reachable speed was 0 - the user could not turn the pump
                # DOWN, which is the wrong direction for a safety interlock.
                speed = requested
                pump.set_speed(speed)
            else:
                # A start. This path used to call pump.set_speed()
                # unconditionally, so setting a speed would start a stopped pump
                # with no water check whatsoever.
                if start_pump(requested, client):
                    speed = requested
                # On refusal `speed` is deliberately left alone: adopting it
                # would leave the global disagreeing with what HA was just told,
                # and a later bare `pump/command ON` would start at a speed the
                # user never saw take effect.
            publish_pump_state(client)

        # === Light Logic ===
        elif topic_suffix == "light/command":
            if payload.upper() == "ON":
                light.set_duty_cycle(brightness)
                publish_light_state(client)
            elif payload.upper() == "OFF":
                light.off()
                publish_light_state(client)

        elif topic_suffix == "light/brightness/set" and payload.isdigit():
            brightness = int(payload)
            light.set_duty_cycle(brightness)
            publish_light_state(client)

        # === Water Level ===
        elif topic_suffix == "water/level/get":
            refresh_water_state(client)

        elif topic_suffix == "water/low/cm/set":
            try:
                candidate = float(payload)
            except ValueError:
                logger.error(f"Invalid water low cm value: {payload}")
            else:
                if not _threshold_is_acceptable(candidate):
                    logger.error(
                        f"Rejecting water low threshold {payload!r} - "
                        f"must be 0 (disabled) or within "
                        f"{WATER_VALID_MIN_CM:.2f}-{WATER_VALID_MAX_CM:.2f}cm"
                    )
                    # Re-assert what we are actually running with, so HA's
                    # number entity does not sit showing a value we rejected.
                    publish_water_low_threshold(client)
                else:
                    WATER_LOW_CM = candidate or None
                    publish_water_low_threshold(client)
                    publish_water_low_mode(client)
                    refresh_water_state(client)

        # === Sensor Data on Request ===
        elif topic_suffix == "pcb/temperature/get":
            pcb_temp = get_pcb_temperature()
            client.publish(BASE_TOPIC + "/pcb/temperature", f"{pcb_temp:.2f}")

        elif topic_suffix == "temperature/get":
            temperature = temperature_sensor.read()
            client.publish(BASE_TOPIC + "/temperature", f"{temperature:.2f}")

        elif topic_suffix == "humidity/get":
            humidity = humidity_sensor.read()
            client.publish(BASE_TOPIC + "/humidity", f"{humidity:.2f}")

    except Exception as e:
        logger.exception(f"Error handling message on topic {msg.topic}: {e}")

def publish_pcb_temperature(client):
    while True:
        try:
            pcb_temp = get_pcb_temperature()
            logger.info(f"Publishing PCB Temperature: {pcb_temp:.2f}°C")
            client.publish(BASE_TOPIC + "/pcb/temperature", f"{pcb_temp:.2f}")
        except Exception as e:
            logger.error(f"Failed to read or publish PCB temperature: {e}")
        sleep(30*60)  # Publish frequency, every x seconds

def publish_temperature(client):
    while True:
        try:
            temperature = temperature_sensor.read()
            logger.info(f"Publishing Temperature: {temperature:.2f}°C")
            client.publish(BASE_TOPIC + "/temperature", f"{temperature:.2f}")
        except Exception as e:
            logger.error(f"Failed to read or publish ambient temperature: {e}")
        sleep(30*60)  # Publish frequency, every x seconds

def publish_humidity(client):
    while True:
        try:
            humidity = humidity_sensor.read()
            logger.info(f"Publishing Humidity: {humidity:.2f}%")
            client.publish(BASE_TOPIC + "/humidity", f"{humidity:.2f}")
        except Exception as e:
            logger.error(f"Failed to read or publish ambient humidity: {e}")
        sleep(30*60)  # Publish frequency, every x seconds

def publish_water_level(client):
    while True:
        try:
            # refresh_water_state() also re-evaluates the low-water threshold.
            # It used to publish only the level, so update_water_low_state() had
            # exactly one caller - the water/low/cm/set handler - and the binary
            # sensor reflected whatever the last threshold edit happened to see,
            # sometimes days earlier. That is upstream issue #86.
            refresh_water_state(client)
        except Exception as e:
            logger.exception(f"Failed to refresh water state: {e}")
        sleep(30 * 60)

def _capture_and_publish(client, label, device, resolution, image_path, topic):
    """Capture one camera and publish it independently.

    Each camera gets its own try/except so a failing camera (e.g. the lower
    camera's intermittent USB error-32) never blocks the other's publish, and
    its own resolution so the healthy upper camera can run at a higher setting
    than the flaky lower one.
    """
    try:
        subprocess.check_call([
            'fswebcam', '-d', device, '-r', resolution,
            '-S', '2', '-F', '2', '--no-banner', image_path
        ])
        with open(image_path, 'rb') as f:
            client.publish(BASE_TOPIC + topic, payload=f.read(), qos=0, retain=False)
        logger.info(f"Captured+published {label} camera ({device}) @ {resolution}")
    except subprocess.CalledProcessError as e:
        logger.error(f"{label} camera capture failed ({device}): {e}")
    except Exception:
        logger.exception(f"Unexpected error during {label} image capture/publish")


def publish_images(client):
    while True:
        _capture_and_publish(client, "upper", UPPER_CAMERA_DEVICE,
                             UPPER_CAMERA_RESOLUTION, UPPER_IMAGE_PATH, "/image/upper_camera")
        _capture_and_publish(client, "lower", LOWER_CAMERA_DEVICE,
                             LOWER_CAMERA_RESOLUTION, LOWER_IMAGE_PATH, "/image/lower_camera")
        sleep(IMAGE_INTERVAL_SECONDS)


_publisher_threads_started = False
_publisher_threads_lock = threading.Lock()

def start_publisher_threads(client):
    """Start the periodic publishers, once, after the first successful connect.

    They used to start in __main__ ahead of a blocking connect(), which was safe
    only because the socket already existed by then. Under connect_async the
    connection is established by loop_forever(), so a thread's first publish
    would race it and be dropped with MQTT_ERR_NO_CONN — and these loops sleep 30
    minutes, so a lost first publish means an entity sits `unknown` for half an
    hour. on_connect fires on every reconnect, hence the once-only guard.
    """
    global _publisher_threads_started
    with _publisher_threads_lock:
        if _publisher_threads_started:
            return
        _publisher_threads_started = True

    for target in (publish_pcb_temperature, publish_temperature, publish_humidity,
                   publish_water_level, publish_images):
        threading.Thread(target=target, args=(client,), daemon=True).start()
    logger.warning("Publisher threads started")


def on_disconnect(client, userdata, flags, rc, properties=None):
    """Make a drop visible. Nothing logged one before, on either side."""
    if rc == 0:
        logger.warning("Disconnected from broker cleanly")
    else:
        logger.error(f"Unexpectedly disconnected from broker (rc={rc}); paho will retry")


if __name__ == "__main__":
    logger.warning(f"Connecting to {BROKER} on port {PORT} with keep alive {KEEP_ALIVE_INTERVAL}")
    # A durable session (clean_session=False) is what makes a QoS 1 command
    # survive a brief drop: the broker keeps this client's subscriptions and
    # queues QoS 1+ messages for it while it is away, instead of discarding them
    # silently. It requires a STABLE client_id — paho generates a random one
    # otherwise, which would orphan a fresh session on every restart, and it
    # refuses clean_session=False with an empty id.
    #
    # This is only safe because COMMAND_SUBSCRIPTIONS no longer contains the
    # image topics. Under the old "/#" wildcard a durable session would have had
    # the broker queueing megabytes of JPEG for every minute of downtime.
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=IDENTIFIER,
        clean_session=False,
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect
    client.username_pw_set(USERNAME, PASSWORD)
    # Register the will BEFORE connecting — the broker records it at CONNECT
    # time, so a will_set() after connect() would never take effect.
    client.will_set(STATUS_TOPIC, "offline", qos=1, retain=True)
    # Back off on repeated failures instead of hammering a broker that is down.
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    # connect_async + retry_first_connection is the pair that survives a broker
    # that is not up yet. A bare connect() raises when the broker is unreachable,
    # the exception propagates out of __main__, and the process exits — which,
    # against systemd's default start-limit, burns through the restart budget in
    # seconds and parks the unit in `failed` permanently. A router reboot that
    # brought this Pi up before the broker used to leave the garden dark.
    client.connect_async(BROKER, PORT, KEEP_ALIVE_INTERVAL)

    # The periodic publishers are started from on_connect, not here — see
    # start_publisher_threads().
    client.loop_forever(retry_first_connection=True)
