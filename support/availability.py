import calendar
from datetime import datetime, timedelta
from urllib.parse import quote
from zoneinfo import ZoneInfo

from django.utils import timezone

from . import conf


def _fmt(t):
    return t.strftime("%I:%M %p").lstrip("0")


def get_availability(now=None):
    """Is support online right now, and when are they next in?"""
    tz = ZoneInfo(conf.TIMEZONE)
    now = (now or timezone.now()).astimezone(tz)
    today = conf.HOURS.get(now.weekday())

    if today and today[0] <= now.time() < today[1]:
        is_open = True
        label = f"Support is online until {_fmt(today[1])}"
    else:
        is_open = False
        label = "Support is offline right now"
        for offset in range(0, 8):
            day = now + timedelta(days=offset)
            hours = conf.HOURS.get(day.weekday())
            if not hours:
                continue
            opens = datetime.combine(day.date(), hours[0], tzinfo=tz)
            if opens > now:
                when = (
                    "today"
                    if offset == 0
                    else "tomorrow"
                    if offset == 1
                    else calendar.day_name[day.weekday()]
                )
                label = f"Support is offline. We're back {when} at {_fmt(hours[0])}"
                break

    schedule = []
    for i in range(7):
        h = conf.HOURS.get(i)
        schedule.append(
            (calendar.day_name[i], f"{_fmt(h[0])} to {_fmt(h[1])}" if h else "Closed")
        )

    return {
        "is_open": is_open,
        "label": label,
        "summary": conf.RESPONSE_SUMMARY,
        "schedule": schedule,
        "timezone": conf.TIMEZONE,
        "targets": [
            ("Critical", conf.RESPONSE_TARGETS.get("CRITICAL", "")),
            ("High", conf.RESPONSE_TARGETS.get("HIGH", "")),
            ("Medium", conf.RESPONSE_TARGETS.get("MEDIUM", "")),
            ("Low", conf.RESPONSE_TARGETS.get("LOW", "")),
        ],
    }


def whatsapp_url(message=""):
    number = "".join(ch for ch in conf.WHATSAPP_NUMBER if ch.isdigit())
    if not number:
        return ""
    url = f"https://wa.me/{number}"
    return f"{url}?text={quote(message)}" if message else url
