# mqtt.py
#
# Reviewed: 2026-08-12 against 6de8a77 (T-527.12, second round) - eight more.
#           The first round's own headline finding was REFUTED here: it said a
#           durable session still holds the pre-9e00c2f `gardyn/#` wildcard, and
#           9e00c2f is the commit that INTRODUCED the durable session, in the
#           same change that removed the wildcard. No unit ever had both. I had
#           already written that mechanism into two comments, a commit message
#           and this header. A review finding is a claim; I verified the defect
#           and not the mechanism.
# Reviewed: 2026-08-12 against 251992a (T-527.12) - eight findings. The one that
#           mattered was a pre-existing process-killer: on_message read
#           `msg.topic` inside its own UnicodeDecodeError handler, and the
#           attribute re-decodes on every access, so a topic that is not valid
#           UTF-8 re-raised out of loop_forever() into a Restart=always loop
#           with the light off. Six of the eight were comments, two of which
#           went false in the commit that wrote them.
#           BOTH REMEDIATIONS POSTDATE THEIR REVIEW; the second round's is the
#           commit after 6de8a77 and is itself unreviewed.
# Reviewed: 2026-08-07 against c1549f8 (T-527.1) - three rounds; each found a
#           confident comment that was wrong, and the third found one in the
#           fix for the second. The code was never the weak part.
# Reviewed: 2026-08-01 against 3181aac (T-475, T-478)
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
from time import monotonic, sleep
from config import USERNAME, PASSWORD, BROKER, PORT, KEEP_ALIVE_INTERVAL, BASE_TOPIC, IDENTIFIER, MODEL, VERSION, WATER_LOW_CM, WATER_VALID_MIN_CM, WATER_VALID_MAX_CM, UPPER_CAMERA_DEVICE, LOWER_CAMERA_DEVICE, UPPER_IMAGE_PATH, LOWER_IMAGE_PATH, CAMERA_RESOLUTION, UPPER_CAMERA_RESOLUTION, LOWER_CAMERA_RESOLUTION, UPPER_CAMERA_JPEG_QUALITY, LOWER_CAMERA_JPEG_QUALITY, IMAGE_INTERVAL_SECONDS

from gpiozero import Button  # Import gpiozero Button
from gpiozero.pins.pigpio import PiGPIOFactory

from light_scheduler import LightScheduler
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

# Which writer currently owns the lamp: "schedule", "override", "hold_unsynced"
# or "fallback_unsynced" (the SOURCE_* constants in light_schedule.py). Retained
# and published by the scheduler, never by a handler — see
# publish_light_decision().
LIGHT_SOURCE_TOPIC = BASE_TOPIC + "/light/source"

# Home Assistant's OWN lifecycle topic — the opposite direction to STATUS_TOPIC
# above. That one is this device telling HA it is alive; this one is HA telling
# the world that HA is alive, and it is the trigger for re-announcing discovery.
#
# The MQTT integration docs are explicit that this is the device's job:
#
#   "By default, Home Assistant sends `online` and `offline` to
#    `homeassistant/status`."
#   "A device or service that exposes the MQTT discovery should subscribe to
#    the Birth message and use this as a trigger to send the discovery
#    payload."
#
# This client did neither until T-527.1, and that is the root cause of the
# 2026-08-05 outage's PERSISTENCE. HA's MQTT integration reconnected at
# 16:38:50, dropped all four gardyn* entities during entity setup, and they
# stayed `unavailable` until a manual config-entry reload — while the Pi was
# healthy the whole time, publishing camera frames. Retained discovery is
# delivered once per subscribe, so nothing ever re-sent it. zigbee2mqtt and
# govee2mqtt both subscribe here and recovered on their own; the broker log
# shows the birth message delivered to exactly those two clients and no others.
#
# The prefix must match the one every discovery topic in send_discovery_
# messages() is published under. Both are the literal "homeassistant" — this
# file has never carried a configurable discovery prefix, and introducing one
# here would put the two halves out of sync the first time it was changed.
HA_STATUS_TOPIC = "homeassistant/status"
HA_BIRTH_PAYLOAD = "online"
# HA's last-will payload on the same topic, published by the broker on an
# unclean drop and by HA itself on a clean shutdown. It marks the boundary
# between two HA lifecycles, which is why the birth handler treats it as a
# debounce reset rather than as noise.
HA_DEATH_PAYLOAD = "offline"

# Subscribed at QoS 1, and kept OUT of COMMAND_SUBSCRIPTIONS on purpose: that
# list is scoped to BASE_TOPIC and documents what this device consumes as
# commands. This is neither — it is somebody else's lifecycle announcement, and
# folding it in there would make the QoS-1 rationale in that block (the broker
# queuing commands against the durable session) read as if it applied here too.
#
# Whether HA retains its birth message is a per-installation setting, so this
# client may receive an `online` immediately on subscribing, moments after
# on_connect already announced.
#
# That case is COVERED, not harmless — and this comment said "harmless" until
# review pointed out it was the copy attached to the one redundant announce the
# design predicts by name. announce_to_home_assistant() is idempotent in EFFECT
# (every publish is retained and overwrites, and the clear is a no-op after the
# first), but it is not free: 21 publishes and ~1.8 KB each time. The connect
# path stamps the debounce clock precisely so this retained `online` is
# suppressed instead of doubling the announce. See _birth_is_debounced().
LIFECYCLE_SUBSCRIPTIONS = [(HA_STATUS_TOPIC, 1)]

