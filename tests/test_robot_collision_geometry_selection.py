from pathlib import Path

from collision.assets.robot_visual import prepare_robot_visual_collisions


def _write_urdf(path: Path, *, with_collision: bool) -> None:
    collision = '<collision><geometry><box size="0.1 0.1 0.1"/></geometry></collision>' if with_collision else ''
    path.write_text(
        f'''<robot name="test">
  <link name="base"><visual><geometry><box size="0.1 0.1 0.1"/></geometry></visual>{collision}</link>
</robot>''',
        encoding='utf-8',
    )


def test_auto_prefers_complete_authored_collision(tmp_path):
    source = tmp_path / 'robot.urdf'
    _write_urdf(source, with_collision=True)
    assert prepare_robot_visual_collisions(source) == source.resolve()


def test_authored_mode_is_explicit_and_auto_does_not_ignore_exclusions(tmp_path):
    source = tmp_path / 'robot.urdf'
    _write_urdf(source, with_collision=True)
    assert prepare_robot_visual_collisions(source, {'mode': 'authored'}) == source.resolve()
    # An exclusion requires a derived URDF even when authored collision exists.
    derived = prepare_robot_visual_collisions(source, {'mode': 'auto', 'exclude_links': ['base']})
    assert derived != source.resolve()
