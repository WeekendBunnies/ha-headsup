"""Constants for Heads-up."""

from __future__ import annotations

import logging

DOMAIN = "headsup"
LOGGER = logging.getLogger(__package__)

# Config entry: data holds the linked phones, options hold the profiles.
CONF_PHONES = "phones"
CONF_PROFILES = "profiles"
CONF_DEFAULT_PROFILE = "default_profile"
# The phones the admin put in the household (empty: the first linked phones).
CONF_HOUSEHOLD = "household"
# Scheduled alarms (schedule.py), also in the options.
CONF_ALARMS = "alarms"

EVENT_ACTION = "headsup_action"
SERVICE_SEND = "send"
SERVICE_CLEAR = "clear"

# The phone keeps this WebSocket open while it is on the home network.
WS_PATH = "/api/headsup/ws"
# Button taps from a phone away from home (through HA's internet address).
EVENT_PATH = "/api/headsup/event"

# Paths on the phone's small pairing server (only listening while pairing).
PHONE_PAIR_START = "/pair/start"
PHONE_PAIR_CONFIRM = "/pair/confirm"

# A message sent over the home network must be confirmed within this time,
# otherwise it goes through the relay.
ACK_TIMEOUT = 5.0
# Allowed clock difference for the phone's hello on the WebSocket.
MAX_SKEW = 120
# An event from a phone (button tap) older than this is ignored; also the time
# its id is remembered, so a copied event can't be replayed.
MAX_EVENT_AGE = 24 * 3600

# Firebase Cloud Functions only forward a data message up to 4 KB; the relay
# refuses larger blobs.
MAX_RELAY_BLOB = 3500

# Firebase Cloud Function that forwards encrypted messages to phones away
# from home (Firebase project of WeekendBunnies).
RELAY_URL = "https://europe-west4-headsup-weekendbunnies.cloudfunctions.net/send"
HOUSEHOLD_URL = "https://europe-west4-headsup-weekendbunnies.cloudfunctions.net/household"
# The household plan: up to this many phones on one Home Assistant, the owner's included.
HOUSEHOLD_PLAN = "household-yearly"
MAX_HOUSEHOLD = 6

# Tones and vibration patterns built into the app.
SOUNDS = ["default", "chime", "bell", "beep", "soft", "siren", "alarm", "none"]
VIBRATIONS = ["default", "short", "long", "pulse", "sos", "none"]
IMPORTANCE = ["low", "default", "high"]
BUTTONS = ["acknowledge", "snooze"]
