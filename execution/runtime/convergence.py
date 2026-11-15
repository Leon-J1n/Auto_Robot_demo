"""Bounded endpoint settling without changing the simulated physical state."""
import math
from copy import deepcopy


class ContactMonitor:
    """Keep raw contact observations, but distinguish chatter from loss."""
    def __init__(self, *, sustained_loss_limit_s=.1):
        if sustained_loss_limit_s <= 0:
            raise ValueError('Contact monitor interval must be positive')
        self.sustained_loss_limit_s = float(sustained_loss_limit_s)
        self.samples = 0
        self.loss_started = None
        self.first_evidence = None
        self.episodes = 0
        self.longest_loss = 0.0
        self.failure = None

    def update(self, payload, simulation_time):
        if self.failure is not None:
            return self.failure
        self.samples += 1
        if payload.get('contact_lost'):
            if self.loss_started is None:
                self.loss_started = simulation_time
                self.first_evidence = deepcopy(payload)
                self.episodes += 1
            duration = simulation_time - self.loss_started
            self.longest_loss = max(self.longest_loss, duration)
            displaced = (payload.get('relative_position_error_m', 0.) > .005 or
                         payload.get('relative_orientation_error_rad', 0.) > .05)
            if duration >= self.sustained_loss_limit_s - 1e-10 or displaced:
                self.failure = {'error_code': 'GRASP_FAILED',
                    'error_message': 'Grasp lost: sustained contact loss or measured payload slip.',
                    'failed_stage': 'execution', 'first_detected_simulation_time': self.loss_started,
                    'contact_loss_duration_s': duration, 'evidence': deepcopy(payload)}
        else:
            if self.loss_started is not None:
                self.longest_loss = max(self.longest_loss, simulation_time - self.loss_started)
            self.loss_started = None
        return self.failure

    def snapshot(self):
        return {'contact_loss_episodes': self.episodes, 'longest_contact_loss_s': self.longest_loss,
                'sustained_loss_limit_s': self.sustained_loss_limit_s,
                'samples': self.samples,
                'failure_confirmed': self.failure is not None}


class TrajectoryTimeout(TimeoutError):
    def __init__(self, reason, details):
        self.details = {'reason': reason, **details}
        super().__init__(f'Trajectory execution timed out ({reason}); '
                         f"joint_error={details['joint_error_rad']:.6f} rad")


class GoalConvergence:
    def __init__(self, *, position_tolerance=0.002, orientation_tolerance=0.02):
        self.position_tolerance = min(float(position_tolerance), 0.002)
        self.orientation_tolerance = min(float(orientation_tolerance), 0.02)
        self.stable_since = None

    def update(self, elapsed, *, reference_complete, joint_error, joint_speed,
               position_error, orientation_error, holding_required=False, contact_valid=True):
        # A small static servo deflection under a confirmed physical load is
        # acceptable only with tight Cartesian accuracy and stable contact.
        joint_limit = 0.002 if holding_required else 0.001
        acceptable = (reference_complete and contact_valid
                      and all(math.isfinite(x) for x in
                              (joint_error, joint_speed, position_error, orientation_error))
                      and joint_error < joint_limit and joint_speed < 0.01
                      and position_error <= self.position_tolerance
                      and orientation_error <= self.orientation_tolerance)
        if not acceptable:
            self.stable_since = None
        elif self.stable_since is None:
            self.stable_since = elapsed
        settled = self.stable_since is not None and elapsed - self.stable_since >= 0.1 - 1e-10
        return {'converged': settled, 'joint_error_rad': float(joint_error),
                'joint_speed_rad_s': float(joint_speed), 'position_error_m': float(position_error),
                'orientation_error_rad': float(orientation_error), 'contact_valid': bool(contact_valid),
                'holding_required': bool(holding_required), 'joint_tolerance_rad': joint_limit,
                'position_tolerance_m': self.position_tolerance,
                'orientation_tolerance_rad': self.orientation_tolerance,
                'stable_duration_s': 0. if self.stable_since is None else elapsed - self.stable_since}
