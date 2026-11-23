from execution.output.trajectory_export import _frame_pose


def _driver(sequence, *, role=None):
    value = {"joint_sequence": list(sequence),
             "initial_joint_positions_rad": {joint: 0.0 for joint in sequence}}
    if role is not None:
        value["role"] = role
    return {"component_type": "RobotDriver", "properties": value}


def _component(kind, properties):
    return {"component_type": kind, "properties": properties}


def test_final_replay_frame_uses_exact_publication_state(tmp_path):
    import json
    from execution.output.trajectory_export import append_final_frame
    path = tmp_path / 'frames.jsonl'
    state = {'joints': [.123456789], 'scene_driver_joints': {'2': {'j': .123456789}}}
    assert append_final_frame(path, state, 0) == 0
    assert not path.exists()
    path.write_text('{"frame_index": 0, "joints": [0.12345]}\n')
    assert append_final_frame(path, state, 1) == 2
    last = json.loads(path.read_text().splitlines()[-1])
    assert last['joints'] == state['joints']
    assert last['scene_driver_joints'] == state['scene_driver_joints']
    assert last['substep'] == 'final_state'


def test_passive_joint_frames_require_object_namespace():
    driver = {'joint_sequence': ['slide']}
    state = {'mechanisms': {'slide': {'joint_position': .8},
                            'cabinet_a__slide': {'joint_position': .2}}}
    ids = {'cabinet_a': 7, 'cabinet_b': 8}
    assert _frame_pose(state, 'cabinet_a', 2, 3, ids, driver)[0] == .2
    assert _frame_pose(state, 'cabinet_b', 2, 3, ids, driver) is None


def test_seven_axis_robot_uses_all_seven_pose_values_and_gripper_stays_separate(tmp_path):
    from execution.output.trajectory_export import validate_traj, write_traj

    arm = {"object_id_in_scene": 2, "object_name_in_scene": "Nero",
           "components": [_component("MetadataRef", {"type": "robot"}),
                          _driver([f"joint{i}" for i in range(1, 8)])]}
    gripper = {"object_id_in_scene": 3, "object_name_in_scene": "Gripper",
               "components": [_component("MetadataRef", {"type": "robot"}),
                              _component("RobotAttachment", {"parent_object_id": 2}),
                              _driver(["gripper_joint"])]}
    scene = {"objects": [arm, gripper]}
    joints = [float(i) for i in range(1, 8)]
    frames = [{"frame_index": 0, "joints": joints,
               "scene_driver_joints": {"3": {"gripper_joint": 0.4}},
               "objects": {"Nero": {"position": [0, 0, 0], "quaternion_xyzw": [0, 0, 0, 1]},
                            "Gripper": {"position": [0, 0, 0], "quaternion_xyzw": [0, 0, 0, 1]}}}]
    path = tmp_path / "nero.traj"
    write_traj(path, scene, frames, initial_joints=joints)
    report = validate_traj(path)
    by_id = {item["object_id"]: item for item in report["objects"]}
    assert by_id[2]["pose_dim"] == 7
    assert by_id[2]["default_pose"] == joints
    assert by_id[3]["pose_dim"] == 7
