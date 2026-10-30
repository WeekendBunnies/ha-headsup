"""The hub: linked phones, profiles, and the connections to the phones.

A phone on the home network keeps a WebSocket open to WS_PATH. Messages go
there first; when the phone doesn't confirm in time (or isn't connected) they
go through the relay, if one is configured. Every payload is encrypted with
the phone's pairing key, also on the home network, so the transport itself
needs no trust (works over http or https, by name or by IP).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import hashlib
import json
import secrets
import time
from typing import Any

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.instance_id import async_get as async_get_instance_id
from homeassistant.util import dt as dt_util

from . import crypto, schedule
from .const import (
    ACK_TIMEOUT,
    CONF_ALARMS,
    CONF_DEFAULT_PROFILE,
    CONF_HOUSEHOLD,
    CONF_PHONES,
    CONF_PROFILES,
    DOMAIN,
    EVENT_ACTION,
    LOGGER,
    MAX_EVENT_AGE,
    MAX_RELAY_BLOB,
    MAX_SKEW,
    EVENT_PATH,
    HOUSEHOLD_PLAN,
    HOUSEHOLD_URL,
    MAX_HOUSEHOLD,
    RELAY_URL,
    WS_PATH,
)

SIGNAL_PHONE_UPDATE = f"{DOMAIN}_phone_update"


class Hub:
    """State for the one Heads-up config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.instance_id = ""
        self._sockets: dict[str, web.WebSocketResponse] = {}
        self._acks: dict[str, asyncio.Future[str]] = {}
        self.last_seen: dict[str, Any] = {}
        # Recently used hello nonces and event ids, with when they expire: replays are refused.
        self._used_nonces: dict[str, float] = {}
        self._used_events: dict[str, float] = {}
        # What each phone last said about its subscription (state, free_left, until).
        self.subscription: dict[str, dict[str, Any]] = {
            phone_id: {"state": phone["subscription"], "plan": phone.get("plan"), "via": phone.get("via")}
            for phone_id, phone in self.phones.items()
            if phone.get("subscription")
        }
        # The household as the relay last confirmed it: the owner and the phones it covers.
        self.household: dict[str, Any] = {"owner": None, "members": []}
        # Scheduled alarms per phone as last worked out, earliest first; what each phone confirmed.
        self.schedules: dict[str, list[dict[str, Any]]] = {}
        self._schedule_sent: dict[str, str] = {}
        self._schedule_relay_at: dict[str, tuple[str, float]] = {}
        self._schedule_lock = asyncio.Lock()

    async def async_setup(self) -> None:
        self.instance_id = await async_get_instance_id(self.hass)

    # Data ---------------------------------------------------------------

    @property
    def phones(self) -> dict[str, dict[str, Any]]:
        return self.entry.data.get(CONF_PHONES, {})

    @property
    def profiles(self) -> dict[str, dict[str, Any]]:
        return self.entry.options.get(CONF_PROFILES, {})

    def profile_id(self, name_or_id: str | None) -> str | None:
        """Accept a profile's id or its name; None means the default profile."""
        profiles = self.profiles
        if not name_or_id:
            default = self.entry.options.get(CONF_DEFAULT_PROFILE)
            return default if default in profiles else next(iter(profiles), None)
        if name_or_id in profiles:
            return name_or_id
        wanted = name_or_id.strip().lower()
        return next((pid for pid, p in profiles.items() if p["name"].lower() == wanted), None)

    @property
    def alarms(self) -> dict[str, dict[str, Any]]:
        return self.entry.options.get(CONF_ALARMS, {})

    def key(self, phone_id: str) -> bytes:
        return bytes.fromhex(self.phones[phone_id]["key"])

    def is_connected(self, phone_id: str) -> bool:
        ws = self._sockets.get(phone_id)
        return ws is not None and not ws.closed

    # Sending ------------------------------------------------------------

    @callback
    def async_send(self, phone_ids: list[str] | None, message: dict[str, Any], kind: str = "message") -> str:
        """Send to the given phones (None: all), each by the fastest path. Returns at once.

        kind "message" shows a notification; kind "clear" removes the one with the
        given tag, a running alarm screen and a pending snooze.
        """
        targets = list(self.phones) if phone_ids is None else [p for p in phone_ids if p in self.phones]
        message = {"id": secrets.token_hex(8), "ts": int(time.time()), **message}
        for phone_id in targets:
            self.entry.async_create_background_task(
                self.hass, self._send_one(phone_id, kind, message), f"{DOMAIN} send {message['id']}"
            )
        return message["id"]

    async def _send_one(self, phone_id: str, kind: str, message: dict[str, Any]) -> None:
        name = self.phones[phone_id].get("name", phone_id)
        blob = crypto.seal(self.key(phone_id), {"type": kind, **message})
        local = await self._send_local(phone_id, message["id"], blob)
        if local is True or phone_id not in self.phones:
            return  # shown, or the phone was removed (unlinked) while waiting for its receipt
        if local == "subscription_required":
            LOGGER.warning(
                "Heads-up %s not shown on %s: its free notifications are used up, it needs a subscription",
                message["id"], name,
            )
            self._subscription_needed(phone_id, "used_up")
            return
        # No receipt (or no home connection): also try the relay; the phone shows a message once.
        relay = await self._send_relay(phone_id, blob)
        if relay == "ok":
            return
        if relay == "subscription" and local is False:
            LOGGER.warning(
                "Heads-up %s not delivered to %s: away from home, and without a subscription "
                "notifications only arrive at home",
                message["id"], name,
            )
            self._subscription_needed(phone_id, None)
            return
        if local is None:
            LOGGER.info(
                "Heads-up %s sent to %s at home, no receipt within %s s%s", message["id"], name, ACK_TIMEOUT,
                " (the relay refused it: no subscription)" if relay == "subscription" else "",
            )
        else:
            LOGGER.warning("Heads-up %s not delivered to %s: not on the home network, and %s", message["id"], name, {
                "no_relay": "the phone has no relay registration",
                "too_large": "the message is too long for the relay (fewer entities or a shorter text helps)",
                "rate_limited": "the relay limit was reached (10 a minute, 200 a day)",
                "unregistered": "the phone's push registration has expired (opening the app renews it)",
            }.get(relay, "the relay could not deliver it"))

    async def _send_local(self, phone_id: str, msg_id: str, blob: str) -> bool | str | None:
        """True: shown. "subscription_required": received but not shown (free notifications used up).

        None: sent over the home connection, but no receipt in time. False: not sent.
        """
        ws = self._sockets.get(phone_id)
        if ws is None or ws.closed:
            return False
        future: asyncio.Future[str] = self.hass.loop.create_future()
        self._acks[msg_id] = future
        try:
            await ws.send_json({"type": "message", "id": msg_id, "blob": blob})
        except (ConnectionError, RuntimeError) as err:
            LOGGER.debug("Home connection to %s failed for %s: %r", phone_id, msg_id, err)
            self._acks.pop(msg_id, None)
            return False
        try:
            status = await asyncio.wait_for(future, ACK_TIMEOUT)
            return status if status == "subscription_required" else True
        except TimeoutError:
            return None
        finally:
            self._acks.pop(msg_id, None)

    async def _send_relay(self, phone_id: str, blob: str) -> str:
        """"ok", "subscription" (the phone has none, so the relay refuses), "no_relay",
        "too_large", "rate_limited", "unregistered" or "failed"."""
        relay = self.phones[phone_id].get("relay")
        if not RELAY_URL or not relay:
            return "no_relay"
        if len(blob) > MAX_RELAY_BLOB:
            return "too_large"
        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                RELAY_URL,
                json={"device": relay["device"], "key": relay["key"], "blob": blob},
                timeout=10,
            ) as resp:
                return {200: "ok", 402: "subscription", 410: "unregistered", 429: "rate_limited"}.get(resp.status, "failed")
        except Exception as err:  # noqa: BLE001 - logged, the message counts as not delivered
            LOGGER.warning("Relay error for %s: %r", phone_id, err)
            return "failed"

    @callback
    def _subscription_needed(self, phone_id: str, state: str | None) -> None:
        """A repair issue explaining that this phone needs a subscription."""
        if state is not None:
            self.subscription[phone_id] = {**self.subscription.get(phone_id, {}), "state": state, "free_left": 0}
            async_dispatcher_send(self.hass, SIGNAL_PHONE_UPDATE, phone_id)
        ir.async_create_issue(
            self.hass, DOMAIN, f"subscription_{phone_id}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="subscription_required",
            translation_placeholders={"phone": self.phones[phone_id].get("name", phone_id)},
        )

    @callback
    def _on_subscription(self, phone_id: str, sub: dict[str, Any]) -> None:
        state = sub.get("state")
        if state not in ("subscribed", "household", "free", "used_up"):
            return
        self.subscription[phone_id] = {k: sub.get(k) for k in ("state", "free_left", "until", "plan", "via")}
        if state in ("subscribed", "household"):
            ir.async_delete_issue(self.hass, DOMAIN, f"subscription_{phone_id}")
        async_dispatcher_send(self.hass, SIGNAL_PHONE_UPDATE, phone_id)
        # State, plan and how it is covered are kept across restarts (the household needs them);
        # the count changes with every free notification. A change reaches the household through
        # the update listener.
        kept = {"subscription": state, "plan": sub.get("plan"), "via": sub.get("via")}
        if any(self.phones[phone_id].get(k) != v for k, v in kept.items()):
            phones = {**self.phones, phone_id: {**self.phones[phone_id], **kept}}
            self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_PHONES: phones})

    # Household -----------------------------------------------------------------

    def household_owner(self) -> str | None:
        """The first phone with its own household plan."""
        return next((p for p in self.phones
                     if self.subscription.get(p, {}).get("via") == "own"
                     and self.subscription.get(p, {}).get("plan") == HOUSEHOLD_PLAN), None)

    async def async_update_household(self) -> None:
        """Tell the relay which phones share the owner's household plan, and the phones that it changed."""
        owner = self.household_owner()
        chosen = [p for p in self.entry.options.get(CONF_HOUSEHOLD, []) if p in self.phones]
        others = [p for p in (chosen or self.phones) if p != owner]
        members, left_out = others[:MAX_HOUSEHOLD - 1], others[MAX_HOUSEHOLD - 1:]
        accepted: list[str] = []
        if owner is not None and (relay := self.phones[owner].get("relay")):
            payload = {"owner": {"device": relay["device"], "key": relay["key"]},
                       "members": [{"device": r["device"], "key": r["key"]}
                                   for p in members if (r := self.phones[p].get("relay"))]}
            try:
                async with async_get_clientsession(self.hass).post(HOUSEHOLD_URL, json=payload, timeout=20) as resp:
                    answer = await resp.json() if resp.status == 200 else {}
                    if resp.status != 200:
                        LOGGER.warning("The relay refused the household of %s: %s", self.phones[owner].get("name"), resp.status)
            except Exception as err:  # noqa: BLE001 - tried again on the next change and twice a day
                LOGGER.warning("Could not update the household: %r", err)
                return
            by_device = {(self.phones[p].get("relay") or {}).get("device"): p for p in self.phones}
            accepted = [by_device[d] for d in answer.get("members", []) if d in by_device]
        before = self.household
        self.household = {"owner": owner, "members": accepted}
        # Only when the admin didn't choose: a phone beyond the limit gets a repair issue.
        for phone_id in self.phones:
            issue = f"household_full_{phone_id}"
            if owner is not None and not chosen and phone_id in left_out \
                    and self.subscription.get(phone_id, {}).get("via") != "own":
                ir.async_create_issue(
                    self.hass, DOMAIN, issue, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                    translation_key="household_full",
                    translation_placeholders={"phone": self.phones[phone_id].get("name", phone_id),
                                              "owner": self.phones[owner].get("name", owner)},
                )
            else:
                ir.async_delete_issue(self.hass, DOMAIN, issue)
        if before != self.household:
            for phone_id in self.phones:
                async_dispatcher_send(self.hass, SIGNAL_PHONE_UPDATE, phone_id)
                await self._hint(phone_id, {"type": "subscription_changed"})

    async def _hint(self, phone_id: str, message: dict[str, Any]) -> None:
        """A plain hint over the home connection (no content, so not encrypted)."""
        ws = self._sockets.get(phone_id)
        if ws is None or ws.closed:
            return
        try:
            await ws.send_json(message)
        except (ConnectionError, RuntimeError):
            pass

    async def async_push_profiles(self) -> None:
        """Tell every connected phone the current profiles (after a change in the options)."""
        for phone_id in list(self._sockets):
            if phone_id in self.phones:
                await self._send_profiles(phone_id)

    async def _send_profiles(self, phone_id: str) -> None:
        ws = self._sockets.get(phone_id)
        if ws is None or ws.closed:
            return
        payload = {"type": "profiles", "profiles": self.profiles}
        try:
            await ws.send_json({"type": "profiles", "blob": crypto.seal(self.key(phone_id), payload)})
        except (ConnectionError, RuntimeError) as err:
            LOGGER.debug("Could not send profiles to %s: %r", phone_id, err)

    # Scheduled alarms --------------------------------------------------------

    async def async_refresh_schedules(self) -> None:
        """Work out the scheduled alarms again; each phone gets its list when it changed."""
        async with self._schedule_lock:
            try:
                found = await schedule.async_work_out(self.hass, self.alarms, list(self.phones))
            except Exception:  # noqa: BLE001 - logged where it failed; the phones keep their lists
                return
            self.schedules = found
            for phone_id in list(self.phones):
                async_dispatcher_send(self.hass, SIGNAL_PHONE_UPDATE, phone_id)
                await self._send_schedule(phone_id)

    def _schedule_payload(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """The phone's list: each alarm as the message it shows, the profiles they use once.

        The phone replaces what it has up to "until" and keeps later alarms (a list
        cut short for the relay covers less).
        """
        alarms = []
        for entry in entries:
            profile = self.profile_id(entry.get("profile"))
            if profile is None:
                continue
            alarms.append({"at": int(entry["at"].timestamp()), "msg": {
                "id": entry["id"], "profile": profile, "title": entry["title"],
                "message": entry["message"], "tag": f"alarm_{entry['alarm']}",
            }})
        used = {a["msg"]["profile"] for a in alarms}
        return {
            "type": "schedule",
            "until": int((dt_util.utcnow() + timedelta(days=schedule.HORIZON_DAYS + 1)).timestamp()),
            "alarms": alarms,
            "profiles": {p: d for p, d in self.profiles.items() if p in used},
        }

    async def _send_schedule(self, phone_id: str, force: bool = False) -> None:
        """Over the home connection; otherwise through the relay: a changed list at once, the same one again after an hour."""
        entries = self.schedules.get(phone_id)
        if entries is None or phone_id not in self.phones:
            return
        payload = self._schedule_payload(entries)
        digest = hashlib.sha256(json.dumps([payload["alarms"], payload["profiles"]], sort_keys=True).encode()).hexdigest()
        if not force and self._schedule_sent.get(phone_id) == digest:
            return
        key = self.key(phone_id)
        if await self._send_local(phone_id, secrets.token_hex(8), crypto.seal(key, payload)) is True:
            self._schedule_sent[phone_id] = digest
            return
        tried_digest, tried_at = self._schedule_relay_at.get(phone_id, ("", 0.0))
        if tried_digest == digest and time.time() - tried_at < 3600:
            return
        self._schedule_relay_at[phone_id] = (digest, time.time())
        # The relay takes about 3.5 KB: the earliest alarms that fit. The rest follows at home.
        complete = len(payload["alarms"])
        blob = crypto.seal(key, payload)
        while len(blob) > MAX_RELAY_BLOB and payload["alarms"]:
            payload = {**payload, "alarms": payload["alarms"][:-1], "until": payload["alarms"][-1]["at"] - 1}
            blob = crypto.seal(key, payload)
        result = await self._send_relay(phone_id, blob)
        if result == "ok" and len(payload["alarms"]) == complete:
            self._schedule_sent[phone_id] = digest
        elif result != "ok":
            LOGGER.info("Scheduled alarms for %s wait until it is home again (%s)",
                        self.phones[phone_id].get("name", phone_id), result)

    def next_alarm(self, phone_id: str) -> dict[str, Any] | None:
        entries = self.schedules.get(phone_id) or []
        return next((e for e in entries if e["at"] > dt_util.now()), None)

    # Phone connections ---------------------------------------------------

    @callback
    def _attach(self, phone_id: str, ws: web.WebSocketResponse) -> None:
        old = self._sockets.get(phone_id)
        if old is not None and not old.closed:
            self.hass.async_create_task(old.close())
        self._sockets[phone_id] = ws
        self._seen(phone_id)

    @callback
    def _detach(self, phone_id: str, ws: web.WebSocketResponse) -> None:
        if self._sockets.get(phone_id) is ws:
            del self._sockets[phone_id]
        async_dispatcher_send(self.hass, SIGNAL_PHONE_UPDATE, phone_id)

    @callback
    def _seen(self, phone_id: str) -> None:
        self.last_seen[phone_id] = dt_util.utcnow()
        async_dispatcher_send(self.hass, SIGNAL_PHONE_UPDATE, phone_id)

    async def async_handle_socket(self, request: web.Request) -> web.StreamResponse:
        ws = web.WebSocketResponse(heartbeat=30)
        await ws.prepare(request)
        phone_id = await self._hello(ws)
        if phone_id is None:
            await ws.close()
            return ws
        self._attach(phone_id, ws)
        await self._send_profiles(phone_id)
        # Also when nothing changed: the phone may have been reinstalled or restored.
        self.entry.async_create_background_task(
            self.hass, self._send_schedule(phone_id, force=True), f"{DOMAIN} schedule {phone_id}"
        )
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                self._seen(phone_id)
                self._on_phone_message(phone_id, msg.json())
        finally:
            self._detach(phone_id, ws)
        return ws

    async def _hello(self, ws: web.WebSocketResponse) -> str | None:
        """Check the phone's hello and prove to the phone that this is its Home Assistant."""
        try:
            hello = await ws.receive_json(timeout=10)
            phone_id = str(hello.get("phone", ""))
            ts, nonce = int(hello.get("ts", 0)), str(hello.get("nonce", ""))
        except (TimeoutError, ValueError, TypeError, AttributeError):
            return None
        if phone_id not in self.phones:
            return None
        key = self.key(phone_id)
        now = time.time()
        if (
            abs(now - ts) > MAX_SKEW
            or not nonce
            or not crypto.mac_ok(key, f"hello|{phone_id}|{ts}|{nonce}", str(hello.get("mac", "")))
            or not self._first_use(self._used_nonces, f"{phone_id}|{nonce}", now + 2 * MAX_SKEW)
        ):
            LOGGER.warning("Rejected a Heads-up connection claiming to be %s", phone_id)
            return None
        await ws.send_json({
            "type": "welcome",
            "instance": self.instance_id,
            "proof": crypto.mac(key, f"proof|{self.instance_id}|{hello.get('challenge', '')}"),
        })
        return phone_id

    async def async_handle_event_post(self, request: web.Request) -> web.Response:
        """A button tap from a phone away from home; authenticated by decrypting with its key."""
        try:
            body = await request.json()
            phone_id = str(body["phone"])
            if phone_id not in self.phones:
                return web.Response(status=404)
            crypto.unseal(self.key(phone_id), body["blob"])
        except Exception:  # noqa: BLE001 - anything that doesn't decrypt is refused
            return web.Response(status=403)
        self._on_phone_message(phone_id, {"type": "event", "blob": body["blob"]})
        return web.json_response({"ok": True})

    @staticmethod
    def _first_use(used: dict[str, float], key: str, expires: float) -> bool:
        """Remember key until expires; False when it was already used."""
        now = time.time()
        for old in [k for k, until in used.items() if until < now]:
            del used[old]
        if key in used:
            return False
        used[key] = expires
        return True

    @callback
    def _on_phone_message(self, phone_id: str, msg: dict[str, Any]) -> None:
        if phone_id not in self.phones:
            return  # removed while its connection was still open
        kind = msg.get("type")
        if kind == "ack":
            future = self._acks.get(str(msg.get("id")))
            if future is not None and not future.done():
                future.set_result(str(msg.get("status", "shown")))
            return
        if kind != "event" or "blob" not in msg:
            return
        try:
            event = crypto.unseal(self.key(phone_id), msg["blob"])
        except Exception:  # noqa: BLE001 - a bad blob is ignored
            LOGGER.warning("Undecryptable event from %s", phone_id)
            return
        # A copy of an earlier event (or one older than a day) is ignored.
        try:
            age = time.time() - float(event.get("ts", 0))
        except (TypeError, ValueError):
            age = MAX_EVENT_AGE + 1
        if age > MAX_EVENT_AGE or age < -MAX_SKEW:
            # Also what a phone whose clock is far off would see: worth a line in the log.
            LOGGER.warning(
                "Ignored an event from %s: it is %d s %s (phone clock wrong, or sent long ago)",
                self.phones[phone_id].get("name", phone_id), abs(age), "old" if age > 0 else "in the future",
            )
            return
        if not self._first_use(self._used_events, f"{phone_id}|{event.get('eid')}", time.time() + MAX_EVENT_AGE + MAX_SKEW):
            LOGGER.debug("Ignored a repeated event from %s", phone_id)
            return
        if event.get("type") == "unlink":
            self._remove_phone(phone_id)
            return
        if event.get("type") == "action":
            self.hass.bus.async_fire(EVENT_ACTION, {
                "device_id": self.device_id(phone_id),
                "phone": self.phones[phone_id].get("name"),
                "message_id": event.get("message_id"),
                "action": event.get("action"),
                "tag": event.get("tag"),
                **({"count": event["count"]} if "count" in event else {}),
            })
        elif event.get("type") == "info":
            info = {k: event[k] for k in ("app_version", "model", "relay") if k in event}
            if info and any(self.phones[phone_id].get(k) != v for k, v in info.items()):
                phones = {**self.phones, phone_id: {**self.phones[phone_id], **info}}
                self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_PHONES: phones})
                device_id = self.device_id(phone_id)
                if device_id is not None:
                    dr.async_get(self.hass).async_update_device(
                        device_id, sw_version=info.get("app_version"), model=info.get("model")
                    )
            if isinstance(event.get("subscription"), dict):
                self._on_subscription(phone_id, event["subscription"])

    @callback
    def _remove_phone(self, phone_id: str) -> None:
        """The user unlinked the phone in the app: remove it here too."""
        LOGGER.info("%s was unlinked in the Heads-up app; removing it", self.phones[phone_id].get("name", phone_id))
        ir.async_delete_issue(self.hass, DOMAIN, f"subscription_{phone_id}")
        device_id = self.device_id(phone_id)
        phones = {k: v for k, v in self.phones.items() if k != phone_id}
        self.hass.config_entries.async_update_entry(self.entry, data={**self.entry.data, CONF_PHONES: phones})
        if device_id is not None:
            dr.async_get(self.hass).async_update_device(device_id, remove_config_entry_id=self.entry.entry_id)

    def device_id(self, phone_id: str) -> str | None:
        devices = dr.async_entries_for_config_entry(dr.async_get(self.hass), self.entry.entry_id)
        return next((d.id for d in devices if (DOMAIN, phone_id) in d.identifiers), None)

    def phone_for_device(self, device_id: str) -> str | None:
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None:
            return None
        return next((ident for dom, ident in device.identifiers if dom == DOMAIN), None)


class HeadsUpSocketView(HomeAssistantView):
    """The phone's connection on the home network; authenticated with the pairing key, not a login."""

    url = WS_PATH
    name = "api:headsup:ws"
    requires_auth = False

    def __init__(self, get_hub: Callable[[], Hub | None]) -> None:
        self._get_hub = get_hub

    async def get(self, request: web.Request) -> web.StreamResponse:
        hub = self._get_hub()
        if hub is None:
            return web.Response(status=404)
        return await hub.async_handle_socket(request)


class HeadsUpEventView(HomeAssistantView):
    """Encrypted button taps from phones away from home."""

    url = EVENT_PATH
    name = "api:headsup:event"
    requires_auth = False

    def __init__(self, get_hub: Callable[[], Hub | None]) -> None:
        self._get_hub = get_hub

    async def post(self, request: web.Request) -> web.Response:
        hub = self._get_hub()
        if hub is None:
            return web.Response(status=404)
        return await hub.async_handle_event_post(request)