# Minimum seconds between two re-announces. Review measured one birth message
# as 21 publishes / ~1.8 KB outbound, plus a synchronous pigpio round-trip for
# the light's real duty cycle, all on paho's network-loop thread - a ~90x byte
# amplification of a ~30-byte inbound message, on a topic OUTSIDE this device's
# namespace that any client with broker publish rights can write to.
#
# Ten seconds, chosen against the failure that matters. Suppressing a LEGITIMATE
# re-announce is far worse than serving a redundant one: a missed announce is
# the 2026-08-05 outage. Home Assistant takes tens of seconds to start, so two
# genuine births inside ten seconds is not a shape that occurs, while a flapping
# or hostile publisher is stopped dead. Longer would start trading away the
# thing this file exists to guarantee.
#
# time.monotonic(), never time.time(): this Pi has no RTC and gets its clock
# from NTP after boot, so a wall clock that steps backwards mid-window would
# disable the debounce, and one that steps forward would extend it. monotonic()
# is immune to both, which matters more here than on a host with a battery.
BIRTH_DEBOUNCE_SECONDS = 10
_last_birth_announce = None

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
# temperature/get and humidity/get are dropped from this list. temperature_sensor
# and humidity_sensor are None on this unit, so those handlers could only ever
# raise AttributeError into on_message's catch-all - a subscription whose sole
# effect was a logged traceback.
#
# Note what removing them from this list does NOT do. The session is durable
# (clean_session=False with a stable client_id), so the broker still holds the
# subscriptions it was given on earlier connects; subscribe() only ever adds,
# and nothing here calls unsubscribe(). Those two topics therefore keep being
# delivered until the session is reset. That is harmless - the handlers are
# gone, so the elif chain falls through and the message is ignored, which is
# strictly better than the AttributeError it used to raise - but this list
# describes the CLIENT's intent, not the broker's state.
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

# Assigned in __main__, and declared here so the command handlers can be read
# and imported without it. Several test modules import this file under stubs
# and never run __main__, so a bare reference would be a NameError inside
# on_message rather than in a place anyone would look. The handlers branch on
# None explicitly and say so in the log — a silent fallback to driving the pin
# directly is exactly the second writer T-527.6 exists to remove.
light_scheduler = None

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


def publish_light_decision(client, decision):
    """Publish the lamp's state AND which writer decided it (T-527.6).

    WHY THE SECOND HALF IS NOT DECORATION. Once the Pi owns the photoperiod,
    "the light is at 50%" stops being a complete observation: it is the right
    answer at 18:30 and a person's override at 12:00, and nothing else on the
    network can tell those apart. The obedience automation in Home Assistant
    has to make exactly that distinction — its old no-false-alarm argument was
    written on HA's 15-minute re-assert, which T-527.8 retires — so publishing
    the source is what gives its replacement something to condition on.

    Retained, like the two state topics beside it, so a subscriber that arrives
    later is told immediately rather than at the next transition.
    """
    publish_light_state(client)
    client.publish(LIGHT_SOURCE_TOPIC, decision.source, retain=True)


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
    # INFO, not WARNING. This fires on every connect - roughly 25 times a day,
    # forever - for what is a no-op after the first one. The logging policy at
    # the top of this file is a deliberate trade that demoted the periodic
    # publishers to debug so ~576 camera lines a day could not bury four
    # meaningful light events; a WARNING here would spend that budget again.
    logger.info(
        f"Cleared {len(RETIRED_DISCOVERY_TOPICS)} retired discovery configs "
        f"and {len(RETIRED_STATE_TOPICS)} retained state topics"
    )


# Button press callbacks.
#
# These read hardware state rather than a shadow variable. light_state/pump_state
# used to be module globals that only the button path updated, so after any HA
# command the shadow was stale and the next button press just re-sent the state
# the device was already in — the press appeared to do nothing.
def apply_light_override(target):
    """Put the lamp at `target` as a manual override, through the scheduler.

    The ONE path from a human's intent to the pin, shared by the MQTT command
    handlers and the physical button. Both used to drive the light directly,
    which was correct while nothing else owned it.

    The None branch is not a fallback to the old behaviour, and that is
    deliberate: reaching here with no scheduler means __main__ never ran, so
    this is a test import or a hand-run REPL, not a garden. Driving the pin
    anyway would hide the one condition under which this file's model of who
    owns the lamp is wrong.
    """
    if light_scheduler is None:
        logger.error(
            "Light command for %s%% ignored: no scheduler is running, so "
            "nothing owns the photoperiod. This means mqtt.py's __main__ did "
            "not run.", target,
        )
        return
    light_scheduler.override_now(target)


def toggle_light():
    # Reads the hardware, then hands the result to the same override path the
    # MQTT handlers use. A person standing at the garden pressing the button is
    # as much an override as a person tapping the entity in Home Assistant, and
    # before T-527.6 the button's effect would have been reverted by the next
    # scheduler tick within 30 seconds.
    if light.get_brightness() > 0:
        logger.info("Toggling Light OFF")
        apply_light_override(0)
    else:
        logger.info("Toggling Light ON")
        apply_light_override(brightness)

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
    # THE ONE REMAINING DIRECT WRITER OF THE PIN, and deliberately so: this is a
    # ~2 s attention signal that restores what it found, not a statement about
    # what the lamp should be at, so routing it through the scheduler would
    # make a low-water abort look like a manual override holding until 03:00.
    # A scheduler tick landing inside the flash would set the scheduled value
    # and the restore would then put back the pre-flash brightness — corrected
    # at the following tick, at most one cadence later.
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

    # Config for the light's current OWNER (T-527.6). A diagnostic sensor, not
    # a control: it answers "is the photoperiod running, or has somebody taken
    # the lamp", which stopped being derivable from the brightness the moment
    # the Pi became the writer.
    #
    # entity_category diagnostic keeps it out of the main card and out of
    # auto-generated dashboards while leaving it fully available to templates
    # and automations — which is where it is actually needed, since the
    # obedience automation T-527.9 rebuilds has to condition on it.
    SOURCE_CONFIG_TOPIC = ("homeassistant/sensor/gardyn/" + IDENTIFIER
                           + "_light_source/config")
    publish_config(SOURCE_CONFIG_TOPIC, {
        "name": "Light Schedule Source",
        "unique_id": IDENTIFIER + "_light_source",
        "state_topic": LIGHT_SOURCE_TOPIC,
        "icon": "mdi:account-clock",
        "entity_category": "diagnostic",
        "device": device_info,
    })

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

