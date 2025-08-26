#!/usr/bin/env python3
import time
import json
import math
import requests
import RPi.GPIO as GPIO
import paho.mqtt.client as mqtt
import threading
from datetime import datetime
state_lock = threading.Lock()

LED_GREEN = 20
LED_RED = 21

# ========== MQTT CONFIG ==========
#MQTT_BROKER = "192.168.129.48" #monash
#MQTT_BROKER = "172.20.10.2" #ryan
MQTT_BROKER = "192.168.0.220" #d7
MQTT_PORT   = 1883
CLIENT_ID   = "pi-aggregator"
TOPIC_QUEUE  = "park/ride1/queue/seconds"    # {wait_min:int, distance_cm:float, ts:int}
TOPIC_RIDE   = "park/ride1/ride/state"       # {green:int, seats:int, yellow:int, red:int, ts:int}
TOPIC_HEIGHT = "park/ride1/height/state"     # {status:int, height_cm:float, ts:int}
TOPIC_STATUS = f"park/ride1/status/{CLIENT_ID}"

# ========== THINGSPEAK CONFIG ==========
THINGSPEAK_WRITE_KEY = "LDTWH8ZB2Q4VMBU7"
THINGSPEAK_URL = "https://api.thingspeak.com/update"
PUSH_INTERVAL_S = 1.0

# Field mapping (ThingSpeak):
# field1=green, field2=seats, field3=yellow, field4=red,
# field5=waitingtime, field6=distance,
# field7=height_status, field8=height_cm

# ========== TELEGRAM CONFIG ==========
TELEGRAM_BOT_TOKEN = "8155442401:AAEGgII1csFk7kKdHwXmiD6T_L8f5YOVnHc"   
# e.g., 8205472041:AAH...
ALLOWED_CHAT_ID = "6446281934"                 # int chat id to restrict, or None for public 
TELEGRAM_BASE = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_POLL_TIMEOUT = 50                     # long-poll seconds

# ========== LOCAL HEIGHT SENSOR (YOUR CALIBRATED SETTINGS) ==========
PIN_TRIGGER = 7    # BOARD pin 7  (GPIO4)
PIN_ECHO    = 11   # BOARD pin 11 (GPIO17) via 1k/2k divider

DOOR_CLEARANCE_CM = 235.0     # sensor face -> floor (empty doorway)
MIN_HEIGHT_CM     = 120.0     # ride minimum height
SAMPLE_DELAY_S    = 0.015
SAMPLES_PER_READ  = 5

# ========== STATE (latest values) ==========
latest_queue  = {"wait_min": None, "distance_cm": None, "ts": None}
latest_ride   = {"green": None, "seats": None, "yellow": None, "red": None, "ts": None}
latest_height = {"status": None, "height_cm": None, "ts": None}

# ---------------- GPIO / Height helpers ----------------
def gpio_setup():
    GPIO.setmode(GPIO.BOARD)
    GPIO.setwarnings(False)
    GPIO.setup(PIN_TRIGGER, GPIO.OUT)
    GPIO.setup(PIN_ECHO, GPIO.IN)
    GPIO.output(PIN_TRIGGER, GPIO.LOW)
    print("Waiting for sensor to settle")
    GPIO.setup(LED_GREEN, GPIO.OUT)
    GPIO.setup(LED_RED, GPIO.OUT)
    time.sleep(2)

def gpio_cleanup():
    GPIO.cleanup()
    print("GPIO cleaned up")

def read_distance_once(timeout_s=0.06):
    """One HC-SR04 reading in cm; returns math.nan on timeout."""
    GPIO.output(PIN_TRIGGER, GPIO.HIGH)
    time.sleep(0.00001)
    GPIO.output(PIN_TRIGGER, GPIO.LOW)

    t0 = time.time()
    while GPIO.input(PIN_ECHO) == 0:
        if time.time() - t0 > timeout_s:
            return math.nan
    pulse_start = time.time()

    while GPIO.input(PIN_ECHO) == 1:
        if time.time() - pulse_start > timeout_s:
            return math.nan
    pulse_end = time.time()

    duration = pulse_end - pulse_start
    distance_cm = duration * 17150.0  # (34300 cm/s) / 2
    return distance_cm

DOOR_CLEARANCE_CM = 235.0     # sensor face -> floor (empty doorway)
MIN_HEIGHT_CM     = 120.0     # ride minimum height

