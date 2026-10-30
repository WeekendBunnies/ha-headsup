"""Scheduled alarms: Home Assistant works out when they ring, the phones keep the times.

Two kinds, made under Configure:

- "weekly": on the chosen days at a time. With calendars to check, a time that
  falls in an appointment moves to `after` minutes after its end; when that is
  in an appointment too, after the end of that one, and so on, until there are
  `needed` free minutes. Never later than `latest`: then it rings there anyway
  (better a reminder during an appointment than none).
- "calendar": for every appointment whose title contains a word (or is exactly
  a title), `offset` minutes before its start or end.

Every REFRESH (and after a change) the next HORIZON_DAYS are worked out. Each
phone gets its list once it changed; the phone sets every time with Android's
alarm clock, so an alarm rings even when the phone has no connection then. A
calendar change reaches a phone without a connection only when it has one again.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
import hashlib
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import LOGGER

HORIZON_DAYS = 14
REFRESH = timedelta(minutes=15)
WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


@dataclass(frozen=True)
class Appointment:
    calendar: str
    start: datetime
    end: datetime
    summary: str
    all_day: bool
    location: str = ""


def _when(value: Any) -> tuple[datetime, bool] | None:
    """A calendar time as an aware local datetime; True when it was a date (all day)."""
    if isinstance(value, datetime):
        return dt_util.as_local(value if value.tzinfo else value.replace(tzinfo=dt_util.get_default_time_zone())), False
    if isinstance(value, date):
        return dt_util.start_of_local_day(value), True
    text = str(value or "")
    if len(text) == 10:
        day = dt_util.parse_date(text)
        return (dt_util.start_of_local_day(day), True) if day else None
    moment = dt_util.parse_datetime(text)
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_local(moment), False


async def async_appointments(
    hass: HomeAssistant, calendars: set[str], start: datetime, end: datetime
) -> list[Appointment]:
    """The appointments in these calendars between start and end (calendar.get_events)."""
    calendars = {c for c in calendars if hass.states.get(c) is not None}
    if not calendars or not hass.services.has_service("calendar", "get_events"):
        return []
    try:
        answer = await hass.services.async_call(
            "calendar", "get_events",
            {"entity_id": sorted(calendars), "start_date_time": start, "end_date_time": end},
            blocking=True, return_response=True,
        )
    except Exception as err:  # noqa: BLE001 - tried again on the next refresh; the phones keep their list
        LOGGER.warning("Could not read the calendars for scheduled alarms: %r", err)
        raise
    found = []
    for calendar, result in (answer or {}).items():
        for event in (result or {}).get("events", []):
            begin, end_ = _when(event.get("start")), _when(event.get("end"))
            if begin is None or end_ is None:
                continue
            found.append(Appointment(
                calendar=calendar, start=begin[0], end=max(end_[0], begin[0]),
                summary=str(event.get("summary") or ""), all_day=begin[1],
                location=str(event.get("location") or ""),
            ))
    return sorted(found, key=lambda a: a.start)


def _clock(value: str | None, default: time) -> time:
    try:
        return time.fromisoformat(value) if value else default
    except ValueError:
        return default


def place(preferred: datetime, busy: list[Appointment], after: timedelta, needed: timedelta, latest: datetime) -> datetime:
    """The first moment from preferred on with `needed` free minutes, each move `after` an appointment's end."""
    moment = preferred
    while moment <= latest:
        clash = [a for a in busy if a.start < moment + needed and a.end > moment]
        if not clash:
            return moment
        moment = max(a.end for a in clash) + after
    return max(latest, preferred)


def _occurrence_id(alarm_id: str, key: str) -> str:
    """The same occurrence keeps its id when its time moves, so the phone rings it once."""
    return hashlib.sha256(f"{alarm_id}|{key}".encode()).hexdigest()[:16]


# Conditions: an alarm rings only when all of its conditions hold ----------------
#
#   {"type": "start", "from": "05:00:00", "to": "10:00:00"}   the appointment starts then (calendar alarms)
#   {"type": "weekdays", "days": ["mon", ...]}                the appointment is on these days (calendar alarms)
#   {"type": "title", "mode": "has"|"not", "text": ...}       its title contains the text, or doesn't (calendar alarms)
#   {"type": "day", "mode": "has"|"not", "calendars": [...], "text": ...}
#                                                             that day has an appointment with the text, or none
#   {"type": "state", "mode": "is"|"not", "entity": ..., "state": ...}
#                                                             checked when the list is worked out (async_work_out)


