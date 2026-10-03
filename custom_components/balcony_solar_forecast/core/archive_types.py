"""Frozen forecast-as-issued contract, independent of HA entity lifecycle.

The legacy types module reexports this class. Numeric version validation is imported only while loading, keeping direct
archive imports free of initialization cycles.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PlaneHourlyModeled:
    """Per-plane per-hour modeled curves stored in the issued snapshot v2.

    Enables training the shademap from HOURLY long-term statistics (the
    backfill and the nightly LTS path both work at hourly resolution, SPEC §12.4).
    Each dict is keyed by ISO-8601 UTC hour start.
      - ``beam_wh`` / ``diffuse_wh``: modeled DC energy split for the plane;
      - ``raw_wh`` / ``slow_wh`` / ``corrected_wh``: exact issued per-plane
        curves for attribution against a partially metered site;
      - ``ghi_wh`` proxy and ``kc``: the mean clear-sky index that hour, so the
        quasi-clear gate can be reconstructed offline.
    """

    beam_wh: dict[str, float] = field(default_factory=dict)
    diffuse_wh: dict[str, float] = field(default_factory=dict)
    ghi: dict[str, float] = field(default_factory=dict)
    kc: dict[str, float] = field(default_factory=dict)
    raw_wh: dict[str, float] = field(default_factory=dict)
    slow_wh: dict[str, float] = field(default_factory=dict)
    corrected_wh: dict[str, float] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: object) -> PlaneHourlyModeled:
        if not isinstance(d, dict):
            return cls()

        def _fd(key: str) -> dict[str, float]:
            v = d.get(key, {})
            if not isinstance(v, dict):
                return {}
            return {k: float(x) for k, x in v.items()
                    if isinstance(k, str) and isinstance(x, (int, float))}

        return cls(
            beam_wh=_fd("beam_wh"),
            diffuse_wh=_fd("diffuse_wh"),
            ghi=_fd("ghi"),
            kc=_fd("kc"),
            raw_wh=_fd("raw_wh"),
            slow_wh=_fd("slow_wh"),
            corrected_wh=_fd("corrected_wh"),
        )

    def to_dict(self) -> dict:
        # Store trim: omit EMPTY curves entirely. ``from_dict`` treats a missing
        # key as {}, so this is round-trip safe — and it stops serializing the
        # vestigial ``ghi`` dict (never populated by the coordinator) into every
        # plane of every snapshot of the 90-day issued ring.
        out: dict = {}
        for key, curve in (
            ("beam_wh", self.beam_wh),
            ("diffuse_wh", self.diffuse_wh),
            ("ghi", self.ghi),
            ("kc", self.kc),
            ("raw_wh", self.raw_wh),
            ("slow_wh", self.slow_wh),
            ("corrected_wh", self.corrected_wh),
        ):
            if curve:
                out[key] = dict(curve)
        return out



@dataclass(frozen=True, slots=True)
class IssuedSnapshot:
    """The v2 forecast-as-issued snapshot (one per calendar day, SPEC §16.2).

    Stores BOTH site-hourly curves plus the per-plane modeled
    beam/diffuse/ghi/kc and exact RAW/SLOW/CORRECTED curves needed by the
    shademap trainer and partial-metering evaluators. Round-trips through the issued ring in the
    store. ``version`` == 2 distinguishes it from the v1 issued dict (which had
    only ``hourly_wh`` / ``daily_kwh`` / ``status``); the store carries v1
    entries forward untouched and writes v2 going forward.

    ``slow_only_hourly_wh`` (audit #13b) is the hourly Wh curve with ONLY the
    SLOW layer (shademap beam_tau) applied — no day-ahead factor — so the drift
    monitor can decompose ``corrected = slow ∘ day-ahead`` and attribute a losing day
    to the guilty layer. It is written only when the slow layer was active (else
    it equals raw and is omitted); an empty value means the monitor falls back
    to day-ahead-vs-raw only; it does not invent a slow-layer verdict.
    """

    issued_at: str  # iso utc
    status: str
    raw_hourly_wh: dict[str, float] = field(default_factory=dict)
    corrected_hourly_wh: dict[str, float] = field(default_factory=dict)
    raw_daily_kwh: dict[str, float] = field(default_factory=dict)
    corrected_daily_kwh: dict[str, float] = field(default_factory=dict)
    per_plane: dict[str, PlaneHourlyModeled] = field(default_factory=dict)
    # Forecast cloud class per ISO-UTC hour (SPEC §8 day-ahead conditioning): so
    # the nightly RLS trainer can key the (cloud class x day part) cell on the
    # ACTUAL forecast weather, not a fixed "clear" label. Empty on legacy/v0.1.
    cloud_class_by_hour: dict[str, str] = field(default_factory=dict)
    # Slow-only (shademap ∘ physics, NO day-ahead factor) hourly Wh curve for the
    # drift monitor's per-layer attribution (audit #13b). Empty on legacy/v0.1 or
    # a slow-inactive day (slow-only == raw); the monitor then uses the legacy
    # shared signal.
    slow_only_hourly_wh: dict[str, float] = field(default_factory=dict)
    # Site inverter DC->AC efficiency in effect AT ISSUE TIME (IRC-5/SCT-4): lets
    # a reader convert the stored DC curves to AC without hindsight. ``None`` on
    # legacy/v0.1 snapshots (written before v0.20.7); the reader then falls back
    # to the CURRENT learned eta and flags the substitution.
    eta: float | None = None
    version: int = 2
    # Exact engine AC curve; None distinguishes unknown legacy from zero.
    corrected_ac_hourly_wh: dict[str, float] | None = None
    computed_at: str | None = None
    archived_at: str | None = None
    provenance: dict[str, str] | None = None
    bands: dict[str, dict[str, float]] | None = None

    @classmethod
    def from_dict(cls, d: object) -> IssuedSnapshot:
        if not isinstance(d, dict):
            return cls(issued_at="", status="")

        from .provenance import bounded_provenance
        from .types import _safe_int

        def _fd(key: str) -> dict[str, float]:
            v = d.get(key, {})
            if not isinstance(v, dict):
                return {}
            return {k: float(x) for k, x in v.items()
                    if isinstance(k, str) and isinstance(x, (int, float))
                    and not isinstance(x, bool) and math.isfinite(x) and x >= 0}

        per_plane_raw = d.get("per_plane", {})
        per_plane: dict[str, PlaneHourlyModeled] = {}
        if isinstance(per_plane_raw, dict):
            for k, v in per_plane_raw.items():
                if isinstance(k, str):
                    per_plane[k] = PlaneHourlyModeled.from_dict(v)

        cloud_raw = d.get("cloud_class_by_hour", {})
        cloud_class_by_hour: dict[str, str] = {}
        if isinstance(cloud_raw, dict):
            cloud_class_by_hour = {
                k: str(v) for k, v in cloud_raw.items()
                if isinstance(k, str) and isinstance(v, str)
            }

        eta_raw = d.get("eta")
        eta = (
            float(eta_raw)
            if isinstance(eta_raw, (int, float)) and math.isfinite(float(eta_raw))
            else None
        )

        bands_raw = d.get("bands")
        bands: dict[str, dict[str, float]] | None = None
        if isinstance(bands_raw, dict):
            allowed = {"dc_p10_slot_wh", "dc_p50_slot_wh", "dc_p90_slot_wh",
                       "ac_p10_slot_wh", "ac_p90_slot_wh"}
            bands = {key: {stamp: float(v) for stamp, v in curve.items()
                          if isinstance(stamp, str) and isinstance(v, (int, float))
                          and not isinstance(v, bool) and math.isfinite(v) and v >= 0}
                     for key, curve in bands_raw.items() if key in allowed and isinstance(curve, dict)}
        return cls(
            issued_at=str(d.get("issued_at", "")),
            status=str(d.get("status", "")),
            raw_hourly_wh=_fd("raw_hourly_wh"),
            corrected_hourly_wh=_fd("corrected_hourly_wh"),
            raw_daily_kwh=_fd("raw_daily_kwh"),
            corrected_daily_kwh=_fd("corrected_daily_kwh"),
            per_plane=per_plane,
            cloud_class_by_hour=cloud_class_by_hour,
            slow_only_hourly_wh=_fd("slow_only_hourly_wh"),
            eta=eta,
            version=_safe_int(d.get("version", 2), 2),
            corrected_ac_hourly_wh=(
                _fd("corrected_ac_hourly_wh")
                if isinstance(d.get("corrected_ac_hourly_wh"), dict) else None
            ),
            computed_at=d.get("computed_at") if isinstance(d.get("computed_at"), str) else None,
            archived_at=d.get("archived_at") if isinstance(d.get("archived_at"), str) else None,
            provenance=bounded_provenance(d.get("provenance")),
            bands=bands,
        )

    def to_dict(self) -> dict:
        out: dict = {
            "version": self.version,
            "issued_at": self.issued_at,
            "status": self.status,
            "raw_hourly_wh": dict(self.raw_hourly_wh),
            "corrected_hourly_wh": dict(self.corrected_hourly_wh),
            "raw_daily_kwh": dict(self.raw_daily_kwh),
            "corrected_daily_kwh": dict(self.corrected_daily_kwh),
            "per_plane": {k: v.to_dict() for k, v in self.per_plane.items()},
            "cloud_class_by_hour": dict(self.cloud_class_by_hour),
        }
        # Store trim: write the slow-only curve ONLY when non-empty (slow layer
        # was active). Empty == slow-only == raw, and ``from_dict`` reads a
        # missing key as {}, so omitting it keeps the round-trip exact while
        # avoiding a second full copy of the raw curve in every snapshot of the
        # 90-day issued ring.
        if self.slow_only_hourly_wh:
            out["slow_only_hourly_wh"] = dict(self.slow_only_hourly_wh)
        # Written only when known (a legacy/omitted eta round-trips as None, which
        # the reader replaces with the current learned eta).
        if self.eta is not None:
            out["eta"] = self.eta
        if self.corrected_ac_hourly_wh is not None:
            out["corrected_ac_hourly_wh"] = dict(self.corrected_ac_hourly_wh)
        if self.computed_at is not None:
            out["computed_at"] = self.computed_at
        if self.archived_at is not None:
            out["archived_at"] = self.archived_at
        if self.provenance is not None:
            out["provenance"] = dict(self.provenance)
        if self.bands is not None:
            out["bands"] = {key: dict(curve) for key, curve in self.bands.items()}
        return out
