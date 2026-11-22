"""Live-world adapter for the Robotiq 2F-85 gripper.

Two things work differently from a naive joint command.

The 2F-85's finger linkage is a closed loop: five joints follow the driver joint.
MuJoCo enforces the source URDF's `mimic` tags as mechanical constraints. This
controller supplies consistent targets and a shared effort budget to all six
joints. The multipliers below are the linkage's, not a URDF lookup -- the
gripper's geometry is a fixed property of the hardware, like the pad names and the
85 mm stroke this class also hard-codes.

Closing is a clamp. The fingers travel until *both* distal fingers touch the target object, and
if they never both touch it they keep going until the two pads meet each other. Then
they close a little further, because contact alone is not a grip: at the geometric
contact angle the pads exert no force, so there is no friction and the object slides
out of them as soon as the arm moves. The post-contact steps set that overshoot.

Nothing is reparented onto the gripper and no pose is rewritten per step: the object
stays a scene object owned by physics, held only by friction between the pads. A
clamped object is therefore carried the way a real gripper carries one -- and dropped
the way a real one drops it, if the grip is too weak for what the arm does next. How
weak that is depends sharply on the object's restitution; a bouncy contact makes the
pads rebound instead of settling into steady friction, and no amount of extra preload
recovers it.

The ramp stops only after actual bilateral contact persists for the configured
number of simulation steps. Joint state remains owned by physics, including when
contact stalls the finite-effort servo short of its commanded target.
"""

from __future__ import annotations

import time
from typing import Any

import numpy as np


