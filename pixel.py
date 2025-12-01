"""
=============================================================
AWTRIX Weather + Indoor Air Quality Display Script
=============================================================

This script retrieves:
1) Real-time weather data from your Ecowitt weather station
2) Current weather condition (Sunny / Cloudy / Rain / Snow)
   using the free Open-Meteo API (no API key required)
3) Indoor CO₂, temperature, and humidity from your local
    CO₂ sensor (e.g., SCD4x)

It then:
- Converts Ecowitt units (F→C, mph→km/h, in→mm, inHg→hPa)
- Selects an AWTRIX icon based on weather, CO₂ level,
  and daytime/nighttime
- Overrides the weather icon if CO₂ is high (e.g., showing
  “Turn on fan” icon when ventilation is needed)
- Pushes everything to AWTRIX as a custom app via MQTT
=============================================================
"""


import requests
import json
import time
import paho.mqtt.client as mqtt
import datetime

# ================================
# Configuration — Ecowitt & MQTT
# ================================
APPLICATION_KEY = "45C7C5CF22984646933C4D3A888C027F"
API_KEY = "7e6e2dd9-bc5d-424b-90f9-c807fd30c9b1"
MAC = "2C:F4:32:4E:39:53"

MQTT_BROKER = "10.0.0.11"
MQTT_USER = "mqtt"
MQTT_PASS = "mqtt"

AWTRIX_UID = "awtrix_bb6b64"
INTERVAL = 40
# ================================

pm25_data = {"pm25": None, "aqi": None}
def calculate_aqi(pm25):
    """Calculate AQI using the official US EPA breakpoint formula."""
    if pm25 is None:
        return None

    pm25 = float(pm25)

    # --- EPA PM2.5 Breakpoints ---
    breakpoints = [
        (0.0,   12.0,   0,   50),
        (12.1,  35.4,  51,  100),
        (35.5,  55.4, 101,  150),
        (55.5, 150.4, 151,  200),
        (150.5,250.4, 201,  300),
        (250.5,350.4, 301,  400),
        (350.5,500.4, 401,  500)
    ]

    for (c_low, c_high, aqi_low, aqi_high) in breakpoints:
        if c_low <= pm25 <= c_high:
            # linear interpolation:
            aqi = ((aqi_high - aqi_low) / (c_high - c_low)) * (pm25 - c_low) + aqi_low
            return round(aqi)

    # PM2.5 > 500.4
    return 500


def mqtt_pm25_listener():
    client = mqtt.Client()

    def on_message(client, userdata, msg):
        payload = json.loads(msg.payload.decode())
        pm = payload.get("PM25") or payload.get("pm25")


        if pm is not None:
            pm25_data["pm25"] = float(pm)
            pm25_data["aqi"] = calculate_aqi(float(pm))

            pm25_data["pm25"] = float(pm)
            pm25_data["aqi"] = calculate_aqi(float(pm))

    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_message = on_message
    client.connect(MQTT_BROKER, 1883, 60)
    client.subscribe("home/sensors/pm25")
    client.loop_start()



LIGHTNING_TOPIC = "kanata/lightning-station-001"

lightning_data = {"storm_dist": None}

def mqtt_lightning_listener():
    """Background listener for lightning events."""
    client = mqtt.Client()

    def on_message(client, userdata, msg):
        try:
            payload = msg.payload.decode()
            data = json.loads(payload)

            if "storm_dist" in data:
                lightning_data["storm_dist"] = data["storm_dist"]
                print("⚡ Lightning detected:", data)
        except Exception as e:
            print("Lightning parse error:", e)

    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_message = on_message
    client.connect(MQTT_BROKER, 1883, 60)
    client.subscribe(LIGHTNING_TOPIC)
    client.loop_start()

def fetch_realtime():
    """Fetch real-time weather JSON from Ecowitt API."""
    url = (
        "https://api.ecowitt.net/api/v3/device/real_time?"
        f"application_key={APPLICATION_KEY}"
        f"&api_key={API_KEY}"
        f"&mac={MAC}"
        "&call_back=all"
    )
    r = requests.get(url)
    return r.json()


def parse_weather(api):
    """Extract weather values from Ecowitt API and convert units."""
    try:
        d = api["data"]
        # ---- Outdoor temperature ----
        temp_f = float(d["outdoor"]["temperature"]["value"])
        temp_c = round((temp_f - 32) * 5/9, 1)

        humidity = int(d["outdoor"]["humidity"]["value"])

        # ---- Wind ----
        wind_mph = float(d["wind"]["wind_speed"]["value"])
        wind_kmh = round(wind_mph * 1.60934, 1)

        # ---- Rain ----
        rain_in = float(d["rainfall"]["rain_rate"]["value"])
        rain_mm = round(rain_in * 25.4, 2)

        # ---- UV ----
        uv = float(d["solar_and_uvi"]["uvi"]["value"])

        # ---- Pressure ----
        pressure_inhg = float(d["pressure"]["relative"]["value"])
        pressure_hpa = round(pressure_inhg * 33.8639, 1)

        return temp_c, humidity, wind_kmh, rain_mm, uv, pressure_hpa
    except:
        return ("-3", "99%", "2", "0.0", "0", "1000")


