#!/usr/bin/env python3
import time
import statistics
import requests
import RPi.GPIO as GPIO
from collections import Counter
from datetime import datetime

# ------------- NEW: MQTT imports/config -------------
import json
import paho.mqtt.client as mqtt

MQTT_BROKER = "192.168.0.31"   # <--- change here if broker IP changes
MQTT_PORT = 1883
CLIENT_ID = "pi-queue-01"      # give each Pi a unique ID
MQTT_TOPIC_PERSEC = "park/ride1/queue/seconds"
MQTT_STATUS_TOPIC = f"park/ride1/status/{CLIENT_ID}"
# ----------------------------------------------------

# ================= USER SETTINGS =================
GPIO.setmode(GPIO.BOARD)
GPIO.setwarnings(False)

TRIG_PIN = 7       # BOARD 7  -> SR04P TRIG
ECHO_PIN = 11      # BOARD 11 -> SR04P ECHO (use a level shifter/voltage divider to 3.3V)

THINGSPEAK_API_KEY = "LDTWH8ZB2Q4VMBU7"
THINGSPEAK_URL = "https://api.thingspeak.com/update"
# (ThingSpeak is not used in this publisher; aggregator will push to TS.)

# Distance bands (in cm) -> wait time (minutes)
BAND_15_MAX_CM = 60     # 0..60cm   => 15 minutes
BAND_10_MAX_CM = 120    # 60..120cm => 10 minutes
# >120cm => 5 minutes

SAMPLES_PER_READING = 5     # median over N pings each second
PER_SECOND_INTERVAL = 1.0   # read/print every second
WINDOW_SECONDS = 15         # still used only for debug summary prints
ECHO_TIMEOUT_SEC = 0.03     # per phase (start/end) timeout seconds
SPEED_OF_SOUND_CM_S = 34300 # ~20°C;
# =================================================

def setup_gpio():
    GPIO.setup(TRIG_PIN, GPIO.OUT)
    GPIO.setup(ECHO_PIN, GPIO.IN)
    GPIO.output(TRIG_PIN, GPIO.LOW)
    time.sleep(0.1)  # settle

def cleanup_gpio():
    GPIO.cleanup()

def _wait_for_level(pin, level, timeout_s):
    start = time.time()
    while GPIO.input(pin) != level:
        if time.time() - start > timeout_s:
            return False
    return True

def read_distance_once_cm():
    # 10 µs trigger pulse
    GPIO.output(TRIG_PIN, GPIO.LOW)
    time.sleep(0.000002)
    GPIO.output(TRIG_PIN, GPIO.HIGH)
    time.sleep(0.00001)
    GPIO.output(TRIG_PIN, GPIO.LOW)

    # Echo start
    if not _wait_for_level(ECHO_PIN, 1, ECHO_TIMEOUT_SEC):
        return None
    t_start = time.time()

    # Echo end
    if not _wait_for_level(ECHO_PIN, 0, ECHO_TIMEOUT_SEC):
        return None
    t_end = time.time()

    duration = t_end - t_start
    distance_cm = (duration * SPEED_OF_SOUND_CM_S) / 2.0
    return distance_cm

def read_distance_filtered_cm(samples=SAMPLES_PER_READING):
    vals = []
    attempts = 0
    while len(vals) < samples and attempts < samples * 3:
        d = read_distance_once_cm()
        attempts += 1
        if d is not None and 2.0 <= d <= 400.0:  # typical SR04P sanity bounds
            vals.append(d)
        time.sleep(0.02)
    if not vals:
        return None
    return statistics.mode(vals)

def classify_wait_time(distance_cm):
    """Return one of {5, 10, 15} minutes."""
    if distance_cm <= BAND_15_MAX_CM:
        return 15
    elif distance_cm <= BAND_10_MAX_CM:
        return 10
    else:
        return 5

def longest_consecutive_run_length(seq, value):
    """Length of the longest consecutive run of 'value' in seq."""
    best = cur = 0
    for x in seq:
        if x == value:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best

