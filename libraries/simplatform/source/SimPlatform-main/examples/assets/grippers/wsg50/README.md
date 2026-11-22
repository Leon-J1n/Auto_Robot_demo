# WSG50 test gripper

Load `wsg50.urdf` directly in RobotSimPlatform. The URDF uses only relative mesh
paths, so the directory can be moved as a unit.

The model has a `base_link` and two independently controlled prismatic joints:

- `left_finger_joint`
- `right_finger_joint`

Both joints use a range of -0.05 to 0 m. Set both joints to 0 to open the
gripper and -0.05 to close it symmetrically. They are intentionally independent
because the current RobotSimPlatform URDF importer does not apply URDF
mimic-joint constraints.

The visual geometry is the WSG50 sample distributed with Bullet3. Collision
geometry uses simple boxes for predictable real-time physics behavior.

Source: https://github.com/bulletphysics/bullet3/tree/master/data/gripper

See `LICENSE.txt` for the asset license and attribution.
