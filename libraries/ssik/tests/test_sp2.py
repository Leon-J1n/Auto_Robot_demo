"""Tests for :mod:`ssik.subproblems.sp2`.

Coverage:

- **Roundtrip**: pick random ``k1, k2, theta1, theta2, p``; build
  ``q = Rot(k2, -theta2) Rot(k1, theta1) p`` (so the equation holds for
  ``(theta1, theta2)``); assert sp2 returns at least one solution and that
  every returned solution satisfies the equation.
- **Parallel axes**: ``k1 parallel k2`` triggers the degenerate branch,
  which returns a canonical LS solution.
- **Infeasible |p| != |q|**: LS flag set.
"""

from __future__ import annotations

import numpy as np
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from ssik.subproblems import sp2
from ssik.subproblems._rotation import rotate


def _unit(v: np.ndarray) -> np.ndarray:
    return v / float(np.linalg.norm(v))


_FINITE = st.floats(min_value=-2.0, max_value=2.0, allow_nan=False, allow_infinity=False, width=64)
_ANGLE = st.floats(min_value=-np.pi + 1e-2, max_value=np.pi - 1e-2, allow_nan=False, width=64)


@st.composite
def _sp2_case(
    draw: st.DrawFn,
) -> tuple[np.ndarray, np.ndarray, float, float, np.ndarray]:
    k1 = np.array([draw(_FINITE), draw(_FINITE), draw(_FINITE)])
    k2 = np.array([draw(_FINITE), draw(_FINITE), draw(_FINITE)])
    p = np.array([draw(_FINITE), draw(_FINITE), draw(_FINITE)])
    t1 = draw(_ANGLE)
    t2 = draw(_ANGLE)
    assume(float(np.linalg.norm(k1)) > 1e-2)
    assume(float(np.linalg.norm(k2)) > 1e-2)
    assume(float(np.linalg.norm(p)) > 1e-2)
    k1u, k2u = _unit(k1), _unit(k2)
    # Ensure axes aren't too close to parallel (degenerate branch).
    assume(float(np.linalg.norm(np.cross(k1u, k2u))) > 0.2)
    # Reject p parallel to k1: Rot(k1, t1) p is invariant under t1 so the
    # seeded t1 is one of infinitely many valid solutions and the test
    # can't assert recovery of a specific value.
    p_perp_k1_sq = float(np.dot(p, p)) - float(np.dot(k1u, p)) ** 2
    assume(p_perp_k1_sq > 1e-3)
    # Similarly, reject q-parallel-to-k2 by computing q here and checking.
    q = rotate(k2u, -t2, rotate(k1u, t1, p))
    q_perp_k2_sq = float(np.dot(q, q)) - float(np.dot(k2u, q)) ** 2
    assume(q_perp_k2_sq > 1e-3)
    return k1u, k2u, t1, t2, p


@given(_sp2_case())
@settings(max_examples=200, deadline=None, suppress_health_check=[HealthCheck.filter_too_much])
def test_roundtrip_all_solutions_satisfy(
    case: tuple[np.ndarray, np.ndarray, float, float, np.ndarray],
) -> None:
    k1, k2, t1, t2, p = case
    # Build q such that Rot(k1, t1) p = Rot(k2, t2) q, i.e.
    # q = Rot(k2, -t2) Rot(k1, t1) p.
    lhs = rotate(k1, t1, p)
    q = rotate(k2, -t2, lhs)

    solutions, is_ls = sp2.solve(k1, k2, p, q)

    assert not is_ls
    assert len(solutions) in (1, 2)

    # Every returned solution must satisfy Rot(k1, t1') p == Rot(k2, t2') q.
    for t1_s, t2_s in solutions:
        left = rotate(k1, t1_s, p)
        right = rotate(k2, t2_s, q)
        assert np.allclose(left, right, atol=1e-8), f"solution ({t1_s}, {t2_s}) fails the equation"

    # The original (t1, t2) should appear as one of the solutions (mod 2pi).
    found = False
    for t1_s, t2_s in solutions:
        if (
            abs(((t1_s - t1 + np.pi) % (2 * np.pi)) - np.pi) < 1e-6
            and abs(((t2_s - t2 + np.pi) % (2 * np.pi)) - np.pi) < 1e-6
        ):
            found = True
            break
    assert found, f"the seeded (t1={t1}, t2={t2}) not among recovered solutions {solutions}"


def test_two_solutions_generic() -> None:
    """Generic non-degenerate SP2 should return two solutions."""
    k1 = np.array([0.0, 0.0, 1.0])
    k2 = np.array([0.0, 1.0, 0.0])
    p = np.array([1.0, 0.5, 0.3])
    # Arbitrary q with |q| = |p| and the right axial projections.
    t1, t2 = 0.7, -0.4
    q = rotate(k2, -t2, rotate(k1, t1, p))

    solutions, is_ls = sp2.solve(k1, k2, p, q)
    assert not is_ls
    assert len(solutions) == 2
    for t1_s, t2_s in solutions:
        assert np.allclose(rotate(k1, t1_s, p), rotate(k2, t2_s, q), atol=1e-10)


def test_parallel_axes_flags_ls() -> None:
    k1 = np.array([0.0, 0.0, 1.0])
    k2 = np.array([0.0, 0.0, 1.0])  # parallel
    p = np.array([1.0, 0.0, 0.5])
    q = rotate(k1, 0.5, p)  # valid rotation
    solutions, is_ls = sp2.solve(k1, k2, p, q)
    assert is_ls
    assert len(solutions) == 1  # canonical representative


def test_infeasible_magnitude_mismatch() -> None:
    k1 = np.array([0.0, 0.0, 1.0])
    k2 = np.array([0.0, 1.0, 0.0])
    p = np.array([1.0, 0.0, 0.0])
    q = np.array([2.0, 0.0, 0.0])  # magnitude 2 != 1
    _, is_ls = sp2.solve(k1, k2, p, q)
    assert is_ls
