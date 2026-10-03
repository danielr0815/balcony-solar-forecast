"""Seeded full-output equivalence against frozen pre-extraction production code."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from homeassistant.util import dt as dt_util

from custom_components.balcony_solar_forecast import _nightly
from custom_components.balcony_solar_forecast.const import SLOT_HOURS, SLOT_MINUTES
from custom_components.balcony_solar_forecast.core import clearsky, shademap
from custom_components.balcony_solar_forecast.core.engine import compute_forecast
from custom_components.balcony_solar_forecast.core.types import (
    IssuedSnapshot,
    PlaneConfig,
    ShademapState,
    SiteConfig,
    WeatherSeries,
    WeatherSlot,
)


def test_seeded_learning_reducers_preserve_complete_outputs(monkeypatch):
    namespace = {**vars(_nightly),'clearsky':clearsky,'shademap_mod':shademap,
                 'SLOT_HOURS':SLOT_HOURS,'SLOT_MINUTES':SLOT_MINUTES}
    frozen=Path(__file__).parent/'fixtures/contracts/learning_inputs_before.txt'
    exec(compile(frozen.read_text(),str(frozen),'exec'),namespace)
    rng=Random(20261003)
    for _case in range(250):
        tz=ZoneInfo(rng.choice(['UTC','Europe/Berlin','Asia/Tokyo','Australia/Adelaide']))
        monkeypatch.setattr(dt_util,'as_local',lambda at,zone=tz:at.astimezone(zone))
        start=datetime(2026,rng.choice([3,6,9,10]),rng.randrange(1,28),tzinfo=tz).astimezone(UTC)
        planes=tuple(PlaneConfig(f'P{i}',rng.uniform(0,360),rng.uniform(0,90),rng.uniform(300,600),
                                actual_entity=f'sensor.p{i}' if rng.random()>.25 else None) for i in range(rng.randrange(1,5)))
        site=SiteConfig(rng.uniform(-55,55),rng.uniform(-140,140),planes,())
        slots=tuple(WeatherSlot(start+timedelta(minutes=15*i),rng.uniform(100,700),rng.uniform(100,700),rng.uniform(50,150),20) for i in range(96))
        result=compute_forecast(site,WeatherSeries(slots),start,tz=tz)
        coord=SimpleNamespace(_site=site,_last_result=result,hass=SimpleNamespace(config=SimpleNamespace(time_zone=str(tz))))
        coord._day_part_for_hourkey=lambda key,owner=coord:_nightly.day_part_for_hourkey(owner,key)
        iso=start.astimezone(tz).date().isoformat()
        old=namespace['per_plane_modeled'](coord,iso)
        new=_nightly.per_plane_modeled(coord,iso)
        assert old==new
        snap=IssuedSnapshot(issued_at=start.isoformat(),status='test',per_plane=old,
                            raw_hourly_wh=result.raw_hourly_wh,corrected_hourly_wh=result.hourly_wh)
        measured={h:wh*rng.uniform(.7,1.1) for h,wh in result.hourly_wh.items()}
        totals={plane.name:rng.uniform(100,1000) for plane in planes if plane.actual_entity}
        for layer in ('raw','slow','corrected'):
            assert namespace['metered_modeled_hourly'](coord,snap,result.hourly_wh,layer=layer)==_nightly.metered_modeled_hourly(coord,snap,result.hourly_wh,layer=layer)
        for labels in (None,measured):
            assert namespace['day_ahead_samples'](coord,result.hourly_wh,totals,snap,labels)==_nightly.day_ahead_samples(coord,result.hourly_wh,totals,snap,labels)
        for channel,modeled in old.items():
            state=ShademapState()
            assert namespace['train_channel'](coord,deepcopy(state),channel,modeled,measured)==_nightly.train_channel(coord,deepcopy(state),channel,modeled,measured)
