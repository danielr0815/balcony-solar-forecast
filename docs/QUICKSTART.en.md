# Balcony Solar Forecast: quickstart

A Home Assistant custom integration for multi-orientation balcony PV. It
fetches irradiance from Open-Meteo and computes a local forecast, optionally
learning from individual DC power measurements. The installed release version
is shown by HACS; development changes listed as Unreleased are not installed yet.

## Install and configure

1. In HACS add `danielr0815/balcony-solar-forecast` as a custom integration
   repository, download the integration, then restart Home Assistant.
2. Add **Balcony Solar Forecast** in Settings → Devices & services.
3. Choose **Guided setup**, check the HA location and name the installation.
   Add panels one at a time, with their inverter group and optional DC sensors.
   The initial 400-Wp, 30°, south-facing/open-sky values are editable starting
   values. Panels sharing an inverter use the same group name and limits.
4. Enter each panel's rated watts, azimuth (north 0°, east 90°, south 180°,
   west 270°), tilt (horizontal 0°, vertical 90°), and inverter group/AC limit.
   Review the panels and groups before creating the entry. Select the advanced
   editor on the review screen to add known walls/obstructions or an AC meter;
   your entered panels remain intact. Open sky is only a starting assumption.
5. DC sensors are optional. Each panel needs a distinct **power** source in W
   or kW. Energy sensors in Wh/kWh and one shared source assigned to multiple
   panels are unsuitable. The forecast also works without measurement sensors.
   An optional total AC power meter supports inverter-efficiency calibration.

Example site object (the flow applies your chosen location):

```yaml
latitude: 0.0
longitude: 0.0
planes:
  - name: Panel 1
    azimuth_deg: 180
    tilt_deg: 30
    wp: 400
    # actual_entity: sensor.your_panel_dc_power
    horizon: []
groups:
  - name: Inverter 1
    plane_names: [Panel 1]
    ac_limit_w: 800
```

Use your real location in the form; the zeros above are placeholders.

## Read the forecast

Main energy/power sensors are **AC**. Per-panel measurement and learning use
**DC**; compare like with like. Day-ahead total, produced energy and remaining
energy are different quantities. Missing/unavailable data must not be read as
zero. Learning needs closed days with complete usable statistics; missing,
frozen or implausible sources quarantine a training day.

A cold empirical band is not evidence of high confidence. Quantile evidence
needs multiple days and enough samples; the point forecast is not automatically
an empirical median. Observational panel/weather diagnostics report common
changes and ambiguity, never a measured cloud percentage. They do not change
the forecast or learning.

Synthetic dashboard previews: [mobile](images/cards-synthetic-mobile.png) and
[desktop](images/cards-synthetic-desktop.png). These show missing data and partial
learning deliberately; they contain no live installation data. Regenerate with
`scripts/validation/browser_cards.mjs`.

## Rename a panel without losing learning

In the advanced site editor, keep each plane's `name` and group `plane_names`
unchanged and set `display_name`, for example `name: M1` and
`display_name: South balcony`. Labels must be unique. The displayed name can
change while measurements, archives, shading and selection stay attached to M1.
Legacy configurations work unchanged and need no migration.

## Update, recovery and support

Install regular releases through HACS and restart/reload as instructed. Keep a
private copy of your site configuration before geometry changes. Diagnostics
redact location and are not a configuration backup. [Bootstrap](BACKFILL.md)
has a dry run; review it before importing. Existing learner rollback actions
restore recorded learner state, not installed software.

If an update fails, choose the previous release in HACS and restart. New optional
archive fields may not survive a downgrade; established learner data remains
backward compatible. Report reproducible failures using the issue form, with
release/HA version, minimal site shape and redacted diagnostics. Never attach
HA tokens, browser profiles, raw private location exports or login data.

Contributors: [CONTRIBUTING.md](../CONTRIBUTING.md),
[architecture and glossary](ARCHITECTURE.en.md), [SPEC](SPEC.md) (German contract).

Code is [MIT licensed](../LICENSE). Weather data is provided by
[Open-Meteo](https://open-meteo.com/); its data attribution and licence are
separate from the integration's code licence. Preserve provider attribution
when sharing datasets and include each dataset's own provenance/licence.
