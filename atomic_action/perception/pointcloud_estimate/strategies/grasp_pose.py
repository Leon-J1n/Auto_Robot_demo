"""Placeholder for point-cloud based grasp pose estimation."""


class GraspPoseStrategy:
    def estimate(self, point_cloud, constraints=None):
        raise NotImplementedError
