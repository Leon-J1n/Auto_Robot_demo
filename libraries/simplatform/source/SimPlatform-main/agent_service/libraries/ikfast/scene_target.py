"""Convert world TCP targets to the vendored UR5e wrist/base convention."""
import numpy as np


def solver_target(runtime, position, rotation):
    # The vendored solver's FK is base_link -> wrist_3_link. Read current scene
    # transforms on each solve so relocated bases and changed tools are respected.
    base = runtime.link_pose("base_link")
    wrist = runtime.link_pose("wrist_3_link")
    tcp = runtime.get_end_effector_pose()
    bw = np.asarray(base["rotation_matrix"])
    rw = np.asarray(wrist["rotation_matrix"])
    rt = np.asarray(tcp["rotation_matrix"])
    wrist_to_tcp_rotation = rw.T @ rt
    wrist_to_tcp_position = rw.T @ (np.asarray(tcp["position"]) - wrist["position"])
    goal_wrist_rotation = rotation @ wrist_to_tcp_rotation.T
    goal_wrist_position = position - goal_wrist_rotation @ wrist_to_tcp_position
    return (bw.T @ (goal_wrist_position - base["position"]),
            bw.T @ goal_wrist_rotation)
