from pathlib import Path
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import patch
import pytest

from collision.assets.robot_visual import prepare_robot_visual_collisions


def _write_urdf(path: Path, *, with_collision: bool) -> None:
    collision = '<collision><geometry><box size="0.1 0.1 0.1"/></geometry></collision>' if with_collision else ''
    path.write_text(
        f'''<robot name="test">
  <link name="base"><visual><geometry><box size="0.1 0.1 0.1"/></geometry></visual>{collision}</link>
</robot>''',
        encoding='utf-8',
    )


@pytest.mark.parametrize('mode', [None, 'auto', 'authored', 'visual'])
def test_all_modes_replace_complete_authored_collision(tmp_path, mode):
    source = tmp_path / 'robot.urdf'
    _write_urdf(source, with_collision=True)
    derived = prepare_robot_visual_collisions(source, {} if mode is None else {'mode': mode},
                                              cache_root=tmp_path / 'cache')
    assert derived != source.resolve()
    assert ET.parse(derived).find('link/collision/geometry/mesh') is not None
    assert ET.parse(derived).find('link/collision/geometry/box') is None


def test_authored_mode_is_explicit_and_auto_does_not_ignore_exclusions(tmp_path):
    source = tmp_path / 'robot.urdf'
    _write_urdf(source, with_collision=True)
    # An exclusion requires a derived URDF even when authored collision exists.
    derived = prepare_robot_visual_collisions(source, {'mode': 'auto', 'exclude_links': ['base']},
                                              cache_root=tmp_path / 'cache')
    assert derived != source.resolve()


def test_collision_only_link_requires_visual(tmp_path):
    source = tmp_path / 'robot.urdf'
    source.write_text('<robot><link name="base"><collision><geometry>'
                      '<box size="1 1 1"/></geometry></collision></link></robot>')
    with pytest.raises(ValueError, match='base.*no visual'):
        prepare_robot_visual_collisions(source, cache_root=tmp_path / 'cache')


def test_bundle_cannot_bypass_visual_policy(tmp_path):
    source = tmp_path / 'robot.urdf'
    _write_urdf(source, with_collision=True)
    with patch('collision.assets.bundle.configured_bundle', return_value=SimpleNamespace(manifest={})):
        with pytest.raises(ValueError, match='visual_policy_mismatch'):
            prepare_robot_visual_collisions(source)
