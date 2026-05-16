# garmin-gear-bot

A self-hosted automation that prompts you via Telegram to log which shoes (or other gear) you used after every Garmin activity — and then writes your selection back to Garmin Connect automatically.

---

## The Problem

Garmin Connect tracks cumulative usage on gear like running shoes, but it only auto-assigns gear if you've set a default. If you rotate between multiple pairs, you have to manually update each activity after the fact — which is easy to forget. Over time your shoe mileage logs become inaccurate, and you lose the ability to know when a pair needs replacing.

---

## How It Works

### System Overview

```
┌─────────────────┐                     ┌─────────────────┐
│    Node-RED     │ ── garmin/trigger ─► │  garmin-gear-bot│
│  (Inject node)  │      /check         │ (Python service) │
└─────────────────┘                     └────────┬────────┘
                                                  │ Garmin Connect API
                                                  ▼
                                         ┌─────────────────┐
                                         │  Garmin Connect │
                                         │ (activity log)  │
                                         └────────┬────────┘
                                                  │ new activity found
                                                  ▼
                                        garmin/activity/new
                                                  │
                                                  ▼
                                         ┌─────────────────┐
                                         │    Node-RED     │
                                         └────────┬────────┘
                                                  │ Telegram message
                                                  ▼
                                         ┌─────────────────┐
                                         │  Your Phone     │
                                         │  (Telegram app) │
                                         └────────┬────────┘
                                                  │ Button tap
                                                  ▼
                                         ┌─────────────────┐
                                         │    Node-RED     │
                                         └────────┬────────┘
                                                  │ MQTT publish
                                                  ▼
                                        garmin/activity/gear_select
                                                  │
                                                  ▼
                                        ┌─────────────────┐
                                        │  garmin-gear-bot│
                                        │ (Python service) │
                                        └────────┬────────┘
                                                  │ PUT API call
                                                  ▼
                                        ┌─────────────────┐
                                        │  Garmin Connect │
                                        │  (gear updated) │
                                        └─────────────────┘
```

### Step-by-Step Data Flow

1. **Trigger** — A Node-RED Inject node fires on a configurable schedule and publishes a message to the MQTT topic `garmin/trigger/check`. The Python service receives this and runs an activity check.

2. **Activity check** — The service fetches the most recent activity from Garmin Connect and compares its ID against a persisted last-seen ID stored in `/data/state.json`. If nothing is new, it logs accordingly and waits for the next trigger.

3. **New activity detected** — When a new activity is found, the service fetches your gear list and filters it to types relevant to the activity (e.g. shoes for a run, bikes for a ride). It publishes a JSON payload to the MQTT topic `garmin/activity/new`.

4. **Telegram notification** — A Node-RED flow subscribes to that MQTT topic. On receiving a message, it formats a Telegram message with the activity details and presents your gear options as inline keyboard buttons, with a "None" option always included.

5. **User responds** — You tap a gear option on your phone. Telegram sends a callback query back to the Node-RED bot receiver. Node-RED extracts your selection, edits the original message to show a confirmation (removing the buttons), and publishes your choice to the MQTT topic `garmin/activity/gear_select`.

6. **Garmin updated** — The Python service receives the gear selection via MQTT and calls the Garmin Connect v2 API to associate the selected gear with the activity. The gear's usage mileage is updated accordingly.

---

## Repository Structure

```
garmin-gear-bot/
├── garmin_service.py      # Main Python service (MQTT trigger, Garmin API)
├── Dockerfile             # Container definition
├── requirements.txt       # Python dependencies
├── compose.yaml           # Docker Compose service definition
├── .env                   # Environment variables (never commit this)
└── .github/
    └── workflows/
        └── build.yml      # GitHub Actions — builds and pushes image to ghcr.io
```

### Key Files Explained

**`garmin_service.py`**
The core of the system. Handles:
- Garmin Connect authentication with token persistence (avoids repeated SSO logins)
- Listening for MQTT trigger messages from Node-RED
- Checking Garmin Connect for new activities on demand
- Filtering gear by activity type
- Publishing activity data to MQTT
- Receiving gear selections from MQTT and writing them to Garmin Connect via the v2 API

**`Dockerfile`**
Builds a minimal Python 3.12 slim image. Dependencies are installed in a separate layer for efficient caching — rebuilds after code-only changes are fast.

