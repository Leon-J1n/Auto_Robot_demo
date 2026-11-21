"""Placeholder for point-cloud based placement pose estimation."""


class PlacementPoseStrategy:
    def estimate(self, point_cloud, constraints=None):
        raise NotImplementedError