class LiveGripperController:
    """Control and inspect the live scene's Robotiq fingers."""

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
    # that results is what actually holds it. At 0.0 an object stays behind entirely when
    # the arm lifts. Larger values do not hold better and press implausibly far into the
    # object.
    contact_preload = 0.05
    # After both pads touch, keep the preload command active for this many physics
    # steps before declaring the clamp complete. At dt=0.002, 20 steps is 0.04 s.
    post_contact_steps = 20
    # A full driver stroke closes over 0.5 s. Partial travel is scaled by the same
    # constant angular velocity so the timeline never jumps to the grasp pose.
    closure_duration = 0.5
    # After release, only keep enough unpaced physics for a payload dropped from
    # roughly 10 cm to reach the table (100 * 0.002 s = 0.2 s). The old 250-step
    # opening animation plus another 250-step settle made the payload appear to
    # fall over an artificially long interval in the published timeline.
    release_settle_steps = 100
    # Steps allowed for the fingers to reach the commanded angle, and the angular
    # tolerance that counts as arrived.
    closure_steps = 240
    closure_tolerance = 2e-3

    def __init__(self, runtime: Any) -> None:
        self.runtime = runtime
        self.robot = runtime.robot
        self.scene = runtime.scene
        names = tuple(runtime.platform_joint_names)
        missing = [name for name in self.coupled_joints if name not in names]
        if missing:
            raise ValueError(f"Scene gripper is missing joints: {', '.join(missing)}")
        self._coupled_indices = np.asarray(
            [names.index(name) for name in self.coupled_joints], dtype=int)
        self._multipliers = np.asarray(list(self.coupled_joints.values()), dtype=float)
        self.driver_indices = np.asarray(
            [names.index(name) for name in self.driver_joint_names], dtype=int)
        limits = runtime.all_joint_limits
        # Every coupled joint must stay inside its own limits, so the commandable
        # driver range is the intersection of what each follower permits.
        lower, upper = 0.0, self.driver_range
        for index, multiplier in zip(self._coupled_indices, self._multipliers):
            bounds = sorted(limits[index] / multiplier)
            lower, upper = max(lower, bounds[0]), min(upper, bounds[1])
        self._angle_bounds = (lower, upper)
        self._holding = False
        self._commanded_force = 0.0
        self._force_control_active = False
        self._commanded_angle: float | None = None
        self._clamped_entity: int | None = None
        self.step_observer = None

    # ------------------------------------------------------------------ commands

    def open(self, width: float, *, speed: float = 0.5,
             timeout: float = 5.0) -> dict[str, Any]:
        self._holding = False
        self._set_force(self.maximum_force)
        was_clamped = self._clamped_entity is not None
        released_entity = self._clamped_entity
        ensure_dynamic = getattr(self.runtime, "ensure_dynamic_payload", None)
        if was_clamped and released_entity is not None and callable(ensure_dynamic):
            ensure_dynamic(released_entity)
        self._clamped_entity = None
        # Detach first, then stage the open pose. Release is not a grasp-motion
        # trajectory: advancing physics while the fingers are still commanded at
        # their closed target delays the start of the payload's free fall.
        final_angle = self._angle_for_width(width)
        open_pose = self._angle_vector(final_angle)
        direct_set = getattr(self.runtime, "set_full_positions", None)
        if callable(direct_set):
            try:
                direct_set(open_pose, limits="clamp")
            except TypeError:
                # Keep lightweight runtimes/fakes compatible with the live API's
                # optional limits keyword; production LiveSceneRuntime accepts it.
                direct_set(open_pose)
            self._commanded_angle = float(final_angle)
        else:
            self._command_angle(final_angle)
        self._sync_world()
        result = {"target_width": float(np.clip(width, 0.0, self.maximum_width)),
                  "final_width": self.width, "contact_detected": False,
                  "object_present": False, "fingers_met": False}
        if was_clamped:
            # Let gravity resolve the released payload, but do not append an
            # unrelated half-second idle tail to the timeline.
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
        self._set_force(force)
        self._holding = False
        self._clamped_entity = None
        result = self._move(width, speed=speed, timeout=timeout,
                            target_object_id=target_object_id, stop_on_contact=True,
                            post_contact_steps=(self.post_contact_steps if post_contact_steps is None
                                                else int(post_contact_steps)))
        if result["contact_detected"]:
            self._clamped_entity = self._target_entity(target_object_id)
        result["force"] = self._commanded_force if result["contact_detected"] else 0.0
        return result

    def hold(self, force: float) -> None:
        self._set_force(force)
        self._holding = True
        self.runtime.command_full(self.preserve_commanded_target(self.runtime.full_joint_positions()))

    def settle(self, seconds: float) -> None:
        """Resolve the hold-force transition in simulation, retaining the target."""
        dt = float(getattr(self.runtime, "fixed_time_step", .002))
        self._settle(max(0, int(np.ceil(float(seconds) / dt))))

    def _set_force(self, force: float) -> None:
        if (isinstance(force, bool) or not isinstance(force, (int, float))
                or not np.isfinite(force) or not 0 <= force <= self.maximum_force):
            raise ValueError(f"gripper force must be between 0 and {self.maximum_force} N")
        self._commanded_force = float(force)
        self._force_control_active = True

    def joint_effort_limits(self) -> list[float | None] | None:
        """Approximate total two-jaw normal force as a shared joint torque budget.

        Virtual work: Q = (F_total / 2) * abs(d(width)/d(driver_angle)).
        The six independently driven mimic joints share Q; applying Q to each
        would multiply the force budget sixfold. This uses the controller's linear
        85 mm / 0.8 rad aperture model, not a measured contact-force sensor.
        """
        if not getattr(self, "_force_control_active", False):
            return None
        generalized_effort = self._commanded_force * 0.5 * self.maximum_width / self.driver_range
        effort = generalized_effort / float(np.sum(np.abs(self._multipliers)))
        limits = [None] * len(self.runtime.platform_joint_names)
        for index in self._coupled_indices:
            limits[index] = effort
        return limits

    def preserve_commanded_target(self, positions: np.ndarray) -> np.ndarray:
        """Arm commands must not unload the grip by targeting deflected fingers."""
        angle = getattr(self, "_commanded_angle", None)
        return np.asarray(positions, dtype=float).copy() if angle is None else self._angle_vector(angle, positions)

    def _command_angle(self, angle: float) -> None:
        self._commanded_angle = float(angle)
        self.runtime.command_full(self._angle_vector(angle))

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
            "force_source": "commanded_total_jaw_force",
            "contact_detected": clamped,
            "object_present": clamped,
            "holding": self._holding and clamped,
            "slip_detected": False,
        }

    @property
    def width(self) -> float:
        angle = self._driver_angle()
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
        return tuple(self.scene.colliders_of(self._clamped_entity))

    # ------------------------------------------------------------------ motion

    def _driver_angle(self) -> float:
        return float(np.mean(self.runtime.full_joint_positions()[self.driver_indices]))

    def _angle_for_width(self, width: float) -> float:
        target = float(np.clip(width, 0.0, self.maximum_width))
        angle = self.driver_range * (1.0 - target / self.maximum_width)
        return float(np.clip(angle, *self._angle_bounds))

    def _angle_vector(self, angle: float,
                      base: np.ndarray | None = None) -> np.ndarray:
        """A full joint vector with the finger linkage at one driver angle."""
        full = (self.runtime.full_joint_positions() if base is None
                else np.asarray(base, dtype=float).copy())
        full[self._coupled_indices] = angle * self._multipliers
        return full

    def _stop_angle(self, start_angle: float, final_angle: float,
                    target: set[int]) -> tuple[float, bool, bool]:
        """Smallest travel toward `final_angle` that satisfies a stop condition.

        Returns the angle plus whether the target ended up clamped and whether the
        pads met each other. Two conditions end the closure, and the search takes
        whichever comes first: both pads touching the target, or the pads touching
        each other. Both are geometric, so both can be evaluated on a hypothetical
        configuration without committing it.

        Probing hypothetical configurations rather than stepping is what makes this
        affordable: a contact query against an untouched world runs in about 5 ms, but
        any live-world write dirties the collision sync and the next query pays roughly
        365 ms to rebuild it -- mesh colliders included -- on top of a round trip.
        Sweeping with a write per step would cost minutes, so the stop angle is
        searched first and only that angle is ever committed.

        A coarse scan brackets the stop angle instead of assuming the conditions are
        monotonic in the closing angle, then bisection sharpens the bracket so the
        reported width is not quantised to the scan's step.
        """
        base = self.runtime.full_joint_positions()
        state = self._contact_state(self._angle_vector(start_angle, base), target)
        if any(state):
            return start_angle, state[0], state[1]
        previous = start_angle
        for index in range(1, self.contact_scan_samples + 1):
            alpha = index / self.contact_scan_samples
            candidate = start_angle + (final_angle - start_angle) * alpha
            state = self._contact_state(self._angle_vector(candidate, base), target)
            if any(state):
                low, high = previous, candidate
                for _ in range(self.contact_refine_steps):
                    middle = (low + high) / 2.0
                    probed = self._contact_state(
                        self._angle_vector(middle, base), target)
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

        Ramp the finite-effort servo, observing actual contact during closure.
        """
        target_width = float(np.clip(target_width, 0.0, self.maximum_width))
        if stop_on_contact:
            return self._close_on_live_contact(target_width, target_object_id, timeout, post_contact_steps)
        final_angle = self._angle_for_width(target_width)
        start_angle = self._driver_angle()
        motion_started = time.perf_counter()
        travel_fraction = abs(final_angle - start_angle) / max(self.driver_range, 1e-9)
        self._drive_to(
            final_angle, timeout=timeout, started=motion_started, settle=False,
            minimum_settle_steps=0,
            motion_duration=self.closure_duration * travel_fraction)
        return {
            "target_width": target_width,
            "final_width": self.width,
            "contact_detected": False,
            "object_present": False,
            "fingers_met": False,
        }

    def _close_on_live_contact(self, width: float, target: str | None,
                               timeout: float, post_steps: int) -> dict[str, Any]:
        """Ramp until actual bilateral finger contact, then count physics steps.

        A hypothetical rigid-mimic contact angle cannot predict the configuration
        of compliant, force-limited fingers. Observe the actual mechanism instead.
        """
        dt = float(getattr(self.runtime, "fixed_time_step", .002))
        start = self._driver_angle()
        final = self._angle_for_width(width)
        rate = self.driver_range / self.closure_duration
        colliders = self._target_colliders(target)
        began = time.perf_counter()
        contact_step = None
        clamped = met = False
        for step in range(1, max(1, int(np.ceil(timeout / dt))) + 1):
            angle = min(final, start + rate * step * dt)
            self._command_angle(angle)
            self.runtime.step()
            self._notify_step()
            actual = self.runtime.full_joint_positions()
            clamped, met = self._live_contact_state(colliders)
            if clamped:
                if contact_step is None:
                    contact_step = step
                if step - contact_step >= post_steps:
                    break
            else:
                contact_step = None
                if met or np.max(np.abs(actual[self._coupled_indices] - final*self._multipliers)) <= self.closure_tolerance:
                    break
            if getattr(self.runtime, "realtime", False) and time.perf_counter() - began >= timeout:
                raise TimeoutError("Gripper motion timed out before stable bilateral contact.")
        else:
            raise TimeoutError(
                "Gripper motion timed out before stable bilateral contact "
                f"({step * dt:.3f}s simulation, {time.perf_counter() - began:.3f}s wall time).")
        return {"target_width": width, "final_width": self.width,
                "contact_detected": clamped, "object_present": clamped,
                "fingers_met": met}

    def _drive_to(self, angle: float, *, timeout: float, started: float,
                  settle: bool, minimum_settle_steps: int = 0,
                  motion_duration: float = 0.0) -> None:
        """Place the linkage at one driver angle.

        The contact probe decides the final angle, but the approach is still emitted
        at a constant angular velocity so playback and physics do not jump to the
        grasp pose in one command.

        The final target remains active while physics settles. Contact may prevent
        exact arrival during a clamp; an unobstructed opening must converge.
        """
        start_angle = self._driver_angle()
        delta = angle - start_angle
        time_step = max(float(getattr(self.runtime, "fixed_time_step", 0.002)), 1e-6)
        motion_steps = (0 if motion_duration <= 0.0 else
                        max(1, int(np.ceil(float(motion_duration) / time_step))))
        for step in range(1, motion_steps + 1):
            fraction = step / motion_steps
            self._command_angle(start_angle + delta * fraction)
            self.runtime.step()
            self._notify_step()
            simulated_elapsed = step * time_step
            if (getattr(self.runtime, "realtime", False)
                    and time.perf_counter() - started >= timeout) or (
                        not getattr(self.runtime, "realtime", False)
                        and simulated_elapsed >= timeout):
                raise TimeoutError("Gripper motion timed out.")
        # Keep pushing toward the target with bounded torque; never teleport
        # contact-blocked fingers or replace the target with their deflected pose.
        self._command_angle(angle)
        for step in range(self.closure_steps):
            self.runtime.step()
            self._notify_step()
            simulated_elapsed = motion_duration + (step + 1) * time_step
            if (getattr(self.runtime, "realtime", False)
                    and time.perf_counter() - started >= timeout) or (
                        not getattr(self.runtime, "realtime", False)
                        and simulated_elapsed >= timeout):
                raise TimeoutError("Gripper motion timed out.")
            reached = self.runtime.full_joint_positions()[self._coupled_indices]
            if (step + 1 >= minimum_settle_steps and
                    np.max(np.abs(reached - angle * self._multipliers)) <= self.closure_tolerance):
                break
        else:
            if not settle:
                raise TimeoutError("Gripper did not reach its open/close target.")
        self._sync_world()

    def _settle(self, steps: int) -> None:
        """Step physics while the joint controller holds the current configuration."""
        self.runtime.command_full(self.preserve_commanded_target(self.runtime.full_joint_positions()))
        for _ in range(steps):
            self.runtime.step()
            self._notify_step()

    def _notify_step(self) -> None:
        observer = getattr(self, "step_observer", None)
        if callable(observer):
            observer()

    def _sync_world(self) -> None:
        """Push the finger pose into the physics and collision backends.

        Without this the collision backend would answer for a stale pose, so the next
        contact query would judge the fingers to be where they were before the
        closure.
        """
        self.runtime.sync_backends()
        self.runtime.invalidate()

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
        for pair in self.robot.configuration_contacts(
                [float(value) for value in positions], limits="ignore"):
            colliding, first, second = _pair(pair)
            if not colliding:
                continue
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
        return self._live_contact_state(target)[0]

    def _live_contact_state(self, target: set[int]) -> tuple[bool, bool]:
        """Use solver contact forces, not overlap of FCL's different mesh surface."""
        pads: set[str] = set()
        met = False
        for pair in self.runtime.session.physics_contacts():
            if float(pair["normal_force"]) <= 1e-6:
                continue
            first, second = int(pair["first"]), int(pair["second"])
            first_side, second_side = self._finger_side(first), self._finger_side(second)
            if first_side and second_side and first_side != second_side:
                met = True
            if first in target and second_side:
                pads.add(second_side)
            if second in target and first_side:
                pads.add(first_side)
        return len(pads) == 2, met

    def _finger_side(self, entity: int) -> str | None:
        """The distal finger includes its pad and supporting spring/follower link."""
        pad = self._pad_side(entity)
        if pad:
            return pad
        link = self.scene.link_of(entity) or ""
        if not link.startswith("robotiq_2f85_") or not link.endswith(("spring_link", "follower")):
            return None
        return "left" if "_left_" in link else "right" if "_right_" in link else None


def _pair(value: Any) -> tuple[bool, int, int]:
    """(colliding, robot collider, other) from a Live mapping or a ContactPair."""
    if isinstance(value, dict):
        return (bool(value["colliding"]), int(value["robot_collider"]),
                int(value["other"]))
    return bool(value.colliding), int(value.robot_collider), int(value.other)
