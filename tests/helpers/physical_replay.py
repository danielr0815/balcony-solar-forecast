"""CC0 synthetic hourly measurements and issued weather; no operator data."""
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo


def data(days=('2026-09-03','2026-09-04','2026-09-05'), timezone='UTC'):
    tz = ZoneInfo(timezone)
    rows = []
    for iso in days:
        local = datetime.fromisoformat(iso).replace(tzinfo=tz)
        start, end = local.astimezone(UTC), (local+timedelta(days=1)).astimezone(UTC)
        weather = []
        cursor = start
        while cursor < end:
            daytime = 6 <= cursor.hour < 18
            weather.append({'start':cursor.isoformat(), 'ghi':600 if daytime else 0,
                            'dni':650 if daytime else 0, 'dhi':100 if daytime else 0,'temp_c':20})
            cursor += timedelta(minutes=15)
        actuals = {}
        cursor = start
        while cursor < end:
            actuals[cursor.isoformat()] = 160+cursor.hour*3 if 6 <= cursor.hour < 18 else 0
            cursor += timedelta(hours=1)
        rows.append({'local_day':iso,'issued_at':(start-timedelta(hours=1)).isoformat(),
                     'weather_role':'issued_forecast','weather_available_at':(start-timedelta(hours=2)).isoformat(),
                     'weather_slots':weather,'actuals_available_at':(end+timedelta(minutes=5)).isoformat(),
                     'actuals_hourly':{'P':actuals}})
    return {'schema_version':1,'basis':'DC','timezone':timezone,
            'site':{'latitude':0,'longitude':0,'planes':[{'name':'P','azimuth_deg':180,'tilt_deg':30,'wp':400,'actual_entity':'sensor.p'}],'groups':[]},
            'days':rows}
