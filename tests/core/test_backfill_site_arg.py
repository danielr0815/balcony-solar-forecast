"""The backfill CLI must never reconstruct against a site nobody chose (0.23.1).

Until 0.23.0 ``scripts/backfill.py --site`` was optional: omitting it silently
loaded ``const.DEFAULT_SITE``, the SHIPPED REFERENCE site. That object is a
structural example with knowingly stale geometry (the disproved M4/M8 screen
az 135-175, wall edge az 212 instead of the live 195, no ``albedo`` /
``bifacial_beam_gain`` keys), so a bootstrap built that way trained every
learner against foreign geometry — and looked healthy while doing it, because
the site signature only guards the IMPORT and only on lat/lon + plane names.

These tests pin the closed trap (SPEC §6):

  * no ``--site`` and no opt-in  -> abort with an actionable message that names
    the ``run_bootstrap`` action, the config export and the opt-in flag;
  * ``--use-default-site``       -> runs, but logs a loud WARNING that the
    reference site is NOT the operator's plant;
  * ``--site``                   -> unchanged, and wins over a stray opt-in.

Pure: no network, no Home Assistant — the guard sits at the top of
``run_backfill``, before any fetch.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

# Same import bootstrap as the sibling backfill tests: the conftest registers
# the HA-free namespace packages; make the standalone dev script importable.
_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import backfill as bf  # noqa: E402

_BASE_ARGV = [
    "--ha-url", "http://homeassistant.local:8123",
    "--token", "dummy-token",
    "--start", "2026-07-01",
    "--end", "2026-07-02",
]


def _args(*extra: str):
    return bf.build_arg_parser().parse_args([*_BASE_ARGV, *extra])


def test_missing_site_raises_with_an_actionable_message():
    """Without --site (and without the opt-in) the run refuses to start."""
    with pytest.raises(bf.SiteArgumentError) as excinfo:
        bf.resolve_site_arg(_args())

    msg = str(excinfo.value)
    # (i) the recommended path, (ii) how to export the live config,
    # (iii) the explicit reference-site opt-in.
    assert "balcony_solar_forecast.run_bootstrap" in msg
    assert "site.json" in msg
    assert "--use-default-site" in msg


def test_missing_site_message_points_at_reconfigure_and_data_site():
    """The export recipe must match the flows this integration actually has.

    ``CONF_SITE`` is structural: it is rendered ONLY by the reconfigure step
    (three-dot menu -> Reconfigure / "Neu konfigurieren"), never by the
    *Configure* button, whose options schema carries the runtime tunables
    alone. And it is stored in ``entry.data`` — ``async_step_reconfigure``
    even strips the structural keys out of ``entry.options`` — so on the HA
    host the key is ``data.site``, not ``options.site``. Sending the operator
    to *Configure* / ``options.site`` is a dead end on any current entry.
    """
    msg = bf.MISSING_SITE_MESSAGE

    assert "Reconfigure" in msg
    assert "data.site" in msg
    # "Configure" may only appear as the explicit NOT-this warning.
    for line in msg.splitlines():
        if "Configure" in line and "Reconfigure" not in line:
            assert "NOT" in line, line


def test_main_exits_2_on_missing_site_without_touching_the_network(monkeypatch):
    """The CLI entry point reports the failure as exit code 2, not a traceback.

    Nothing is mocked here on purpose: reaching any fetch would need network,
    so a green run also proves the guard fires BEFORE the first request.
    """
    def _boom(*_a, **_kw):  # pragma: no cover - must never be reached
        raise AssertionError("fetch attempted despite missing --site")

    monkeypatch.setattr(bf, "fetch_weather_range", _boom)
    monkeypatch.setattr(bf, "fetch_lts_hourly", _boom)

    assert bf.main(list(_BASE_ARGV)) == 2


def test_use_default_site_opts_in_and_warns(caplog):
    """The old behaviour stays reachable, but only loudly and on purpose."""
    with caplog.at_level(logging.WARNING, logger=bf._LOGGER.name):
        assert bf.resolve_site_arg(_args("--use-default-site")) is None

    warning = "\n".join(
        r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING
    )
    assert "DEFAULT_SITE" in warning
    assert "NOT your plant" in warning


def test_explicit_site_is_used_unchanged_and_beats_a_stray_opt_in(caplog):
    """--site keeps working exactly as before and takes precedence."""
    assert bf.resolve_site_arg(_args("--site", "my-site.json")) == Path(
        "my-site.json"
    )

    with caplog.at_level(logging.WARNING, logger=bf._LOGGER.name):
        resolved = bf.resolve_site_arg(
            _args("--site", "my-site.json", "--use-default-site")
        )
    assert resolved == Path("my-site.json")
    warning = "\n".join(r.getMessage() for r in caplog.records)
    assert "ignored" in warning  # the opt-in, not the site file
