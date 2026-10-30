"""A notify entity per phone, using the default profile (the send action offers all options)."""

from __future__ import annotations

from homeassistant.components.notify import NotifyEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HeadsUpConfigEntry, build_message
from .entity import PhoneEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: HeadsUpConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    async_add_entities(PhoneNotify(hub, phone_id, "notify") for phone_id in hub.phones)


class PhoneNotify(PhoneEntity, NotifyEntity):
    _attr_name = None

    async def async_send_message(self, message: str, title: str | None = None) -> None:
        data = {"message": message, **({"title": title} if title else {})}
        self.hub.async_send([self.phone_id], build_message(self.hass, self.hub, data))
