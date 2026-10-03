# Architecture and forecast glossary

Home Assistant loads `custom_components/balcony_solar_forecast`; this is not a
PyPI package. Runtime requirements are empty. The `core/` calculation layer is
HA-free and uses the standard library. The documented exception is historical
weather fetching with an injected session and a lazy aiohttp import.

| Term | Meaning |
|---|---|
| RAW | Static geometry/physics forecast, group-clipped DC |
| Slow-only | Physics with learned beam transmittance, without bias/intraday |
| θ | Persisted day-ahead correction learned against Slow-only |
| Intraday | Transient recent correction learned against Slow-only × θ |
| Served DC | Corrected postclip curve, clipped again to nominal group limits |
| Served AC | Conversion of that served DC, with group/site η and AC limits |
| Issued | A frozen calculation, never recomputed with today's learned state |
| Slot / hour | UTC interval identity; calendar days use the HA timezone |
| Quantile evidence | Dated residual samples; neutral cold bands are not confidence |
| Panel/weather evidence | Observational shared disturbance, ambiguous with curtailment |

Coordinator: fetch/cache and calculation scheduling. `_actuals` validates
recorder labels. `_nightly` orchestrates closed-day learning. Store owns additive
persistent rings; frontend cards consume entry-scoped read services. Shared
measurement boundaries live in `core/measurement_quality`; bounded disturbance
logic lives in `core/panel_weather`, with `_panel_weather` as the HA adapter.
`core/archive_types` owns frozen issued snapshots; `core/types` preserves its
historical import. `core/config_fingerprint` shares model identity between
learning and configuration previews. `core/training_plan` makes nightly
idempotence, freeze and rollback decisions explicit. `_forecast_presenter`
shapes the shared sensor/service response without owning entity lifecycle.
`core/learning_inputs` reduces issued per-plane references, metered subsets and
bias samples and performs geometric channel updates. Both `_nightly` and the
physical offline replay use these functions. The bias sample contract has a
single owner in `core/bias`, with the historical nightly import retained.

Plane `name` is the stable legacy ID; optional `display_name` is presentation.
Label edits leave model identity, groups, archives and learners intact. The HA
module selector restores by `module_id`, independently of the current label.
See [ADR-0025](adr/ADR-0025-stabile-modulidentitaet-und-anzeigenamen.md).

The German [SPEC](SPEC.md) is authoritative. Design rationale belongs in ADRs;
behavior changes update SPEC and carry an independent regression or feature
contract test. Keep pure functions out of HA adapters, validate external values
at boundaries, and preserve optional-field round trips and old learning data.
