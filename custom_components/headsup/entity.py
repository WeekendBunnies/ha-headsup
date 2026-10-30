"""Shared entity base: one device per linked phone."""

from __future__ import annotations

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN
from .hub import SIGNAL_PHONE_UPDATE, Hub


class PhoneEntity(Entity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hub: Hub, phone_id: str, key: str) -> None:
        self.hub = hub
        self.phone_id = phone_id
        self._attr_unique_id = f"{phone_id}-{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, phone_id)})

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_PHONE_UPDATE, self._on_update)
        )

    @callback
    def _on_update(self, phone_id: str) -> None:
        if phone_id == self.phone_id:
            self.async_write_ha_state()