def weather_status():
    """Get current weather condition using free Open-Meteo API."""
    url = "https://api.open-meteo.com/v1/forecast?latitude=45.312950&longitude=-75.900148&current_weather=true"
    data = requests.get(url).json()

    code = data["current_weather"]["weathercode"]

    if code == 0:
        return "Sunny"
    elif code in [1, 2, 3]:
        return "Cloudy"
    elif 51 <= code <= 67:
        return "Rain"
    elif 40   <= code <= 77:
        return "Snow"
    else:
        return "Other"


def select_icon(status, co2=None):
    """Choose AWTRIX icon based on weather, co2, and night/day."""
    # CO2 override
    if co2 is not None and co2 >= 1000:
        return 420  # Open window icon


    # Get current hour (0–23)
    hour = datetime.datetime.now().hour
    is_night = hour >= 17 or hour < 6   # Night period from 18:00–06:00

    # Rain
    if status == "Rain":
        return 999

    # Cloudy
    if status == "Cloudy":
        return 63

    # Sunny
    if status == "Sunny":
        return 60 if is_night else 50  # 60 = day sun, 61 = night moon

    # Snow
    if status == "Snow":
        return 777


def push_awtrix(payload):
    """Send weather data to AWTRIX and force refresh."""

    client = mqtt.Client()
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.connect(MQTT_BROKER, 1883, 60)

    # 1️⃣ Destroy previous "weather" app (important for refresh)
    destroy_topic = f"{AWTRIX_UID}/custom/weather/destroy"
    client.publish(destroy_topic, "1")

    time.sleep(0.05)  # allow AWTRIX firmware to process

    # 2️⃣ Push new weather data
    topic = f"{AWTRIX_UID}/custom/weather"
    client.publish(topic, json.dumps(payload))

    client.disconnect()

def fetch_co2_from_mqtt():
    """Subscribe once to CO2 topic and return {co2, temp, rh}."""
    result = {"co2": None, "temp": None, "rh": None}

    def on_message(client, userdata, msg):
        try:
            payload = msg.payload.decode()
            data = json.loads(payload)
            result["co2"] = float(data.get("co2", 0))
            result["temp"] = float(data.get("temp", 0))
            result["rh"]   = float(data.get("rh", 0))

        except Exception as e:
            print("Parse error:", e)

    client = mqtt.Client()
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.connect(MQTT_BROKER, 1883, 60)

    CO2_TOPIC = "home/sensors/co2"   # ← change if needed

    client.on_message = on_message
    client.subscribe(CO2_TOPIC)

    client.loop_start()
    time.sleep(1)   # wait for one JSON message
    client.loop_stop()
    client.disconnect()

    return result

def send_awtrix_notification(distance_km):
    """Send lightning alert to AWTRIX."""
    topic = f"{AWTRIX_UID}/notify"

    notification = {
        "title": "⚡ Lightning Alert",
        "text": f"Storm: {distance_km} km",
        "duration": 10,
        "icon": 130,
        "color": [255, 200, 0],
        "repeat": 3
    }

    client = mqtt.Client()
    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.connect(MQTT_BROKER, 1883, 60)
    client.publish(topic, json.dumps(notification))
    client.disconnect()

    print("⚡ Lightning notification sent:", distance_km)

def select_pm25_icon(pm25):
    """If PM2.5 is high, override icon."""
    if pm25 is None:
        return None
    try:
        pm25 = float(pm25)
    except:
        return None

    if pm25 >= 55:
        return 421   # air pollution / mask icon
    return None

def loop():
    """Main loop — fetch, parse, format, send to AWTRIX."""
    last_lightning_notified = None
    while True:
        pm25 = pm25_data["pm25"]
        aqi = pm25_data["aqi"]
        if pm25 is None:
            pm25_text = "?"
        else:
            pm25_text = f"{pm25}µg/m³"
        #print(pm25_text)
        try:
            # ---------- Lightning check ----------
            if lightning_data["storm_dist"] is not None:
                dist = lightning_data["storm_dist"]


                if last_lightning_notified != dist:
                    send_awtrix_notification(dist)
                    last_lightning_notified = dist
                lightning_icon = 130
                lightning_data["storm_dist"] = None
            else:
                lightning_icon = None

            api = fetch_realtime()
            temp_c, hum, wind, rain, uv, pressure = parse_weather(api)
            status = weather_status()
            co2sensor = fetch_co2_from_mqtt()
            co2 = co2sensor["co2"]
            icon_id = select_icon(status, co2)
            if lightning_icon is not None:
                icon_id = lightning_icon
            # PM2.5 override
            pm25_icon = select_pm25_icon(pm25)
            if pm25_icon is not None:
                icon_id = pm25_icon
            temp_indoor = co2sensor["temp"]
            rh_indoor = co2sensor["rh"]
            if co2 is None:
                co2 = "?"

            text = (
                f"🌡{temp_c}°C  💧{hum}% "
                f"🌬{wind}km/h  ☔{rain}mm  P:{pressure}hpa UV:{uv}"
                f" PM2.5:{pm25_text} AQI:{aqi} "
                f" Indoor CO2:{co2}  {temp_indoor}°C  {rh_indoor}%"
            )

            payload = {
                "id": "weather",
                "text": text,
                "icon": icon_id,
                "color": [255, 200, 100],
                "scrollSpeed": 40,
                "repeat": 1,
                "unique": time.time()  # force AWTRIX to refresh
            }

            push_awtrix(payload)
            print("Sent:", text)

        except Exception as e:
            print("Error:", e)

        time.sleep(INTERVAL)


if __name__ == "__main__":
    mqtt_lightning_listener()
    mqtt_pm25_listener()
    loop()
