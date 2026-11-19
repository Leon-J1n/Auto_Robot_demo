from execution.output.trajectory_export import _frame_pose


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
