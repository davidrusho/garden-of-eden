<img src="docs/_banner.svg" width="800px">

# Garden of Eden

Truly own that which is yours!

If you are interested in collaborating please review the [CONTRIBUTORS](CONTRIBUTORS.md) for commit styling guides.

## Video Tutorial for Gardyn of Eden and Homeassistant

Thanks to "Yong" for very well edited video tutorial.

[Video Tutorial](https://www.youtube.com/watch?v=gH5yu8JwS8Y)

## Project Status & Milestones

Work in progress. We should be picking up some steam here to give the DYI community the features you deserve.

[Milestones](https://github.com/iot-root/garden-of-eden/milestones)

![image](https://github.com/user-attachments/assets/403248f5-b7d4-4cb1-921a-0458f515f387)


## Table of Contents

- [Garden of Eden](#garden-of-eden)
  - [Project Status \& Milestones](#project-status--milestones)
  - [Table of Contents](#table-of-contents)
  - [Getting Started](#getting-started)
    - [Prerequisites](#prerequisites)
  - [Usage](#usage)
    - [MQTT with HomeAssistant](#mqtt-with-homeassistant)
    - [Testing](#testing)
    - [Controlling Individual Sensors](#controlling-individual-sensors)
    - [REST API](#rest-api)
      - [Endpoints](#endpoints)
      - [Postman](#postman)
    - [Cron Job](#cron-job)
  - [Hardware Overview](#hardware-overview)
    - [Air Temp \& Humidity Sensor](#air-temp--humidity-sensor)
    - [Pump Power Monitor](#pump-power-monitor)
    - [PCB Temp Sensor](#pcb-temp-sensor)
    - [Lights](#lights)
      - [Method](#method)
      - [Pins](#pins)
    - [Pump](#pump)
      - [Method](#method-1)
      - [Pins](#pins-1)
    - [Camera](#camera)
      - [Method](#method-2)
      - [Devices](#devices)
    - [Water Level Sensor](#water-level-sensor)
      - [Pins](#pins-2)
      - [Method](#method-3)
      - [References](#references)
    - [Momentary Button](#momentary-button)
    - [Electrical Diagrams](#electrical-diagrams)
      - [Sensors](#sensors)
      - [Power and Header](#power-and-header)
    - [Recommendations](#recommendations)
      - [Upgrading the Pi Zero 2](#upgrading-the-pi-zero-2)
  - [Design Decisions](#design-decisions)
    - [Python Version 3.6 \>=](#python-version-36-)
    - [Delays in Reading Temp/Humidity data](#delays-in-reading-temphumidity-data)
    - [GPIO](#gpio)
  - [Folder Structure](#folder-structure)

## Getting Started

### Prerequisites

Start with a clean install of Linux. Use the [RaspberryPi Imager](https://www.raspberrypi.com/software/). Ensure ssh and wifi is setup. Once the image is written, pop the SDcard into the pi and ssh into it.

```bash
# clone repo
git clone git@github.com:iot-root/garden-of-eden.git
cd garden-of-eden 
```

Update the `.env` with mqtt broker info

```
cp .env-dist .env
nano .env
```

Install dependencies, and run services pigpiod, mqtt.service

```
./bin/setup.sh`
```

Ensure the pigpiod daemon is running

```
sudo systemctl status pigpiod
sudo systemctl status mqtt.service
```

### systemd units

Every unit this project ships is a tracked file under
`services/etc/systemd/system/`. `setup.sh` **copies** them into
`/etc/systemd/system/`; it does not generate them, so the repository copy is
the single source of truth and a setup run leaves the working tree clean.

To redeploy the units without re-running the whole setup:

```
./bin/install-systemd-units.sh
```

It installs every unit file in that directory — the list is derived from the
directory, so a new unit needs no edit to the script — enables the ones that
carry an `[Install]` section, and restarts only the units whose file actually
changed. Units with no `[Install]` section are `Type=oneshot` jobs started by
their timer, so they are installed but never enabled directly.

Two flags, both off by default:

```
./bin/install-systemd-units.sh [--remove-retired] [--restart-on-code-change]
```

**`--remove-retired`** disables and deletes deployed units the repository no
longer ships. Without it they are reported and left alone, which is the safer
default but does mean a unit deleted from the repo stays armed on the Pi —
including `gardyn-netwatch`, which can reboot the host. Removal only ever
considers names recorded in `.gardyn-installed-units`, the manifest the script
writes beside the units, so a host that has never run this version can lose
nothing and a unit belonging to another package is never a candidate.

**`--restart-on-code-change`** covers the deploy that changes no unit file at
all. `git pull && ./bin/install-systemd-units.sh` is the usual redeploy, and a
pull that touches only Python leaves every unit byte-identical — so nothing is
restarted and `mqtt.service` goes on running the previous revision. The script
records the checkout's git revision beside the units at the moment it actually
restarts that service, and a later run whose revision differs **exits non-zero
and says so** rather than printing a column of PASS lines. Either restart the
service yourself:

```
sudo systemctl restart mqtt.service
```

…or pass `--restart-on-code-change` and let the installer do it. A checkout
with no git metadata skips the check and reports that it skipped.

> **The shipped units are written for one specific deployment.** They hardcode
> `User=gardyn` and `/home/gardyn/garden-of-eden`, and `gardyn-netwatch` is a
> watchdog that reconnects Wi-Fi and can **reboot the host**. The installer
> will not enable a unit whose `ExecStart` path does not exist on the machine,
> so a checkout somewhere else gets the files but nothing armed. If your paths
> happen to match, delete the two `gardyn-netwatch` units before running setup.

### Configuring the network watchdog

`bin/gardyn-netwatch.py` needs `/etc/gardyn/netwatch.env`, and **there is no
default**. The ping targets, the MQTT probe host and the wlan0 profile UUID
used to be constants in the script — one LAN's topology, published from a
public repository, attached to something that reboots the machine it runs on.

```
sudo install -d -m 0755 /etc/gardyn
sudo install -m 0644 services/etc/gardyn/netwatch.env.example /etc/gardyn/netwatch.env
sudoedit /etc/gardyn/netwatch.env        # replace every CHANGEME
```

Find the wlan0 profile's UUID with `nmcli -g UUID,NAME,DEVICE connection show`.

`bin/install-systemd-units.sh` refuses to enable or start the watchdog unless
that file is *usable*, and exits non-zero saying so — the other units are still
installed and armed, so a missing config never keeps the grow-light controller
down. Usable means more than present: it refuses a missing or empty file, a
directory where the file belongs, a file it cannot read, and — the likely one —
a copy of the template whose values are still `CHANGEME`. Copying and
forgetting to edit would otherwise arm a watchdog that then fails on every
tick, over a completely green install.

Every incomplete state — the file absent, a key missing or blank, a `CHANGEME`
left in place, a port that is not a number, fewer than two ping targets, a
target `ping` would read as an option, a connection name where a UUID belongs —
makes the watchdog refuse to run and exit non-zero, so `systemctl status
gardyn-netwatch` shows a failed unit and the reason lands on the journal:

```
journalctl -t gardyn-netwatch --since -1h
# action=stand_down reason=config_missing_key config_path=/etc/gardyn/netwatch.env ...
```

It will never fall back to a target this repository chose, which is the point:
a watchdog quietly deciding a stranger's network is down and rebooting their
machine is worse than one that does not start.

> **Upgrading an existing install: create the file BEFORE deploying the new
> script.** The timer is already enabled on a host that has been running the
> watchdog, so the installer's refusal does not disarm it — it keeps firing
> every two minutes and every run fails until the config exists. Noisy, and
> with no network watchdog in the meantime. Nothing else is affected: the
> grow-light controller (`mqtt.service`) does not read this file.
>
> That same deploy restarts `mqtt.service`, because its unit file changed. With
> `Type=exec` the installer now blocks on that unit's `execve()` and **fails
> the run if the venv is broken**, where it previously reported success. That
> is the intent, but it means the first run after this change can go red on a
> host whose Python environment was already unusable.

## Usage

## Quick Toggle Guide

> Ensure your press is quick and within the time frame for the action to register correctly. The press time window can be modified directly in the `mqtt.py` file.

- **One Press** (within 1 second): 
  - **Action**: Toggles the **Lights** on or off. 
  - **Description**: A single, swift press will illuminate or darken your space with ease.

- **Two Presses** (within 1 second): 
  - **Action**: Toggles the **Pump** on or off.
  - **Description**: Need to water the garden or fill up the pool? Double tap for action!


### MQTT with HomeAssistant

For homeassistant:

You need a mqtt broker either on the gardyn pi or homeassistant.

To install on the pi run

```
sudo apt-get install mosquitto mosquitto-clients
```

Add mqtt-broker username and password:

`sudo mosquitto_passwd -c /etc/mosquitto/passwd <USERNAME>`

> Note: make sure to update the .env file which is used by `config.py` for `mqtt.py`

Run `sudo nano /etc/mosquitto/mosquitto.conf` and change the following lines to match:

```
allow_anonymous false
password_file /etc/mosquitto/passwd
listener 1883
```


Here are some additional options that you could set in `/etc/mosquitto/mosquitto.conf`:

```
pid_file /run/mosquitto/mosquitto.pid

persistence true
persistence_location /var/lib/mosquitto/

log_dest file /var/log/mosquitto/mosquitto.log

listener 1883 0.0.0.0

allow_anonymous false
password_file /etc/mosquitto/passwd

include_dir /etc/mosquitto/conf.d
```


Restart the service

```
sudo systemctl restart mosquitto
```

you just need to edit the `.env` with the mosquitto username and password created above in /etc/mosquitto/passwd.


Check the configuration works:

`sudo journalctl -xeu mosquitto.service`


If you havent already, run `./bin/setup.sh`, this will install all OS dependencies, install the python libs, and run services pigpiod, mqtt.service

Ensure the pigpiod, mqtt, and broker daemon is running

```
sudo systemctl status pigpiod
sudo systemctl status mqtt.service
sudo systemctl status mosquitto
```

Go to your homeassistant instance:
If your broker is on the gardyn pi, make sure to install the service mqtt, go to settings->devices&services->mqtt and add your gardyn pi host, port, username and password.
The device should then appear in your homeassistant discovery settings.

To test locally on gardyn pi:

Light:

```
mosquitto_pub -t "gardyn/light/command" -m "ON" -u gardyn -P "somepassword"
mosquitto_pub -t "gardyn/light/command" -m "OFF" -u gardyn -P "somepassword"
```

Pump:

```
mosquitto_pub -t "gardyn/pump/command" -m "ON" -u gardyn -P "somepassword"
mosquitto_pub -t "gardyn/pump/command" -m "OFF" -u gardyn -P "somepassword"
```

The pump still answers these and is still gated by the low-water interlock, but
it no longer has a Home Assistant entity — the Gardyn's own pump was replaced by
a third-party unit on a separate smart plug, so the GPIO header drives nothing.
Nothing is published back, so `gardyn/pump/state` will stay silent.

Sensors:

The reservoir can still be probed, but its reading goes to the log rather than
to a topic: the water entities were retired when the fitted DYP-A01A's 28 cm
dead zone turned out to cover the whole plausibility band (see Water Level
Sensor below). So watch the service, not `gardyn/water/level`, which is now
actively cleared and never republished.

Open two terminals on the gardyn pi, in one run:

`journalctl -u mqtt -f`

In the second gardyn pi terminal, run:

`mosquitto_pub -t "gardyn/water/level/get" -m "" -u gardyn -P "somepassword"`

```

### Testing

Activate python venv `source venv/bin/activate`

Start the Flask REST API `python run.py`

Test options:

```bash
# REST endpoints
./bin/api-test.sh

# unit test
python -m unittest -v

# individual tests
python tests/test_distance.py
```

### Controlling Individual Sensors

Activate python venv `source venv/bin/activate`

Examples:

```bash
python app/sensors/distance/distance.py
python app/sensors/humidity/humidity.py
python app/sensors/light/light.py [--on] [--off] [--brightness INT%]
python app/sensors/pcb_temp/pcb_temp.py
python app/sensors/pump/pump.py [--on] [--off] [--speed INT%] [--factory-host STR%] [--factory-port INT%]
python app/sensors/temperature/temperature.py
```

### REST API

Activate python venv `source venv/bin/activate`

Then Run `python run.py`, this will print the ip to send requests.

> **Note:** if run.py errors with: AttributeError: module 'dotenv' has no attribute 'find_dotenv'

```
pip uninstall python-dotenv
python run.py
```

#### Endpoints

```
[GET] http://<pi-ip>:5000/distance

[GET] http://<pi-ip>:5000/humidity

[POST] http://<pi-ip>:5000/light/on
[POST] http://<pi-ip>:5000/light/off
[POST] http://<pi-ip>:5000/light/brightness body:{"value": 50 }
[GET] http://<pi-ip>:5000/light/brightness

[GET] http://<pi-ip>:5000/temperature

[GET] http://<pi-ip>:5000/pcb-temp

[POST] http://<pi-ip>:5000/pump/off
[GET] http://<pi-ip>:5000/pump/speed
[GET] http://<pi-ip>:5000/pump/stats
```

> **The REST API cannot start the pump, by design.** `POST /pump/on` and
> `POST /pump/speed` existed and drove the GPIO with no water check at all. The
> low-water interlock lives in `mqtt.py`'s `start_pump()`, bound to the one
> process that owns the GPIO, so the Flask app can neither call it nor
> reimplement it without a second copy of the same safety decision. The routes
> were removed instead. To run the pump *with* the interlock:
>
> ```
> mosquitto_pub -t "gardyn/pump/command" -m "ON" -u gardyn -P "somepassword"
> ```
>
> Stopping is still served — a control that can refuse to start a pump but
> cannot stop one is not a safety control.
>
> The Postman collection below predates this and still lists the removed
> routes.

#### Postman

Export this [Postman collection](https://www.postman.com/orange-shadow-8689/workspace/garden-of-eden/collection/8244324-e9d8f79e-d3f2-423e-b0d1-a4ca5b1b08ca?action=share&creator=8244324&active-environment=8244324-861384b4-b4e3-48a3-8da1-181705bd2d8c), add to your private workspace, add the `pi-ip` env variable and you should be good to go.

### Cron Job

Run `crontab -e`, select your preferred editor and then add the following job. Edit as needed.

> Note: update your paths for the following...

> **The pump entries below are NOT interlocked.** `pump.py --on` is the raw
> driver and makes no water check — the same gap that removed the REST API's
> start routes (T-489), and `bin/water.sh` inherits it because it shells out to
> the same CLI. Nothing in this repository schedules them; they are upstream
> example content. Prefer a Home Assistant automation publishing
> `gardyn/pump/command`, which is gated by `start_pump()`. If you do install
> these, understand that they will run the pump on an empty reservoir.

```text
# †urn on lights at 6am, 9am, 5pm, and turn off at 8pm
0 6 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/light/light.py --on --brightness 50
0 9 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/light/light.py --on --brightness 70
0 17 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/light/light.py --on --brightness 50
0 20 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/light/light.py --off

# Pump run at 8am for 5 minutes
0 8 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/pump/pump.py --on --speed 100
5 8 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/pump/pump.py --off

# Pump run at 4pm 5 minutes
0 16 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/pump/pump.py --on --speed 100
5 16 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/pump/pump.py --off

# Pump run at 9pm for 5 minutes
0 21 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/pump/pump.py --on --speed 100
5 21 * * * /home/gardyn/projects/garden-of-eden/venv/bin/python /home/gardyn/projects/garden-of-eden/app/sensors/pump/pump.py --off

# Collect sensor data every 30 mins
*/30 * * * * /home/gardyn/projects/garden-of-eden/bin/get-sensor-data.sh
```

## Hardware Overview

Depending on the system you have, here is a breakdown of the hardware.

Notes:

- GPIO num is different than pin number. See (<https://pinout.xyz/>)

### Air Temp & Humidity Sensor

- temp/humidity sensor AM2320 at address of `0x38`

### Pump Power Monitor

- motor power usage sensor INA219 at address of `0x40`

### PCB Temp Sensor

- pcb temp sensor PCT2075 at address `pf 0x48`

When you run `sudo i2cdetect -y 1`, you should see something like:

```
     0  1  2  3  4  5  6  7  8  9  a  b  c  d  e  f
00:          -- -- -- -- -- -- -- -- -- -- -- -- --
10: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
20: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
30: -- -- -- -- -- -- -- -- 38 -- -- -- -- -- -- --
40: 40 -- -- -- -- -- -- -- 48 -- -- -- -- -- -- --
50: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
60: -- -- -- -- -- -- -- -- -- -- -- -- -- -- -- --
70: -- -- -- -- -- -- -- --
```

### Lights

LED full spectrum lights.

#### Method

- Lights are driven by PWM duty and a frequency of 8 kHz.

#### Pins

- [GPIO-18 | PIN-12](https://pinout.xyz/pinout/pin12_gpio18/)

### Pump

#### Method

- The pump is driven by PWM with max duty of 30% and frequency of 50 Hz
- There is a current sensor to measure pump draw and a overtemp sensor to determine if board monitor PCB temp.

#### Pins

- [GPIO-24 | PIN-18](https://pinout.xyz/pinout/pin18_gpio24/)

Notes:

- Pump duty cycle is limited, likely full on is too much current draw for the system.

### Camera

Two USB cameras.

#### Method

- image capture with fswebcam

#### Devices

- /dev/video0
- /dev/video1

### Water Level Sensor

Uses the ultrasonic distance sensor DYP-A01-V2.0.

> **Minimum measuring distance is 28 cm.** Per DYP, the A01A series has a 28 cm
> dead zone, and anything closer is reported as 28 cm rather than as an error.
> If the sensor sits closer than 28 cm to the water at any fill level, it keeps
> returning 28 cm instead of a value that would show something is wrong. Mount
> it high enough to clear the dead zone across the full range, or use a sensor
> with a shorter minimum for a shallow reservoir.

#### Pins

- [GPIO-19 | PIN-35](https://pinout.xyz/pinout/pin35_gpio19/): water level in (trigger)
- [GPIO-26 | PIN-37](https://pinout.xyz/pinout/pin37_gpio26/): water level out (echo)

#### Method

- Uses time between the echo and response to determine the distances.

#### References

- [DYP-A01 product page](https://www.dypcn.com/high-performance-ultrasonic-precision-rangefinder-dyp-a01-product/):
  measuring range 280 mm to 7500 mm, 28 cm dead zone
- <https://www.google.com/search?q=DYP-A01-V2.0>

### Momentary Button

`<section incomplete>`

### Electrical Diagrams

Incase you need to troubleshoot any problems with your system.

#### Sensors

<img src="docs/pcb1.png" width="800px">

#### Power and Header

<img src="docs/pcb2.png" width="800px">

### Recommendations

#### Upgrading the Pi Zero 2

For better performance, the Pi Zero can be replaced with a Pi Zero 2. This will enable the use of VS Code Remote Server to edit files and debug the python code remotely. The VS Code remote server uses OpenSSH and the minimum architecture is ARMv7.

> Buy one **without** a header, you will need to solder one on in the opposite direction.

## Design Decisions

### Python Version 3.6 >=

Minimum python version of 3.6 to support `printf()`

### Delays in Reading Temp/Humidity data

Reading sensor values  with inherently long delays and responding to the REST API. To minimize the delay in subsequent readings the value is cached and given if another read occurs within two seconds.

### GPIO

Using `gpiozero` to leverage `pigpio` daemon which is hardware driven and more efficient.This ensures better accuracy of the distance sensor and is less cpu intensive when using PWMs.

## Folder Structure

```text
<gardyn-of-eden>
├── run.py
├── app
│   ├── __init__.py
│   └── sensors
│       ├── config.py
│       ├── distance
│       │   ├── distance.py
│       │   ├── __init__.py
│       │   └── routes.py
│       ├── __init__.py
│       ├── light
│       │   ├── __init__.py
│       │   ├── light.py
│       │   └── routes.py
│       └── pump
│           ├── __init__.py
│           ├── pump.py
│           └── routes.py
└── tests
    ├── __init__.py
    ├── test_distance.py
    ├── test_light.py
    └── test_pump.py
```
