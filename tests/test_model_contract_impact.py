"""Impact reports keep diffuse visibility separate from total DC/AC energy."""
from pathlib import Path

import pytest

from scripts.validation.model_contract_impact import impact, matrix


def test_matrix_self_comparison_and_electrical_reference():
    result = matrix(Path(__file__).resolve().parents[1])
    assert len(result['records']) == 18
    paired = impact(result, result)
    assert all(value == 0 for row in paired['records'] for value in row['delta'].values())
    clipped = next(row for row in result['records'] if row['case'] == 'electrical_clipped_True_factor_0.5')
    assert clipped['dc_wh'] == pytest.approx(12.5)
    assert clipped['ac_wh'] == pytest.approx(11.25)
    diffuse = next(row for row in result['records'] if row['case'] == 'diffuse_tilt_30_horizon_30')
    assert 0 < diffuse['sky_view_factor'] < 1
    assert diffuse['dc_wh'] > 0
    with pytest.raises(ValueError, match='same matrix'):
        impact(result, {'records': []})
