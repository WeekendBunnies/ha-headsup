"""Setting up Heads-up: the hub entry, linking phones, and the profiles (in Configure)."""

from __future__ import annotations

import re
import secrets
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.components import network
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.instance_id import async_get as async_get_instance_id
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from homeassistant.helpers.translation import async_get_translations

from . import crypto
from .const import (
    BUTTONS,
    CONF_ALARMS,
    CONF_DEFAULT_PROFILE,
    CONF_HOUSEHOLD,
    CONF_PHONES,
    CONF_PROFILES,
    DOMAIN,
    IMPORTANCE,
    LOGGER,
    MAX_HOUSEHOLD,
    PHONE_PAIR_CONFIRM,
    PHONE_PAIR_START,
    SOUNDS,
    VIBRATIONS,
)
from .schedule import WEEKDAYS

PAIR_TIMEOUT = aiohttp.ClientTimeout(total=10)


class HeadsUpConfigFlow(ConfigFlow, domain=DOMAIN):
    """One hub entry; phones are linked into it when they are discovered."""

    VERSION = 1

    def __init__(self) -> None:
        self._phone: dict[str, Any] = {}
        self._private = None
        self._key = b""
        self._code = ""

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return ProfilesOptionsFlow()

    def _hub(self) -> ConfigEntry | None:
        return next(iter(self._async_current_entries(include_ignore=False)), None)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Added by hand: just the hub. Phones show up by themselves once the app runs."""
        if self._hub() is not None:
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            await self.async_set_unique_id(DOMAIN)
            return self.async_create_entry(title="Heads-up", data={CONF_PHONES: {}}, options={CONF_PROFILES: {}})
        return self.async_show_form(step_id="user")

    async def async_step_zeroconf(self, discovery_info: ZeroconfServiceInfo) -> ConfigFlowResult:
        """A phone with the app open announced itself on the network."""
        props = discovery_info.properties
        phone_id, public_key = props.get("id"), props.get("pk")
        if not phone_id or not public_key:
            return self.async_abort(reason="not_supported")
        # The app names the Home Assistant the user picked; other instances stay out.
        wanted = props.get("ha")
        if wanted and wanted != await async_get_instance_id(self.hass):
            return self.async_abort(reason="other_instance")
        hub = self._hub()
        if hub is not None and phone_id in hub.data.get(CONF_PHONES, {}):
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(f"phone-{phone_id}")
        self._abort_if_unique_id_configured()
        host = next((str(a) for a in discovery_info.ip_addresses if a.version == 4), discovery_info.host)
        self._phone = {
            "id": phone_id,
            "pk": public_key,
            "name": props.get("name") or "Phone",
            "model": props.get("model"),
            "url": f"http://{host}:{discovery_info.port}",
        }
        self.context["title_placeholders"] = {"name": self._phone["name"]}
        return await self.async_step_pair()

    async def async_step_pair(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Both screens show the same code; the user confirms it here."""
        errors: dict[str, str] = {}
        if user_input is None:
            try:
                await self._start_pairing()
            except (aiohttp.ClientError, TimeoutError, ValueError) as err:
                LOGGER.warning("Could not reach the phone to link it: %r", err)
                return self.async_abort(reason="cannot_connect")
        else:
            try:
                phone = await self._confirm_pairing()
            except (aiohttp.ClientError, TimeoutError, ValueError, KeyError) as err:
                LOGGER.warning("Linking the phone failed: %r", err)
                errors["base"] = "pair_failed"
            else:
                return await self._store_phone(phone)
        return self.async_show_form(
            step_id="pair",
            description_placeholders={"name": self._phone["name"], "code": self._code},
            errors=errors,
        )

    async def _start_pairing(self) -> None:
        if not self._code:
            self._private = crypto.new_private_key()
            self._key, self._code = crypto.derive(self._private, self._phone["pk"])
        session = async_get_clientsession(self.hass)
        async with session.post(
            self._phone["url"] + PHONE_PAIR_START,
            json={
                "pk": crypto.public_b64(self._private),
                "instance": await async_get_instance_id(self.hass),
                "name": self.hass.config.location_name,
            },
            timeout=PAIR_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"phone answered {resp.status}")

    async def _confirm_pairing(self) -> dict[str, Any]:
        """Send the phone how to reach this Home Assistant; it answers with its relay details."""
        local: dict[str, Any] = {
            "ip": await network.async_get_source_ip(self.hass),
            "port": self.hass.http.server_port,
            "ssl": bool(self.hass.config.api and self.hass.config.api.use_ssl),
        }
        urls = {}
        for name, kwargs in (("internal_url", {"allow_external": False}), ("external_url", {"allow_internal": False})):
            try:
                urls[name] = get_url(self.hass, **kwargs)
            except NoURLAvailableError:
                urls[name] = None
        hub = self._hub()
        payload = {
            "phone": self._phone["id"],
            "instance": await async_get_instance_id(self.hass),
            "name": self.hass.config.location_name,
            "local": local,
            **urls,
            "profiles": hub.options.get(CONF_PROFILES, {}) if hub else {},
        }
        session = async_get_clientsession(self.hass)
        async with session.post(
            self._phone["url"] + PHONE_PAIR_CONFIRM,
            json={"blob": crypto.seal(self._key, payload)},
            timeout=PAIR_TIMEOUT,
        ) as resp:
            if resp.status != 200:
                raise ValueError(f"phone answered {resp.status}")
            answer = crypto.unseal(self._key, (await resp.json())["blob"])
        return {
            "name": self._phone["name"],
            "model": answer.get("model") or self._phone.get("model"),
            "app_version": answer.get("app_version"),
            "key": self._key.hex(),
            "relay": answer.get("relay"),
        }

    async def _store_phone(self, phone: dict[str, Any]) -> ConfigFlowResult:
        hub = self._hub()
        if hub is None:
            await self.async_set_unique_id(DOMAIN, raise_on_progress=False)
            return self.async_create_entry(
                title="Heads-up",
                data={CONF_PHONES: {self._phone["id"]: phone}},
                options={CONF_PROFILES: {}},
            )
        phones = {**hub.data.get(CONF_PHONES, {}), self._phone["id"]: phone}
        self.hass.config_entries.async_update_entry(hub, data={**hub.data, CONF_PHONES: phones})
        return self.async_abort(reason="phone_added", description_placeholders={"name": phone["name"]})