def _conditions(alarm: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [c for c in alarm.get("conditions") or [] if c.get("type") == kind]


def day_ok(alarm: dict[str, Any], day: date, appointments: list[Appointment]) -> bool:
    start = dt_util.start_of_local_day(day)
    end = dt_util.start_of_local_day(day + timedelta(days=1))
    for c in _conditions(alarm, "day"):
        calendars, text = set(c.get("calendars") or []), str(c.get("text") or "").strip().casefold()
        found = any(
            a.calendar in calendars and text in a.summary.casefold()
            and a.start < end and max(a.end, a.start + timedelta(minutes=1)) > start
            for a in appointments
        )
        if found != (c.get("mode") != "not"):
            return False
    return True


def _between(moment: time, start: time, end: time) -> bool:
    """Also across midnight: from 22:00 to 06:00."""
    return start <= moment <= end if start <= end else moment >= start or moment <= end


def appointment_ok(alarm: dict[str, Any], a: Appointment, appointments: list[Appointment]) -> bool:
    for c in _conditions(alarm, "start"):
        # An all-day appointment has no start time, so it never starts "between".
        if a.all_day or not _between(a.start.time(), _clock(c.get("from"), time(0)), _clock(c.get("to"), time(23, 59))):
            return False
    for c in _conditions(alarm, "weekdays"):
        if WEEKDAYS[a.start.weekday()] not in (c.get("days") or []):
            return False
    for c in _conditions(alarm, "title"):
        if (str(c.get("text") or "").strip().casefold() in a.summary.casefold()) != (c.get("mode") != "not"):
            return False
    return day_ok(alarm, a.start.date(), appointments)


def state_ok(hass: HomeAssistant, alarm: dict[str, Any]) -> bool:
    for c in _conditions(alarm, "state"):
        state = hass.states.get(str(c.get("entity")))
        same = state is not None and state.state.casefold() == str(c.get("state", "")).strip().casefold()
        if same != (c.get("mode") != "not"):
            return False
    return True


def weekly(alarm_id: str, alarm: dict[str, Any], appointments: list[Appointment], now: datetime) -> list[dict[str, Any]]:
    days = {WEEKDAYS.index(d) for d in alarm.get("days", []) if d in WEEKDAYS}
    at_time = _clock(alarm.get("time"), time(12))
    latest_time = _clock(alarm.get("latest"), time(22))
    checked = set(alarm.get("calendars") or [])
    busy = [a for a in appointments if a.calendar in checked and (alarm.get("all_day") or not a.all_day)]
    after = timedelta(minutes=int(alarm.get("after", 60)))
    needed = timedelta(minutes=int(alarm.get("needed", 15)))
    out = []
    today = now.date()
    for offset in range(HORIZON_DAYS + 1):
        day = today + timedelta(days=offset)
        if day.weekday() not in days or not day_ok(alarm, day, appointments):
            continue
        preferred = dt_util.start_of_local_day(day).replace(hour=at_time.hour, minute=at_time.minute)
        latest = dt_util.start_of_local_day(day).replace(hour=latest_time.hour, minute=latest_time.minute)
        moment = place(preferred, busy, after, needed, latest) if busy else preferred
        if moment <= now:
            continue
        out.append({
            "id": _occurrence_id(alarm_id, day.isoformat()),
            "at": moment,
            "title": alarm["name"],
            "message": alarm.get("message") or "",
        })
    return out


def matches(alarm: dict[str, Any], summary: str) -> bool:
    wanted = str(alarm.get("text") or "").strip().casefold()
    title = summary.strip().casefold()
    if not wanted:
        return False
    return title == wanted if alarm.get("match") == "exact" else wanted in title


def from_calendar(alarm_id: str, alarm: dict[str, Any], appointments: list[Appointment], now: datetime) -> list[dict[str, Any]]:
    checked = set(alarm.get("calendars") or [])
    before = timedelta(minutes=int(alarm.get("offset", 0)))
    out = []
    for a in appointments:
        if a.calendar not in checked or not matches(alarm, a.summary) or not appointment_ok(alarm, a, appointments):
            continue
        moment = (a.end if alarm.get("anchor") == "end" else a.start) - before
        if moment <= now or moment > now + timedelta(days=HORIZON_DAYS):
            continue
        # Without a message of its own: when (and where) the appointment is.
        message = alarm.get("message") or ", ".join(
            p for p in ("" if a.all_day else a.start.strftime("%H:%M"), a.location) if p
        )
        out.append({
            "id": _occurrence_id(alarm_id, f"{a.calendar}|{a.start.isoformat()}|{a.summary}"),
            "at": moment,
            "title": a.summary or alarm["name"],
            "message": message,
        })
    return out


async def async_work_out(
    hass: HomeAssistant, alarms: dict[str, dict[str, Any]], phones: list[str]
) -> dict[str, list[dict[str, Any]]]:
    """Per phone, its coming alarms, earliest first: {id, at, title, message, alarm, profile}."""
    now = dt_util.now()
    calendars = {c for a in alarms.values() for c in (a.get("calendars") or [])}
    calendars |= {c for a in alarms.values() for cond in _conditions(a, "day") for c in (cond.get("calendars") or [])}
    appointments = await async_appointments(
        hass, calendars, now - timedelta(days=1), now + timedelta(days=HORIZON_DAYS + 1)
    )
    per_phone: dict[str, list[dict[str, Any]]] = {p: [] for p in phones}
    for alarm_id, alarm in alarms.items():
        # A state is only known now: with the condition not met, the phones get none of its times.
        if alarm.get("enabled") is False or not state_ok(hass, alarm):
            continue
        work = weekly if alarm.get("kind") == "weekly" else from_calendar
        found = work(alarm_id, alarm, appointments, now)
        for phone_id in alarm.get("phones") or phones:
            if phone_id in per_phone:
                per_phone[phone_id] += [{**o, "alarm": alarm_id, "profile": alarm.get("profile")} for o in found]
    for entries in per_phone.values():
        entries.sort(key=lambda o: o["at"])
    return per_phone
