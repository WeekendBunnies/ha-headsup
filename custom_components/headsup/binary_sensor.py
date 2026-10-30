"""Whether a phone is connected over the home network right now."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HeadsUpConfigEntry
from .entity import PhoneEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: HeadsUpConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    async_add_entities(HomeConnection(hub, phone_id, "home_connection") for phone_id in hub.phones)


class HomeConnection(PhoneEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        return self.hub.is_connected(self.phone_id)
