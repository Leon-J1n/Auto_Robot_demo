"""RobotSimPlatform adapter for the Robotiq 2F-85 gripper.

Replaces the MuJoCo adapter. Two things work differently underneath.

The 2F-85's finger linkage is a closed loop: five joints follow the driver joint. The
URDF states that with `mimic` tags, but the platform has no mimic concept, so those
joints are independent here and this controller drives all six explicitly from one
commanded angle. The multipliers below are the linkage's, not a URDF lookup -- the
gripper's geometry is a fixed property of the hardware, like the pad names and the
85 mm stroke this class already hard-codes.

Closing is a clamp. The fingers travel until *both* pads touch the target object, and
if they never both touch it they keep going until the two pads meet each other. Then
they close a little further, because contact alone is not a grip: at the geometric
contact angle the pads exert no force, so there is no friction and the object slides
out of them as soon as the arm moves. That overshoot is `contact_preload`.

Nothing is reparented onto the gripper and no pose is rewritten per step: the object
stays a scene object owned by physics, held only by friction between the pads. A
clamped object is therefore carried the way a real gripper carries one -- and dropped
the way a real one drops it, if the grip is too weak for what the arm does next.

How weak that is depends on the object's restitution, and sharply. Measured on the
demo's 40 g ball, carried 250 mm: at restitution 0.0 the grip holds; at 0.2 and above
it does not, because a bouncy contact makes the pads rebound off the object instead of
settling into steady friction. Neither more preload (tested to 58% penetration into the
ball) nor letting the contact settle before moving (tested to 500 steps, which shakes
the ball loose before the arm even starts) recovers it. Carrying a bouncy object needs a
force-controlled grip that tracks the object as it rebounds, which the fixed joint
damping in the physics backend rules out -- not a larger constant here.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np


class PlatformGripperController:
    """Control and inspect the scene's Robotiq fingers."""

    driver_joint_names = (
        "robotiq_2f85_left_driver_joint",
        "robotiq_2f85_right_driver_joint",
    )
    # Joint name -> angle multiplier, the 2F-85 linkage's coupling to the driver.
    coupled_joints = {
        "robotiq_2f85_right_driver_joint": 1.0,
        "robotiq_2f85_left_driver_joint": 1.0,
        "robotiq_2f85_right_spring_link_joint": 1.0,
        "robotiq_2f85_left_spring_link_joint": 1.0,
        "robotiq_2f85_right_follower_joint": -1.0,
        "robotiq_2f85_left_follower_joint": -1.0,
    }
    maximum_width = 0.085
    maximum_force = 40.0
    driver_range = 0.8
    # Stop-angle search resolution. The coarse scan brackets the first angle that
    # satisfies a stop condition; bisection then sharpens it to
    # driver_range / (samples * 2**steps), about 0.4 mm of finger travel here, for
    # roughly 16 contact queries instead of one per frame.
    contact_scan_samples = 12
    contact_refine_steps = 4
    # How much further the driver closes once the pads are on the target, in radians --
    # roughly 5 mm of finger travel against the linkage's 0.8 rad / 85 mm stroke. The
    # position servo pushes the pads into the object by this much, and the contact force
    # that results is what actually holds it. Measured on the demo's ball: at 0.0 the ball
    # stays behind entirely when the arm lifts 150 mm, and at 0.05 it follows within 3 mm.
    # Larger values do not hold better and press implausibly far into the object -- 0.12 rad
    # compresses the ball by a quarter of its width, which a real object would resist.
    contact_preload = 0.05
    post_contact_steps = 20
    closure_duration = 0.5
    release_settle_steps = 100

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.robot = runtime.robot
        self.scene = runtime.scene
        names = self.robot.joint_names
        missing = [name for name in self.coupled_joints if name not in names]
        if missing:
            raise ValueError(f"Scene gripper is missing joints: {', '.join(missing)}")
        self._coupled_indices = np.asarray(
            [names.index(name) for name in self.coupled_joints], dtype=int)
        self._multipliers = np.asarray(list(self.coupled_joints.values()), dtype=float)
        self.driver_indices = np.asarray(
            [names.index(name) for name in self.driver_joint_names], dtype=int)
        limits = self.robot.joint_limits
        # Every coupled joint must stay inside its own limits, so the commandable
        # driver range is the intersection of what each follower permits.
        lower, upper = 0.0, self.driver_range
        for index, multiplier in zip(self._coupled_indices, self._multipliers):
            bounds = sorted(limits[index] / multiplier)
            lower, upper = max(lower, bounds[0]), min(upper, bounds[1])
        self._angle_bounds = (lower, upper)
        self._holding = False
        self._commanded_force = 0.0
        self._clamped_entity: int | None = None
        self.step_observer = None

    # ------------------------------------------------------------------ commands

    def open(self, width: float, *, speed: float = 0.5,
             timeout: float = 5.0) -> dict[str, Any]:
        self._holding = False
        self._commanded_force = 0.0
        was_clamped = self._clamped_entity is not None
        self._clamped_entity = None
        # Release starts from an already-open physical pose.  Animating the opening
        # through the position writer leaves the old closed geometry in the physics
        # backend until the next sync, so a payload can remain contact-constrained
        # while the release settle window is already running.
        final_angle = self._angle_for_width(width)
        self._apply_angle(final_angle)
        self._sync_world()
        result = {
            "target_width": float(np.clip(width, 0.0, self.maximum_width)),
            "final_width": self.width,
            "contact_detected": False,
            "object_present": False,
            "fingers_met": False,
        }
        if was_clamped:
            # Let physics resolve whatever the pads were pressing on, holding the arm
            # in place so stepping does not let it sag.
            self._settle(self.release_settle_steps)
        return result
    def close(self, width: float, *, speed: float = 0.5, force: float = 20.0,
              timeout: float = 5.0,
              target_object_id: str | None = None,
              post_contact_steps: int | None = None) -> dict[str, Any]:
        """Clamp: close until both pads touch the target, else until the pads meet."""
        if post_contact_steps is not None and (
                isinstance(post_contact_steps, bool) or
                not isinstance(post_contact_steps, int) or post_contact_steps < 0):
            raise ValueError("post_contact_steps must be a non-negative integer")
        self._commanded_force = float(force)
        result = self._move(width, speed=speed, timeout=timeout,
                            target_object_id=target_object_id, stop_on_contact=True,
                            post_contact_steps=(self.post_contact_steps if post_contact_steps is None
                                                else int(post_contact_steps)))
        if result["contact_detected"]:
            self._clamped_entity = self._target_entity(target_object_id)
        result["force"] = self._commanded_force if result["contact_detected"] else 0.0
        return result

    def hold(self, force: float) -> None:
        self._holding = True
        self._commanded_force = float(force)

    def release(self, *, width: float | None = None,
                timeout: float = 5.0) -> dict[str, Any]:
        return self.open(self.maximum_width if width is None else width,
                         timeout=timeout)

    def get_state(self, target_object_id: str | None = None) -> dict[str, Any]:
        clamped = self._target_clamped(target_object_id)
        return {
            "known": True,
            "initialized": True,
            "width": self.width,
            "force": self._commanded_force if clamped else 0.0,
            "contact_detected": clamped,
            "object_present": clamped,
            "holding": self._holding and clamped,
            "slip_detected": False,
        }

    @property
    def width(self) -> float:
        angle = float(np.mean(self.robot.joint_state[0][self.driver_indices]))
        return float(np.clip(self.maximum_width * (1.0 - angle / self.driver_range),
                             0.0, self.maximum_width))

    @property
    def held_entity(self) -> int | None:
        """The object currently clamped between the pads, if any.

        Named `held` for the callers that predate the clamp; nothing is attached to
        the gripper, so this entity is still an ordinary scene object owned by
        physics and it stays where it is when the fingers open.
        """
        return self._clamped_entity

    @property
    def held_colliders(self) -> tuple[int, ...]:
        """Colliders of the clamped object, for the collision checker's filtering.

        A clamp works by touching, so the pads are always in contact with whatever
        they hold; the checker has to know which colliders those are or every
        successful clamp reads as a collision.
        """
        if self._clamped_entity is None:
            return ()
        return self.scene.colliders_of(self._clamped_entity)
    # ------------------------------------------------------------------ motion

    def _angle_for_width(self, width: float) -> float:
        target = float(np.clip(width, 0.0, self.maximum_width))
        angle = self.driver_range * (1.0 - target / self.maximum_width)
        return float(np.clip(angle, *self._angle_bounds))

    def _angle_vector(self, angle: float) -> np.ndarray:
        """A full joint vector with the finger linkage at one driver angle."""
        full = self.runtime.full_joint_positions()
        full[self._coupled_indices] = angle * self._multipliers
        return full

    def _apply_angle(self, angle: float) -> None:
        """Drive all six linkage joints from one commanded driver angle."""
        self.robot.set_joint_positions(self._angle_vector(angle), limits="clamp")

    def _stop_angle(self, start_angle: float, final_angle: float,
                    target: set[int]) -> tuple[float, bool, bool]:
        """Smallest travel toward `final_angle` that satisfies a stop condition.

        Returns the angle plus whether the target ended up clamped and whether the
        pads met each other. Two conditions end the closure, and the search takes
        whichever comes first: both pads touching the target, or the pads touching
        each other. Both are geometric, so both can be evaluated on a hypothetical
        configuration without committing it.

        Probing hypothetical configurations rather than stepping is what makes this
        affordable: a contact query on an untouched world runs in about 5 ms, but any
        live-world write dirties the collision sync and the next query pays roughly
        365 ms to rebuild it -- mesh colliders included. Sweeping with a write per
        step would cost minutes, so the stop angle is searched first and only that
        angle is ever committed.

        A coarse scan brackets the stop angle instead of assuming the conditions are
        monotonic in the closing angle, then bisection sharpens the bracket so the
        reported width is not quantised to the scan's step.
        """
        state = self._contact_state(self._angle_vector(start_angle), target)
        if any(state):
            return start_angle, state[0], state[1]
        previous = start_angle
        for index in range(1, self.contact_scan_samples + 1):
            alpha = index / self.contact_scan_samples
            candidate = start_angle + (final_angle - start_angle) * alpha
            state = self._contact_state(self._angle_vector(candidate), target)
            if any(state):
                low, high = previous, candidate
                for _ in range(self.contact_refine_steps):
                    middle = (low + high) / 2.0
                    probed = self._contact_state(self._angle_vector(middle), target)
                    if any(probed):
                        high, state = middle, probed
                    else:
                        low = middle
                return high, state[0], state[1]
            previous = candidate
        return final_angle, False, False
    def _move(self, target_width: float, *, speed: float, timeout: float,
              target_object_id: str | None,
              stop_on_contact: bool,
              post_contact_steps: int = 0) -> dict[str, Any]:
        """Open the fingers, or clamp them shut on whatever is between them.

        Where the fingers stop is decided before anything moves, by probing
        hypothetical configurations; see _stop_angle for why the live world must not
        be written during that search. The visible motion is then a plain animation
        from the start angle to the stop angle, which needs no queries.

        The search runs whether or not a target object was named: with no target the
        pads simply close until they meet each other, which is the commanded width
        here anyway but stops at the true geometric limit rather than trusting the
        linkage model.
        """
        target_width = float(np.clip(target_width, 0.0, self.maximum_width))
        final_angle = self._angle_for_width(target_width)
        start_angle = float(np.mean(self.robot.joint_state[0][self.driver_indices]))
        clamped = fingers_met = False
        stop_angle = final_angle
        time_step = max(float(getattr(self.runtime, "fixed_time_step", 0.002)), 1e-6)
        if stop_on_contact:
            stop_angle, clamped, fingers_met = self._stop_angle(
                start_angle, final_angle, self._target_colliders(target_object_id))
            if clamped:
                # Keep the same closing speed after both pads touch, then stop after
                # the configured post-contact interval (10 steps = 0.02 s by default).
                rate = self.driver_range / self.closure_duration
                extra = min(float(post_contact_steps) * time_step,
                            max(final_angle - stop_angle, 0.0) / max(rate, 1e-9))
                stop_angle = min(final_angle, stop_angle + rate * extra)
        # Contact probing may require several collision queries. Start the motion
        # timeout only after that planning work has completed.
        motion_started = time.perf_counter()

        travel = abs(stop_angle - start_angle) / self.driver_range
        # A complete driver stroke is always 0.5 s; scale partial travel linearly
        # instead of interpreting the normalized speed as an excessively fast angle.
        duration = max(self.closure_duration * travel, 0.05)
        # Intermediate poses only matter to something watching them.
        observed = self.runtime.viewer is not None or self.runtime.realtime
        steps = max(2, int(np.ceil(duration * self.runtime.playback_fps))) if observed else 1
        for step in range(1, steps + 1):
            alpha = step / steps
            self._apply_angle(start_angle + (stop_angle - start_angle) * alpha)
            if self.runtime.viewer is not None:
                self.runtime.viewer.sync()
            elapsed = duration * alpha
            if ((self.runtime.realtime and time.perf_counter() - motion_started >= timeout)
                    or (not self.runtime.realtime and elapsed >= timeout)):
                raise TimeoutError("Gripper motion timed out.")
            if self.runtime.realtime:
                remaining = duration * alpha - elapsed
                if remaining > 0:
                    time.sleep(remaining)
        self._sync_world()
        if clamped:
            self._settle(1)
        return {
            "target_width": target_width,
            "final_width": self.width,
            "contact_detected": clamped,
            "object_present": clamped,
            "fingers_met": fingers_met,
        }
    def _settle(self, steps: int) -> None:
        """Step physics while the joint controller holds the current configuration."""
        self.robot.command_joint_positions(self.runtime.full_joint_positions())
        for _ in range(steps):
            self.runtime.simulator.step_unpaced(self.runtime.world)
            self._notify_step()
            if self.runtime.viewer is not None:
                self.runtime.viewer.sync()

    def _notify_step(self) -> None:
        observer = getattr(self, "step_observer", None)
        if callable(observer):
            observer()

    def _sync_world(self) -> None:
        """Push the animated finger pose into the physics and collision backends.

        The animation above writes joint positions straight into the scene, which the
        backends do not see on their own. Without this the physics backend would
        still hold the pre-clamp finger angles and would replay the whole closure on
        the next step -- driving the pads through the object instead of resting
        against it -- and contact queries would answer for a stale pose.
        """
        self.runtime.simulator.sync_physics(self.runtime.world)
        self.runtime.simulator.sync_collision(self.runtime.world)

    # ------------------------------------------------------------------ queries

    def _target_entity(self, target_object_id: str | None) -> int | None:
        if not target_object_id:
            return None
        return self.scene.object_entity(target_object_id)

    def _target_colliders(self, target_object_id: str | None) -> set[int]:
        """Collider entities belonging to the target object."""
        entity = self._target_entity(target_object_id)
        if entity is None:
            return set()
        return set(self.scene.colliders_of(entity))

    def _pad_side(self, entity: int) -> str | None:
        """Which finger a pad collider belongs to, or None if it is not a pad."""
        if not self.scene.is_gripper_pad(entity):
            return None
        link = self.scene.link_of(entity) or ""
        if "left" in link:
            return "left"
        return "right" if "right" in link else None

    def _contact_state(self, positions: np.ndarray,
                       target: set[int]) -> tuple[bool, bool]:
        """(both pads on the target, pads on each other) at the given configuration.

        One query answers both: the pads are only considered met when a left pad
        touches a right pad, because the gripper reports around nineteen self-pairs
        at every angle from adjacent links, so any coarser test would fire
        immediately and the fingers would never close at all.
        """
        pads: set[str] = set()
        met = False
        for pair in self.robot.configuration_contacts(positions, limits="ignore"):
            if not pair.colliding:
                continue
            first, second = pair.robot_collider, pair.other
            first_side, second_side = self._pad_side(first), self._pad_side(second)
            if first_side is not None and second_side is not None:
                if first_side != second_side:
                    met = True
                continue
            if not target:
                continue
            if first in target and second_side is not None:
                pads.add(second_side)
            elif second in target and first_side is not None:
                pads.add(first_side)
        return len(pads) >= 2, met

    def _target_clamped(self, target_object_id: str | None) -> bool:
        """Whether both pads touch the target object at the live pose."""
        target = self._target_colliders(target_object_id)
        if not target:
            return False
        return self._contact_state(self.runtime.full_joint_positions(), target)[0]
