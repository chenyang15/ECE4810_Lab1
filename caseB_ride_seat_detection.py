#!/usr/bin/env python3
import time
import requests
import RPi.GPIO as GPIO

# ============= NEW: MQTT imports / config =============
import json
import paho.mqtt.client as mqtt

#MQTT_BROKER = "192.168.129.48"   # <--- set your broker IP here
#MQTT_BROKER = "192.168.0.220" #D7
MQTT_BROKER = "172.20.10.2" #ryans hotspot
MQTT_PORT = 1883
CLIENT_ID = "pi-ride-ctrl"     # make unique per Pi
MQTT_TOPIC_STATE = "park/ride1/ride/state"
MQTT_TOPIC_STATUS = f"park/ride1/status/{CLIENT_ID}"
# ======================================================

# =========================
# ===== USER SETTINGS =====
# =========================
# BOARD numbering (change if you prefer BCM)
ENT_TRIGGER = 13     # Entrance sensor TRIG
ENT_ECHO    = 15     # Entrance sensor ECHO
EXT_TRIGGER = 7      # Exit sensor TRIG
EXT_ECHO    = 11     # Exit sensor ECHO

LED_GREEN = 29
LED_YELLOW = 31 
LED_RED = 33
SEAT1 = 35
SEAT2 = 37
SEAT3 = 36
SEAT4 = 38

SEAT_CAPACITY       = 4
NEAR_THRESH_CM      = 60.0   # Tune on-site
SAMPLE_PERIOD_S     = 0.05   # ~20 Hz sensor poll
PERSON_COOLDOWN_S   = 1.0    # Debounce so one person counts once
GROUP_GAP_S         = 5.0    # Defines a batch (internal)
BOARDING_WINDOW_S   = 30.0   # Restarted on each new entry
RIDE_DURATION_S     = 30.0   # Ride run time

# ---- (ThingSpeak removed; aggregator will handle cloud) ----

# =========================
# ===== GPIO / SENSOR =====
# =========================
def init_gpio():
    GPIO.setmode(GPIO.BOARD)
    GPIO.setwarnings(False)
    for trig, echo in [(ENT_TRIGGER, ENT_ECHO), (EXT_TRIGGER, EXT_ECHO)]:
        GPIO.setup(trig, GPIO.OUT)
        GPIO.setup(echo, GPIO.IN)
        GPIO.output(trig, GPIO.LOW)
    # LEDs & seat indicators
    GPIO.setup(LED_GREEN, GPIO.OUT)
    GPIO.setup(LED_YELLOW, GPIO.OUT)
    GPIO.setup(LED_RED, GPIO.OUT)
    GPIO.setup(SEAT1, GPIO.OUT)
    GPIO.setup(SEAT2, GPIO.OUT)
    GPIO.setup(SEAT3, GPIO.OUT)
    GPIO.setup(SEAT4, GPIO.OUT)
    time.sleep(0.2)

def cleanup_gpio():
    GPIO.cleanup()

def read_distance_cm(trigger_pin, echo_pin, timeout_s=0.03):
    """Single distance read in cm; returns None on timeout."""
    GPIO.output(trigger_pin, GPIO.HIGH)
    time.sleep(0.00001)
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
        d = read_distance_cm(self.trig, self.echo)
        if d is None:
            return 0
        is_near = d <= self.near
        count = 0
        if is_near and not self.was_near:
            now = time.monotonic()
            if (now - self.last_count_time) >= self.cooldown:
                count = 1
                self.last_count_time = now
        self.was_near = is_near
        return count

# =========================
# ===== MQTT helper =======
# =========================
def mqtt_setup():
    client = mqtt.Client(client_id=CLIENT_ID, clean_session=True)
    # client.username_pw_set("user", "pass")  # if you enable auth on broker
    client.will_set(MQTT_TOPIC_STATUS, payload="offline", qos=1, retain=True)
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.loop_start()
    # announce online
    client.publish(MQTT_TOPIC_STATUS, "online", qos=1, retain=True)
    return client

def mqtt_publish_state(client, green_flag, seats, yellow_flag, red_flag):
    payload = {
        "green": int(green_flag),
        "seats": int(seats),
        "yellow": int(yellow_flag),
        "red": int(red_flag),
        "ts": int(time.time())
    }
    client.publish(MQTT_TOPIC_STATE, json.dumps(payload), qos=1, retain=False)

