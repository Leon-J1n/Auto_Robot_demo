import sys

import numpy as np
import pytest

from agent_service.planing.timing import parameterize


def test_auto_uses_installed_ruckig_and_respects_limits():
    pytest.importorskip("ruckig")
    times, positions, velocities, accelerations, jerks, backend = parameterize(1., .75, .25, .25)
    assert backend == "ruckig"
    assert positions[-1] == pytest.approx(1.)
    assert np.all(np.diff(times) > 0)
    assert max(abs(velocities)) <= .75 + 1e-9
    assert max(abs(accelerations)) <= .25 + 1e-9
    assert max(abs(jerks)) <= .25 + 1e-8


def test_missing_ruckig_reports_interpreter_and_fallback(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, "ruckig", None)
    assert parameterize(1., 1., 1., 10.)[-1] == "quintic"
    assert sys.executable in caplog.text
    assert "using quintic" in caplog.text
    with pytest.raises(ValueError, match="pip install"):
        parameterize(1., 1., 1., 10., method="ruckig")
