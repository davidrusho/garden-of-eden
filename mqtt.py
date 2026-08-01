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
from config import USERNAME, PASSWORD, BROKER, PORT, KEEP_ALIVE_INTERVAL, BASE_TOPIC, IDENTIFIER, MODEL, VERSION, WATER_LOW_CM, WATER_VALID_MIN_CM, WATER_VALID_MAX_CM, UPPER_CAMERA_DEVICE, LOWER_CAMERA_DEVICE, UPPER_IMAGE_PATH, LOWER_IMAGE_PATH, CAMERA_RESOLUTION, UPPER_CAMERA_RESOLUTION, LOWER_CAMERA_RESOLUTION, UPPER_CAMERA_JPEG_QUALITY, LOWER_CAMERA_JPEG_QUALITY, IMAGE_INTERVAL_SECONDS

from gpiozero import Button  # Import gpiozero Button
from gpiozero.pins.pigpio import PiGPIOFactory

from app.sensors.light.light import Light
from app.sensors.pump.pump import Pump
from app.sensors.pcb_temp.pcb_temp import get_pcb_temperature
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
# app/sensors/light/light.py) so that policy travels with the module rather than
# depending on this file having run. The root stays at WARNING above; both
# module levels are asserted by tests/test_water_interlock.py, which already
# imports this file under stubs — so "mqtt.py is untestable" is NOT a reason to
# leave a logging policy uncovered here.
logger = logging.getLogger(__name__)

# Retained availability topic backing every entity's availability_config.
# Published "online" on connect; the broker publishes "offline" from the LWT
# when this client stops answering keepalives (~1.5x KEEP_ALIVE_INTERVAL).
STATUS_TOPIC = BASE_TOPIC + "/status"

# Retired hardware (T-475).
#
# Seven entities are permanently non-functional by HARDWARE fact, not by
# software gap, and are withdrawn from Home Assistant rather than left to sit
# `unavailable` forever:
#
#   pump                  the Gardyn's own pump was physically replaced by a
#                         third-party unit on a smart plug that is not on the
#                         network; the GPIO header drives nothing.
#   water_level           the fitted DYP-A01A has a 28 cm datasheet dead zone
#   water_low             ("objects closer than 28cm range as 28cm") against a
#   water_low_cm          shallow reservoir, and the whole 3-25 cm plausibility
#   water_low_mode        band sits inside it. A perfectly healthy unit would
#                         report a constant 28 cm, be discarded as implausible,
#                         and leave these unavailable forever. No replacement
#                         sensor is coming.
#   temperature           no AHTx0 at 0x38 and no AM2320 at 0x5C on either I2C
#   humidity              bus (T-299); ambient is covered by Zigbee (T-300).
#
# The INTERLOCK IS NOT RETIRED. safe_distance_measure(), the plausibility band,
# _threshold_is_acceptable() and start_pump()'s fail-closed refusal all survive
# unchanged - what goes away is their MQTT surface, not their decisions. A pump
# that refused to start on an untrustworthy reading still refuses.
#
# Discovery configs are retained by publish_config(), so deleting the blocks
# that emit them is not enough on its own: the broker keeps serving the last one
# it saw. An empty retained payload is how MQTT deletes a retained message and
# how HA removes a discovered entity, which is what clear_retired_entities()
# below publishes on every connect.
RETIRED_DISCOVERY_TOPICS = [
    f"homeassistant/light/gardyn/{IDENTIFIER}_pump/config",
    f"homeassistant/sensor/gardyn/{IDENTIFIER}_water_level/config",
    f"homeassistant/binary_sensor/gardyn/{IDENTIFIER}_water_low/config",
    f"homeassistant/number/gardyn/{IDENTIFIER}_water_low_cm/config",
    f"homeassistant/sensor/gardyn/{IDENTIFIER}_water_low_mode/config",
    f"homeassistant/sensor/gardyn/{IDENTIFIER}_temperature/config",
    f"homeassistant/sensor/gardyn/{IDENTIFIER}_humidity/config",
]