# =========================
# ===== RIDE LOGIC ========
# =========================
class RideController:
    STATE_GREEN   = "GREEN"       # boarding open (green lamp)
    STATE_RIDING  = "RIDING"      # ride in progress (yellow lamp)
    STATE_UNLOAD  = "UNLOADING"   # waiting for exits (red lamp)

    def __init__(self, mqtt_client):
        self.mqtt = mqtt_client
        self.state = self.STATE_GREEN

        self.remaining = SEAT_CAPACITY
        self.entered_this_cycle = 0
        self.exited_this_cycle = 0

        self.boarding_deadline = None
        self.ride_end_time = None

        # Batch detection (internal only)
        self.current_batch_size = 0
        self.last_entry_time = None

        print("[SYSTEM] Initialized. State = GREEN, seats =", self.remaining)
        self._publish(force=True)

    # ----- Publishing -----
    def _publish(self, force=False):
        green = 1 if self.state == self.STATE_GREEN else 0
        yellow = 1 if self.state == self.STATE_RIDING else 0
        red = 1 if self.state == self.STATE_UNLOAD else 0

        # Local LEDs
        GPIO.output(LED_GREEN, GPIO.HIGH if green else GPIO.LOW)
        GPIO.output(LED_YELLOW, GPIO.HIGH if yellow else GPIO.LOW)
        GPIO.output(LED_RED, GPIO.HIGH if red else GPIO.LOW)

        # MQTT (every call; aggregator will throttle to ThingSpeak)
        mqtt_publish_state(self.mqtt, green, self.remaining, yellow, red)

    # ----- Events -----
    def on_entry(self):
        if self.state != self.STATE_GREEN:
            return
        if self.remaining <= 0:
            print("[ENTRY] Person ignored — ride full.")
            return

        self.entered_this_cycle += 1
        self.remaining = max(0, SEAT_CAPACITY - self.entered_this_cycle)

        # Seat indicator LEDs
        if self.remaining == 4:
            GPIO.output(SEAT1, GPIO.LOW);  GPIO.output(SEAT2, GPIO.LOW)
            GPIO.output(SEAT3, GPIO.LOW);  GPIO.output(SEAT4, GPIO.LOW)
        elif self.remaining == 3:
            GPIO.output(SEAT1, GPIO.HIGH); GPIO.output(SEAT2, GPIO.LOW)
            GPIO.output(SEAT3, GPIO.LOW);  GPIO.output(SEAT4, GPIO.LOW)
        elif self.remaining == 2:
            GPIO.output(SEAT1, GPIO.HIGH); GPIO.output(SEAT2, GPIO.HIGH)
            GPIO.output(SEAT3, GPIO.LOW);  GPIO.output(SEAT4, GPIO.LOW)
        elif self.remaining == 1:
            GPIO.output(SEAT1, GPIO.HIGH); GPIO.output(SEAT2, GPIO.HIGH)
            GPIO.output(SEAT3, GPIO.HIGH); GPIO.output(SEAT4, GPIO.LOW)
        elif self.remaining == 0:
            GPIO.output(SEAT1, GPIO.HIGH); GPIO.output(SEAT2, GPIO.HIGH)
            GPIO.output(SEAT3, GPIO.HIGH); GPIO.output(SEAT4, GPIO.HIGH)

        print(f"[ENTRY] Person entered. Seats remaining = {self.remaining}")

        now = time.monotonic()
        if self.current_batch_size == 0:
            self.current_batch_size = 1
        else:
            if self.last_entry_time and (now - self.last_entry_time) <= GROUP_GAP_S:
                self.current_batch_size += 1
            else:
                print(f"[BATCH] Finalized batch of {self.current_batch_size}")
                self.current_batch_size = 1
        self.last_entry_time = now

        # Start/reset boarding window
        self.boarding_deadline = now + BOARDING_WINDOW_S

        if self.remaining == 0:
            GPIO.output(LED_YELLOW, GPIO.HIGH)
            print("[STATE] All seats filled — starting ride now.")
            self._start_ride()
        else:
            self._publish()

    def on_exit(self):
        if self.state != self.STATE_UNLOAD:
            return
        self.exited_this_cycle += 1
        # Clear seat LEDs on each exit (your choice)
        GPIO.output(SEAT1, GPIO.LOW); GPIO.output(SEAT2, GPIO.LOW)
        GPIO.output(SEAT3, GPIO.LOW); GPIO.output(SEAT4, GPIO.LOW)

        print(f"[EXIT] Person exited. Total exited = {self.exited_this_cycle}/{self.entered_this_cycle}")
        if self.exited_this_cycle >= self.entered_this_cycle:
            GPIO.output(LED_GREEN, GPIO.HIGH)
            GPIO.output(LED_YELLOW, GPIO.LOW)
            GPIO.output(LED_RED, GPIO.LOW)
            print("[STATE] All riders exited. Resetting to GREEN.")
            self._reset_to_green()
        else:
            self._publish()  # still unloading

    def tick(self):
        now = time.monotonic()
        # Publish a heartbeat snapshot ~every second from main loop outside as well
        if self.state == self.STATE_GREEN:
            if self.entered_this_cycle > 0 and self.boarding_deadline and now >= self.boarding_deadline:
                print("[STATE] Boarding window expired. Starting ride.")
                self._start_ride()

        elif self.state == self.STATE_RIDING:
            if now >= self.ride_end_time:
                GPIO.output(LED_YELLOW, GPIO.LOW)
                print("[STATE] Ride finished. Waiting for riders to exit.")
                self.state = self.STATE_UNLOAD
                self._publish()

        elif self.state == self.STATE_UNLOAD:
            # Waiting for exit counts to match — no timer here
            pass

    # ----- Transitions -----
    def _start_ride(self):
        self.state = self.STATE_RIDING
        self.ride_end_time = time.monotonic() + RIDE_DURATION_S
        self.boarding_deadline = None
        self.current_batch_size = 0
        GPIO.output(LED_YELLOW, GPIO.HIGH)
        print("[STATE] Ride started. Running for", RIDE_DURATION_S, "seconds. (Yellow lamp ON)")
        self._publish()

    def _reset_to_green(self):
        self.state = self.STATE_GREEN
        self.remaining = SEAT_CAPACITY
        self.entered_this_cycle = 0
        self.exited_this_cycle = 0
        self.boarding_deadline = None
        self.ride_end_time = None
        self.current_batch_size = 0
        self.last_entry_time = None
        GPIO.output(LED_GREEN, GPIO.HIGH)
        print("[STATE] Reset complete. Boarding open. Seats =", self.remaining, "(GREEN lamp ON)")
        self._publish()