def warn_if_topic_collides():
    """Report a colliding BASE_TOPIC at STARTUP, not at first connect.

    The two runtime guards for this both live in callbacks, so if the broker is
    down at boot - the case connect_async(retry_first_connection=True) exists
    for - neither fires and the misconfiguration is reported nowhere until the
    broker comes back. This puts it in `journalctl -u mqtt` at the moment anyone
    would look.

    Aborts nothing. See the guards for why a service that refuses to boot is the
    wrong answer on a host with no console.

    A MODULE-LEVEL FUNCTION rather than three lines inside `if __name__ ==
    "__main__"`, because nothing can reach code in that block: it was written
    there first, and deleting it survived the whole suite. It is only a log
    line, but it was the one fix in this change with no evidence behind it.
    """
    if STATUS_TOPIC == HA_STATUS_TOPIC:
        logger.error(
            f"MISCONFIGURED: BASE_TOPIC is {BASE_TOPIC!r}, so this device's "
            f"status topic is {HA_STATUS_TOPIC}, which is Home Assistant's own. "
            f"Discovery will not self-heal after an HA restart. Set "
            f"MQTT_BASETOPIC to something else."
        )
        return True
    return False


def _birth_is_debounced():
    """True if a birth message arrived too soon after the last re-announce.

    READS the clock; never writes it. announce_to_home_assistant() is the single
    writer, so every announce is stamped however it was reached - including the
    connect path, which does not consult this function.

    That split fixes a case LIFECYCLE_SUBSCRIPTIONS predicts by name: if HA
    retains its birth message, this client receives an `online` the instant it
    subscribes, milliseconds after on_connect already announced. When only the
    birth path stamped the clock, that produced two full announces back to back
    - the most predictable redundant announce in the design, sailing straight
    through the thing built to stop it.

    A (re)connect still announces UNCONDITIONALLY. It records that it did; it
    never asks permission. The broker drops this client roughly 25 times a day
    and a debounced reconnect is a device that comes back with no entities.

    Single-threaded by construction and deliberately not locked. paho dispatches
    on_connect and on_message from the same network-loop thread (both come from
    _packet_handle), so two birth messages are handled strictly in sequence and
    there is no window for a lock to protect. A lock here would imply a
    concurrency that does not exist and invite someone to relax it.

    FIXED window, not sliding: a suppressed birth must NOT push the deadline
    forward. If it did, a publisher writing `online` every five seconds would
    suppress Home Assistant's genuine birth message forever - turning a rate
    limit into a permanent mute, which is the exact outage this file exists to
    prevent, reached from the opposite direction.
    """
    return (_last_birth_announce is not None
            and monotonic() - _last_birth_announce < BIRTH_DEBOUNCE_SECONDS)


def announce_to_home_assistant(client):
    """Publish everything HA needs to (re)build this device's four entities.

    Extracted from on_connect() by T-527.1 so the birth-message path and the
    connect path run the IDENTICAL sequence. Two half-implementations of an
    announce would be the obvious way to reintroduce the 2026-08-05 outage in a
    new shape: HA came back, this client never re-sent discovery, and four
    entities sat `unavailable` while every light command failed log-only.

    What is deliberately NOT in here, and must not be moved in. Both are
    PER-CONNECTION or PER-PROCESS lifecycle, and an announcement is neither:

      client.subscribe(...)      per-CONNECTION. The session is durable
                                 (clean_session=False), so the broker already
                                 holds the subscriptions; re-subscribing on
                                 every birth message is pointless traffic on a
                                 single-antenna Zero W.
      start_publisher_threads()  per-PROCESS. It carries its own lock-guarded
                                 once-only flag (see its definition), so
                                 calling it from here would not leak threads -
                                 it would return immediately, every time. That
                                 is the argument for leaving it out rather than
                                 against: a call that is always a no-op reads
                                 to the next maintainer as though it does
                                 something, and hides where the publishers
                                 actually start.

    An earlier version of this docstring claimed the opposite - that
    start_publisher_threads() spawns threads unchecked and would leak a set per
    HA restart. That was wrong; review caught it. The exclusion was right and
    its stated reason was not, which is the more dangerous of the two to leave
    in place: it would send someone hunting a leak that cannot happen, or
    adding a second guard while the real one silently returns early.

    Idempotent in EFFECT, which is what makes it safe to call from a message
    handler. Note that is not the same as free: every call puts 21 publishes
    (~1.8 KB) on the wire, 14 of them the retired-entity clear, whose no-op-ness
    is about what HA ends up with rather than about what crosses the link. That
    cost is why the caller debounces - see _birth_is_debounced().
    """
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
    # …and who currently owns the lamp (T-527.6). Unconditional, which is the
    # whole point: the value has not changed, the SUBSCRIBER has, and a
    # retained message is delivered once per subscribe. Without this the source
    # entity sits at whatever the broker still holds — or at `unknown` on a
    # first announce — until the next transition, which can be hours away.
    #
    # A no-op before the scheduler's first tick, which is a real window: this
    # runs on the connect path and the scheduler starts on its own thread at
    # the same moment. The next tick publishes it, at most one cadence later.
    if light_scheduler is not None:
        light_scheduler.publish_now()
    # The single writer of the debounce clock. Stamped here rather than in
    # _birth_is_debounced() so that EVERY announce counts, including the connect
    # path's - see that function for the retained-birth case this covers.
    global _last_birth_announce
    _last_birth_announce = monotonic()


def _connack_refused(rc):
    """True when the CONNACK carried a refusal rather than an acceptance.

    `rc`'s TYPE is decided by the callback API version, so this reads both
    rather than assuming one. Both forms are in paho 2.0.0's _handle_connack():

      VERSION2 — what this file registers — always hands over a ReasonCode, on
        the v5 and the v3.1.1 path alike; a v3 return code is passed through
        convert_connack_rc_to_reason_code() first. That mapping is why the v3
        NUMBERS must not be compared against: "server unavailable" is 3 on the
        wire and arrives here as ReasonCode 136. `is_failure` is the property
        paho provides for this question and it is `value >= 0x80`; every CONNACK
        refusal maps at or above 128, so it answers the whole space.
      VERSION1 with MQTTv3 hands over the bare int return code instead, 0 being
        accepted. Nothing here registers that today. The int branch is also what
        the test suite's on_connect(client, None, None, 0) call sites take.

    Measured against paho 2.0.0 rather than reasoned about: driving a synthetic
    CONNACK through Client._handle_connack with client_id set and VERSION2
    registered produced ReasonCode 133/136/134/135 for v3 codes 2/3/4/5, every
    one of them is_failure=True.
    """
    is_failure = getattr(rc, "is_failure", None)
    if is_failure is None:
        return rc != 0
    return bool(is_failure)


