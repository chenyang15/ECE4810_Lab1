#!/usr/bin/env python3
import time, json, requests
import RPi.GPIO as GPIO
import paho.mqtt.client as mqtt
import threading

# ====== CONFIG ======
MQTT_BROKER = "192.168.0.31"
MQTT_PORT   = 1883
CLIENT_ID   = "pi-aggregator"
TOPIC_STATUS = f"park/ride1/status/{CLIENT_ID}"

TOPIC_PREFIX = "park/ride1/gates"
REMOTE_GATES = ["A_in", "B_in", "A2B", "B2A"]
TOPICS = [f"{TOPIC_PREFIX}/{gate}" for gate in REMOTE_GATES]

THINGSPEAK_WRITE_KEY = "8YOHE78US7GHV774"
THINGSPEAK_URL = "https://api.thingspeak.com/update"
PUSH_INTERVAL_S = 1.0   # keep ThingSpeak push every 1s

TELEGRAM_TOKEN = "8205472041:AAHsqNCMlEb2Jd6-mRhqy3lp2ri_pWFCzls"
TELEGRAM_CHAT_ID = "8178934019"
TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

PIN_TRIGGER = 7
PIN_ECHO = 11
NEAR_THRESH_CM = 60.0
COOLDOWN_S = 1.0

# ====== Person Detection ======
def read_distance_cm(trigger_pin, echo_pin, timeout_s=0.03):
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
    return (pulse_end - pulse_start) * 17150.0

class PersonCounter:
    def __init__(self, trig, echo, near_thresh_cm, cooldown_s):
        self.trig = trig
        self.echo = echo
        self.near = near_thresh_cm
        self.cooldown = cooldown_s
        self.was_near = False
        self.last_count_time = 0.0

    def poll(self):
        d = read_distance_cm(self.trig, self.echo)
        if d is None: return 0
        is_near = d <= self.near
        if is_near and not self.was_near:
            now = time.monotonic()
            if (now - self.last_count_time) >= self.cooldown:
                self.last_count_time = now
                self.was_near = True
                return 1
        self.was_near = is_near
        return 0

def gpio_setup():
    GPIO.setmode(GPIO.BOARD)
    GPIO.setwarnings(False)
    GPIO.setup(PIN_TRIGGER, GPIO.OUT)
    GPIO.setup(PIN_ECHO, GPIO.IN)
    GPIO.output(PIN_TRIGGER, GPIO.LOW)
    time.sleep(0.2)

def gpio_cleanup():
    GPIO.cleanup()

# ====== State ======
people_A = 0
people_B = 0
people_exit = 0
people_A2B = 0
people_B2A = 0

# ====== MQTT callbacks ======
def on_connect(client, userdata, flags, rc):
    print("MQTT connected:", rc)
    subs = [(t, 1) for t in TOPICS]
    client.subscribe(subs)
    print("Subscribed:", ", ".join(TOPICS))

def on_message(client, userdata, msg):
    global people_A, people_B, people_A2B, people_B2A
    try:
        payload = json.loads(msg.payload.decode())
        count = payload.get("people_count", 0)
        gate = msg.topic.split("/")[-1]
        #depending on the gate obtained from the msg, increment the corresponding counter
        if gate == "A_in":
            people_A += count
        elif gate == "B_in":
            people_B += count
        elif gate == "A2B":
            people_A2B += count
        elif gate == "B2A":
            people_B2A += count

        print(f"[MQTT] {gate} +{count}")

    except Exception as e:
        print("Bad message:", e)

def mqtt_setup():
    c = mqtt.Client(client_id=CLIENT_ID, clean_session=True)
    c.on_connect = on_connect
    c.on_message = on_message
    c.will_set(TOPIC_STATUS, payload="offline", qos=1, retain=True)
    c.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    c.loop_start()
    c.publish(TOPIC_STATUS, "online", qos=1, retain=True)
    return c

# ====== ThingSpeak Pusher ======
def push_thingspeak():
    total_people = people_A + people_B
    a_only = people_A - people_A2B
    b_only = people_B - people_B2A
    params = {
        "api_key": THINGSPEAK_WRITE_KEY,
        "field1": people_A,
        "field2": people_B,
        "field3": total_people,
        "field4": people_exit,
        "field5": a_only,
        "field6": b_only
    }
    try:
        r = requests.get(THINGSPEAK_URL, params=params, timeout=8)
        ok = (r.status_code == 200) and r.text.strip().isdigit() and int(r.text.strip()) > 0
        print("TS push:", r.status_code, r.text.strip(), "OK" if ok else "FAIL")
    except Exception as e:
        print("TS error:", e)

# ====== Telegram: respond only to /status ======
def telegram_poll_loop():
    offset = None
    while True:
        try:
            url = f"{TELEGRAM_URL}/getUpdates"
            if offset: url += f"?offset={offset}"
            r = requests.get(url, timeout=10).json()
            for update in r.get("result", []):
                offset = update["update_id"] + 1
                message = update.get("message", {})
                text = message.get("text", "")
                chat_id = message.get("chat", {}).get("id")
                if text.strip().lower() == "/status" and str(chat_id) == TELEGRAM_CHAT_ID:
                    send_status()
        except Exception as e:
            print("Telegram poll error:", e)
        time.sleep(2)

def send_status():
    total_people = people_A + people_B
    a_only = people_A - people_A2B
    b_only = people_B - people_B2A
    lines = [
        f"Section A total: {people_A}",
        f"Section B total: {people_B}",
        f"Park total:      {total_people}",
        f"Exited total:    {people_exit}",
        f"A-only: {a_only}, B-only: {b_only}"
    ]
    text = "\n".join(lines)
    try:
        requests.post(f"{TELEGRAM_URL}/sendMessage",
                      data={"chat_id": TELEGRAM_CHAT_ID, "text": text},
                      timeout=6)
        print("Telegram /status replied.")
    except Exception as e:
        print("Telegram error:", e)

# ====== Main ======
def main():
    global people_exit
    gpio_setup()
    client = mqtt_setup()
    print("Aggregator running… (Exit sensor + remote gates)")

    exit_sensor = PersonCounter(PIN_TRIGGER, PIN_ECHO, NEAR_THRESH_CM, COOLDOWN_S)

    # start Telegram poller in background thread
    threading.Thread(target=telegram_poll_loop, daemon=True).start()

    last_ts = 0.0

    try:
        while True:
            now = time.time()

            # Poll Exit gate locally
            if exit_sensor.poll():
                people_exit += 1
                print("[Exit] Person exited")

            # Every 1s: push to ThingSpeak
            if now - last_ts >= PUSH_INTERVAL_S:
                push_thingspeak()
                last_ts = now

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        try:
            client.publish(TOPIC_STATUS, "offline", qos=1, retain=True)
            client.loop_stop(); client.disconnect()
        except Exception:
            pass
        gpio_cleanup()

if __name__ == "__main__":
    main()