# =========================
# ========= MAIN ==========
# =========================
def main():
    init_gpio()
    mqtt_client = mqtt_setup()
    try:
        ent = PersonCounter(ENT_TRIGGER, ENT_ECHO, NEAR_THRESH_CM, PERSON_COOLDOWN_S)
        ext = PersonCounter(EXT_TRIGGER, EXT_ECHO, NEAR_THRESH_CM, PERSON_COOLDOWN_S)
        ctrl = RideController(mqtt_client)

        print("Ride controller running (publishing to MQTT). Ctrl+C to stop.")
        last_heartbeat = 0.0
        while True:
            if ent.poll():
                ctrl.on_entry()
            if ext.poll():
                ctrl.on_exit()
            ctrl.tick()

            # Publish a heartbeat snapshot once per second so aggregator always has a fresh value
            now = time.monotonic()
            if (now - last_heartbeat) >= 1.0:
                green = 1 if ctrl.state == ctrl.STATE_GREEN else 0
                yellow = 1 if ctrl.state == ctrl.STATE_RIDING else 0
                red = 1 if ctrl.state == ctrl.STATE_UNLOAD else 0
                mqtt_publish_state(mqtt_client, green, ctrl.remaining, yellow, red)
                last_heartbeat = now

            time.sleep(SAMPLE_PERIOD_S)

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        try:
            mqtt_client.publish(MQTT_TOPIC_STATUS, "offline", qos=1, retain=True)
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        except Exception:
            pass
        cleanup_gpio()

if __name__ == "__main__":
    main()




