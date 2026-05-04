#!/usr/bin/env python3
"""
garmin_gear_bot — main service
-------------------------------
Polls Garmin Connect for new activities, publishes them to MQTT,
and listens for gear selection responses to write back to Garmin.

Flow:
  1. Poll Garmin every POLL_INTERVAL seconds
  2. On new activity → publish to MQTT_TOPIC_NEW_ACTIVITY
  3. Node-RED receives → sends Telegram inline-keyboard prompt to user
  4. User taps gear choice → Node-RED publishes to MQTT_TOPIC_GEAR_SELECT
  5. This service receives → writes gear association to Garmin Connect

MQTT topics (configurable via env):
  Publish:   garmin/activity/new          — new activity + gear options
  Subscribe: garmin/activity/gear_select  — user's gear selection
"""

import json
import logging
import os
import pickle
import signal
import sys
import time

import paho.mqtt.client as mqtt

try:
    from garminconnect import Garmin
except ImportError:
    print("ERROR: garminconnect not installed.")
    sys.exit(1)

# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("garmin_gear_bot")

# ── Config from environment ───────────────────────────────────────────────────

GARMIN_EMAIL      = os.environ["GARMIN_EMAIL"]
GARMIN_PASSWORD   = os.environ["GARMIN_PASSWORD"]
GARMIN_PROFILE_ID = int(os.environ["GARMIN_PROFILE_ID"])  # profileId, not id

MQTT_HOST      = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT      = int(os.environ.get("MQTT_PORT", 1883))
MQTT_USER      = os.environ.get("MQTT_USER", "")
MQTT_PASS      = os.environ.get("MQTT_PASSWORD", "")
MQTT_TOPIC_NEW_ACTIVITY = os.environ.get("MQTT_TOPIC_NEW_ACTIVITY", "garmin/activity/new")
MQTT_TOPIC_GEAR_SELECT  = os.environ.get("MQTT_TOPIC_GEAR_SELECT",  "garmin/activity/gear_select")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", 300))   # seconds
STATE_FILE    = os.environ.get("STATE_FILE", "/data/state.json")
TOKEN_FILE    = os.environ.get("TOKEN_FILE", "/data/garmin_tokens.pkl")

# Activity type → relevant gear types to present to user.
# Activities whose type isn't listed will get all active gear.
ACTIVITY_GEAR_FILTER = {
    "running":           {"Shoes"},
    "track_running":     {"Shoes"},
    "trail_running":     {"Shoes"},
    "treadmill_running": {"Shoes"},
    "virtual_run":       {"Shoes"},
    "indoor_running":    {"Shoes"},
    "cycling":           {"Bike", "Bikes"},
    "road_biking":       {"Bike", "Bikes"},
    "mountain_biking":   {"Bike", "Bikes"},
    "indoor_cycling":    {"Bike", "Bikes"},
}

# ── Startup validation ────────────────────────────────────────────────────────

if POLL_INTERVAL < 120:
    log.warning(
        "POLL_INTERVAL is %ss — this is very aggressive. "
        "Increasing to 120s to avoid Garmin rate limiting.",
        POLL_INTERVAL,
    )
    POLL_INTERVAL = 120

# ── State persistence ─────────────────────────────────────────────────────────

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"last_activity_id": None}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Garmin auth ───────────────────────────────────────────────────────────────

def garmin_authenticate():
    """
    Authenticate with Garmin Connect, reusing saved tokens when available.
    Always returns a ready-to-use Garmin API object.
    """
    if os.path.exists(TOKEN_FILE):
        log.info("Loading saved Garmin tokens from %s", TOKEN_FILE)
        try:
            api = Garmin()
            with open(TOKEN_FILE, "rb") as f:
                tokens = pickle.load(f)
            api.login(tokens)
            log.info("Garmin token login successful")
            return api
        except Exception as e:
            log.warning("Saved tokens invalid (%s), falling back to password login", e)

    log.info("Logging in to Garmin Connect with email/password")
    api = Garmin(email=GARMIN_EMAIL, password=GARMIN_PASSWORD)
    api.login()

    os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
    try:
        tokens = api.garth.dumps()
    except AttributeError:
        tokens = api.client.dumps() if hasattr(api.client, "dumps") else None

    if tokens:
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(tokens, f)
        log.info("Garmin tokens saved to %s", TOKEN_FILE)
    else:
        log.warning("Could not save Garmin tokens — will re-authenticate on next restart")

    return api


# ── Garmin HTTP helpers ───────────────────────────────────────────────────────

