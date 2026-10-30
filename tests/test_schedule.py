"""Checks the scheduled-alarm rules in schedule.py without Home Assistant (stubs its few helpers).

    python test_schedule.py   (from its own folder or any other; needs no Home Assistant)
"""
import os
import sys
import types
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

try:
    TZ = ZoneInfo("Europe/Paris")
except Exception:  # Windows without the tzdata package: winter time is enough here
    TZ = timezone(timedelta(hours=1))

# Just enough of homeassistant for schedule.py.
dt = types.SimpleNamespace(
    get_default_time_zone=lambda: TZ,
    as_local=lambda d: d.astimezone(TZ),
    start_of_local_day=lambda d: datetime(d.year, d.month, d.day, tzinfo=TZ),
    parse_date=lambda s: date.fromisoformat(s),
    parse_datetime=lambda s: datetime.fromisoformat(s),
    now=lambda: NOW,
)
for name in ("homeassistant", "homeassistant.core", "homeassistant.util"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["homeassistant.core"].HomeAssistant = object
sys.modules["homeassistant.util"].dt = dt
pkg = types.ModuleType("headsup")
pkg.__path__ = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "custom_components", "headsup")]
sys.modules["headsup"] = pkg
const = types.ModuleType("headsup.const")
const.LOGGER = None
sys.modules["headsup.const"] = const
from headsup import schedule  # noqa: E402

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=TZ)  # a Tuesday
WED = date(2026, 10, 28)


def at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime(day.year, day.month, day.day, h, m, tzinfo=TZ)


def appt(day, start, end, summary="Busy", cal="calendar.family", all_day=False):
    return schedule.Appointment(cal, at(day, start), at(day, end), summary, all_day)


BIN = {"kind": "weekly", "name": "Bin", "days": ["wed"], "time": "12:00:00", "calendars": ["calendar.family"],
       "after": 60, "needed": 15, "latest": "22:00:00"}
failed = 0


def check(label, appointments, expected, alarm=BIN):
    global failed
    got = [o["at"].strftime("%a %H:%M") for o in schedule.weekly("bin", alarm, appointments, NOW)][:1]
    ok = got == expected
    failed += not ok
    print(("ok  " if ok else "FAIL"), label, got, "" if ok else f"expected {expected}")


check("free Wednesday", [], ["Wed 12:00"])
check("appointment at 12", [appt(WED, "12:00", "13:00")], ["Wed 14:00"])
check("12 and the next one at 13:30", [appt(WED, "12:00", "13:00"), appt(WED, "13:30", "14:30")], ["Wed 15:30"])
check("appointment from 11:30", [appt(WED, "11:30", "12:15")], ["Wed 13:15"])
check("appointment starts 12:10 (needs 15 free)", [appt(WED, "12:10", "12:40")], ["Wed 13:40"])
check("appointment starts 12:30 (room before it)", [appt(WED, "12:30", "13:00")], ["Wed 12:00"])
check("busy all afternoon: rings at latest", [appt(WED, "11:00", "21:30")], ["Wed 22:00"])
check("other calendar is ignored", [appt(WED, "12:00", "13:00", cal="calendar.work")], ["Wed 12:00"])
check("all-day ignored by default", [schedule.Appointment("calendar.family", at(WED, "00:00"), at(WED + timedelta(days=1), "00:00"), "Holiday", True)], ["Wed 12:00"])
check("all-day counts when asked",
      [schedule.Appointment("calendar.family", at(WED, "00:00"), at(WED + timedelta(days=1), "00:00"), "Holiday", True)],
      ["Wed 22:00"], {**BIN, "all_day": True})

# Calendar-linked alarms.
DENT = {"kind": "calendar", "name": "Dentist", "calendars": ["calendar.family"], "match": "contains", "text": "dentist",
        "offset": 30, "anchor": "start"}