def read_height_sample():
    """Returns (height_cm, status) or (None, None) on failure."""
    reads = []
    for _ in range(SAMPLES_PER_READ):
        d = read_distance_once()

        # check if distance read is within the regular bounds of the HC-SR04 Ultrasonic sensor
        if not math.isnan(d) and 2.0 < d < 450.0:
            reads.append(d)
        time.sleep(SAMPLE_DELAY_S)
    if not reads:
        return None, None
    reads.sort()
    distance = reads[len(reads)//2]  # median

    # height = door height - measured distance
    height_cm = DOOR_CLEARANCE_CM - distance
    status = 1 if height_cm >= MIN_HEIGHT_CM else 0

    # light up green if the passenger has a valid height and red if not
    if status == 1:
        GPIO.output(LED_GREEN, GPIO.HIGH)
        GPIO.output(LED_RED, GPIO.LOW)
    else: 
        GPIO.output(LED_GREEN, GPIO.LOW)
        GPIO.output(LED_RED, GPIO.HIGH)
    
    return height_cm, status


# ---------------- MQTT helpers ----------------
def on_connect(client, userdata, flags, rc):
    print("MQTT connected:", rc)
    client.subscribe([(TOPIC_QUEUE, 1), (TOPIC_RIDE, 1), (TOPIC_HEIGHT, 1)])
    print("Subscribed to:", TOPIC_QUEUE, TOPIC_RIDE, TOPIC_HEIGHT)

def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
    except Exception as e:
        print("Bad JSON on", msg.topic, ":", e)
        return
    with state_lock:
        if msg.topic == TOPIC_QUEUE:
            latest_queue["wait_min"]     = payload.get("wait_min")
            latest_queue["distance_cm"]  = payload.get("distance_cm")
            latest_queue["ts"]           = payload.get("ts")
            print(f"[queue] wait={latest_queue['wait_min']} dist={latest_queue['distance_cm']}")
        elif msg.topic == TOPIC_RIDE:
            for k in ("green","seats","yellow","red","ts"):
                latest_ride[k] = payload.get(k)
            print(f"[ride ] g={latest_ride['green']} y={latest_ride['yellow']} r={latest_ride['red']} seats={latest_ride['seats']}")
        elif msg.topic == TOPIC_HEIGHT:
            latest_height["status"]     = payload.get("status")
            latest_height["height_cm"]  = payload.get("height_cm")
            latest_height["ts"]         = payload.get("ts")
            print(f"[height] status={latest_height['status']} height={latest_height['height_cm']}")

def mqtt_setup():
    c = mqtt.Client(client_id=CLIENT_ID, clean_session=True)
    c.on_connect = on_connect
    c.on_message = on_message
    c.will_set(TOPIC_STATUS, payload="offline", qos=1, retain=True)
    c.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)
    c.loop_start()
    c.publish(TOPIC_STATUS, "online", qos=1, retain=True)
    return c

def mqtt_publish_height(client, status, height_cm):
    payload = {
        "status": int(status),
        "height_cm": None if height_cm is None else float(f"{height_cm:.1f}"),
        "ts": int(time.time())
    }
    client.publish(TOPIC_HEIGHT, json.dumps(payload), qos=1, retain=False)

# ---------------- ThingSpeak push ----------------
session = requests.Session()

def build_ts_params():
    params = {"api_key": THINGSPEAK_WRITE_KEY}
    with state_lock:
        # Ride
        if latest_ride["green"]  is not None: params["field1"] = int(latest_ride["green"])
        if latest_ride["seats"]  is not None: params["field2"] = int(latest_ride["seats"])
        if latest_ride["yellow"] is not None: params["field3"] = int(latest_ride["yellow"])
        if latest_ride["red"]    is not None: params["field4"] = int(latest_ride["red"])
        # Queue
        if latest_queue["wait_min"]    is not None: params["field5"] = int(latest_queue["wait_min"])
        if latest_queue["distance_cm"] is not None: params["field6"] = f"{float(latest_queue['distance_cm']):.1f}"
        # Height
        if latest_height["status"]    is not None: params["field7"] = int(latest_height["status"])
        if latest_height["height_cm"] is not None: params["field8"] = f"{float(latest_height['height_cm']):.1f}"
    return params

def push_thingspeak():
    params = build_ts_params()
    if len(params) <= 1:
        print("TS: nothing to send yet.")
        return
    try:
        r = session.get(THINGSPEAK_URL, params=params, timeout=2)
        ok = (r.status_code == 200) and r.text.strip().isdigit() and int(r.text.strip()) > 0
        print("TS push:", r.status_code, r.text.strip(), "OK" if ok else "FAIL")
    except Exception as e:
        print("TS error:", e)

def push_async():
    threading.Thread(target=push_thingspeak, daemon=True).start()

