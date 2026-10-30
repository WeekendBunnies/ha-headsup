"""Device action "Send heads-up", for picking a phone in the automation editor."""

from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.const import ATTR_DEVICE_ID, CONF_DEVICE_ID, CONF_DOMAIN, CONF_TYPE
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import config_validation as cv, device_registry as dr, selector
from homeassistant.helpers.typing import ConfigType, TemplateVarsType

from .const import CONF_PROFILES, DOMAIN, SERVICE_CLEAR, SERVICE_SEND

ACTION_SEND = "send"
ACTION_CLEAR = "clear"

ACTION_SCHEMA = vol.Any(
    cv.DEVICE_ACTION_BASE_SCHEMA.extend({
        vol.Required(CONF_TYPE): ACTION_SEND,
        vol.Optional("profile"): cv.string,
        vol.Optional("title"): cv.template,
        vol.Required("message"): cv.template,
        vol.Optional("entities"): cv.entity_ids,
        vol.Optional("tag"): cv.string,
    }),
    cv.DEVICE_ACTION_BASE_SCHEMA.extend({
        vol.Required(CONF_TYPE): ACTION_CLEAR,
        vol.Required("tag"): cv.string,
    }),
)


async def async_get_actions(hass: HomeAssistant, device_id: str) -> list[dict[str, Any]]:
    device = dr.async_get(hass).async_get(device_id)
    if device is None or not any(dom == DOMAIN for dom, _ in device.identifiers):
        return []
    return [{CONF_DOMAIN: DOMAIN, CONF_DEVICE_ID: device_id, CONF_TYPE: t} for t in (ACTION_SEND, ACTION_CLEAR)]


async def async_get_action_capabilities(hass: HomeAssistant, config: ConfigType) -> dict[str, vol.Schema]:
    if config[CONF_TYPE] == ACTION_CLEAR:
        return {"extra_fields": vol.Schema({vol.Required("tag"): selector.TextSelector()})}
    hub = hass.data.get(DOMAIN)
    profiles = hub.entry.options.get(CONF_PROFILES, {}) if hub else {}
    return {"extra_fields": vol.Schema({
        vol.Required("profile"): selector.SelectSelector(selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=pid, label=p["name"]) for pid, p in profiles.items()],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )),
        vol.Optional("title"): selector.TemplateSelector(),
        vol.Required("message"): selector.TemplateSelector(),
        vol.Optional("entities"): selector.EntitySelector(selector.EntitySelectorConfig(multiple=True)),
        vol.Optional("tag"): selector.TextSelector(),
    })}


async def async_call_action_from_config(
    hass: HomeAssistant, config: ConfigType, variables: TemplateVarsType, context: Context | None
) -> None:
    data: dict[str, Any] = {ATTR_DEVICE_ID: [config[CONF_DEVICE_ID]]}
    if config[CONF_TYPE] == ACTION_CLEAR:
        data["tag"] = config["tag"]
        await hass.services.async_call(DOMAIN, SERVICE_CLEAR, data, blocking=True, context=context)
        return
    for key in ("title", "message"):
        if key in config:
            config[key].hass = hass
            data[key] = config[key].async_render(variables, parse_result=False)
    for key in ("profile", "entities", "tag"):
        if config.get(key):
            data[key] = config[key]
    await hass.services.async_call(DOMAIN, SERVICE_SEND, data, blocking=True, context=context)