def choose_most_consistent(window_classes, window_distances):
    """
    Decide a representative wait_time and distance for a 15s window.
    Strategy:
      1) Mode of classes.
      2) Tie-breaker: class with longest consecutive run.
      3) Final tie: choose the smallest class (5 < 10 < 15) conservatively.
      Distance: median among readings of the chosen class.
    """
    counts = Counter(window_classes).most_common()
    top = counts[0][1]
    cands = [cls for cls, c in counts if c == top]

    if len(cands) == 1:
        chosen_class = cands[0]
    else:
        runs = {c: longest_consecutive_run_length(window_classes, c) for c in cands}
        max_run = max(runs.values())
        run_cands = [c for c, r in runs.items() if r == max_run]
        chosen_class = min(run_cands)

    dists_for_class = [d for d, c in zip(window_distances, window_classes) if c == chosen_class]
    rep_distance = statistics.median(dists_for_class) if dists_for_class else statistics.median(window_distances)
    return chosen_class, rep_distance

# ------------- NEW: MQTT helper -------------
def mqtt_setup():
    client = mqtt.Client(client_id=CLIENT_ID, clean_session=True)
    # client.username_pw_set("user","pass")  # if you enable auth later
    client.will_set(MQTT_STATUS_TOPIC, payload="offline", qos=1, retain=True)
    client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    client.loop_start()
    client.publish(MQTT_STATUS_TOPIC, "online", qos=1, retain=True)
    return client

def mqtt_publish_per_second(client, wait_min, distance_cm):
    payload = {
        "ts": int(time.time()),
        "wait_min": int(wait_min),
        "distance_cm": float(f"{distance_cm:.1f}")
    }
    client.publish(MQTT_TOPIC_PERSEC, json.dumps(payload), qos=1, retain=False)
# --------------------------------------------

# (ThingSpeak function left here but UNUSED in this publisher.)
def update_thingspeak(wait_minutes, distance_cm):
    try:
        params = {
            "api_key": THINGSPEAK_API_KEY,
            "field5": str(wait_minutes),         # waitingtime
            "field6": f"{distance_cm:.1f}",      # distance in cm
        }
        r = requests.get(THINGSPEAK_URL, params=params, timeout=6)
        return r.status_code, r.text.strip()
    except Exception as e:
        return None, f"Error: {e}"

def main():
    print("Queue Wait-Time Sensor (publisher) : starting…")
    print(f"Bands: <= {BAND_15_MAX_CM}cm = 15min, <= {BAND_10_MAX_CM}cm = 10min, else 5min")
    setup_gpio()

    # NEW: start MQTT
    mqtt_client = mqtt_setup()

    window_classes = []   # for optional 15s debug summary only
    window_distances = []

    try:
        last_summary = time.time()
        while True:
            t0 = time.time()
            d_cm = read_distance_filtered_cm()
            ts = datetime.now().strftime("%H:%M:%S")

            if d_cm is None:
                print(f"[{ts}] No echo / timeout")
            else:
                wait_min = classify_wait_time(d_cm)
                print(f"[{ts}] distance={d_cm:.1f} cm  -> wait={wait_min} min")

                # NEW: publish every second to MQTT (aggregator will forward to ThingSpeak)
                mqtt_publish_per_second(mqtt_client, wait_min, d_cm)

                # keep some debug history for a 15s console summary (no TS upload here)
                window_distances.append(d_cm)
                window_classes.append(wait_min)

            # Optional: print a 15s “most consistent” summary for debugging
            if (time.time() - last_summary) >= WINDOW_SECONDS:
                if window_classes:
                    chosen_wait, rep_distance = choose_most_consistent(window_classes, window_distances)
                    print("--- 15s debug summary (publisher) ---")
                    print(f"Wait Time: {chosen_wait} minutes, Distance={rep_distance:.1f} cm, n={len(window_classes)}")
                else:
                    print("--- 15s debug summary ---")
                    print("No valid readings in window.")
                window_classes.clear()
                window_distances.clear()
                last_summary = time.time()
                print("---------------------------------------")

            # sleep to maintain ~1Hz loop (account for work time)
            elapsed = time.time() - t0
            time.sleep(max(0.0, PER_SECOND_INTERVAL - elapsed))

    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        try:
            mqtt_client.publish(MQTT_STATUS_TOPIC, "offline", qos=1, retain=True)
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        except Exception:
            pass
        print("Cleaning up GPIO…")
        cleanup_gpio()

if __name__ == "__main__":
    main()