def garmin_put(api, path, body=None):
    """PUT via the underlying client (bypasses connectapi's hardcoded GET)."""
    kwargs = {"json": body} if body is not None else {}
    resp   = api.client._run_request("PUT", path, **kwargs)
    status = getattr(resp, "status_code", None)
    text   = getattr(resp, "text", "")
    log.info("Garmin PUT %s → status %s body: %s", path, status, repr(text[:500]))
    try:
        return resp.json()
    except Exception:
        # 204 No Content is the expected success response for the v2 API
        return None


# ── Gear helpers ──────────────────────────────────────────────────────────────

def fmt_duration(seconds):
    if not seconds:
        return "unknown"
    h, rem = divmod(int(seconds), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_distance(meters):
    if not meters:
        return "unknown"
    return f"{meters / 1000:.2f} km"


def to_dashed_uuid(uuid_str):
    """Convert a UUID to dashed format if not already dashed."""
    u = uuid_str.replace("-", "")
    return f"{u[0:8]}-{u[8:12]}-{u[12:16]}-{u[16:20]}-{u[20:]}"


def get_relevant_gear(api, activity_type):
    """
    Fetch all active gear, optionally filtered by activity type.
    Returns a list of dicts with uuid, name, type.
    """
    all_gear = api.get_gear(GARMIN_PROFILE_ID)
    active = [g for g in all_gear if g.get("gearStatusName", "").lower() == "active"]

    allowed_types = ACTIVITY_GEAR_FILTER.get(activity_type)
    if allowed_types:
        filtered = [g for g in active if g.get("gearTypeName") in allowed_types]
        # Fall back to all active gear if filter returns nothing
        if filtered:
            active = filtered

    return [
        {
            "uuid": g["uuid"],
            "name": g.get("displayName") or g.get("customMakeModel", "Unknown"),
            "type": g.get("gearTypeName", "Unknown"),
        }
        for g in active
    ]

# ── Garmin polling ────────────────────────────────────────────────────────────

def check_for_new_activity(api, state, mqtt_client):
    """
    Fetch the latest activity. If it's new, publish it to MQTT with gear options.
    Returns (updated_state, api) — api may be a new object if re-auth occurred.
    """
    try:
        activities = api.get_activities(0, 1)
    except Exception as e:
        # Attempt re-authentication if the error looks like a token expiry
        if any(k in str(e).lower() for k in ("401", "403", "authentication", "unauthorized")):
            log.warning("Possible token expiry detected — attempting re-authentication")
            try:
                # Remove stale token file so garmin_authenticate does a fresh login
                if os.path.exists(TOKEN_FILE):
                    os.remove(TOKEN_FILE)
                api = garmin_authenticate()
                activities = api.get_activities(0, 1)
            except Exception as auth_e:
                log.error("Re-authentication failed: %s", auth_e)
                return state, api
        else:
            log.error("Failed to fetch activities from Garmin: %s", e)
            return state, api

    if not activities:
        log.debug("No activities returned from Garmin")
        return state, api

    latest   = activities[0]
    act_id   = latest.get("activityId")
    act_name = latest.get("activityName", "Activity")
    act_type = latest.get("activityType", {}).get("typeKey", "unknown")
    start    = latest.get("startTimeLocal", "")
    distance = latest.get("distance")
    duration = latest.get("duration")

    if str(act_id) == str(state.get("last_activity_id")):
        log.debug("No new activity (last seen: %s)", act_id)
        return state, api

    log.info("New activity detected: %s (ID: %s, type: %s)", act_name, act_id, act_type)

    # Fetch relevant gear options
    try:
        gear_options = get_relevant_gear(api, act_type)
    except Exception as e:
        log.error("Failed to fetch gear: %s", e)
        gear_options = []

    # If no gear is available for this activity type, record the activity as
    # seen but skip the notification — there's nothing useful to ask the user.
    if not gear_options:
        log.warning(
            "No relevant active gear found for activity type '%s' — "
            "skipping notification for activity %s", act_type, act_id
        )
        state["last_activity_id"] = str(act_id)
        save_state(state)
        return state, api

    payload = {
        "activity_id":   act_id,
        "activity_name": act_name,
        "activity_type": act_type,
        "start_time":    start,
        "distance":      fmt_distance(distance),
        "duration":      fmt_duration(duration),
        "gear_options":  gear_options,
    }

    log.info("Publishing new activity to MQTT topic '%s'", MQTT_TOPIC_NEW_ACTIVITY)
    log.info("Gear options: %s", [g["name"] for g in gear_options])

    mqtt_client.publish(
        MQTT_TOPIC_NEW_ACTIVITY,
        json.dumps(payload),
        qos=1,
        retain=False,
    )

    state["last_activity_id"] = str(act_id)
    save_state(state)
    return state, api


# ── Gear selection handler ────────────────────────────────────────────────────

def handle_gear_selection(api, payload_str):
    """
    Handle an inbound gear selection from Node-RED.

    Expected payload:
        {"activity_id": 12345, "gear_uuid": "abc123"}  — associate gear
        {"activity_id": 12345, "gear_uuid": null}      — user chose "None"
    """
    try:
        payload = json.loads(payload_str)
    except json.JSONDecodeError as e:
        log.error("Invalid JSON in gear selection payload: %s", e)
        return

    # Guard before int() conversion to avoid TypeError on missing key
    raw_id = payload.get("activity_id")
    if not raw_id:
        log.error("Gear selection payload missing activity_id: %s", payload)
        return
    act_id = int(raw_id)

    gear_uuid = payload.get("gear_uuid")
    if not gear_uuid:
        log.info("User selected 'None' for activity %s — no gear will be associated", act_id)
        return

    # v2 API requires dashed UUID format
    gear_uuid_dashed = to_dashed_uuid(gear_uuid)

    log.info("Associating gear %s with activity %s", gear_uuid_dashed, act_id)
    try:
        result = garmin_put(
            api,
            f"/gear-service/activity/v2/{act_id}/associated-gear",
            [gear_uuid_dashed],
        )
        if result is None:
            log.info("Gear association successful (204 No Content — expected for v2 API)")
        else:
            log.info("Gear association result: %s", result)
    except Exception as e:
        log.error("Failed to associate gear: %s", e)


# ── MQTT setup ────────────────────────────────────────────────────────────────

def build_mqtt_client(api_ref):
    """
    Build and return a configured MQTT client.
    api_ref is a list containing the current api object, allowing the
    message handler to always use the latest api (e.g. after re-auth).
    """
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="garmin_gear_bot",
        clean_session=True,
    )

    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)

    def on_connect(client, userdata, connect_flags, reason_code, properties):
        if reason_code.is_failure:
            log.error("MQTT connection failed: %s", reason_code)
        else:
            log.info("MQTT connected to %s:%s", MQTT_HOST, MQTT_PORT)
            client.subscribe(MQTT_TOPIC_GEAR_SELECT, qos=1)
            log.info("Subscribed to %s", MQTT_TOPIC_GEAR_SELECT)

    def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
        if reason_code.value != 0:
            log.warning("Unexpected MQTT disconnect (%s) — will auto-reconnect", reason_code)

    def on_message(client, userdata, msg):
        log.info("MQTT message received on %s", msg.topic)
        if msg.topic == MQTT_TOPIC_GEAR_SELECT:
            # Always use the current api object from the mutable reference
            handle_gear_selection(api_ref[0], msg.payload.decode("utf-8"))

    client.on_connect    = on_connect
    client.on_disconnect = on_disconnect
    client.on_message    = on_message

    client.reconnect_delay_set(min_delay=5, max_delay=60)

    return client


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("garmin_gear_bot starting up")
    log.info("Poll interval: %ss | MQTT: %s:%s", POLL_INTERVAL, MQTT_HOST, MQTT_PORT)

    # Authenticate with Garmin
    api = garmin_authenticate()

    # Load persisted state
    state = load_state()
    log.info("Last seen activity ID: %s", state.get("last_activity_id", "none"))

    # Mutable reference so on_message always uses the latest api object,
    # even after a re-authentication replaces it in the poll loop.
    api_ref = [api]

    # Connect to MQTT
    mqtt_client = build_mqtt_client(api_ref)
    try:
        mqtt_client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    except Exception as e:
        log.error("Could not connect to MQTT broker at %s:%s — %s", MQTT_HOST, MQTT_PORT, e)
        log.error("Check MQTT_HOST and MQTT_PORT and that the broker is reachable.")
        sys.exit(1)

    # Start MQTT network loop in background thread
    mqtt_client.loop_start()

    # Graceful shutdown
    def shutdown(sig, frame):
        log.info("Shutdown signal received — stopping")
        mqtt_client.loop_stop()
        mqtt_client.disconnect()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Main polling loop
    log.info("Starting poll loop (checking every %s seconds)", POLL_INTERVAL)
    while True:
        try:
            state, api = check_for_new_activity(api, state, mqtt_client)
            api_ref[0] = api  # keep the MQTT handler's reference current
        except Exception as e:
            log.error("Unexpected error in poll loop: %s", e, exc_info=True)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
