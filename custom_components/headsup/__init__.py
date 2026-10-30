"""Heads-up: notifications with their own behaviour per profile, on Android phones."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from datetime import timedelta

from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.start import async_at_started
from homeassistant.helpers.typing import ConfigType

from .const import CONF_PHONES, DOMAIN, SERVICE_CLEAR, SERVICE_SEND
from .hub import HeadsUpEventView, HeadsUpSocketView, Hub
from .schedule import REFRESH

PLATFORMS: list[Platform] = [Platform.NOTIFY, Platform.BINARY_SENSOR, Platform.SENSOR]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

SEND_SCHEMA = vol.Schema({
    vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
    vol.Optional("profile"): cv.string,
    vol.Optional("title"): cv.string,
    vol.Required("message"): cv.string,
    vol.Optional("entities"): cv.entity_ids,
    vol.Optional("buttons"): vol.All(cv.ensure_list, [vol.Any(cv.string, {vol.Required("id"): cv.string, vol.Required("title"): cv.string})]),
    vol.Optional("tag"): cv.string,
    vol.Optional("ttl"): vol.All(vol.Coerce(int), vol.Range(min=0)),
})

CLEAR_SCHEMA = vol.Schema({
    vol.Optional(ATTR_DEVICE_ID): vol.All(cv.ensure_list, [cv.string]),
    vol.Required("tag"): cv.string,
})

type HeadsUpConfigEntry = ConfigEntry[Hub]


def get_hub(hass: HomeAssistant) -> Hub | None:
    return hass.data.get(DOMAIN)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    hass.http.register_view(HeadsUpSocketView(lambda: get_hub(hass)))
    hass.http.register_view(HeadsUpEventView(lambda: get_hub(hass)))

    def targets(call: ServiceCall) -> tuple[Hub, list[str] | None]:
        hub = get_hub(hass)
        if hub is None:
            raise ServiceValidationError("Heads-up is not set up")
        phone_ids = None
        if call.data.get(ATTR_DEVICE_ID):
            phone_ids = [hub.phone_for_device(d) for d in call.data[ATTR_DEVICE_ID]]
            if None in phone_ids:
                raise ServiceValidationError("Unknown Heads-up device")
        return hub, phone_ids

    async def send(call: ServiceCall) -> None:
        hub, phone_ids = targets(call)
        hub.async_send(phone_ids, build_message(hass, hub, call.data))

    async def clear(call: ServiceCall) -> None:
        hub, phone_ids = targets(call)
        hub.async_send(phone_ids, {"tag": call.data["tag"]}, kind="clear")

    hass.services.async_register(DOMAIN, SERVICE_SEND, send, schema=SEND_SCHEMA)
    hass.services.async_register(DOMAIN, SERVICE_CLEAR, clear, schema=CLEAR_SCHEMA)
    return True


def build_message(hass: HomeAssistant, hub: Hub, data: dict[str, Any]) -> dict[str, Any]:
    """The message as the phone gets it: profile resolved, entity states filled in."""
    profile = hub.profile_id(data.get("profile"))
    if profile is None:
        raise ServiceValidationError(f"Unknown profile: {data.get('profile') or '(none defined yet)'}")
    # The profile goes along, so a phone that was away when it was made or changed still knows it.
    message: dict[str, Any] = {"profile": profile, "profile_def": hub.profiles[profile], "message": data["message"]}
    for key in ("title", "tag", "ttl"):
        if data.get(key) not in (None, ""):
            message[key] = data[key]
    if data.get("entities"):
        rows = []
        for entity_id in data["entities"]:
            state = hass.states.get(entity_id)
            if state is None:
                continue
            unit = state.attributes.get("unit_of_measurement")
            rows.append({
                "name": state.attributes.get("friendly_name", entity_id),
                "state": f"{state.state} {unit}" if unit else state.state,
            })
        message["entities"] = rows
    if data.get("buttons"):
        message["buttons"] = [b if isinstance(b, dict) else {"id": b, "title": b} for b in data["buttons"]]
    return message


async def async_setup_entry(hass: HomeAssistant, entry: HeadsUpConfigEntry) -> bool:
    hub = Hub(hass, entry)
    await hub.async_setup()
    entry.runtime_data = hub
    hass.data[DOMAIN] = hub
    device_registry = dr.async_get(hass)
    for phone_id, phone in entry.data.get(CONF_PHONES, {}).items():
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, phone_id)},
            name=phone.get("name", "Phone"),
            manufacturer="Heads-up",
            model=phone.get("model"),
            sw_version=phone.get("app_version"),
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_updated))
    # The household follows changes (update listener) and is renewed twice a day,
    # so a subscription that ended or was renewed reaches the relay's list.
    # Both timers run on the event loop (@callback); a plain function would run in a worker thread.
    @callback
    def household(_now: Any = None) -> None:
        entry.async_create_background_task(hass, hub.async_update_household(), "headsup household")

    entry.async_on_unload(async_track_time_interval(hass, household, timedelta(hours=12)))
    household()

    # Scheduled alarms follow the calendars every 15 minutes; first once the calendars are loaded.
    @callback
    def refresh(_now: Any = None) -> None:
        entry.async_create_background_task(hass, hub.async_refresh_schedules(), "headsup schedules")

    entry.async_on_unload(async_track_time_interval(hass, refresh, REFRESH))
    entry.async_on_unload(async_at_started(hass, refresh))
    return True


async def _async_updated(hass: HomeAssistant, entry: HeadsUpConfigEntry) -> None:
    """Profiles or alarms changed: tell the phones. A new phone: reload so it gets its entities."""
    hub = entry.runtime_data
    known = {ident for d in dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
             for dom, ident in d.identifiers if dom == DOMAIN}
    if set(entry.data.get(CONF_PHONES, {})) - known:
        await hass.config_entries.async_reload(entry.entry_id)
        return
    await hub.async_push_profiles()
    await hub.async_update_household()
    await hub.async_refresh_schedules()


async def async_unload_entry(hass: HomeAssistant, entry: HeadsUpConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data.pop(DOMAIN, None)
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: HeadsUpConfigEntry, device: dr.DeviceEntry
) -> bool:
    """Deleting a phone's device unlinks the phone."""
    phone_id = next((ident for dom, ident in device.identifiers if dom == DOMAIN), None)
    phones = dict(entry.data.get(CONF_PHONES, {}))
    if phone_id in phones:
        del phones[phone_id]
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_PHONES: phones})
    return True
