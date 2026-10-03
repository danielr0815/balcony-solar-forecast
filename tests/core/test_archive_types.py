"""Historical imports and runtime type introspection survive the archive split."""
from typing import get_type_hints

from balcony_solar_forecast.core.archive_types import IssuedSnapshot, PlaneHourlyModeled
from balcony_solar_forecast.core.types import IssuedSnapshot as LegacySnapshot
from balcony_solar_forecast.core.types import PlaneHourlyModeled as LegacyPlane


def test_reexports_are_the_same_class_and_annotations_resolve():
    assert LegacySnapshot is IssuedSnapshot
    assert LegacyPlane is PlaneHourlyModeled
    assert get_type_hints(IssuedSnapshot)['per_plane'] == dict[str, PlaneHourlyModeled]
    assert IssuedSnapshot.from_dict(None).to_dict() == IssuedSnapshot('', '').to_dict()
    assert PlaneHourlyModeled.from_dict([]).to_dict() == {}