# The state topics those entities were fed from, and the reason clearing
# discovery alone is not enough.
#
# Every topic here was published with retain=True, so each still has a live
# retained message on the broker naming a value HA would replay to any new
# subscriber. Removing only the discovery blocks would leave seven retained
# payloads behind describing hardware that does not exist.
#
# gardyn/temperature and gardyn/humidity are deliberately ABSENT: their
# publishers never passed retain=True (paho defaults to False), so there is no
# retained message to delete - only their discovery configs need clearing.
# Confirmed against the source and against the full history of mqtt.py.
RETIRED_STATE_TOPICS = [
    BASE_TOPIC + "/pump/state",
    BASE_TOPIC + "/pump/speed/state",
    BASE_TOPIC + "/water/level",
    BASE_TOPIC + "/water/low/state",
    BASE_TOPIC + "/water/low/cm",
    BASE_TOPIC + "/water/low/mode",
    # The reservoir trust topic. It backed the water entities' availability
    # list; with those entities gone nothing subscribes to it, and it is
    # retained with no will attached, so left alone it would sit on the broker
    # asserting a verdict about a sensor nobody reads.
    BASE_TOPIC + "/water/status",
]

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
#
# The pump and water command topics stay subscribed even though T-475 withdrew
# their HA entities, and that is a deliberate call rather than an oversight:
#
#   pump/command, pump/speed/set   the only remaining way to exercise the pump
#                                  GPIO and, with it, start_pump()'s low-water
#                                  interlock. Dropping them would leave the
#                                  interlock reachable solely from the physical
#                                  button's double-press.
#   water/low/cm/set               the only way to change the threshold the
#                                  interlock compares against without editing
#                                  .env and restarting, and the only caller of
#                                  _threshold_is_acceptable(). Dropping it
#                                  would make that validation dead code.
#   water/level/get                a read-only probe of the ultrasonic that does
#                                  not energise anything. Its answer now goes to
#                                  the log rather than to a topic.
#
# temperature/get and humidity/get are GONE. temperature_sensor and
# humidity_sensor are None on this unit, so those handlers could only ever raise
# AttributeError into on_message's catch-all - a subscription whose sole effect
# was a logged traceback.
COMMAND_SUBSCRIPTIONS = [
    (BASE_TOPIC + "/light/command", 1),
    (BASE_TOPIC + "/light/brightness/set", 1),
    (BASE_TOPIC + "/pump/command", 1),
    (BASE_TOPIC + "/pump/speed/set", 1),
    (BASE_TOPIC + "/water/low/cm/set", 1),
    (BASE_TOPIC + "/water/level/get", 0),
    (BASE_TOPIC + "/pcb/temperature/get", 0),
]

# INFO, so a command can be ATTRIBUTED. The previous line here set WARNING
# under a comment claiming it set INFO - the code and its comment said opposite
# things, and the code won: every command-attribution site below (the button
# toggles at :148/:151, the pump toggles, the low-water abort flash at :209) and
# the inbound decode in on_message were discarded before reaching a handler.
#
# That is what made 2026-07-31 20:08 unanswerable. The grow light asserted 50%
# an hour after its scheduled off - real, the Zigbee plug metered 55W for 28s -
# and nothing recorded either the command or where it came from. Recording the
# light's own action (see app/sensors/light/light.py) says WHAT happened; this
# says WHO asked for it, which is the half that identifies a replayed queued
# message, a button press and an HA automation as different causes.
#
# The periodic publishers below are demoted to debug in exchange, so this is a
# trade rather than a blanket INFO: the camera pair alone publishes ~576 lines a
# day and would bury four meaningful light events. Same trade light.py makes,
# for the same signal-to-noise reason - not for disk, which is a non-issue
# (~26 KB/day against 25 GB free, journald capped at 64M).
logger.setLevel(logging.INFO)

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
# Both light state topics are retained. Without retain, HA has nothing to
# subscribe to on restart and the entity sits at `unknown` until something
# changes it — which is why a service restart left HA showing a stale "on" for a
# light that PWMLED had just driven to 0. Retained state means HA gets the truth
# the moment it subscribes.
#
# The pump pair used to live here and is gone (T-475). Its retained values are
# actively withdrawn by clear_retired_entities() rather than merely no longer
# written: a retained message outlives the code that wrote it.
def publish_light_state(client):
    """Publish the light's ACTUAL duty cycle, not a shadow variable."""
    duty = light.get_brightness()
    client.publish(BASE_TOPIC + "/light/state", "ON" if duty > 0 else "OFF", retain=True)
    client.publish(BASE_TOPIC + "/light/brightness/state", str(int(round(duty))), retain=True)


