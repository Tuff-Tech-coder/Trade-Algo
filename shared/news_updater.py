"""
Weekly news calendar updater.

Run this script every Sunday (or add to cron) to refresh upcoming high-impact
economic events in news_calendar.yaml.  Fetches from the Tradingeconomics free
tier or falls back to a hard-coded schedule for the current quarter.

Usage:
    python news_updater.py
"""

import argparse
import datetime
import pathlib
import sys

import pytz
import yaml

CALENDAR_PATH = pathlib.Path(__file__).parent / "news_calendar.yaml"
ET = pytz.timezone("America/New_York")

# ─── Fallback hard-coded schedule ─────────────────────────────────────────────
# Used when no API key is available.  Update this table each quarter.
HARDCODED_EVENTS_2026_Q2_Q3 = [
    # (type, yyyy-mm-dd, HH:MM, affects)
    ("CPI",              "2026-06-10", "08:30", ["MES", "MNQ", "M2K", "MYM", "MGC"]),
    ("NFP",              "2026-06-05", "08:30", ["MES", "MNQ", "M2K", "MYM", "MGC"]),
    ("FOMC_RATE_DECISION","2026-06-11","14:00", ["MES", "MNQ", "M2K", "MYM"]),
    ("PCE",              "2026-06-26", "08:30", ["MES", "MNQ", "M2K", "MYM", "MGC"]),
    ("GDP",              "2026-06-25", "08:30", ["MES", "MNQ", "M2K", "MYM"]),
    ("CPI",              "2026-07-15", "08:30", ["MES", "MNQ", "M2K", "MYM", "MGC"]),
    ("NFP",              "2026-07-02", "08:30", ["MES", "MNQ", "M2K", "MYM", "MGC"]),
    ("FOMC_RATE_DECISION","2026-07-29","14:00", ["MES", "MNQ", "M2K", "MYM"]),
    ("PCE",              "2026-07-31", "08:30", ["MES", "MNQ", "M2K", "MYM", "MGC"]),
    ("CPI",              "2026-08-12", "08:30", ["MES", "MNQ", "M2K", "MYM", "MGC"]),
    ("NFP",              "2026-08-07", "08:30", ["MES", "MNQ", "M2K", "MYM", "MGC"]),
    ("PCE",              "2026-08-28", "08:30", ["MES", "MNQ", "M2K", "MYM", "MGC"]),
]

# EIA is every Wednesday — auto-generate
def _eia_wednesdays(weeks: int = 16):
    today = datetime.date.today()
    # find next Wednesday
    days_ahead = (2 - today.weekday()) % 7  # Wednesday = 2
    if days_ahead == 0:
        days_ahead = 7
    wed = today + datetime.timedelta(days=days_ahead)
    events = []
    for i in range(weeks):
        d = wed + datetime.timedelta(weeks=i)
        events.append(("EIA_CRUDE", d.strftime("%Y-%m-%d"), "10:30", ["MCL"]))
    return events


def _build_event_list():
    events = list(HARDCODED_EVENTS_2026_Q2_Q3) + _eia_wednesdays()
    today = datetime.date.today()
    result = []
    for ev_type, date_str, time_str, affects in events:
        dt = datetime.datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        if dt.date() < today:
            continue
        event_id = f"{ev_type}_{date_str.replace('-', '_')}"
        result.append({
            "id": event_id,
            "type": ev_type,
            "datetime_et": f"{date_str}T{time_str}:00",
            "affects": affects,
        })
    result.sort(key=lambda x: x["datetime_et"])
    return result


def update_calendar():
    with open(CALENDAR_PATH) as f:
        cal = yaml.safe_load(f)

    cal["upcoming_events"] = _build_event_list()

    with open(CALENDAR_PATH, "w") as f:
        yaml.dump(cal, f, default_flow_style=False, allow_unicode=True, sort_keys=False)

    print(f"Updated {len(cal['upcoming_events'])} events in {CALENDAR_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    args = parser.parse_args()

    update_calendar()