# ---------------- Telegram helpers ----------------
def tg_send_message(chat_id: int, text: str):
    try:
        r = session.post(f"{TELEGRAM_BASE}/sendMessage",
                         data={"chat_id": chat_id, "text": text},
                         timeout=10)
        return r.status_code, r.text
    except Exception as e:
        return None, f"send_message error: {e}"

def tg_format_status():
    with state_lock:
        g = latest_ride["green"]; y = latest_ride["yellow"]; r_ = latest_ride["red"]; seats = latest_ride["seats"]
        w = latest_queue["wait_min"]; d = latest_queue["distance_cm"]
        hs = latest_height["status"]; hc = latest_height["height_cm"]

    lines = []
    lines.append(" Ride Status")
    lines.append(f"🟢 Green: {g if g is not None else 'N/A'}")
    lines.append(f"🟡 Yellow: {y if y is not None else 'N/A'}")
    lines.append(f"🔴 Red: {r_ if r_ is not None else 'N/A'}")
    lines.append(f"💺 Seats: {seats if seats is not None else 'N/A'}")
    lines.append("")
    lines.append("⏱️ Queue")
    lines.append(f"Waiting Time (min): {w if w is not None else 'N/A'}")
    lines.append(f"Distance (cm): {f'{d:.1f}' if isinstance(d,(int,float)) else ('N/A' if d is None else str(d))}")
    lines.append("")
    lines.append("📏 Height Gate")
    lines.append(f"Status: {'OK' if hs==1 else ('SHORT' if hs==0 else 'N/A')}")
    lines.append(f"Height (cm): {f'{hc:.1f}' if isinstance(hc,(int,float)) else ('N/A' if hc is None else str(hc))}")
    return "\n".join(lines)

def tg_handle_text(chat_id: int, text: str):
    t = (text or "").strip().lower()
    if t in ("/start", "start", "help", "/help"):
        msg = ("Hi! Send /status to get the latest:\n"
               "- Ride lights (green/yellow/red) & seats\n"
               "- Queue waiting time & distance\n"
               "- Height gate status & height")
        tg_send_message(chat_id, msg)
        return
    if t in ("/status", "status"):
        tg_send_message(chat_id, tg_format_status())
        return
    tg_send_message(chat_id, "Try /status")

def tg_poll_loop():
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_BOT_TOKEN.startswith("PASTE_"):
        print("Telegram disabled (no token).")
        return
    print("Telegram bot polling…")
    offset = None
    while True:
        try:
            params = {"timeout": TELEGRAM_POLL_TIMEOUT}
            if offset is not None:
                params["offset"] = offset
            r = session.get(f"{TELEGRAM_BASE}/getUpdates", params=params, timeout=TELEGRAM_POLL_TIMEOUT+10)
            r.raise_for_status()
            updates = r.json().get("result", [])
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message") or upd.get("edited_message")
                if not msg:
                    continue
                chat = msg.get("chat", {})
                chat_id = chat.get("id")
                text = msg.get("text", "")
                if ALLOWED_CHAT_ID is not None and chat_id != ALLOWED_CHAT_ID:
                    # ignore unauthorized chats
                    continue
                if chat_id:
                    tg_handle_text(chat_id, text)
        except Exception as e:
            print("Telegram poll error:", e)
            time.sleep(2)

# ---------------- Main ----------------
def main():
    gpio_setup()
    mqttc = mqtt_setup()

    # start Telegram polling thread
    threading.Thread(target=tg_poll_loop, daemon=True).start()

    try:
        print("Aggregator running… (local height + MQTT subscribe + 15s ThingSpeak + Telegram /status)")
        last_ts_push = 0.0
        last_height_pub = 0.0
        while True:
            now = time.monotonic()

            # 1) Local height sensor ~1 Hz
            if now - last_height_pub >= 1.0:
                height_cm, status = read_height_sample()
                if height_cm is None:
                    print("[height] No echo / timeout")
                else:
                    print(f"[height] local height={height_cm:.1f} cm status={'OK' if status==1 else 'SHORT'}")
                    with state_lock:
                        latest_height["status"] = status
                        latest_height["height_cm"] = height_cm
                        latest_height["ts"] = int(time.time())
                    mqtt_publish_height(mqttc, status, height_cm)
                last_height_pub = now

            # 2) ThingSpeak every 15 s
            if now - last_ts_push >= PUSH_INTERVAL_S:
                push_async()
                last_ts_push = now

            time.sleep(0.1)

    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        try:
            mqttc.publish(TOPIC_STATUS, "offline", qos=1, retain=True)
            mqttc.loop_stop()
            mqttc.disconnect()
        except Exception:
            pass
        gpio_cleanup()

if __name__ == "__main__":
    main()




