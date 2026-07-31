# distance.py
#
# Reviewed: 2026-07-31 against b0f8f92 (T-472)

from gpiozero import DistanceSensor
from gpiozero.pins.pigpio import PiGPIOFactory
from time import sleep

class MeasurementError(Exception):
    """
    Raised when there's an error in distance measurement.
    """
    def __init__(self, message):
        super().__init__(message)


class Distance:
    """
    Class to handle distance measurements using the Raspberry Pi GPIO with gpiozero.

    Attributes:
        sensor (DistanceSensor): The DistanceSensor object for distance measurements.
        pin_factory (PiGPIOFactory): The factory for GPIO pin configuration.
    """

    def __init__(self, pin_factory=None):
        """
        Initializes the DistanceSensor object with the specified or default pin factory.

        Args:
            pin_factory (PiGPIOFactory, optional): A custom pin factory for GPIO configuration. Defaults to PiGPIOFactory.

        Raises:
            MeasurementError: If the DistanceSensor fails to initialize.
        """
        self.pin_factory = pin_factory if pin_factory else PiGPIOFactory()
        try:
            # partial=True is a safety requirement, not a tuning choice.
            #
            # gpiozero samples this sensor on its own background thread
            # (GPIOQueue.fill) and drops None readings, which is what a no-echo
            # returns. Reading `.distance` just medians whatever that queue
            # holds - it fires no pulse of its own. With the default
            # partial=False, the read instead waits on GPIOQueue.full.wait()
            # with NO TIMEOUT until nine real samples have accumulated, and on a
            # sensor that is silent from process start they never do.
            #
            # That wait is worse than a stalled reader, because of how pigpio is
            # wired: one PiGPIOFactory means one pigpio.pi(), which means ONE
            # callback thread, and gpiozero dispatches Button.when_pressed
            # inline on it - the same thread that delivers this sensor's echo
            # edges. So a button double-press that ends up reading the sensor
            # would block the very thread that fills the queue it is waiting on.
            # Self-deadlock, permanent, taking every GPIO event with it. The
            # paho network thread has the same shape with all MQTT handling.
            #
            # partial=True returns whatever samples exist - and 0.0 when there
            # are none, which is the FULL-TANK end of the scale. Nothing in this
            # class can tell that apart from a real reading, so the caller's
            # plausibility band is what has to reject it. See the band's minimum
            # in config.py: it is the only guard against a dead sensor reading
            # as a full reservoir, which is why it is validated to be > 0.
            self.sensor = DistanceSensor(
                echo=19, trigger=26, partial=True, pin_factory=self.pin_factory
            )
        except Exception as e:
            raise MeasurementError(f"Failed to initialize DistanceSensor: {e}")

    def sample_count(self):
        """How many real samples gpiozero's averaging queue currently holds.

        Zero means the background sampler has not produced a usable reading yet
        - either the process just started, or the sensor has never echoed. A
        measurement taken in that state is 0.0 and means nothing, and "not ready
        yet" is a different thing from "reading is implausible": the first
        should publish no verdict at all, the second should publish a negative
        one.

        Reaches into gpiozero internals because 2.0.1 exposes no public
        equivalent. Guarded so a version change degrades to "assume ready"
        rather than raising.
        """
        try:
            return len(self.sensor._queue.queue)
        except Exception:
            return None

    def measure_once(self):
        """
        Measures the distance once.

        Returns:
            float: The measured distance in centimeters.

        Raises:
            MeasurementError: If the measurement fails.
        """
        try:
            distance = self.sensor.distance * 100  # Convert to cm
            return round(distance, 2)
        except Exception as e:
            raise MeasurementError(f"Measurement failed: {e}")

    def measure(self, samples=10, interval=0.07):
        """
        Sample repeatedly over time and return the median, in cm.

        Only meaningful if `interval` exceeds gpiozero's own sample period
        (sample_wait=0.06 s). Reading `.distance` returns the median of an
        already-smoothed rolling queue, so ten back-to-back reads return the
        same number ten times and average nothing - the default interval is what
        makes the samples independent.

        Prefer measure_once() on a latency-sensitive thread. This costs
        samples * interval (~0.7 s by default), and on the pigpio callback
        thread that delay also stalls this sensor's own echo edges.

        Raises:
            MeasurementError: If no successful measurements are obtained.
        """
        measurements = []
        for i in range(samples):
            try:
                measurements.append(self.measure_once())
            except MeasurementError:
                pass  # Handle individual measurement errors gracefully
            if i < samples - 1:
                sleep(interval)
        if not measurements:
            raise MeasurementError("No successful measurements")
        median_value = self.median(measurements)
        return round(sum(median_value) / len(median_value), 2)

    def median(self, data):
        """
        Returns the median value from a list of numbers.

        Args:
            data (list): A list of distance measurements.

        Returns:
            list[float]: A list containing the median value.

        Raises:
            MeasurementError: If the data is invalid.
        """
        if not isinstance(data, list) or not data:
            raise MeasurementError("Invalid data for median calculation")
        sorted_data = sorted(data)
        data_length = len(data)
        if data_length % 2 > 0:
            return [sorted_data[data_length // 2]]
        else:
            mid = data_length // 2
            return [(sorted_data[mid - 1] + sorted_data[mid]) / 2]

if __name__ == "__main__":
    """
    If the module is executed as a standalone script, it will return the distance in a telegraf friendly format.
    """
    try:
        distance_instance = Distance()
        distance = distance_instance.measure()
        print(f"distance, value={distance:.2f}")
    except MeasurementError as e:
        print(f"Error: {e}")
    except KeyboardInterrupt:
        print("Script interrupted.")
