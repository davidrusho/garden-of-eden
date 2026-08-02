# app/sensors/pump/routes.py
#
# Reviewed: 2026-08-01 against a18c612 (T-489)
#
# THIS BLUEPRINT MUST NEVER OFFER A WAY TO START THE PUMP.
#
# Every other pump-start path in this repository is gated by mqtt.py's
# start_pump(), which refuses on a low reservoir AND on an untrustworthy
# reading - an unknown water level is not a safe one, and running a hydroponic
# pump dry is the failure that interlock exists to prevent. This blueprint used
# to serve POST /pump/on and POST /pump/speed, and both drove the GPIO directly
# with no water check of any kind.
#
# Routing them through start_pump() is not available, and that is why they are
# deleted rather than fixed:
#
#   * start_pump() reads mqtt.py module globals - `pump`, `distance_sensor`,
#     `WATER_LOW_CM` - bound to the one process that holds the GPIO. Importing
#     mqtt.py from here does not import a function, it starts a second
#     controller: at module scope mqtt.py builds a PiGPIOFactory, a Pump on
#     GPIO 24, a Light, a Distance on GPIO 19/26 and a Button on GPIO 13, then
#     binds button.when_pressed. This module has already built a Pump on GPIO
#     24 by then, so gpiozero refuses the second reservation in the same
#     process - and had it not, a web server would be running the physical
#     button's callbacks.
#
#   * Re-implementing the check here instead would put a second copy of one
#     safety decision in a second process, each with its own DistanceSensor
#     driving the same trigger pin. Two writers of one interlock is a worse
#     outcome than the bypass it replaces, because each copy looks correct on
#     its own.
#
# So the API keeps only what it can serve safely. Nothing automated called the
# deleted routes: no systemd unit deploys run.py, and the README's own cron
# examples invoke app/sensors/pump/pump.py directly rather than over HTTP. The
# capability itself survives, interlocked, on the path that owns it -
# `mosquitto_pub -t gardyn/pump/command -m ON` runs the pump through
# start_pump().
#
# STOPPING IS DELIBERATELY STILL SERVED. A control that can refuse to start a
# pump but cannot stop one is not a safety control, and mqtt.py makes the same
# call for the physical button. /pump/off is gated on nothing and never should
# be.
#
# Enforced by tests/test_pump_api_interlock.py, whose central assertion
# enumerates the app's URL map rather than naming these two paths - a start
# route added back under a different name, or bolted onto a read-only rule by
# widening its `methods`, fails it too.

from flask import Blueprint, jsonify
from app.lib.lib import check_sensor_guard
from .pump import Pump as PumpControl
from .pump_power import fetch_ina219_data

pump_blueprint = Blueprint('pump', __name__)
pump_control = PumpControl()
check_sensor = check_sensor_guard(sensor=pump_control, sensor_name='Pump')

@pump_blueprint.route('/off', methods=['POST'])
@check_sensor
def turn_off():
    pump_control.off()
    return jsonify(message="Pump turned off!"), 200

@pump_blueprint.route('/speed', methods=['GET'])
@check_sensor
def get_speed():
    current_speed = pump_control.get_speed()
    return jsonify(value=current_speed), 200

@pump_blueprint.route('/stats', methods=['GET'])
@check_sensor
def get_pump_data():
    data = fetch_ina219_data()
    return jsonify(data)
