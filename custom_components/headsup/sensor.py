"""Per phone: when it was last in touch over the home network, its subscription, its next scheduled alarm."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from . import HeadsUpConfigEntry
from .entity import PhoneEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: HeadsUpConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    hub = entry.runtime_data
    async_add_entities(
        entity
        for phone_id in hub.phones
        for entity in (
            LastSeen(hub, phone_id, "last_seen"),
            Subscription(hub, phone_id, "subscription"),
            NextAlarm(hub, phone_id, "next_alarm"),
        )
    )


class LastSeen(PhoneEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> datetime | None:
        return self.hub.last_seen.get(self.phone_id)


class Subscription(PhoneEntity, SensorEntity):
    """Subscribed, in a household, free (10 notifications, at home only) or free notifications used up."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["subscribed", "household", "free", "used_up"]

    @property
    def native_value(self) -> str | None:
        return self.hub.subscription.get(self.phone_id, {}).get("state")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sub = self.hub.subscription.get(self.phone_id, {})
        until = sub.get("until")
        return {
            "plan": sub.get("plan"),
            "free_left": sub.get("free_left"),
            "paid_until": dt_util.utc_from_timestamp(until / 1000).isoformat() if until else None,
        }


class NextAlarm(PhoneEntity, SensorEntity):
    """When this phone's next scheduled alarm rings, as Home Assistant worked it out."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self) -> datetime | None:
        entry = self.hub.next_alarm(self.phone_id)
        return entry["at"] if entry else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        entry = self.hub.next_alarm(self.phone_id)
        alarm = self.hub.alarms.get(entry["alarm"], {}) if entry else {}
        return {"title": entry["title"] if entry else None, "alarm": alarm.get("name")}
