#!/usr/bin/env python3
import time, json
import RPi.GPIO as GPIO
import paho.mqtt.client as mqtt

# ====== CONFIG ======

SENSOR_ID   = "B"                 # <-- change per Pi: "B", "C", "D", or "E"

MQTT_BROKER = "192.168.0.31"
MQTT_PORT   = 1883
CLIENT_ID   = f"pi-sensor-{SENSOR_ID}"
TOPIC_PEOPLE = f"park/ride1/sensors/{SENSOR_ID}/people"

# Ultrasonic pins (BOARD numbering) — change as wired
PIN_TRIGGER = 7
PIN_ECHO    = 11

NEAR_THRESH_CM   = 60.0   # distance threshold for "person near"
COOLDOWN_S       = 1.0    # debounce time (so same person not counted twice)
PUBLISH_PERIOD_S = 1.0   # publish people count every 1s

# ====================

def gpio_setup():
    GPIO.setmode(GPIO.BOARD)
    GPIO.setwarnings(False)
    GPIO.setup(PIN_TRIGGER, GPIO.OUT)
    GPIO.setup(PIN_ECHO, GPIO.IN)
    GPIO.output(PIN_TRIGGER, GPIO.LOW)
    time.sleep(0.2)

def gpio_cleanup():
    GPIO.cleanup()

def read_distance_cm(trigger_pin, echo_pin, timeout_s=0.03):
    """Single distance read in cm; returns None on timeout."""
    GPIO.output(trigger_pin, GPIO.HIGH)
    time.sleep(0.00001)  # 10 µs pulse
    GPIO.output(trigger_pin, GPIO.LOW)

    t0 = time.monotonic()
    while GPIO.input(echo_pin) == 0:
        if time.monotonic() - t0 > timeout_s:
            return None
    pulse_start = time.monotonic()

    while GPIO.input(echo_pin) == 1:
        if time.monotonic() - pulse_start > timeout_s:
            return None
    pulse_end = time.monotonic()

    return (pulse_end - pulse_start) * 17150.0  # cm

class PersonCounter:
    """Debounced near-edge counter for an ultrasonic sensor."""
    def __init__(self, trig, echo, near_thresh_cm, cooldown_s):
        self.trig = trig
        self.echo = echo
        self.near = near_thresh_cm
        self.cooldown = cooldown_s
        self.was_near = False
        self.last_count_time = 0.0

    def poll(self):
        """Poll once. Returns 1 if a new person is detected, else 0."""
        d = read_distance_cm(self.trig, self.echo)
        if d is None:
            return 0
        is_near = d <= self.near
        count = 0
        # Edge detection + cooldown prevents multiple counts per person
        if is_near and not self.was_near:
            now = time.monotonic()
            if (now - self.last_count_time) >= self.cooldown:
                count = 1
                self.last_count_time = now
        self.was_near = is_near
        return count

# =========================

def mqtt_setup():
    c = mqtt.Client(client_id=CLIENT_ID, clean_session=True)
    c.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    c.loop_start()
    return c

def main():
    gpio_setup()
    client = mqtt_setup()
    # setup counter as an object of class PersonCounter to be used later on to call the poll function to detect people
    counter = PersonCounter(PIN_TRIGGER, PIN_ECHO, NEAR_THRESH_CM, COOLDOWN_S)

    print(f"[{SENSOR_ID}] people counter running…")

    people_count = 0
    last_pub_time = time.time()

    try:
        while True:
            # poll sensor once
            people_count += counter.poll()

            # check if it's time to publish (every 1s)
            now = time.time()
            if (now - last_pub_time) >= PUBLISH_PERIOD_S:
                payload = {
                    "people_count": people_count,
                    "ts": int(now)  # timestamp of publish
                }
                client.publish(TOPIC_PEOPLE, json.dumps(payload), qos=1, retain=False)
                print(f"[{SENSOR_ID}] Published people_count={people_count} at ts={payload['ts']}")
                # reset for next window
                people_count = 0
                last_pub_time = now

            time.sleep(0.1)  # ~10 Hz polling loop

    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop(); client.disconnect()
        gpio_cleanup()

if __name__ == "__main__":
    main()
