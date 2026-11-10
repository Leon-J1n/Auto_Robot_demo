# Final robot asset catalogue
| ID | Asset | Type | Upstream model / solver path |
|---:|---|---|---|
| 13 | PR2 | Existing mobile dual arm | ROS PR2 ecosystem |
| 14 | TIAGo++ | Mobile, omnidirectional base, lift torso, two 7-DoF arms | PAL Robotics TIAGo dual ROS/MoveIt; converted to local URDF from MuJoCo Menagerie |
| 15 | Baxter | Fixed dual arm, two 7-DoF arms | Rethink Robotics `baxter_common`, ROS/MoveIt |
| 16 | Epson RS4-C351S | Four-axis industrial SCARA | Epson `epson-robot-ros2` Apache-2.0 URDF; ROS MoveIt/KDL or Pinocchio |
| 17 | Franka Research 3 | Single 7-DoF arm | Official FR3 URDF; MoveIt 2 KDL/TRAC-IK or Pinocchio |
| 18 | Franka Hand | Standalone parallel gripper | Official hand geometry split from FR3 description; attach at `fr3_link8` |

All entries use the package convention `id/<name>.urdf`, `id/<name>.json`, `id/<name>_files.json`, local mesh references, and a thumbnail. DAE visual meshes were converted to STL because the current MuJoCo viewer accepts STL/OBJ directly. The TIAGo++ import preserves the upstream link, lift, wheel, arm, and gripper joints; the conversion only changes the container format from MJCF to URDF.

The separate Franka assets are intentionally not fused: ID 17 is the arm with flange (`fr3_link8`), and ID 18 is the hand with root `fr3_hand_mount` and the two finger joints. The metadata keeps exact URDF joint limits and provides the terminal links for attaching a hand or a solver. The JSON and URDF files describe geometry and joints; they do not embed an IK solver.
