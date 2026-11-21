# Point-cloud Pose Estimation

This package will estimate grasp and placement poses from a point cloud supplied by the caller. It does not acquire point clouds from a sensor; callers provide XYZ points and their coordinate frame.

Current files define the request/output shape and the future strategy boundaries only. No point-cloud processing, pose estimation, or sensor integration is implemented yet. The estimator raises `NotImplementedError` after validating the operation and presence of point-cloud input.

`operation` selects `grasp` or `placement`. The planned result is a pose in the input point cloud frame, with position in meters, orientation as a wxyz quaternion, confidence, and optional diagnostics. Strategy-specific constraints are supplied through `constraints`.