def _profile_schema(profile: dict[str, Any]) -> vol.Schema:
    def select(options: list[str], key: str) -> selector.SelectSelector:
        return selector.SelectSelector(selector.SelectSelectorConfig(
            options=options, translation_key=key, mode=selector.SelectSelectorMode.DROPDOWN,
        ))

    return vol.Schema({
        vol.Required("name", default=profile.get("name", "")): selector.TextSelector(),
        vol.Required("sound", default=profile.get("sound", "default")): select(SOUNDS, "sound"),
        vol.Required("vibration", default=profile.get("vibration", "default")): select(VIBRATIONS, "vibration"),
        vol.Required("importance", default=profile.get("importance", "high")): select(IMPORTANCE, "importance"),
        vol.Required("alarm_stream", default=profile.get("alarm_stream", False)): selector.BooleanSelector(),
        vol.Required("bypass_dnd", default=profile.get("bypass_dnd", False)): selector.BooleanSelector(),
        vol.Required("full_screen", default=profile.get("full_screen", False)): selector.BooleanSelector(),
        # Profiles from before have repeat_seconds; only "above 0" ever counted.
        vol.Required("repeat", default=bool(profile.get("repeat", int(profile.get("repeat_seconds", 0)) > 0))): (
            selector.BooleanSelector()
        ),
        vol.Required("buttons", default=profile.get("buttons", [])): selector.SelectSelector(selector.SelectSelectorConfig(
            options=BUTTONS, multiple=True, translation_key="buttons", mode=selector.SelectSelectorMode.LIST,
        )),
        vol.Required("snooze_minutes", default=profile.get("snooze_minutes", 10)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=1, max=120, step=1, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Required("ring_minutes", default=profile.get("ring_minutes", 2)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=60, step=1, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX)
        ),
        vol.Required("auto_snoozes", default=profile.get("auto_snoozes", 3)): selector.NumberSelector(
            selector.NumberSelectorConfig(min=0, max=10, step=1, mode=selector.NumberSelectorMode.BOX)
        ),
    })


def _minutes(low: int, high: int) -> selector.NumberSelector:
    return selector.NumberSelector(selector.NumberSelectorConfig(
        min=low, max=high, step=1, unit_of_measurement="min", mode=selector.NumberSelectorMode.BOX,
    ))