# FIVE POSITIONAL ARGUMENTS, NOT FOUR. The client below registers
# CallbackAPIVersion.VERSION2, and paho 2.0.0's _handle_connack calls a VERSION2
# on_connect as on_connect(self, userdata, connect_flags, reason, properties) —
# always five, with `properties` synthesised as Properties(PacketTypes.CONNACK)
# when the CONNACK carried none, so it is never omitted and never None.
# `properties=None` is therefore a default nothing in production supplies, and
# deleting it makes every connect raise TypeError INSIDE the callback:
# suppress_exceptions defaults to False, so _handle_connack re-raises, the
# exception leaves loop_forever(), the process exits, and Restart=always with
# RestartSec=10 and StartLimitIntervalSec=0 turns that into a permanent
# ten-second restart loop with the grow light off.
# Measured against paho 2.0.0, not recalled — see tests/test_connack_refusal.py,
# which drives all three callbacks the way paho drives them.
def on_connect(client, userdata, flags, rc, properties=None):
    # A REFUSED CONNACK REACHES THIS CALLBACK. paho calls on_connect from
    # _handle_connack whatever the reason code says — the `if result == 0`
    # blocks around the call site gate the client's own state, not the
    # callback. Only one CONNACK is answered by paho instead of by us, and only
    # on its FIRST occurrence: a v3.1.1 protocol-version rejection, which paho
    # handles by setting self._protocol = MQTTv31 and reconnecting. The guard
    # around that is `if self._protocol == MQTTv311`, so once the downgrade has
    # happened it no longer matches — a broker that rejects the version again
    # after the downgrade DOES reach this callback, measured, as ReasonCode 132.
    # (There is a second route to non-arrival that is not the downgrade at all:
    # with reconnect_on_failure=False paho returns MQTT_ERR_PROTOCOL instead.
    # Nothing here sets that.) Its sibling early return, for a rejected client
    # identifier, is guarded by `self._client_id == b''` — this client is
    # constructed with client_id=IDENTIFIER, so that one never fires and a
    # rejected identifier lands here too.
    #
    # Without this gate the refusal ran the entire connected path against a
    # socket the broker is in the middle of closing: it logged "Connected with
    # result code Not authorized", subscribed, and announced. Those three are
    # wasted work and heal on the next connect. The last statement does not.
    #
    # BE EXACT ABOUT THE MECHANISM, because the tempting shorthand — "the
    # publishers never start" — is wrong and points at the wrong fix.
    # start_publisher_threads() sets its once-only flag AND spawns both loops,
    # so a refused CONNACK spawns them against a client that is not connected.
    # Their first publish is then LOST — by whichever of two routes wins the
    # race, which is why neither is named as the mechanism: _send_publish()
    # returns MQTT_ERR_NO_CONN once the network loop has closed the socket, and
    # a publish that got queued before that is discarded by reconnect(), which
    # clears _out_packet. Each loop then sleeps to its own period — 30 minutes
    # for publish_pcb_temperature, IMAGE_INTERVAL_SECONDS (an hour, as shipped)
    # for publish_images.
    #
    # The connect that succeeds seconds later finds the flag already set and
    # correctly returns early, so nothing re-sends. Nothing is leaked and
    # nothing looks broken — the device is simply up with no fresh PCB
    # temperature for up to half an hour and no fresh camera frame for up to an
    # hour.
    #
    # WHAT HOME ASSISTANT SHOWS in that window depends on what it already had,
    # and an earlier version of this comment named one case as if it were the
    # only one ("`unknown` for up to half an hour"). NEITHER PUBLISHER RETAINS:
    # publish_pcb_temperature() omits retain= and paho defaults it to False,
    # and _capture_and_publish() passes retain=False explicitly — so there is no
    # retained message for a subscriber to read on connect and nothing replays
    # the gap. After a device-only reconnect HA is therefore still holding the
    # value it last received, which is STALE rather than `unknown`; `unknown` is
    # what an HA that restarted in the window gets, having no earlier value.
    # Either way the entity is not reporting the garden for that window, which
    # is the whole reason for the gate below.
    #
    # This is the race start_publisher_threads()'s docstring describes, but do
    # NOT say its once-only guard exists to prevent it — an earlier draft of
    # this comment did, and review caught it. That docstring credits two
    # different things: moving the start out of __main__ and into on_connect is
    # what avoids the NO_CONN race, and the guard is for on_connect firing on
    # every reconnect. The guard never prevented the race; a refused CONNACK
    # simply satisfies "on_connect fired" without a connection behind it.
    #
    # Returning is not giving up. The broker drops the socket, loop_forever()
    # takes its _reconnect_wait() branch and reconnects on the backoff set by
    # reconnect_delay_set() above — so a credential fixed at the broker heals on
    # the next attempt with nothing to restart by hand, which matters on a host
    # with no console.
    if _connack_refused(rc):
        logger.error(
            f"Connection REFUSED by broker: {rc}. Not subscribing, not "
            f"announcing discovery, and NOT starting the publisher threads - "
            f"their once-only guard must survive for the connect that succeeds. "
            f"paho will retry with backoff."
        )
        return
    logger.warning(f"Connected with result code {rc}")
    # Explicit topic list, not BASE_TOPIC + "/#" — see COMMAND_SUBSCRIPTIONS.
    client.subscribe(COMMAND_SUBSCRIPTIONS)
    # HA's birth topic, subscribed separately — see LIFECYCLE_SUBSCRIPTIONS for
    # why it is not folded into the list above.
    #
    # Refused when BASE_TOPIC collides with HA's discovery prefix. BASE_TOPIC is
    # env-configurable (MQTT_BASETOPIC, default "gardyn"), so setting it to
    # "homeassistant" makes STATUS_TOPIC == HA_STATUS_TOPIC — and the announce
    # publishes "online" to STATUS_TOPIC. The broker would echo that straight
    # back to this client (MQTT 3.1.1 has no no-local option), re-enter the
    # birth branch, and re-announce forever: a tight loop on the network-loop
    # thread, on a device with no console and no physical recovery.
    #
    # DEGRADE, do not abort. Refusing to start would be the safest-looking
    # option and is the wrong one here — a service that will not boot on a host
    # nobody can reach by hand is unrecoverable, while a device that runs
    # without the birth re-announce is merely back to the pre-T-527.1 behaviour
    # and still drives the light.
    #
    # THIS IS THE WEAKER HALF OF THE GUARD, and an earlier version of this
    # comment had it exactly backwards — it claimed "the debounce would blunt
    # the loop but not stop it; this stops it." Reversed. Refusing to SUBSCRIBE
    # cannot refuse to RECEIVE: the durable session means the broker keeps a
    # subscription this client asked for on any earlier run, so on the
    # transition path (a device that ran normally, then had MQTT_BASETOPIC
    # changed) the birth message still arrives and this branch never sees it.
    # The guard that actually closes the loop is in on_message; this one only
    # stops a FRESH session from acquiring the subscription in the first place.
    if STATUS_TOPIC == HA_STATUS_TOPIC:
        logger.error(
            f"NOT subscribing to {HA_STATUS_TOPIC}: BASE_TOPIC is "
            f"{BASE_TOPIC!r}, which collides with Home Assistant's discovery "
            f"prefix and would make this client re-announce in a loop. "
            f"A birth message may still be delivered from a subscription the "
            f"broker already holds; the handler ignores it. NOTE this device "
            f"still PUBLISHES retained 'online' to that topic on every connect, "
            f"which other discovery clients read as Home Assistant restarting. "
            f"Discovery will not self-heal until MQTT_BASETOPIC is changed."
        )
    else:
        client.subscribe(LIFECYCLE_SUBSCRIPTIONS)
    announce_to_home_assistant(client)
    # A water-state refresh stood here, wrapped in a try/except so a sensor
    # explosion could not leave the publisher threads unstarted (T-475 removed
    # it with the entities it refreshed). Nothing between the subscribe and the
    # line below touches hardware any more, so there is no longer a call to
    # guard — but note the hazard if one is ever added back: start_publisher_
    # threads() is the last statement, and an exception escaping above it leaves
    # the PCB and camera loops permanently unstarted while gardyn/status sits at
    # "online" and the device looks perfectly healthy.
    #
    # That hazard now has a second entrance: announce_to_home_assistant() above
    # is the whole announce sequence, so a raise anywhere inside it skips this
    # line just as surely. TestConnectSequencing asserts this call is reached.
    start_publisher_threads(client)

