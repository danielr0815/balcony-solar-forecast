"""Shared synthetic site setup for real HA boundary tests."""

DOMAIN = "balcony_solar_forecast"


async def start_advanced(hass):
    from homeassistant.data_entry_flow import FlowResultType

    menu = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert menu['type'] == FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(menu['flow_id'], {'next_step_id': 'advanced'})


def user_data(name="Test balcony"):
    return {
        "name": name, "latitude": 52.0, "longitude": 13.0,
        "fetch_interval_seconds": 3600, "recompute_interval_seconds": 900,
        "site": {
            "latitude": 52.0, "longitude": 13.0,
            "planes": [{"name": "M1", "azimuth_deg": 180.0, "tilt_deg": 70.0,
                        "wp": 400, "efficiency": 0.2, "horizon": [],
                        "actual_entity": "sensor.module_dc"}],
            "groups": [{"name": "WR", "plane_names": ["M1"], "ac_limit_w": 400}],
        },
    }