class ProfilesOptionsFlow(OptionsFlow):
    """Configure: the profiles the phones use, the scheduled alarms, the household."""

    def __init__(self) -> None:
        self._editing: str | None = None
        self._alarm: str | None = None
        # The alarm being made or changed, until "Save the alarm" in the conditions step.
        self._pending: dict[str, Any] | None = None
        self._state_condition: dict[str, Any] = {}

    @property
    def _profiles(self) -> dict[str, dict[str, Any]]:
        return dict(self.config_entry.options.get(CONF_PROFILES, {}))

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        menu = ["add_profile"]
        if self._profiles:
            menu += ["edit_profile", "delete_profile", "default_profile", "add_alarm"]
        if self._alarms:
            menu += ["edit_alarm", "delete_alarm"]
        if len(self.config_entry.data.get(CONF_PHONES, {})) >= 2:
            menu.append("household")
        return self.async_show_menu(step_id="init", menu_options=menu)

    async def async_step_household(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Which phones share a household plan; none chosen: the first linked phones."""
        phones = self.config_entry.data.get(CONF_PHONES, {})
        errors: dict[str, str] = {}
        if user_input is not None:
            chosen = [p for p in user_input.get("phones", []) if p in phones]
            if len(chosen) > MAX_HOUSEHOLD:
                errors["phones"] = "household_too_many"
            else:
                return self.async_create_entry(data={**self.config_entry.options, CONF_HOUSEHOLD: chosen})
        return self.async_show_form(
            step_id="household",
            data_schema=vol.Schema({vol.Optional("phones", default=self.config_entry.options.get(CONF_HOUSEHOLD, [])):
                selector.SelectSelector(selector.SelectSelectorConfig(
                    options=[selector.SelectOptionDict(value=p, label=v.get("name", p)) for p, v in phones.items()],
                    multiple=True, mode=selector.SelectSelectorMode.LIST,
                ))}),
            errors=errors,
            description_placeholders={"max": str(MAX_HOUSEHOLD)},
        )

    def _save(self, profiles: dict[str, dict[str, Any]], **extra: Any) -> ConfigFlowResult:
        options = {**self.config_entry.options, CONF_PROFILES: profiles, **extra}
        if options.get(CONF_DEFAULT_PROFILE) not in profiles:
            options[CONF_DEFAULT_PROFILE] = next(iter(profiles), None)
        return self.async_create_entry(data=options)

    def _choice(self) -> selector.SelectSelector:
        return selector.SelectSelector(selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=pid, label=p["name"]) for pid, p in self._profiles.items()],
            mode=selector.SelectSelectorMode.LIST,
        ))

    async def async_step_add_profile(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._profile_form("add_profile", None, user_input)

    async def async_step_edit_profile(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._editing = user_input["profile"]
            return await self.async_step_profile()
        return self.async_show_form(step_id="edit_profile", data_schema=vol.Schema({vol.Required("profile"): self._choice()}))

    async def async_step_profile(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._profile_form("profile", self._editing, user_input)

    async def _profile_form(self, step_id: str, profile_id: str | None, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        profiles = self._profiles
        current = profiles.get(profile_id, {}) if profile_id else {}
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input["name"].strip()
            if not name:
                errors["name"] = "name_required"
            elif any(p["name"].lower() == name.lower() and pid != profile_id for pid, p in profiles.items()):
                errors["name"] = "name_taken"
            else:
                pid = profile_id or (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "profile") + "_" + secrets.token_hex(2)
                profiles[pid] = {
                    **user_input,
                    "name": name,
                    "snooze_minutes": int(user_input["snooze_minutes"]),
                    "ring_minutes": int(user_input["ring_minutes"]),
                    "auto_snoozes": int(user_input["auto_snoozes"]),
                    # The phone makes a new Android channel when the revision changes.
                    "rev": int(current.get("rev", 0)) + 1,
                }
                return self._save(profiles)
        return self.async_show_form(step_id=step_id, data_schema=_profile_schema(user_input or current), errors=errors)

    async def async_step_delete_profile(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            profiles = self._profiles
            profiles.pop(user_input["profile"], None)
            return self._save(profiles)
        return self.async_show_form(step_id="delete_profile", data_schema=vol.Schema({vol.Required("profile"): self._choice()}))

    async def async_step_default_profile(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._save(self._profiles, **{CONF_DEFAULT_PROFILE: user_input["profile"]})
        return self.async_show_form(
            step_id="default_profile",
            data_schema=vol.Schema({vol.Required("profile", default=self.config_entry.options.get(CONF_DEFAULT_PROFILE)): self._choice()}),
        )

    # Scheduled alarms (schedule.py) ------------------------------------------

    @property
    def _alarms(self) -> dict[str, dict[str, Any]]:
        return dict(self.config_entry.options.get(CONF_ALARMS, {}))

    def _alarm_choice(self) -> selector.SelectSelector:
        return selector.SelectSelector(selector.SelectSelectorConfig(
            options=[selector.SelectOptionDict(value=aid, label=a["name"]) for aid, a in self._alarms.items()],
            mode=selector.SelectSelectorMode.LIST,
        ))

    async def async_step_add_alarm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._alarm = None
        self._pending = None
        return self.async_show_menu(step_id="add_alarm", menu_options=["weekly", "calendar"])

    async def async_step_edit_alarm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._alarm = user_input["alarm"]
            self._pending = None
            if self._alarms[self._alarm].get("kind") == "calendar":
                return await self.async_step_calendar()
            return await self.async_step_weekly()
        return self.async_show_form(step_id="edit_alarm", data_schema=vol.Schema({vol.Required("alarm"): self._alarm_choice()}))

    async def async_step_delete_alarm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            alarms = self._alarms
            alarms.pop(user_input["alarm"], None)
            return self.async_create_entry(data={**self.config_entry.options, CONF_ALARMS: alarms})
        return self.async_show_form(step_id="delete_alarm", data_schema=vol.Schema({vol.Required("alarm"): self._alarm_choice()}))

    def _alarm_common(self, alarm: dict[str, Any]) -> dict[Any, Any]:
        """Name, message, profile and phones: the same for both kinds."""
        phones = self.config_entry.data.get(CONF_PHONES, {})
        profiles = self._profiles
        profile = alarm.get("profile")
        if profile not in profiles:
            default = self.config_entry.options.get(CONF_DEFAULT_PROFILE)
            profile = default if default in profiles else next(iter(profiles))
        return {
            vol.Required("name", default=alarm.get("name", "")): selector.TextSelector(),
            vol.Optional("message", description={"suggested_value": alarm.get("message")}): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
            vol.Required("profile", default=profile): selector.SelectSelector(selector.SelectSelectorConfig(
                options=[selector.SelectOptionDict(value=pid, label=p["name"]) for pid, p in profiles.items()],
                mode=selector.SelectSelectorMode.DROPDOWN,
            )),
            vol.Optional("phones", default=[p for p in alarm.get("phones", []) if p in phones]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[selector.SelectOptionDict(value=p, label=v.get("name", p)) for p, v in phones.items()],
                    multiple=True, mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }

    async def async_step_weekly(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """On set days at a set time; moved past appointments in the chosen calendars."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("days"):
                errors["days"] = "days_required"
            elif self._take_alarm("weekly", user_input, errors):
                return await self.async_step_conditions()
        alarm = user_input or (self._alarms.get(self._alarm, {}) if self._alarm else {})
        schema = {
            **self._alarm_common(alarm),
            vol.Required("days", default=alarm.get("days", [])): selector.SelectSelector(selector.SelectSelectorConfig(
                options=WEEKDAYS, multiple=True, translation_key="weekday", mode=selector.SelectSelectorMode.LIST,
            )),
            vol.Required("time", default=alarm.get("time", "12:00:00")): selector.TimeSelector(),
            vol.Optional("calendars", default=alarm.get("calendars", [])): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="calendar", multiple=True)
            ),
            vol.Required("after", default=alarm.get("after", 60)): _minutes(0, 720),
            vol.Required("needed", default=alarm.get("needed", 15)): _minutes(0, 240),
            vol.Required("latest", default=alarm.get("latest", "22:00:00")): selector.TimeSelector(),
            vol.Required("all_day", default=alarm.get("all_day", False)): selector.BooleanSelector(),
        }
        return self.async_show_form(step_id="weekly", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_calendar(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """For appointments with a word (or exactly a title) in their title."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("calendars"):
                errors["calendars"] = "calendars_required"
            elif not str(user_input.get("text", "")).strip():
                errors["text"] = "text_required"
            elif self._take_alarm("calendar", user_input, errors):
                return await self.async_step_conditions()
        alarm = user_input or (self._alarms.get(self._alarm, {}) if self._alarm else {})

        def select(options: list[str], key: str) -> selector.SelectSelector:
            return selector.SelectSelector(selector.SelectSelectorConfig(
                options=options, translation_key=key, mode=selector.SelectSelectorMode.DROPDOWN,
            ))

        schema = {
            **self._alarm_common(alarm),
            vol.Required("calendars", default=alarm.get("calendars", [])): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="calendar", multiple=True)
            ),
            vol.Required("match", default=alarm.get("match", "contains")): select(["contains", "exact"], "match"),
            vol.Required("text", default=alarm.get("text", "")): selector.TextSelector(),
            vol.Required("offset", default=alarm.get("offset", 0)): _minutes(-1440, 10080),
            vol.Required("anchor", default=alarm.get("anchor", "start")): select(["start", "end"], "anchor"),
        }
        return self.async_show_form(step_id="calendar", data_schema=vol.Schema(schema), errors=errors)

    def _take_alarm(self, kind: str, user_input: dict[str, Any], errors: dict[str, str]) -> bool:
        """Keeps the form's alarm for the conditions step; False (errors filled in) when the name is missing or taken."""
        alarms = self._alarms
        name = user_input["name"].strip()
        if not name:
            errors["name"] = "alarm_name_required"
        elif any(a["name"].lower() == name.lower() and aid != self._alarm for aid, a in alarms.items()):
            errors["name"] = "alarm_name_taken"
        if errors:
            return False
        self._alarm = self._alarm or (re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "alarm") + "_" + secrets.token_hex(2)
        alarm = {**user_input, "kind": kind, "name": name, "message": str(user_input.get("message") or "").strip()}
        for key in ("after", "needed", "offset"):
            if key in alarm:
                alarm[key] = int(alarm[key])
        if "text" in alarm:
            alarm["text"] = alarm["text"].strip()
        # Conditions stay as they were until they are changed in the next step.
        alarm["conditions"] = (self._pending or alarms.get(self._alarm) or {}).get("conditions", [])
        self._pending = alarm
        return True

    # Conditions (schedule.py): the alarm rings only when all of them hold -------

    async def _describe(self, condition: dict[str, Any]) -> str:
        """One condition as a line of text, in Home Assistant's language."""
        texts = await async_get_translations(self.hass, self.hass.config.language, "selector", {DOMAIN})

        def text(key: str) -> str:
            return texts.get(f"component.{DOMAIN}.selector.{key}", key.rsplit(".", 1)[-1])

        kind, mode = condition.get("type"), condition.get("mode")
        if kind == "start":
            return text("condition_text.options.start").format(
                **{"from": str(condition.get("from", ""))[:5], "to": str(condition.get("to", ""))[:5]})
        if kind == "weekdays":
            days = ", ".join(text(f"weekday.options.{d}") for d in WEEKDAYS if d in (condition.get("days") or []))
            return text("condition_text.options.weekdays").format(days=days)
        if kind in ("title", "day"):
            key = f"{kind}_{'not' if mode == 'not' else 'has'}"
            return text(f"condition_text.options.{key}").format(text=condition.get("text", ""))
        state = self.hass.states.get(str(condition.get("entity")))
        name = state.name if state else str(condition.get("entity"))
        key = "state_not" if mode == "not" else "state_is"
        return text(f"condition_text.options.{key}").format(entity=name, state=condition.get("state", ""))

    async def async_step_conditions(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        conditions = self._pending["conditions"]
        menu = ["add_condition"] + (["remove_condition"] if conditions else []) + ["save_alarm"]
        if conditions:
            listed = "\n".join(["•  " + await self._describe(c) for c in conditions])
        else:
            listed = await self._describe_none()
        return self.async_show_menu(step_id="conditions", menu_options=menu, description_placeholders={"conditions": listed})

    async def _describe_none(self) -> str:
        texts = await async_get_translations(self.hass, self.hass.config.language, "selector", {DOMAIN})
        return texts.get(f"component.{DOMAIN}.selector.condition_text.options.none", "None")

    async def async_step_add_condition(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Calendar alarms can also look at the appointment itself."""
        menu = ["cond_day", "cond_state"]
        if self._pending.get("kind") == "calendar":
            menu = ["cond_start", "cond_weekdays", "cond_title"] + menu
        return self.async_show_menu(step_id="add_condition", menu_options=menu)

    def _add(self, condition: dict[str, Any]) -> None:
        self._pending["conditions"] = [*self._pending["conditions"], condition]

    @staticmethod
    def _mode(options: list[str], key: str) -> selector.SelectSelector:
        return selector.SelectSelector(selector.SelectSelectorConfig(
            options=options, translation_key=key, mode=selector.SelectSelectorMode.LIST,
        ))

    async def async_step_cond_start(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._add({"type": "start", "from": user_input["from"], "to": user_input["to"]})
            return await self.async_step_conditions()
        return self.async_show_form(step_id="cond_start", data_schema=vol.Schema({
            vol.Required("from", default="05:00:00"): selector.TimeSelector(),
            vol.Required("to", default="10:00:00"): selector.TimeSelector(),
        }))

    async def async_step_cond_weekdays(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input.get("days"):
                self._add({"type": "weekdays", "days": user_input["days"]})
                return await self.async_step_conditions()
            errors["days"] = "days_required"
        return self.async_show_form(step_id="cond_weekdays", errors=errors, data_schema=vol.Schema({
            vol.Required("days", default=[]): selector.SelectSelector(selector.SelectSelectorConfig(
                options=WEEKDAYS, multiple=True, translation_key="weekday", mode=selector.SelectSelectorMode.LIST,
            )),
        }))

    async def async_step_cond_title(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if str(user_input.get("text", "")).strip():
                self._add({"type": "title", "mode": user_input["mode"], "text": user_input["text"].strip()})
                return await self.async_step_conditions()
            errors["text"] = "text_required"
        return self.async_show_form(step_id="cond_title", errors=errors, data_schema=vol.Schema({
            vol.Required("mode", default="has"): self._mode(["has", "not"], "title_mode"),
            vol.Required("text", default=""): selector.TextSelector(),
        }))

    async def async_step_cond_day(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get("calendars"):
                errors["calendars"] = "calendars_required"
            elif not str(user_input.get("text", "")).strip():
                errors["text"] = "text_required"
            else:
                self._add({"type": "day", "mode": user_input["mode"], "calendars": user_input["calendars"],
                           "text": user_input["text"].strip()})
                return await self.async_step_conditions()
        return self.async_show_form(step_id="cond_day", errors=errors, data_schema=vol.Schema({
            vol.Required("calendars", default=self._pending.get("calendars") or []): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="calendar", multiple=True)
            ),
            vol.Required("mode", default="not"): self._mode(["has", "not"], "day_mode"),
            vol.Required("text", default=""): selector.TextSelector(),
        }))

    async def async_step_cond_state(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """First the entity, then its state from the states that entity knows."""
        if user_input is not None:
            self._state_condition = {"type": "state", "entity": user_input["entity"], "mode": user_input["mode"]}
            return await self.async_step_cond_state_value()
        return self.async_show_form(step_id="cond_state", data_schema=vol.Schema({
            vol.Required("entity"): selector.EntitySelector(),
            vol.Required("mode", default="is"): self._mode(["is", "not"], "state_mode"),
        }))

    async def async_step_cond_state_value(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        condition = self._state_condition
        if user_input is not None:
            self._add({**condition, "state": str(user_input["state"])})
            return await self.async_step_conditions()
        state = self.hass.states.get(condition["entity"])
        return self.async_show_form(
            step_id="cond_state_value",
            data_schema=vol.Schema({vol.Required("state", default=state.state if state else ""): selector.StateSelector(
                selector.StateSelectorConfig(entity_id=condition["entity"])
            )}),
            description_placeholders={"entity": state.name if state else condition["entity"]},
        )

    async def async_step_remove_condition(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        conditions = self._pending["conditions"]
        if user_input is not None:
            self._pending["conditions"] = [c for i, c in enumerate(conditions) if str(i) != user_input["condition"]]
            return await self.async_step_conditions()
        options = [selector.SelectOptionDict(value=str(i), label=await self._describe(c)) for i, c in enumerate(conditions)]
        return self.async_show_form(step_id="remove_condition", data_schema=vol.Schema({
            vol.Required("condition"): selector.SelectSelector(selector.SelectSelectorConfig(
                options=options, mode=selector.SelectSelectorMode.LIST,
            )),
        }))

    async def async_step_save_alarm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        alarms = self._alarms
        alarms[self._alarm] = self._pending
        return self.async_create_entry(data={**self.config_entry.options, CONF_ALARMS: alarms})
