#!/bin/bash
# Note requires rest API service to be running `python run.py`
# examples:
# curl http://localhost:5000/distance/measure
# curl http://localhost:5000/temperature
# curl http://localhost:5000/pump/stats
#
# The API cannot START the pump (T-489). POST /pump/on and POST /pump/speed
# drove the GPIO with no low-water check, and this process has no route to
# mqtt.py's interlock, so they were removed rather than duplicated. To run the
# pump under the interlock:
#   mosquitto_pub -t "gardyn/pump/command" -m "ON" -u gardyn -P "<password>"

BASE_URL="http://localhost:5000"
CONTENT_TYPE_HEADER="Content-Type: application/json"
SLEEP_DURATION=1

post_data() {
    local endpoint="$1"
    local data="$2"
    
    curl -X POST -H "$CONTENT_TYPE_HEADER" -d "$data" "$BASE_URL$endpoint"
}

get_data() {
    local endpoint="$1"
    
    curl "$BASE_URL$endpoint"
}

control_light() {
    local value="$1"
    
    post_data "/light/brightness" "{\"value\": $value}"
    get_data "/light/brightness"
    sleep "$SLEEP_DURATION"
}

# Light Control
control_light 30
control_light 0

post_data "/light/on" ""
sleep "$SLEEP_DURATION"
post_data "/light/off" ""

# Pump: stop and read only. There is no start endpoint to exercise.
get_data "/pump/speed"
post_data "/pump/off" ""
sleep "$SLEEP_DURATION"
get_data "/pump/stats"

# Distance Measure
get_data "/distance"

# Ambient temp
get_data "/temperature"

# humidity
get_data "/humidity"

# temperature on the PCB in case of the event that
# the motor or lights are causing board to get too hot
# from current draw
get_data "/pcb-temp"
