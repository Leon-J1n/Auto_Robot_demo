"""Move collision reuse is scoped to an immutable planning pass."""

from types import SimpleNamespace

import numpy as np

from agent_service.skills.platform_runtime.collision import PlatformCollisionChecker


class Runtime:
    def __init__(self):
        self.contacts = []
        self.queries = 0
        self.state_reads = 0
        self.robot = self
        self.scene = SimpleNamespace(name_of=str, is_robot=lambda _: False,
                                     is_gripper=lambda value: value == 1)
        self.gripper_controller = SimpleNamespace(held_colliders=set())

    def full_joint_positions(self):
        self.state_reads += 1
        return np.zeros(6)

    def expand_joints(self, joints, base=None):
        if base is None:
            self.full_joint_positions()
        return np.asarray(joints)

    def configuration_contacts(self, *args, **kwargs):
        self.queries += 1
        return self.contacts


def test_repeated_checks_reuse_queries_without_extra_live_joint_reads():
    runtime = Runtime()
    checker = PlatformCollisionChecker(runtime)
    path = np.array([[0.] * 6, [0.1] * 6, [0.2] * 6])
    with checker.planning_scope():
        checker.check_path(path, resolution=0.05)
        query_count, state_reads = runtime.queries, runtime.state_reads
        for _ in range(10):
            assert not checker.check_path(path, resolution=0.05).in_collision
        assert runtime.queries == query_count
        assert runtime.state_reads == state_reads
        assert checker.cache_hits >= 60


def test_changed_scene_is_seen_on_next_planning_pass_and_outside_scope():
    runtime = Runtime()
    checker = PlatformCollisionChecker(runtime)
    q = np.ones(6)
    with checker.planning_scope():
        assert not checker.check(q).in_collision
    runtime.contacts = [SimpleNamespace(robot_collider=1, other=9, distance=0., colliding=True)]
    with checker.planning_scope():
        assert checker.check(q).in_collision
        checker.end_planning()
        runtime.contacts = []
        assert not checker.check(q).in_collision
    runtime.contacts = [SimpleNamespace(robot_collider=1, other=9, distance=0., colliding=True)]
    assert checker.check(q).in_collision


def test_contact_policy_clearance_and_exact_pose_are_separate_keys():
    runtime = Runtime()
    checker = PlatformCollisionChecker(runtime)
    runtime.contacts = [SimpleNamespace(robot_collider=1, other=9, distance=0., colliding=True)]
    q = np.ones(6)
    with checker.planning_scope():
        assert checker.check(q).in_collision
        assert not checker.check(q, allowed_geom_ids={9}).in_collision
        runtime.gripper_controller.held_colliders = {9}
        assert not checker.check(q).in_collision
        count = runtime.queries
        checker.check(q + 1e-9)
        assert runtime.queries == count + 1
        count = runtime.queries
        checker.check(q, safety_distance=1e-9)
        assert runtime.queries > count


def test_exception_and_baseline_refresh_invalidate_cached_verdicts():
    runtime = Runtime()
    checker = PlatformCollisionChecker(runtime)
    try:
        with checker.planning_scope():
            checker.check(np.ones(6))
            checker.refresh_baseline()
            assert not checker._cache
            raise RuntimeError("planning failed")
    except RuntimeError:
        pass
    assert not checker._cache_enabled
    assert checker._planning_joints is None
    assert not checker._cache


def test_cache_bound_never_skips_an_uncached_collision_query():
    runtime = Runtime()
    checker = PlatformCollisionChecker(runtime, cache_limit=1)
    with checker.planning_scope():
        checker.check(np.ones(6))
        checker.check(np.ones(6) * 2)
        count = runtime.queries
        checker.check(np.ones(6) * 2)
        assert runtime.queries == count + 1
        assert len(checker._cache) == 1


def test_path_still_samples_interior_and_reports_first_collision():
    runtime = Runtime()
    checker = PlatformCollisionChecker(runtime)
    original_query = runtime.configuration_contacts

    def contacts(joints, **kwargs):
        original_query(joints, **kwargs)
        if 0.04 < joints[0] < 0.06:
            return [SimpleNamespace(robot_collider=1, other=9, distance=0., colliding=True)]
        return []

    runtime.configuration_contacts = contacts
    with checker.planning_scope():
        assert not checker.check(np.zeros(6)).in_collision
        assert not checker.check(np.ones(6) * 0.1).in_collision
        result = checker.check_path([np.zeros(6), np.ones(6) * 0.1], resolution=0.025)
        assert result.in_collision
        assert result.contacts[0]["geom2"] == "9"


def test_environment_contact_is_never_hidden_by_start_baseline():
    runtime = Runtime()
    runtime.contacts = [SimpleNamespace(robot_collider=1, other=9, distance=0., colliding=True)]
    checker = PlatformCollisionChecker(runtime)
    assert checker.check(np.zeros(6)).in_collision


def test_carried_clearance_allows_retreat_but_not_grazing_or_worsening():
    runtime = Runtime()
    runtime.gripper_controller.held_colliders = {8}
    pair = SimpleNamespace(robot_collider=1, other=9, distance=.01, colliding=False)
    runtime.contacts = [pair]
    checker = PlatformCollisionChecker(runtime)
    with checker.planning_scope():
        assert not checker.check(np.zeros(6), safety_distance=.05).in_collision
        assert checker.check(np.ones(6) * .1, safety_distance=.05).in_collision
        pair.distance = .04
        assert not checker.check(np.ones(6) * .11, safety_distance=.05).in_collision
        assert checker.check(np.ones(6) * .3, safety_distance=.05).in_collision
        pair.distance = .06
        assert not checker.check(np.ones(6) * .31, safety_distance=.05).in_collision


def test_held_exemption_only_allows_gripper_contact():
    runtime = Runtime()
    checker = PlatformCollisionChecker(runtime)
    runtime.contacts = [SimpleNamespace(robot_collider=2, other=9, distance=0., colliding=True)]
    runtime.gripper_controller.held_colliders = {9}
    assert checker.check(np.ones(6), allowed_geom_ids={9}).in_collision