def clear_retired_entities(client):
    """Withdraw the entities T-475 retired, and the state behind them.

    An EMPTY payload published with retain=True is how MQTT deletes a retained
    message, and how HA's discovery removes an entity it previously created. So
    this both un-announces the entity and takes the value it was showing off the
    broker.

    Idempotent by construction: clearing an already-clear topic is a no-op, so
    this runs on every connect rather than needing a one-shot migration. That
    matters because send_discovery_messages() also runs on every connect and
    this client reconnects roughly 25 times a day - a manual mosquitto_pub sweep
    would be undone within seconds, whereas doing it here is ordered correctly
    against discovery and survives any future rebuild of the Pi.

    MUST be called BEFORE send_discovery_messages(). Ordering is what makes it
    safe to clear by prefix-adjacent topics: nothing here overlaps a surviving
    entity, but running it afterwards would still be a coin-flip against
    whatever HA happened to process first.
    """
    for topic in RETIRED_DISCOVERY_TOPICS + RETIRED_STATE_TOPICS:
        # retain=True is load-bearing and is the whole point. A clear published
        # WITHOUT it deletes nothing: the broker forwards the empty payload to
        # current subscribers and keeps serving the old retained message to
        # every later one, so HA re-creates the entity on its next restart and
        # the clear looks like it worked.
        client.publish(topic, "", retain=True)
    logger.warning(
        f"Cleared {len(RETIRED_DISCOVERY_TOPICS)} retired discovery configs "
        f"and {len(RETIRED_STATE_TOPICS)} retained state topics"
    )


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
        logger.debug("Distance sampler has no readings yet")
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


# publish_water_sensor_status() and publish_water_low_mode() stood here and are
# gone with the entities they fed (T-475). Both were pure publishers - a
# trustworthiness verdict and an Enabled/Disabled string on the wire - so
# removing them took no decision with them. The trustworthiness EVALUATION they
# reported on is safe_distance_measure() above, which is untouched, and the
# enabled/disabled test is `WATER_LOW_CM in (None, 0)`, which start_pump() makes
# for itself.


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

    Zero is allowed and means "disabled" - an explicit opt-out. It used to be
    surfaced by a separate water/low/mode entity; with that entity retired
    (T-475) the opt-out is visible only in the log, but the VALIDATION below is
    unchanged, and it is the validation that keeps the interlock armed.

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


