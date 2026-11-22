"""A rejected smoothing attempt must not turn safe corners into instant moves."""
from types import SimpleNamespace

import numpy as np
import pytest

from agent_service.planing import trajectory as module


PATH = np.array([
    [0., -1.5708, 1.5708, -1.5708, -1.5708, 0.],
    [.34594, -1.24973, 1.14812, -1.51820, -1.60850, .38154],
    [.55845, -.91978, .87918, -1.53019, -1.57080, .55845],
])


def test_rrt_spline_is_not_slower_than_safe_stop_at_corner_profile():
    # Force the original corner to remain, while accepting locally rounded curves.
    class Checker:
        def check_path(self, points, **kwargs):
            return SimpleNamespace(in_collision=len(points) == 2)

    limits = (np.ones(6), np.full(6, .5), np.full(6, 2.))
    processor = module.TrajectoryProcessor(collision_checker=Checker(), config={
        "timing_method": "quintic", "velocity_limits": limits[0],
        "acceleration_limits": limits[1], "jerk_limits": limits[2]})
    result = processor.process({"waypoints": PATH, "path_type": "joint"},
                               {"constraints": {"velocity_scale": 1., "acceleration_scale": 1.}})
    safe = module._retime_polyline(PATH, *limits, .01, "quintic")
    assert result["duration"] <= safe[0][-1] + 1e-9
    assert np.allclose(result["waypoints"][-1], PATH[-1])
    for key, bound in zip(("velocities", "accelerations", "jerks"), limits):
        assert np.all(np.max(np.abs(result[key]), axis=0) <= bound + 1e-8)


@pytest.mark.parametrize("reject_final", [False, True])
def test_rejected_smoothing_preserves_dynamic_limits_and_dense_samples(monkeypatch, reject_final):
    class Checker:
        def __init__(self):
            self.calls = 0

        def check_path(self, points, **kwargs):
            self.calls += 1
            # Input is safe; reject shortcuts, all three smoothing candidates,
            # and the thinned retiming validation. Accept the safe original and
            # dense per-segment retiming unless testing the fail-closed case.
            is_original = any(np.array_equal(points, candidate) for candidate in
                              (PATH, np.vstack((PATH, PATH[-1]))))
            dense = len(points) > 100
            return SimpleNamespace(in_collision=not is_original and
                                   (not dense or reject_final))

    # Model the corner lost by thinning, forcing the formerly unsafe fallback.
    monkeypatch.setattr(module, "_thin_collision_samples", lambda points: points[[0, -1]])
    monkeypatch.setattr(module, "_evaluate", lambda controls, grid: PATH[[0, -1]])
    limits = [3., 2., 1., 3., 3., 3.]
    processor = module.TrajectoryProcessor(collision_checker=Checker(), config={
        "timing_method": "quintic", "velocity_limits": limits,
        "acceleration_limits": 1., "jerk_limits": 10., "sample_period": .01})
    request = {"constraints": {"velocity_scale": 1., "acceleration_scale": 1.}}
    if reject_final:
        with pytest.raises(ValueError, match="Timed trajectory failed"):
            processor.process({"waypoints": PATH, "path_type": "joint"}, request)
        return
    result = processor.process({"waypoints": PATH, "path_type": "joint"}, request)
    assert result["postprocessing"]["timing_method"] == "validated_polyline_quintic"
    q = np.array(result["waypoints"])
    t = np.array(result["time_from_start"])
    assert len(q) > 100
    assert result["duration"] > 1.
    assert np.max(np.diff(t)) <= .01000001
    assert all(any(np.array_equal(point, corner) for point in q) for corner in PATH)
    assert np.all(np.max(np.abs(np.diff(q, axis=0) / np.diff(t)[:, None]), axis=0) <= limits)
    for key, maximum in (("velocities", limits), ("accelerations", 1.), ("jerks", 10.)):
        assert np.all(np.max(np.abs(result[key]), axis=0) <= np.array(maximum) + 1e-8)
    assert np.max(np.abs(result["velocities"])) > 0


def test_linear_validation_batches_fk_without_skipping_samples():
    from agent_service.planing.linear import retime_linear
    class Kinematics:
        def __init__(self):
            self.scalar_calls = 0
            self.batch_size = 0
        def forward(self, q):
            self.scalar_calls += 1
            return {"position": np.asarray([q[0], 0., 0.]), "rotation_matrix": np.eye(3)}
        def forward_batch(self, points):
            self.batch_size = len(points)
            return [{"position": np.asarray([q[0], 0., 0.]), "rotation_matrix": np.eye(3)} for q in points]
    kin = Kinematics()
    retime_linear(np.asarray([[0.], [.05], [.1]]), kin, lambda points: True,
                  {"constraints": {}}, (np.ones(1), np.ones(1), np.ones(1)*10), .02, "quintic")
    assert kin.scalar_calls == 2
    assert kin.batch_size >= 33