apps = [appt(WED, "10:00", "10:30", "Dentist Anna"), appt(WED, "15:00", "16:00", "Dentist"), appt(WED, "17:00", "18:00", "Gym")]
got = [(o["at"].strftime("%H:%M"), o["title"], o["message"]) for o in schedule.from_calendar("d", DENT, apps, NOW)]
exp = [("09:30", "Dentist Anna", "10:00"), ("14:30", "Dentist", "15:00")]
print("ok  " if got == exp else "FAIL", "contains", got)
failed += got != exp
got = [o["at"].strftime("%H:%M") for o in schedule.from_calendar("d", {**DENT, "match": "exact", "text": "DENTIST"}, apps, NOW)]
print("ok  " if got == ["14:30"] else "FAIL", "exact, any case", got)
failed += got != ["14:30"]
got = [o["at"].strftime("%H:%M") for o in schedule.from_calendar("d", {**DENT, "text": "gym", "offset": -10, "anchor": "end"}, apps, NOW)]
print("ok  " if got == ["18:10"] else "FAIL", "10 min after the end", got)
failed += got != ["18:10"]
# Conditions: a wake-up alarm only for morning shifts, not on a holiday, not for a cancelled shift.
THU = WED + timedelta(days=1)
FRI = WED + timedelta(days=2)
WAKE = {"kind": "calendar", "name": "Wake up", "calendars": ["calendar.work"], "match": "contains", "text": "shift",
        "offset": 90, "anchor": "start", "conditions": [
            {"type": "start", "from": "05:00:00", "to": "10:00:00"},
            {"type": "title", "mode": "not", "text": "cancelled"},
            {"type": "day", "mode": "not", "calendars": ["calendar.family"], "text": "holiday"},
        ]}
shifts = [appt(WED, "07:00", "15:00", "Early shift", cal="calendar.work"),
          appt(THU, "14:00", "22:00", "Late shift", cal="calendar.work"),
          appt(FRI, "06:00", "14:00", "Early shift", cal="calendar.work"),
          appt(FRI + timedelta(days=1), "08:00", "16:00", "Shift cancelled", cal="calendar.work"),
          schedule.Appointment("calendar.family", at(FRI, "00:00"), at(FRI + timedelta(days=1), "00:00"), "Holiday", True)]
got = [o["at"].strftime("%a %H:%M") for o in schedule.from_calendar("w", WAKE, shifts, NOW)]
print("ok  " if got == ["Wed 05:30"] else "FAIL", "morning shifts only, not on a holiday, not cancelled", got)
failed += got != ["Wed 05:30"]
got = [o["at"].strftime("%a %d") for o in schedule.weekly("bin", {**BIN, "days": ["wed", "fri"], "conditions": [
    {"type": "day", "mode": "not", "calendars": ["calendar.family"], "text": "holiday"}]}, shifts, NOW)][:3]
print("ok  " if got == ["Wed 28", "Wed 04", "Fri 06"] else "FAIL", "weekly skips the holiday (Fri 30)", got)
failed += got != ["Wed 28", "Wed 04", "Fri 06"]
got = [o["at"].strftime("%H:%M") for o in schedule.from_calendar("w", {**WAKE, "conditions": [
    {"type": "start", "from": "22:00:00", "to": "06:30:00"}]}, shifts, NOW)]
print("ok  " if got == ["04:30"] else "FAIL", "window across midnight", got)
failed += got != ["04:30"]
got = [o["at"].strftime("%a") for o in schedule.from_calendar("w", {**WAKE, "conditions": [
    {"type": "weekdays", "days": ["thu", "sat"]}]}, shifts, NOW)]
print("ok  " if got == ["Thu", "Sat"] else "FAIL", "weekdays", got)
failed += got != ["Thu", "Sat"]

# The same occurrence keeps its id when its time moves.
a = schedule.weekly("bin", BIN, [], NOW)[0]["id"]
b = schedule.weekly("bin", BIN, [appt(WED, "12:00", "13:00")], NOW)[0]["id"]
print("ok  " if a == b else "FAIL", "id stays when moved")
failed += a != b
print("all passed" if not failed else f"{failed} failed")
sys.exit(1 if failed else 0)