# publish_water_low_threshold(), update_water_low_state() and
# refresh_water_state() stood here and are gone (T-475).
#
# All three existed to drive entities that no longer exist, and none of them
# owned a decision that start_pump() does not make for itself:
#
#   publish_water_low_threshold   echoed WATER_LOW_CM to HA's number entity.
#                                 Pure output. The value it echoed is still
#                                 what the interlock compares against.
#   update_water_low_state        published ON/OFF from `distance > WATER_LOW_CM`
#                                 and from `WATER_LOW_CM in (None, 0)`. Both
#                                 tests are made verbatim in start_pump() below,
#                                 so the comparison survives; only the binary
#                                 sensor it fed is gone.
#   refresh_water_state           measured, published trust/level/low-state, and
#                                 returned safe_distance_measure()'s value
#                                 unmodified. start_pump() was the only caller
#                                 whose behaviour depended on that return, and
#                                 it now calls safe_distance_measure() directly
#                                 - an exact substitution, since refresh never
#                                 altered the number or the None.


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

    T-475 retired every MQTT entity this function used to publish through, and
    changed NOTHING about what it decides. The only edit is the line that
    obtains the reading: refresh_water_state() measured and then published, and
    returned safe_distance_measure()'s result unchanged, so calling
    safe_distance_measure() directly is an exact substitution. `client` is now
    unused and is kept anyway - the signature is what every caller and every
    interlock test already speaks, and churning it would obscure the one thing
    this change has to prove, which is that the refusals are untouched.
    """
    if WATER_LOW_CM in (None, 0):
        pump.set_speed(target_speed)
        return True

    distance = safe_distance_measure()

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

    # A two-topic `availability` LIST stood here, holding the water entities to
    # both the controller's liveness and the reservoir reading's
    # trustworthiness. It went with those entities (T-475), and with it the
    # `availability` override parameter on publish_config() — every surviving
    # entity answers to STATUS_TOPIC alone. Anything reintroducing a compound
    # availability must re-read the MQTT integration docs first: the list form
    # and `availability_topic` "must not be used together", and the per-entry
    # payload keys do not fall back to the top-level ones.

    def publish_config(topic, payload):
        client.publish(
            topic,
            json.dumps({**payload, **availability_config}),
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

    # The Pump discovery block stood here (T-475). Retiring it is what removes
    # light.gardyn_pump from HA; RETIRED_DISCOVERY_TOPICS is what removes the
    # copy the broker retained.

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

    # The Temperature, Humidity, Water Level, Water Low, Set Water Low Threshold
    # and Water Low Mode discovery blocks stood here (T-475).
    #
    # Temperature and Humidity announced sensors with no silicon behind them:
    # there is no AHTx0 at 0x38 and no AM2320 at 0x5C on either I2C bus (T-299),
    # so temperature_sensor and humidity_sensor were None and every publish
    # raised. Ambient is covered by a Zigbee sensor instead (T-300).
    #
    # The four water entities were unrecoverable for a subtler reason worth
    # keeping written down, because the code looked healthy: the fitted
    # DYP-A01A has a 28 cm dead zone, its datasheet is explicit that "objects
    # closer than 28cm range as 28cm", and the whole 3-25 cm plausibility band
    # lies inside it. A working sensor would report a constant 28 cm,
    # safe_distance_measure() would correctly discard it as implausible, and the
    # entities would sit unavailable forever. The band is not the bug and must
    # not be widened to "fix" this - 28 cm against a shallow reservoir carries
    # no information about the water at all.

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
    # FIRST, and before anything is announced. Retiring an entity is a delete,
    # and a delete that arrives after the announcement races it: HA would see
    # the surviving device's discovery and the retired entities' clears in
    # whatever order it happened to process them. Clearing first means the only
    # discovery HA can act on is the current one.
    #
    # This runs on EVERY connect rather than as a one-shot migration, which is
    # the point. send_discovery_messages() also runs on every connect and this
    # client reconnects roughly 25 times a day, so a manual sweep against the
    # broker would be undone within seconds by the next reconnect.
    clear_retired_entities(client)
    # Clear the retained "offline" the broker may have left from the last death.
    # Publish before discovery so HA never sees an entity announced unavailable.
    client.publish(STATUS_TOPIC, "online", qos=1, retain=True)
    send_discovery_messages(client)
    # Announce real device state on every (re)connect. Retained, so HA gets it
    # immediately on subscribe instead of sitting at `unknown`.
    publish_light_state(client)
    # A water-state refresh stood here, wrapped in a try/except so a sensor
    # explosion could not leave the publisher threads unstarted (T-475 removed
    # it with the entities it refreshed). Nothing between the subscribe and the
    # line below touches hardware any more, so there is no longer a call to
    # guard — but note the hazard if one is ever added back: start_publisher_
    # threads() is the last statement, and an exception escaping above it leaves
    # the PCB and camera loops permanently unstarted while gardyn/status sits at
    # "online" and the device looks perfectly healthy.
    start_publisher_threads(client)

def on_message(client, userdata, msg):
    global brightness, speed, WATER_LOW_CM

    try:
        payload = msg.payload.decode("utf-8").strip()
        logger.info(f"Decoded payload on {msg.topic}: '{payload}'")
    except UnicodeDecodeError:
        logger.error(f"Failed to decode message on topic {msg.topic}. Likely binary.")
        return

    topic_suffix = msg.topic.replace(BASE_TOPIC + "/", "")

    try:
        # === Pump Logic ===
        if topic_suffix == "pump/command":
            if payload.upper() == "ON":
                # The interlock lives in start_pump(). The pump entity is
                # retired (T-475) so there is no state to publish back, but the
                # command path itself is kept: it is how the interlock is
                # exercised, and it is still gated by it.
                start_pump(speed, client)
            elif payload.upper() == "OFF":
                pump.off()

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
            # Read-only probe of the ultrasonic. Nothing subscribes to a water
            # topic any more (T-475), so the answer goes to the log - which is
            # the only place it can go, and is enough for the one question this
            # is still good for: what does the sensor currently say.
            distance = safe_distance_measure()
            if distance is None:
                logger.info("Reservoir probe: no trustworthy reading")
            else:
                logger.info(f"Reservoir probe: {distance:.2f}cm")

        elif topic_suffix == "water/low/cm/set":
            try:
                candidate = float(payload)
            except ValueError:
                logger.error(f"Invalid water low cm value: {payload}")
            else:
                # The validation is UNCHANGED and stays load-bearing: this is
                # the runtime path by which the pump interlock's threshold can
                # be moved, and _threshold_is_acceptable() is what stops a nan
                # or an out-of-band value silently disarming it. Only the
                # echo-back to HA's number entity is gone with the entity.
                if not _threshold_is_acceptable(candidate):
                    logger.error(
                        f"Rejecting water low threshold {payload!r} - "
                        f"must be 0 (disabled) or within "
                        f"{WATER_VALID_MIN_CM:.2f}-{WATER_VALID_MAX_CM:.2f}cm"
                    )
                else:
                    WATER_LOW_CM = candidate or None
                    logger.info(
                        f"Water low threshold now "
                        f"{'disabled' if WATER_LOW_CM is None else f'{WATER_LOW_CM:.2f}cm'}"
                    )

        # === Sensor Data on Request ===
        elif topic_suffix == "pcb/temperature/get":
            pcb_temp = get_pcb_temperature()
            client.publish(BASE_TOPIC + "/pcb/temperature", f"{pcb_temp:.2f}")

        # temperature/get and humidity/get stood here (T-475). Both read a
        # sensor object that is None on this unit, so both could only raise
        # AttributeError into the catch-all below.

    except Exception as e:
        logger.exception(f"Error handling message on topic {msg.topic}: {e}")

def publish_pcb_temperature(client):
    while True:
        try:
            pcb_temp = get_pcb_temperature()
            logger.debug(f"Publishing PCB Temperature: {pcb_temp:.2f}°C")
            client.publish(BASE_TOPIC + "/pcb/temperature", f"{pcb_temp:.2f}")
        except Exception as e:
            logger.error(f"Failed to read or publish PCB temperature: {e}")
        sleep(30*60)  # Publish frequency, every x seconds

# publish_temperature(), publish_humidity() and publish_water_level() stood here
# and are gone (T-475). Each was a 30-minute loop feeding a retired entity:
#
#   temperature/humidity   read a None sensor, so every cycle logged
#                          "'NoneType' object has no attribute 'read'" and
#                          published nothing. This is the half of the change
#                          that matters most for the water topics: leaving a
#                          publisher running would have re-populated the
#                          retained topics that clear_retired_entities() just
#                          cleared, on the next cycle, silently.
#   water level            re-measured and republished the reservoir. The
#                          measurement path itself (safe_distance_measure) is
#                          untouched and is still called by start_pump() and by
#                          the water/level/get probe.


def _capture_and_publish(client, label, device, resolution, quality, image_path, topic):
    """Capture one camera and publish it independently.

    Each camera gets its own try/except so a failing camera (e.g. the lower
    camera's intermittent USB error-32) never blocks the other's publish, and
    its own resolution and quality so the healthy upper camera can run at a
    different setting from the flaky lower one.

    --jpeg is passed EXPLICITLY and is not optional (T-478). Omitting it does
    not select a sane default - it leaves gd's quality parameter unset, and the
    frames this unit produced carried `quality = 255` in their own JPEG
    comment. That cost ~748 KB per five-minute cycle against ~169 KB at 85, on
    a host whose only sustained TX load this is. Passing it as an argument
    rather than reading a module global mirrors `resolution` and is what lets
    the two cameras differ.
    """
    try:
        subprocess.check_call([
            'fswebcam', '-d', device, '-r', resolution, '--jpeg', str(quality),
            '-S', '2', '-F', '2', '--no-banner', image_path
        ])
        with open(image_path, 'rb') as f:
            client.publish(BASE_TOPIC + topic, payload=f.read(), qos=0, retain=False)
        logger.debug(f"Captured+published {label} camera ({device}) @ {resolution}")
    except subprocess.CalledProcessError as e:
        logger.error(f"{label} camera capture failed ({device}): {e}")
    except Exception:
        logger.exception(f"Unexpected error during {label} image capture/publish")


def publish_images(client):
    while True:
        _capture_and_publish(client, "upper", UPPER_CAMERA_DEVICE,
                             UPPER_CAMERA_RESOLUTION, UPPER_CAMERA_JPEG_QUALITY,
                             UPPER_IMAGE_PATH, "/image/upper_camera")
        _capture_and_publish(client, "lower", LOWER_CAMERA_DEVICE,
                             LOWER_CAMERA_RESOLUTION, LOWER_CAMERA_JPEG_QUALITY,
                             LOWER_IMAGE_PATH, "/image/lower_camera")
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

    for target in (publish_pcb_temperature, publish_images):
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