**`compose.yaml`**
Defines the Docker service. References the pre-built image from GitHub Container Registry (ghcr.io) so the container host never needs to build locally. Mounts a named volume at `/data` to persist authentication tokens and state across container restarts.

**`build.yml`** (GitHub Actions)
Triggered automatically on any push to `main` that modifies `garmin_service.py`, `Dockerfile`, or `requirements.txt`. Builds the Docker image and pushes it to `ghcr.io/<your-username>/garmin-gear-bot:latest`. Deploying an update is just a `git push` followed by a container restart on the host.

**Node-RED flow** (not in this repo — lives in your Node-RED instance)
Three key nodes drive the integration:
- `Inject` — fires on a schedule and publishes to `garmin/trigger/check` to initiate activity checks
- `Build Telegram message` — formats the activity notification and constructs the inline keyboard
- `Handle gear selection` — receives the button tap, edits the message to show confirmation, and publishes the selection to MQTT

---

## Prerequisites

Before deploying, you will need:

- A Docker host with Docker Compose available
- An MQTT broker accessible from the Docker host (e.g. Mosquitto)
- A Node-RED instance with `node-red-contrib-telegrambot` installed
- A Telegram bot token (create one via [@BotFather](https://t.me/botfather))
- A Garmin Connect account with gear configured

### Finding Your Garmin Profile ID

The service requires your Garmin `profileId` — this is **not** the same as your numeric account `id`. To find it:

1. Log into [connect.garmin.com](https://connect.garmin.com)
2. Open your browser's developer tools (F12) → Network tab
3. Refresh the page and search for a request to `socialProfile`
4. Look for the `profileId` field in the response — it will be a 7-digit number

Alternatively, you can run the following one-liner with `garminconnect` installed locally:

```python
from garminconnect import Garmin
api = Garmin(email="you@example.com", password="yourpassword")
api.login()
import json
print(json.dumps(api.connectapi("/userprofile-service/socialProfile"), indent=2))
```

The `profileId` field is what you need — **not** the `id` field.

---

## Deployment

### 1. Configure Environment Variables

Copy `.env.example` to `.env` in the same directory as `compose.yaml` and fill in your values:

```bash
cp .env.example .env
nano .env
```

The `.env` file should **never be committed to version control**.

```env
# Garmin Connect credentials
GARMIN_EMAIL=you@example.com
GARMIN_PASSWORD=yourpassword

# Your Garmin profileId (NOT the 'id' field — see Prerequisites above)
GARMIN_PROFILE_ID=1234567

# MQTT broker
MQTT_HOST=192.168.1.x
MQTT_PORT=1883
MQTT_USER=
MQTT_PASSWORD=

# MQTT topics (defaults shown — only change if needed)
MQTT_TOPIC_TRIGGER=garmin/trigger/check
MQTT_TOPIC_NEW_ACTIVITY=garmin/activity/new
MQTT_TOPIC_GEAR_SELECT=garmin/activity/gear_select
```

### 2. Update the Compose File

Open `compose.yaml` and replace `yourgithubusername` with your actual GitHub username (lowercase):

```yaml
image: ghcr.io/yourgithubusername/garmin-gear-bot:latest
```

**Docker networking note:** If your MQTT broker runs in a separate Docker Compose stack, the container needs to be on the same Docker network to reach it by hostname. Uncomment and update the `networks` block in `compose.yaml`:

```yaml
networks:
  - your_shared_network_name

# and at the bottom:
networks:
  your_shared_network_name:
    external: true
```

If you're using an IP address for `MQTT_HOST` rather than a container hostname, no network changes are needed.

### 3. Pull and Start the Container

```bash
docker compose pull
docker compose up -d
```

### 4. Verify Startup

```bash
docker compose logs -f
```

A healthy startup looks like this:

```
2026-05-16 00:00:00  INFO      garmin_gear_bot starting up v20260516.
2026-05-16 00:00:00  INFO      Trigger topic: garmin/trigger/check | MQTT: mosquitto:1883
2026-05-16 00:00:00  INFO      Loading saved Garmin tokens from /data/garmin_tokens.pkl
2026-05-16 00:00:00  INFO      Garmin token login successful
2026-05-16 00:00:00  INFO      Last seen activity ID: 12345678901
2026-05-16 00:00:00  INFO      MQTT connected to mosquitto:1883
2026-05-16 00:00:00  INFO      Subscribed to trigger topic garmin/trigger/check
2026-05-16 00:00:00  INFO      Subscribed to garmin/activity/gear_select
2026-05-16 00:00:00  INFO      Waiting for trigger messages on garmin/trigger/check
```

When a trigger fires and no new activity is found:

```
2026-05-16 08:00:00  INFO      MQTT message received on garmin/trigger/check
2026-05-16 08:00:00  INFO      Activity check triggered by Node-RED
2026-05-16 08:00:00  INFO      No new activity (last seen: 12345678901)
```

When a new activity is detected:

```
2026-05-16 08:00:00  INFO      Activity check triggered by Node-RED
2026-05-16 08:00:00  INFO      New activity detected: Morning Run (ID: 12345678902, type: running)
2026-05-16 08:00:00  INFO      Publishing new activity to MQTT topic 'garmin/activity/new'
2026-05-16 08:00:00  INFO      Gear options: ['Nike Pegasus 40', 'Saucony Kinvara 14']
```

**First startup note:** On first run, there is no saved token file. The service will log into Garmin Connect using your email and password and save a token to `/data/garmin_tokens.pkl`. Garmin's SSO endpoint can occasionally return a 429 (rate limited) error on the first login attempt — this is normal. The service includes a fallback login method and will typically succeed despite the 429 warning. Once the token is saved, subsequent startups reuse it and never touch the SSO endpoint.

---

## Node-RED Setup

### Import the Flow

The Node-RED flow is not distributed in this repository as it contains personal configuration (chat ID, broker address). Use the JSON below as a starting template — import it via Node-RED's hamburger menu → Import → paste JSON.

After importing, configure the following nodes:

| Node | What to configure |
|---|---|
| `Inject` (trigger) | Set your desired check interval; configure MQTT out to point at `garmin/trigger/check` |
| `garmin/activity/new` (MQTT in) | Select your MQTT broker |
| `Build Telegram message` (function) | Replace `CHAT_ID` with your Telegram user ID |
| `Send via GarminGearBot` (Telegram sender) | Select your bot config |
| `Edit Telegram message` (Telegram sender) | Select your bot config |
| `garmin/activity/gear_select` (MQTT out) | Select your MQTT broker |

### Trigger Node Setup

The trigger is a standard Node-RED **Inject** node wired to an **MQTT Out** node:

- **Inject node** — set to repeat on an interval (e.g. every hour). The payload can be anything; the Python service ignores the message content and treats receipt as the signal to check.
- **MQTT Out node** — topic: `garmin/trigger/check` (or whatever you set `MQTT_TOPIC_TRIGGER` to in `.env`), QoS 1.

You can also wire additional trigger sources — a dashboard button, a flow that fires after your phone connects to home Wi-Fi, or any other event that makes sense for your routine.

### Finding Your Telegram Chat ID

Send any message to [@userinfobot](https://t.me/userinfobot) on Telegram. It will reply with your user ID — use this as `CHAT_ID` in the function node.

### Receiver Node Configuration

The Telegram receiver node must have **"Events: output all events (not only messages)"** enabled. This allows callback queries from inline button taps to pass through the node. Without this, button taps are silently dropped.

### Testing the Flow

Before recording a real activity, inject a test payload to verify the full Telegram flow:

1. Add a temporary inject node wired to the `Build Telegram message` function
2. Set the payload type to JSON and use:

```json
{
  "activity_id": 99999999,
  "activity_name": "Test Run",
  "activity_type": "running",
  "start_time": "2026-01-01 09:00:00",
  "distance": "8.05 km",
  "duration": "40:00",
  "gear_options": [
    {"uuid": "your-shoe-uuid-here", "name": "Your Shoe Name", "type": "Shoes"}
  ]
}
```

3. Click the inject button — you should receive a Telegram message with gear buttons
4. Tap a button — the message should update to show a confirmation

Note: using a fake `activity_id` (like `99999999`) means the gear write to Garmin Connect will fail, which is fine for testing the Telegram flow. Use a real activity ID if you want to test the full end-to-end write.

You can also test the trigger path end-to-end by clicking the Inject node's button manually — this fires a check immediately without waiting for the schedule.

---

## Updating the Service

Since the image is built automatically via GitHub Actions, updating is straightforward:

1. Make your changes to `garmin_service.py`, `Dockerfile`, or `requirements.txt`
2. Commit and push to `main`
3. Watch the **Actions** tab in GitHub to confirm the build succeeds
4. On your Docker host, pull the new image and restart:

```bash
docker compose pull
docker compose up -d
```

The named volume persists across updates — your auth tokens and last-seen activity ID are preserved.

---

## Gear Configuration

Gear is filtered by activity type automatically. The following activity types and gear type mappings are built in:

| Activity Types | Gear Shown |
|---|---|
| running, track_running, trail_running, treadmill_running, virtual_run, indoor_running | Shoes |
| cycling, road_biking, mountain_biking, indoor_cycling | Bike / Bikes |
| All others | All active gear |

Gear items must be **active** (not retired) in Garmin Connect to appear as options. Items with no display name fall back to the custom make/model field.

To add or modify activity type mappings, update the `ACTIVITY_GEAR_FILTER` dict in `garmin_service.py`.

---

## Troubleshooting

**No Telegram message received after a run**

- Check Docker logs: `docker compose logs -f`
- Confirm the activity appeared in Garmin Connect — the service only detects activities that have fully synced
- Check that the Node-RED trigger is firing: look for `Activity check triggered by Node-RED` in the logs
- Verify MQTT is working by subscribing to the trigger topic manually:
  ```bash
  mosquitto_sub -h your-broker-ip -t "garmin/trigger/check" -v
  ```
- Also check the activity output topic:
  ```bash
  mosquitto_sub -h your-broker-ip -t "garmin/activity/new" -v
  ```

**Telegram message arrives but gear buttons don't respond**

- Confirm the receiver node has "Events: output all events" enabled
- Check Node-RED debug panel for errors on the `n-tg-answer-err` debug node

**Gear association not appearing in Garmin Connect**

- Check Docker logs for `Gear association successful` — a 204 No Content response is the expected success
- Verify the gear UUID in the MQTT message matches a gear item in your Garmin account
- Check the MQTT topic directly to confirm Node-RED is publishing the selection:
  ```bash
  mosquitto_sub -h your-broker-ip -t "garmin/activity/gear_select" -v
  ```

**429 rate limit errors on startup**

- This is normal on first run or after a token expiry. The service has a fallback login method that typically succeeds regardless. If it fails entirely, wait 15–30 minutes and restart the container.

**Container starts but immediately exits**

- A required environment variable is missing. Check that `.env` exists and contains `GARMIN_EMAIL`, `GARMIN_PASSWORD`, and `GARMIN_PROFILE_ID`.

**Service is connected but never checks for activities**

- The Python service is passive — it only runs a check when it receives a trigger message on `garmin/trigger/check`. Verify the Node-RED Inject node is running on its schedule and that the MQTT Out node is connected to the right broker and topic.

---

## Architecture Notes

### Why not use the official Garmin Health API?

Garmin provides an official Health API with real webhooks, but it requires applying for partner access and is intended for commercial products. For personal automation, the community-maintained `garminconnect` Python library provides equivalent access via the same endpoints used by the Garmin Connect web app.

### Why is the schedule in Node-RED instead of the Python service?

Putting the schedule in Node-RED keeps the Python service simple and stateless with respect to timing — it does exactly one thing when asked. It also means you can adjust the check frequency, pause checks, or trigger a check manually (by clicking the Inject node) without touching the Python container at all. Any event in Node-RED can serve as a trigger: a time schedule, a dashboard button, a geofence event, or anything else.

### Why MQTT as the message bus?

MQTT decouples the Python service from Node-RED cleanly. Either component can restart independently without losing messages (QoS 1 ensures at-least-once delivery). It also makes the system extensible — other automations can subscribe to `garmin/activity/new` for their own purposes without modifying this service, and additional trigger sources can publish to `garmin/trigger/check` without any changes on the Python side.

### Token persistence

Garmin authentication tokens are persisted to `/data/garmin_tokens.pkl` inside the named Docker volume. This means the SSO login (which is rate-limited) only happens on first startup or after a token expiry. Tokens typically remain valid for several weeks. If the token expires during operation, the service detects the auth error and re-authenticates automatically, deleting the stale token file first.