def on_message(client, userdata, msg):
    global brightness, speed, WATER_LOW_CM, _last_birth_announce

    # DECODE THE TOPIC ONCE, AND BEFORE ANYTHING ELSE. `msg.topic` is a paho
    # PROPERTY - `self._topic.decode("utf-8")`, client.py:618 at the pinned
    # 2.0.0 - so it re-decodes on every access and raises UnicodeDecodeError on
    # every access when the bytes are not valid UTF-8. paho does not validate
    # them: _handle_publish stores whatever arrived, and _handle_on_message
    # calls this callback regardless.
    #
    # Until T-527.12 this was a process-killer, and a quiet one. The block
    # below read `msg.topic` in the try AND again in its own
    # `except UnicodeDecodeError` handler, so a bad topic raised, was caught,
    # and raised again from inside the handler - out of on_message, out of
    # loop_forever(), out of the process. With Restart=always and
    # RestartSec=10 that is a permanent ten-second restart loop with the grow
    # light off, on a host with no physical recovery path. Same shape as the
    # CONNACK case documented above, reached by a different input.
    #
    # Reported without the decoded value, because the value is what could not
    # be produced. `_topic` is paho-private, hence the getattr - the bytes are
    # the only useful thing to put in an incident record, and %r escapes them
    # on the same rule as everything else here.
    try:
        topic = msg.topic
    except (UnicodeDecodeError, AttributeError):
        # AttributeError too, from review. The property is
        # `self._topic.decode(...)`, so a `_topic` holding anything but bytes
        # raises there instead - the same process-exit shape, one exception
        # class over, and a guard presented as closing a class should close it.
        # Deliberately NOT `except Exception`: this runs before any dispatch,
        # so a blanket catch here would swallow faults that belong to the
        # handler chain below and has no business being that wide.
        logger.error("Dropped an inbound message whose TOPIC could not be "
                     "decoded: %r", getattr(msg, "_topic", None))
        return

    try:
        payload = msg.payload.decode("utf-8").strip()
        # !r, not '{payload}'. THE RULE, because this line is not special and an
        # earlier version of this comment claimed it was: EVERY log line that
        # interpolates a payload uses !r. A payload is remote input on every
        # subscribed topic, not only on homeassistant/status - being under
        # BASE_TOPIC means a topic is ADDRESSED to this device, not that the
        # broker vouches for who wrote it. Review found the sibling this comment
        # had just declared nonexistent, still raw, in the water/low/cm/set
        # handler below.
        #
        # Bare interpolation writes a newline, a CR or an ANSI escape to the
        # file exactly as sent, so a payload can forge whole log lines -
        # timestamp, logger name, level and all - in the artifact these
        # incidents get reconstructed from. repr() escapes them to \n / \r /
        # \x1b. Nothing in this repo rotates gardyn.log - no logrotate unit, and
        # basicConfig() above uses a plain FileHandler - so a forged line stays
        # there for as long as anyone will be reading it.
        #
        # THE TOPIC IS ESCAPED ON THE SAME RULE (T-527.12), and it was not
        # until now. An MQTT topic name is UTF-8 with only `+`, `#` and NUL
        # excluded, so it can carry \n, \r and \x1b exactly as a payload can -
        # the forgery argument above does not distinguish the two. Leaving it
        # raw while the payload beside it was escaped read as a considered
        # scope and was an oversight.
        #
        # WHAT THE EXPOSURE IS. Third version of this paragraph; the first two
        # are struck through below because each was wrong in a different way
        # and the sequence is more useful than the answer.
        #
        # A SUBSCRIPTION LIST IS THE CLIENT'S INTENT, NOT THE BROKER'S STATE.
        # `clean_session=False` with a stable client_id, `subscribe()` only ever
        # adds, nothing calls `unsubscribe()` - so the broker holds every
        # subscription any earlier connect asked for, and the two lists below
        # are a floor rather than a ceiling. Measured, that floor is TEN topics
        # rather than the eight the lists name: `gardyn/temperature/get` and
        # `gardyn/humidity/get` left COMMAND_SUBSCRIPTIONS at 3ed7081, which is
        # a descendant of 9e00c2f, so a durable session still carries them.
        #
        # All ten are literal and LOCALLY DERIVED. That is the whole load-
        # bearing property, and it is the only one asserted here on purpose:
        # this sentence said "literal, ASCII and derived from local config"
        # until 2026-08-12, and ASCII was an assumption about a file nobody
        # reading this repo can see. Nine of the ten are ASCII literals in
        # this module, but every one is prefixed with BASE_TOPIC, which is
        # `os.getenv("MQTT_BASETOPIC", "gardyn")` (config.py) read from a
        # GITIGNORED .env - so `MQTT_BASETOPIC=gardyn-café` makes the claim
        # false and changes nothing about the conclusion. Locally derived is
        # what matters: whatever those bytes are, WE chose them, not a peer.
        # So under a
        # CONFORMING broker a topic arriving here is never remotely chosen, and
        # the reachable forgery case is a broker that does not conform, or one
        # an attacker controls - which is the same threat model that justifies
        # escaping the payload, since a broker able to invent a topic chooses
        # the bytes under it too.
        #
        # ~~(1) ...so a CONFORMING broker can only ever deliver the EIGHT
        # literal topics they name.~~ Right conclusion, wrong count: it read
        # the list as the broker's state, which is the error the collision
        # guard below exists because of.
        #
        # ~~(2) ...the broker still holds `BASE_TOPIC + "/#"` from before
        # 9e00c2f, so an ORDINARY broker plus a stale session delivers a forged
        # topic.~~ Wrong, and worse than (1) because it overstated the risk in
        # a comment justifying a fix. That set is empty by construction:
        # 9e00c2f is the commit that INTRODUCED `clean_session` and `client_id`,
        # in the same change that removed the wildcard. Before it,
        # `mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)` took a random id and
        # clean_session defaulted True, so no session survived to hold anything.
        # No commit in this repo's history carries both a live `/#` subscribe
        # and a durable session. Reviewed twice: the first review supplied this
        # mechanism, the second refuted it with `git log -S`.
        logger.info(f"Decoded payload on {topic!r}: {payload!r}")
    except UnicodeDecodeError:
        logger.error(f"Failed to decode message on topic {topic!r}. Likely binary.")
        return
    except AttributeError as exc:
        # THE SAME PROCESS-EXIT ROUTE AS THE TOPIC GUARD ABOVE, one exception
        # class over. `msg.payload` is wire bytes from paho, so `.decode` is
        # there - but a payload that is None, a str, or absent raises
        # AttributeError here, and nothing catches it: out of on_message, out
        # of loop_forever(), out of the process, into a ten-second
        # Restart=always loop with the grow light off on a host with no
        # physical recovery path.
        #
        # Reachability is nil under the pinned paho 2.0.0, whose
        # _handle_publish always hands over bytes - which is EXACTLY the
        # reachability the topic guard's own AttributeError arm was given when
        # review added it. Closing one and leaving the other was an asymmetry
        # rather than a judgement, and the sentence written at that guard
        # applies verbatim: a guard presented as closing a class should close
        # it. T-527.29.
        #
        # A SEPARATE ARM, not `except (UnicodeDecodeError, AttributeError)`,
        # because the two mean different things and this log line is the only
        # thing an incident gets reconstructed from. Undecodable bytes point
        # at the broker or a device; a payload with no `.decode` points at the
        # library having moved underneath us, which sends the reader somewhere
        # completely different. Deliberately NOT `except Exception`: this runs
        # before dispatch and has no business swallowing handler faults.
        #
        # getattr, NOT msg.payload, to name the type. Reading the attribute
        # that just raised, inside the handler for that raise, is the precise
        # shape of the process-killer T-527.12 found at the topic guard - and
        # `msg` here may have no `payload` at all.
        payload_type = type(getattr(msg, "payload", None)).__name__
        logger.error(
            f"Dropped an inbound message on topic {topic!r} whose PAYLOAD has "
            f"no usable bytes (type {payload_type!r}). This is a library "
            f"shape change rather than a bad message: {exc!r}")
        return

    topic_suffix = topic.replace(BASE_TOPIC + "/", "")

    try:
        # === Home Assistant lifecycle ===
        #
        # Matched on the decoded `topic`, NOT topic_suffix. The suffix is
        # produced by stripping a BASE_TOPIC prefix this topic does not have,
        # so it would
        # arrive here as the full "homeassistant/status" and match nothing —
        # which is exactly how this went unnoticed: an unhandled topic falls
        # through the chain silently and looks identical to a topic nobody
        # subscribed to.
        #
        # First in the chain so the reason it exists is the first thing read,
        # and so no future BASE_TOPIC branch can shadow it.
        if topic == HA_STATUS_TOPIC and STATUS_TOPIC == HA_STATUS_TOPIC:
            # THE COLLISION GUARD THAT ACTUALLY WORKS. Its twin at the subscribe
            # site in on_connect refuses to ASK for this topic; it cannot refuse
            # to RECEIVE it, and that distinction is the whole finding.
            #
            # This client runs clean_session=False with a stable client_id, so
            # the BROKER holds the subscription list across restarts. A device
            # that ever ran with the default BASE_TOPIC subscribed to
            # homeassistant/status; MQTT_IDENTIFIER is independent of
            # MQTT_BASETOPIC, so changing the base topic to "homeassistant" and
            # restarting resumes the same session with that subscription intact.
            # The refusal then does nothing, the announce publishes "online" to
            # STATUS_TOPIC — which now IS this topic — the broker echoes it back,
            # and the client re-announces every 10 seconds forever: ~181k
            # publishes and ~15 MB a day, with HA reprocessing four discovery
            # configs each time.
            #
            # Measured, not reasoned: 21 publishes on the first pass, 42 after
            # the echo re-entered. The debounce BOUNDS that loop; it does not
            # stop it. An earlier version of the comment at the subscribe site
            # claimed the reverse.
            #
            # This file already knew the rule and the new guard was written as
            # if it did not — see the note on COMMAND_SUBSCRIPTIONS about the
            # broker still delivering temperature/get and humidity/get long
            # after they left that list. ON THIS CLIENT A SUBSCRIPTION LIST IS A
            # STATEMENT OF INTENT, NOT OF WHAT ARRIVES, so any guard whose safety
            # depends on not RECEIVING a topic has to live in the handler.
            logger.error(
                f"Ignoring {HA_STATUS_TOPIC}: BASE_TOPIC is {BASE_TOPIC!r}, so "
                f"this device's own status topic collides with Home Assistant's "
                f"discovery prefix. Re-announcing would echo back to this client "
                f"and loop. Discovery cannot self-heal until MQTT_BASETOPIC is "
                f"set to something other than {HA_STATUS_TOPIC.split('/')[0]!r}."
            )

        elif topic == HA_STATUS_TOPIC:
            if payload.lower() == HA_BIRTH_PAYLOAD:
                # The whole point of T-527.1. HA has just come up and has no
                # idea this device exists: retained discovery is delivered once
                # per subscribe, so if HA dropped these entities during its own
                # entity setup, nothing re-sends them and they stay
                # `unavailable` until somebody reloads the config entry by hand.
                #
                # No random delay, in a deliberate departure from the MQTT
                # integration docs' "adding some random delay in sending the
                # discovery payload is recommended". That advice is aimed at an
                # estate of devices all answering one birth message at once;
                # this is a single client publishing four small retained
                # configs. Sleeping here would be actively worse than useless —
                # this callback runs on paho's network loop thread, so a sleep
                # stalls keepalives and inbound command delivery for its
                # duration, on the one radio the Pi has.
                if _birth_is_debounced():
                    logger.warning(
                        f"Home Assistant birth message within "
                        f"{BIRTH_DEBOUNCE_SECONDS}s of the last re-announce - "
                        f"skipping. Repeated at length this means a flapping "
                        f"HA or another publisher on {HA_STATUS_TOPIC}."
                    )
                else:
                    logger.info(
                        "Home Assistant birth message - re-announcing discovery"
                    )
                    announce_to_home_assistant(client)
            else:
                # Almost always HA's own LWT saying "offline". Nothing to
                # publish: this device's availability is its own retained
                # STATUS_TOPIC, which HA re-reads when it returns. Logged rather
                # than ignored because "HA went away at 16:38" is the single most
                # useful line to have when reconstructing an outage like
                # 2026-08-05.
                #
                # But it DOES clear the debounce, and that is what makes the
                # debounce safe rather than merely short. An `online` that
                # follows an `offline` is by definition a new Home Assistant
                # lifecycle, so it must never be suppressed - HA publishes its
                # LWT on both a clean shutdown and a crash, so this branch is
                # the boundary between two lifecycles.
                #
                # Without this the 10-second window rests on "HA takes tens of
                # seconds to start", which is a claim about HA's PROCESS. HA
                # publishes its birth on every MQTT CLIENT connect, which is a
                # much cheaper event: a broker restart, a network blip on HA's
                # side, or a config-entry reload - and a reload is precisely
                # what unloads and rediscovers every MQTT entity. Two of those
                # inside ten seconds is reachable.
                #
                # BE HONEST ABOUT WHAT THIS GIVES UP, because the first version
                # of this comment was not. It claimed the reset "leaves the
                # debounce protecting the case it is actually for: a flapping or
                # hostile publisher." Both of those are the cases it does NOT
                # protect. A flapping HA is DEFINED by intervening `offline`s -
                # the broker publishes the LWT on an unclean drop and HA
                # publishes it itself on a clean one - so every flap clears the
                # clock and announces. Measured: ten flaps inside one window
                # cost 210 publishes, not 21.
                #
                # That is not a bug, and it cannot be fixed by tuning: "never
                # suppress a genuine HA reconnect" and "rate-limit HA
                # reconnects" are requirements about THE SAME EVENT, so one has
                # to lose. This picks the first, deliberately - a suppressed
                # re-announce is the 2026-08-05 outage, a redundant one is
                # 1.8 KB. For scale, an HA flapping every 10s costs ~10.8 KB/min
                # against the camera pair's ~12 KB/min baseline: comparable, and
                # not dangerous.
                #
                # What the debounce still bounds is a repeated `online` with no
                # `offline` between - the retained-birth-after-connect case, and
                # a publisher stuck sending one payload.
                #
                # MATCHED ON THE LWT PAYLOAD, not on "any non-online". The first
                # version reset on anything that was not `online`, so a single
                # junk payload between two births bypassed the debounce - wider
                # than the reasoning above justifies, and nothing pinned it.
                if payload.lower() == HA_DEATH_PAYLOAD:
                    _last_birth_announce = None
                logger.info(f"Home Assistant status: {payload!r} - no action")

        # === Pump Logic ===
        elif topic_suffix == "pump/command":
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
        #
        # Both handlers route through the scheduler rather than driving the pin
        # (T-527.6). A manual command is an OVERRIDE: it owns the lamp until the
        # next scheduled boundary, at which point the schedule takes it back.
        # The accepted cost, recorded in the T-527 design: an override applied
        # at 19:05 holds until 03:00, because 19:00 is the last boundary of the
        # day. A TTL was considered and rejected as more state to test than the
        # case justifies.
        #
        # Driving the pin here as well would make two writers of one lamp, and
        # they would differ in the two things that are invisible until they
        # matter — what gets persisted for the unsynced-clock hold, and what
        # gets published as the owner.
        elif topic_suffix == "light/command":
            if payload.upper() == "ON":
                apply_light_override(brightness)
            elif payload.upper() == "OFF":
                apply_light_override(0)

        elif topic_suffix == "light/brightness/set" and payload.isdigit():
            # `brightness` remains the ON level for a later bare `light/command
            # ON`, which is why it is still assigned here. The scheduler does
            # not read it.
            brightness = int(payload)
            apply_light_override(brightness)

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
                # !r for the same reason as the decode line at the top of this
                # function: water/low/cm/set is in COMMAND_SUBSCRIPTIONS, so
                # this payload is whatever a broker client sent, and everything
                # that fails float() reaches this line.
                #
                # NO RANKING BETWEEN THE SINKS. An earlier version of this
                # comment called this one "the more dangerous of the two"
                # because "ERROR is above the root logger's WARNING, so it
                # survives the INFO line being filtered out", and that is wrong
                # twice over. The INFO line is not filtered: logger.setLevel(
                # logging.INFO) near the top of this module raises THIS logger
                # to INFO as a deliberate trade, and test_water_interlock.py's
                # test_service_logger_is_info_so_commands_are_attributable
                # asserts it. And a record's fate never depends on an ANCESTOR
                # logger's level at all — the emitting logger's effective level
                # decides whether a record is created, and from there
                # callHandlers consults each HANDLER's level, which is what
                # test_handlers_do_not_filter_above_the_logger_levels in the
                # same file documents. basicConfig() above leaves both handlers
                # at NOTSET, so INFO and ERROR land in gardyn.log alike and a
                # forged line is worth exactly as much on either.
                logger.error(f"Invalid water low cm value: {payload!r}")
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
        # {e!r}, not {e} (T-527.12). An exception's str() can quote its operand
        # verbatim, and repr() of an exception repr()s its args, which escapes
        # the control characters str() would have written straight out.
        #
        # ~~int("\N{SUPERSCRIPT TWO}") is the live example, since .isdigit()
        # lets it through.~~ WRONG, struck rather than deleted. Measured:
        # `str(int('a\nb'))` is "invalid literal for int() with base 10:
        # 'a\\nb'" - CPython already repr()s the operand into that message, and
        # float() does the same. So the int() path was never a forgery path,
        # .isdigit() is not what saves it, and {e!r} buys nothing THERE.
        #
        # THIS IS DEFENSIVE, NOT A FIX FOR A LIVE PATH, and saying so is the
        # point: no exception reachable from this try carries a
        # REMOTE-CONTROLLED operand verbatim. Narrowed from "has a verbatim
        # str()", which was loose - app/sensors/distance/distance.py:102 raises
        # MeasurementError(f"Measurement failed: {e}"), embedding another
        # exception's str() with no escaping at all. It never reaches here only
        # because safe_distance_measure()'s `except MeasurementError` contains
        # it, and the earlier wording did not mention the containment its own
        # truth depended on. ValueError("bad\n...") raised by hand would carry
        # one - measured - so the escaping is what stops the next raise from
        # reopening it.
        #
        # WHAT THIS DOES NOT CLOSE, said plainly because a half-fix described
        # as a fix is what stops the next reader looking: logger.exception()
        # also renders exc_info, and the traceback's final line is str(e), not
        # repr(e). Measured - the forged text lands at column 0, formatted
        # exactly like a real record. No escaping at this call site can reach
        # it; it is rendered by the logging module. Closing it needs a
        # Formatter, which is wider than this step, and nothing in the suite
        # reads a FORMATTED record for on_message, so nothing pins it either
        # way.
        #
        # The scanner in tests/test_connack_refusal.py sees the topic half of
        # this line and NOT the `e` half - `except ... as e` is outside its
        # taint propagation on purpose. So the topic is machine-checked and
        # `e` is pinned by a source assertion. An earlier version of this
        # comment said the scanner could see neither, which stopped being true
        # in the same commit that widened its seed to topics.
        logger.exception(f"Error handling message on topic {topic!r}: {e!r}")

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
    not select a sane default: fswebcam's documented default factor is -1
    ("automatic"), it holds that in a `char`, and plain `char` is UNSIGNED on
    ARM - so on this Pi -1 becomes 255, libgd's use-the-default branch is never
    taken, and libjpeg clamps 255 to maximum quality. The frames name the bug
    themselves, carrying `quality = 255` in their own JPEG comment. That cost
    ~748 KB per five-minute cycle against ~169 KB at 85, on a host where this
    burst is the only sustained TX load.

    Passing quality as an argument rather than reading a module global mirrors
    `resolution` and is what lets the two cameras differ.
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
    would race it and be lost — and these loops sleep a long time between
    passes, so a lost first publish means an entity sits `unknown` until the
    next one: half an hour for publish_pcb_temperature, and
    IMAGE_INTERVAL_SECONDS (an hour, as shipped) for publish_images. This
    docstring said "these loops sleep 30 minutes" until T-527.11; it was right
    about one of the two.

    on_connect fires on every reconnect, hence the once-only guard. NOTE the
    guard and the placement fix different things — the placement is what avoids
    the race above, the guard only stops a second set of threads being spawned.
    Neither covers on_connect firing for a CONNACK that was REFUSED, which is
    what _connack_refused() is for.
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
    """Make a drop visible. Nothing logged one before, on either side.

    FIVE POSITIONAL ARGUMENTS, for the same reason as on_connect: paho 2.0.0's
    _do_on_disconnect calls a VERSION2 on_disconnect as
    (self, userdata, disconnect_flags, reason, properties), synthesising both
    the flags and the Properties when the drop was local rather than a DISCONNECT
    packet. `rc` is therefore a ReasonCode, not an int — `rc == 0` is a legal
    question to ask of it because ReasonCode.__eq__ compares against a bare int
    by value, and a clean local disconnect arrives as
    ReasonCode(DISCONNECT, 'Normal disconnection') whose value is 0. Measured
    against paho 2.0.0; a connection loss arrives as 'Unspecified error' (128).
    """
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
    warn_if_topic_collides()

    # The local photoperiod (T-527.5), started BEFORE the connection and
    # deliberately not gated on it. That ordering IS the feature: the whole
    # point of T-527 is that the lamp keeps its schedule when Home Assistant is
    # down, when the broker is down, and when this Pi is off the network. Two
    # outages in 2026 came from a schedule that lived at the far end of that
    # chain; a third came from this Pi rebooting mid-phase, which is why the
    # engine computes "what phase is it now" rather than reacting to boundary
    # edges — a restart at 14:00 lands on the right brightness on its first
    # tick.
    #
    # It is NOT started from on_connect() alongside start_publisher_threads():
    # that path only runs once a broker has accepted a CONNACK, so putting the
    # scheduler there would reintroduce the dependency this change removes.
    #
    # The publish is best-effort and is called only when the lamp's brightness
    # or its OWNER changes. A publish on a client that has not connected yet
    # returns an error code rather than raising, and the reconnect path
    # republishes both through announce_to_home_assistant() anyway.
    light_scheduler = LightScheduler(
        light, lambda decision: publish_light_decision(client, decision)
    )
    light_scheduler.start()

    client.connect_async(BROKER, PORT, KEEP_ALIVE_INTERVAL)

    # The periodic publishers are started from on_connect, not here — see
    # start_publisher_threads().
    client.loop_forever(retry_first_connection=True)
