"""Tests for :mod:`ssik.subproblems.sp4`."""

from __future__ import annotations

import numpy as np
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from ssik.subproblems import sp4
from ssik.subproblems._rotation import rotate


def _unit(v: np.ndarray) -> np.ndarray:
    return v / float(np.linalg.norm(v))


_FINITE = st.floats(min_value=-2.0, max_value=2.0, allow_nan=False, allow_infinity=False, width=64)
_ANGLE = st.floats(min_value=-np.pi + 1e-2, max_value=np.pi - 1e-2, allow_nan=False, width=64)


@st.composite
def _sp4_case(
    draw: st.DrawFn,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, float]:
    h = np.array([draw(_FINITE), draw(_FINITE), draw(_FINITE)])
    k = np.array([draw(_FINITE), draw(_FINITE), draw(_FINITE)])
    p = np.array([draw(_FINITE), draw(_FINITE), draw(_FINITE)])
    theta = draw(_ANGLE)
    assume(float(np.linalg.norm(h)) > 1e-2)
    assume(float(np.linalg.norm(k)) > 1e-2)
    assume(float(np.linalg.norm(p)) > 1e-2)
    kk = _unit(k)
    # p not parallel to k (degenerate: h . Rot(k, t) p = const).
    p_perp_sq = float(np.dot(p, p)) - float(np.dot(kk, p)) ** 2
    assume(p_perp_sq > 1e-3)
    d = float(np.dot(h, rotate(kk, theta, p)))
    return h, kk, p, theta, d


@given(_sp4_case())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
def test_roundtrip_every_solution_matches_target(
    case: tuple[np.ndarray, np.ndarray, np.ndarray, float, float],
) -> None:
    h, k, p, _theta, d = case
    solutions, is_ls = sp4.solve(h, k, p, d)
    assert not is_ls
    assert len(solutions) in (1, 2)
    for theta_s in solutions:
        d_s = float(np.dot(h, rotate(k, theta_s, p)))
        assert abs(d_s - d) < 1e-8


def test_ls_target_out_of_range() -> None:
    """Target scalar beyond A^2 + B^2 reach returns LS projection."""
    h = np.array([1.0, 0.0, 0.0])
    k = np.array([0.0, 0.0, 1.0])
    p = np.array([1.0, 0.0, 0.0])
    # Max h.Rot(k,t)p = 1, min = -1; d=5 is out of range.
    solutions, is_ls = sp4.solve(h, k, p, 5.0)
    assert is_ls
    assert len(solutions) == 1


def test_p_along_k_degenerate_exact() -> None:
    """p collinear with k: rotation has no effect; exact iff target = C."""
    h = np.array([1.0, 0.0, 0.0])
    k = np.array([0.0, 0.0, 1.0])
    p = np.array([0.0, 0.0, 3.0])
    # h . p = 0 for any rotation (since Rot(k,t) p = p when p || k).
    solutions, is_ls = sp4.solve(h, k, p, 0.0)
    assert not is_ls
    assert solutions == [0.0]


def test_p_along_k_degenerate_infeasible() -> None:
    h = np.array([1.0, 0.0, 0.0])
    k = np.array([0.0, 0.0, 1.0])
    p = np.array([0.0, 0.0, 3.0])
    # h.p = 0 always; asking for d=5 is infeasible.
    solutions, is_ls = sp4.solve(h, k, p, 5.0)
    assert is_ls
    assert solutions == [0.0]
